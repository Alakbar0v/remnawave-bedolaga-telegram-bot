"""Реконсиляция и статус СБП-рекуррентов Antilopay."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import Settings, settings
from app.database.crud import antilopay_subscription as sub_crud
from app.database.models import (
    AntilopayPayment,
    AntilopaySubscription,
    GraceAccessSessionModel,
    PromoGroup,
    Subscription,
    SubscriptionStatus,
    Tariff,
    TrafficPurchase,
    Transaction,
    User,
    UserPromoGroup,
    UserStatus,
    tariff_promo_groups,
)
from app.services import antilopay_reconcile as reconcile_module
from app.services.antilopay_recurrent import (
    antilopay_reconcile_decision,
    normalize_remote_status,
    parse_next_payment_date,
)
from app.services.payment import antilopay as antilopay_module
from tests.fixtures.sqlite_memory import memory_session


TABLES = (
    User.__table__,
    Subscription.__table__,
    Tariff.__table__,
    TrafficPurchase.__table__,
    PromoGroup.__table__,
    UserPromoGroup.__table__,
    tariff_promo_groups,
    AntilopayPayment.__table__,
    AntilopaySubscription.__table__,
    Transaction.__table__,
    GraceAccessSessionModel.__table__,
)


@pytest.mark.parametrize(
    ('raw', 'expected'),
    [
        ('CREATED', 'pending'),
        ('WAIT_CONFIRM', 'pending'),
        ('ACTIVE', 'active'),
        ('PROCESSING', 'active'),
        ('ACTIVE_FAILED', 'past_due'),
        ('CANCEL', 'cancelled'),
        ('PROVIDER_CANCEL', 'cancelled'),
        ('COMPLETE', 'expired'),
        ('ERROR', 'failed'),
        ('SOMETHING_NEW', 'something_new'),
        (None, None),
        ('', None),
    ],
)
def test_normalize_remote_status(raw, expected):
    assert normalize_remote_status(raw) == expected


def test_reconcile_decision_rules():
    assert antilopay_reconcile_decision('PENDING', 'active', 0) == 'ACTIVE'
    assert antilopay_reconcile_decision('ACTIVE', 'cancelled', 0) == 'CANCELLED'
    assert antilopay_reconcile_decision('ACTIVE', 'past_due', 0) == 'PAST_DUE'
    assert antilopay_reconcile_decision('ACTIVE', 'failed', 0) == 'FAILED'
    assert antilopay_reconcile_decision('CANCELLED', 'active', 0) is None
    # Зависший PENDING хороним только если провайдер достоверно не знает платёж
    assert antilopay_reconcile_decision('PENDING', None, 31) == 'FAILED'
    assert antilopay_reconcile_decision('PENDING', None, 31, remote_missing=False) is None
    assert antilopay_reconcile_decision('PENDING', None, 5) is None


def test_parse_next_payment_date():
    assert parse_next_payment_date('2026-10-01') == datetime(2026, 10, 1, tzinfo=UTC)
    assert parse_next_payment_date('garbage') is None
    assert parse_next_payment_date(None) is None


async def _seed_record(db, *, status='ACTIVE', recurrent_id='rec-1', free_days=0, created_minutes_ago=0):
    user = User(telegram_id=777, username='u777', first_name='U', status=UserStatus.ACTIVE.value, language='ru')
    db.add(user)
    await db.commit()
    tariff = Tariff(name='T', is_active=True, device_limit=1, traffic_limit_gb=0, period_prices={'30': 10000})
    db.add(tariff)
    await db.commit()
    now = datetime.now(UTC)
    subscription = Subscription(
        user_id=user.id,
        tariff_id=tariff.id,
        status=SubscriptionStatus.ACTIVE.value,
        is_trial=False,
        start_date=now,
        end_date=now + timedelta(days=10),
        remnawave_short_id='shortrec',
    )
    db.add(subscription)
    await db.commit()
    record = await sub_crud.create_antilopay_subscription(
        db,
        user_id=user.id,
        subscription_id=subscription.id,
        tariff_id=tariff.id,
        order_id='alp-order-1',
        antilopay_payment_id='APAY-1',
        charge_days=30,
        amount_kopeks=10000,
        redirect_url=None,
        free_days=free_days,
        status=status,
    )
    if recurrent_id:
        record.recurrent_id = recurrent_id
    record.created_at = now - timedelta(minutes=created_minutes_ago)
    await db.commit()
    return user, subscription, record


def _patch(monkeypatch, **service_methods):
    service = SimpleNamespace(**service_methods)
    monkeypatch.setattr(reconcile_module, 'antilopay_service', service)
    monkeypatch.setattr(antilopay_module, 'antilopay_service', service)
    monkeypatch.setattr(Settings, 'is_antilopay_enabled', lambda self: True)
    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_PAYMENT', False)
    return service


async def test_reconcile_cancels_locally_when_provider_cancelled(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        _, _, record = await _seed_record(db)
        _patch(
            monkeypatch,
            check_recurrent=AsyncMock(return_value={'code': 0, 'status': 'PROVIDER_CANCEL'}),
            check_payment=AsyncMock(),
        )

        await reconcile_module.reconcile_antilopay_subscriptions(db)

        await db.refresh(record)
        assert record.status == 'CANCELLED'


async def test_reconcile_syncs_next_charge_date(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        _, _, record = await _seed_record(db)
        _patch(
            monkeypatch,
            check_recurrent=AsyncMock(return_value={'code': 0, 'status': 'ACTIVE', 'next_payment_date': '2026-11-05'}),
            check_payment=AsyncMock(),
        )

        await reconcile_module.reconcile_antilopay_subscriptions(db)

        await db.refresh(record)
        assert record.status == 'ACTIVE'
        assert record.next_charge_at.date().isoformat() == '2026-11-05'


async def test_reconcile_keeps_pending_on_transport_error(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        _, _, record = await _seed_record(db, status='PENDING', created_minutes_ago=120)
        _patch(monkeypatch, check_recurrent=AsyncMock(side_effect=RuntimeError('boom')), check_payment=AsyncMock())

        await reconcile_module.reconcile_antilopay_subscriptions(db)

        await db.refresh(record)
        assert record.status == 'PENDING'


async def test_reconcile_fails_stale_pending_when_provider_unknown(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        _, _, record = await _seed_record(db, status='PENDING', recurrent_id=None, created_minutes_ago=120)
        _patch(
            monkeypatch,
            check_recurrent=AsyncMock(),
            check_payment=AsyncMock(return_value={'code': 8, 'error': 'Payment not found'}),
        )

        await reconcile_module.reconcile_antilopay_subscriptions(db)

        await db.refresh(record)
        assert record.status == 'FAILED'


async def test_reconcile_recovers_lost_binding_callback_and_grants_trial_once(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        _, subscription, record = await _seed_record(db, status='PENDING', recurrent_id=None, free_days=3)
        subscription.status = SubscriptionStatus.PENDING.value
        subscription.is_trial = True
        await db.commit()
        service = _patch(
            monkeypatch,
            check_recurrent=AsyncMock(),
            check_payment=AsyncMock(return_value={'code': 0, 'status': 'SUCCESS', 'recurrent_id': 'rec-9'}),
        )

        await reconcile_module.reconcile_antilopay_subscriptions(db)

        await db.refresh(record)
        await db.refresh(subscription)
        assert record.recurrent_id == 'rec-9'
        assert record.status == 'ACTIVE'
        assert record.trial_activated_at is not None
        assert subscription.status == SubscriptionStatus.ACTIVE.value
        first_end = subscription.end_date

        # Повторный цикл ничего не выдаёт заново
        service.check_recurrent.return_value = {'code': 0, 'status': 'ACTIVE'}
        await reconcile_module.reconcile_antilopay_subscriptions(db)
        await db.refresh(subscription)
        assert subscription.end_date == first_end


async def test_reconcile_repeats_cancel_when_remote_still_active(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        _, _, record = await _seed_record(db, status='CANCELLED')
        service = _patch(
            monkeypatch,
            check_recurrent=AsyncMock(return_value={'code': 0, 'status': 'ACTIVE'}),
            check_payment=AsyncMock(),
            cancel_recurrent=AsyncMock(return_value=True),
        )

        await reconcile_module.reconcile_antilopay_subscriptions(db)

        service.cancel_recurrent.assert_awaited_once_with(recurrent_id='rec-1')


async def test_status_and_cancel_by_local_id(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        _, subscription, record = await _seed_record(db)
        service = _patch(monkeypatch, cancel_recurrent=AsyncMock(return_value=True))

        state = await antilopay_module.get_antilopay_recurring_status(db, subscription.id)
        assert state['status'] == 'ACTIVE'
        assert state['amount_kopeks'] == 10000

        assert await antilopay_module.cancel_antilopay_recurring_by_local_id(db, record.id) is True
        service.cancel_recurrent.assert_awaited_once()
        await db.refresh(record)
        assert record.status == 'CANCELLED'
        assert await antilopay_module.get_antilopay_recurring_status(db, subscription.id) is None


async def test_reconcile_credits_missed_charge_from_history(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        _, subscription, record = await _seed_record(db)
        end_before = subscription.end_date
        _patch(
            monkeypatch,
            check_recurrent=AsyncMock(
                return_value={
                    'code': 0,
                    'status': 'ACTIVE',
                    'payments': [
                        {
                            'payment_id': 'APAY-CHARGE-X',
                            'order_id': 'alp-charge',
                            'status': 'SUCCESS',
                            'ctime': '2026-09-04 10:00:00',
                            'amount': 100.0,
                            'original_amount': 100.0,
                        }
                    ],
                }
            ),
            check_payment=AsyncMock(),
            cancel_recurrent=AsyncMock(return_value=True),
        )

        await reconcile_module.reconcile_antilopay_subscriptions(db)
        await reconcile_module.reconcile_antilopay_subscriptions(db)  # второй цикл — идемпотентно

        await db.refresh(record)
        await db.refresh(subscription)
        assert (record.charges_success, record.last_charge_external_id) == (1, 'APAY-CHARGE-X')
        assert subscription.end_date - end_before >= timedelta(days=29)


async def test_reconcile_marks_past_due_and_notifies_when_failure_callback_lost(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        _, _, record = await _seed_record(db)
        notify = AsyncMock()
        monkeypatch.setattr(antilopay_module.AntilopayPaymentMixin, '_notify_antilopay_recurring', notify)
        _patch(
            monkeypatch,
            check_recurrent=AsyncMock(return_value={'code': 0, 'status': 'ACTIVE_FAILED'}),
            check_payment=AsyncMock(),
        )

        await reconcile_module.reconcile_antilopay_subscriptions(db)
        await reconcile_module.reconcile_antilopay_subscriptions(db)

        await db.refresh(record)
        assert (record.status, record.charges_failed) == ('PAST_DUE', 1)
        assert notify.await_count == 1
