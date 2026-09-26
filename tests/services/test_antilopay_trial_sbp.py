"""Платный триал через СБП-рекуррент Antilopay: оформление, привязка, списания, отмена."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
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

FREE_DAYS = 3
PAYMENT_ID = 'APAY-BIND-1'
RECURRENT_ID = 'rec-1'


async def _seed(db):
    now = datetime.now(UTC)
    user = User(
        telegram_id=555,
        username='user555',
        first_name='User',
        status=UserStatus.ACTIVE.value,
        language='ru',
        balance_kopeks=0,
    )
    db.add(user)
    await db.commit()

    tariff = Tariff(
        name='Базовый',
        is_active=True,
        device_limit=1,
        traffic_limit_gb=0,
        period_prices={'30': 10000},
    )
    db.add(tariff)
    await db.commit()

    # Черновик триала: PENDING, доступа ещё нет
    subscription = Subscription(
        user_id=user.id,
        tariff_id=tariff.id,
        status=SubscriptionStatus.PENDING.value,
        is_trial=True,
        start_date=now,
        end_date=now,
        autopay_enabled=True,
        remnawave_short_id='shortalp',
    )
    db.add(subscription)
    await db.commit()
    return user, tariff, subscription


def _agent(monkeypatch, *, cancel=None):
    """Агент с замоканным сетевым слоем Antilopay."""
    service = SimpleNamespace(
        create_payment=AsyncMock(
            return_value={'code': 0, 'payment_id': PAYMENT_ID, 'payment_url': 'https://gate.antilopay.com/payment/x'}
        ),
        cancel_recurrent=cancel or AsyncMock(return_value=True),
    )
    monkeypatch.setattr(antilopay_module, 'antilopay_service', service)
    monkeypatch.setattr(settings, 'TRIAL_DURATION_DAYS', FREE_DAYS)
    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_PAYMENT', False)
    return antilopay_module._AntilopayRecurrentAgent(), service


def _binding(order_id: str, status: str = 'SUCCESS', *, amount: float = 0.0) -> dict:
    return {
        'type': 'payment',
        'payment_id': PAYMENT_ID,
        'order_id': order_id,
        'status': status,
        'amount': amount,
        'original_amount': amount,
        'recurrent_id': RECURRENT_ID,
    }


def _charge(payment_id: str, status: str = 'SUCCESS', *, order_id: str = 'alp-unknown-order') -> dict:
    return {
        'type': 'payment',
        'payment_id': payment_id,
        'order_id': order_id,
        'status': status,
        'amount': 100.0,
        'original_amount': 100.0,
        'recurrent_id': RECURRENT_ID,
    }


async def _bind(db, agent, user, tariff, subscription) -> str:
    result = await agent.create_antilopay_trial_subscription(db, user=user, tariff=tariff, subscription=subscription)
    return result['order_id']


async def test_create_sends_sbp_subscription_with_delay_and_stores_pending_record(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        user, tariff, subscription = await _seed(db)
        agent, service = _agent(monkeypatch)

        result = await agent.create_antilopay_trial_subscription(
            db, user=user, tariff=tariff, subscription=subscription
        )

        kwargs = service.create_payment.await_args.kwargs
        assert kwargs['prefer_methods'] == ['SBP']
        assert kwargs['amount_rubles'] == 100.0
        recurrent = kwargs['recurrent']
        assert recurrent['category'] == 'SUBSCRIPTION'
        assert recurrent['type'] == 'MONTH'
        assert (recurrent['delay'], recurrent['delay_type']) == (FREE_DAYS, 'DAY')

        assert result['payment_url'] == 'https://gate.antilopay.com/payment/x'
        record = await sub_crud.get_active_antilopay_subscription_by_subscription(db, subscription.id)
        assert record.status == 'PENDING'
        assert (record.charge_days, record.amount_kopeks, record.free_days) == (30, 10000, FREE_DAYS)
        # Рекуррент провайдера и balance-autopay взаимоисключающи
        assert subscription.autopay_enabled is False


async def test_create_rejects_tariff_without_30_day_price(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        user, tariff, subscription = await _seed(db)
        tariff.period_prices = {'90': 30000}
        await db.commit()
        agent, service = _agent(monkeypatch)

        with pytest.raises(ValueError, match='цена'):
            await agent.create_antilopay_trial_subscription(db, user=user, tariff=tariff, subscription=subscription)
        service.create_payment.assert_not_awaited()


async def test_binding_callback_activates_trial_once(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        user, tariff, subscription = await _seed(db)
        agent, _ = _agent(monkeypatch)
        order_id = await _bind(db, agent, user, tariff, subscription)

        assert await agent.process_antilopay_callback(db, _binding(order_id)) is True

        await db.refresh(subscription)
        assert subscription.status == SubscriptionStatus.ACTIVE.value
        assert subscription.is_trial is True
        first_end = subscription.end_date
        assert (
            timedelta(days=FREE_DAYS - 1)
            < first_end - datetime.now(UTC).replace(tzinfo=first_end.tzinfo)
            < timedelta(days=FREE_DAYS + 1)
        )

        record = await sub_crud.get_antilopay_subscription_by_order_id(db, order_id)
        assert record.status == 'ACTIVE'
        assert record.recurrent_id == RECURRENT_ID
        assert record.trial_activated_at is not None

        # Ретрай callback (Antilopay повторяет каждые 3 минуты) не выдаёт второй триал
        assert await agent.process_antilopay_callback(db, _binding(order_id)) is True
        await db.refresh(subscription)
        assert subscription.end_date == first_end


async def test_binding_callback_never_credits_balance(monkeypatch):
    """Привязка — не пополнение: баланс и транзакции не трогаем."""
    async with memory_session(monkeypatch, TABLES) as db:
        user, tariff, subscription = await _seed(db)
        agent, _ = _agent(monkeypatch)
        order_id = await _bind(db, agent, user, tariff, subscription)

        await agent.process_antilopay_callback(db, _binding(order_id, amount=100.0))

        await db.refresh(user)
        assert user.balance_kopeks == 0
        from sqlalchemy import select

        assert (await db.execute(select(Transaction))).first() is None


async def test_failed_binding_cancels_pending_record(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        user, tariff, subscription = await _seed(db)
        agent, _ = _agent(monkeypatch)
        order_id = await _bind(db, agent, user, tariff, subscription)

        assert await agent.process_antilopay_callback(db, _binding(order_id, 'FAIL')) is True

        record = await sub_crud.get_antilopay_subscription_by_order_id(db, order_id)
        assert record.status == 'CANCELLED'
        await db.refresh(subscription)
        assert subscription.status == SubscriptionStatus.PENDING.value


async def test_recurrent_charge_extends_and_converts_trial(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        user, tariff, subscription = await _seed(db)
        agent, _ = _agent(monkeypatch)
        order_id = await _bind(db, agent, user, tariff, subscription)
        await agent.process_antilopay_callback(db, _binding(order_id))
        await db.refresh(subscription)
        end_after_trial = subscription.end_date

        # Регулярное списание: тот же recurrent_id, другой payment_id и незнакомый order_id
        assert await agent.process_antilopay_callback(db, _charge('APAY-CHARGE-1')) is True

        await db.refresh(subscription)
        assert subscription.end_date - end_after_trial >= timedelta(days=29)
        assert subscription.is_trial is False

        record = await sub_crud.get_antilopay_subscription_by_order_id(db, order_id)
        assert (record.charges_success, record.last_charge_external_id) == (1, 'APAY-CHARGE-1')
        from sqlalchemy import select

        tx = (await db.execute(select(Transaction))).scalars().one()
        assert (abs(tx.amount_kopeks), tx.external_id) == (10000, 'APAY-CHARGE-1')

        # Ретрай того же списания не продлевает второй раз
        end_after_charge = subscription.end_date
        assert await agent.process_antilopay_callback(db, _charge('APAY-CHARGE-1')) is True
        await db.refresh(subscription)
        assert subscription.end_date == end_after_charge


async def test_failed_recurrent_charge_marks_past_due(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        user, tariff, subscription = await _seed(db)
        agent, _ = _agent(monkeypatch)
        order_id = await _bind(db, agent, user, tariff, subscription)
        await agent.process_antilopay_callback(db, _binding(order_id))
        await db.refresh(subscription)
        end_after_trial = subscription.end_date

        assert await agent.process_antilopay_callback(db, _charge('APAY-CHARGE-2', 'FAIL')) is True

        record = await sub_crud.get_antilopay_subscription_by_order_id(db, order_id)
        assert (record.status, record.charges_failed) == ('PAST_DUE', 1)
        await db.refresh(subscription)
        assert subscription.end_date == end_after_trial


async def test_unknown_recurrent_callback_is_rejected(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        agent, _ = _agent(monkeypatch)
        assert await agent.process_antilopay_callback(db, _charge('APAY-X')) is False


async def test_cancel_uses_initial_payment_until_recurrent_id_known(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        user, tariff, subscription = await _seed(db)
        agent, service = _agent(monkeypatch)
        order_id = await _bind(db, agent, user, tariff, subscription)

        await antilopay_module.cancel_antilopay_recurring_for_subscription_safe(db, subscription.id)

        service.cancel_recurrent.assert_awaited_once_with(recurrent_id=None, transaction_id=PAYMENT_ID)
        record = await sub_crud.get_antilopay_subscription_by_order_id(db, order_id)
        assert record.status == 'CANCELLED'


async def test_cancel_marks_local_even_when_provider_fails(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        user, tariff, subscription = await _seed(db)
        agent, _ = _agent(monkeypatch, cancel=AsyncMock(side_effect=RuntimeError('network')))
        order_id = await _bind(db, agent, user, tariff, subscription)
        await agent.process_antilopay_callback(db, _binding(order_id))

        await antilopay_module.cancel_antilopay_recurring_for_subscription_safe(db, subscription.id)

        record = await sub_crud.get_antilopay_subscription_by_order_id(db, order_id)
        assert record.status == 'CANCELLED'


async def test_failed_charge_callback_retries_are_counted_once(monkeypatch):
    """Antilopay ретраит callback каждые 3 минуты — один сбой = один счётчик и одно уведомление."""
    async with memory_session(monkeypatch, TABLES) as db:
        user, tariff, subscription = await _seed(db)
        agent, _ = _agent(monkeypatch)
        notify = AsyncMock()
        monkeypatch.setattr(agent, '_notify_antilopay_recurring', notify)
        order_id = await _bind(db, agent, user, tariff, subscription)
        await agent.process_antilopay_callback(db, _binding(order_id))

        for _ in range(3):
            assert await agent.process_antilopay_callback(db, _charge('APAY-FAIL-1', 'FAIL')) is True

        record = await sub_crud.get_antilopay_subscription_by_order_id(db, order_id)
        assert (record.status, record.charges_failed) == ('PAST_DUE', 1)
        assert [c.args[-1] for c in notify.await_args_list].count('failed') == 1

        # Новая неудачная попытка (другой payment_id) — это уже второй сбой
        assert await agent.process_antilopay_callback(db, _charge('APAY-FAIL-2', 'FAIL')) is True
        await db.refresh(record)
        assert record.charges_failed == 2


async def test_cancel_and_expired_charge_statuses_are_failures(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        user, tariff, subscription = await _seed(db)
        agent, _ = _agent(monkeypatch)
        order_id = await _bind(db, agent, user, tariff, subscription)
        await agent.process_antilopay_callback(db, _binding(order_id))

        assert await agent.process_antilopay_callback(db, _charge('APAY-C1', 'CANCEL')) is True
        assert await agent.process_antilopay_callback(db, _charge('APAY-C2', 'EXPIRED')) is True

        record = await sub_crud.get_antilopay_subscription_by_order_id(db, order_id)
        assert (record.status, record.charges_failed) == ('PAST_DUE', 2)


async def test_successful_charge_after_failure_restores_active(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        user, tariff, subscription = await _seed(db)
        agent, _ = _agent(monkeypatch)
        order_id = await _bind(db, agent, user, tariff, subscription)
        await agent.process_antilopay_callback(db, _binding(order_id))
        await agent.process_antilopay_callback(db, _charge('APAY-F', 'FAIL'))

        assert await agent.process_antilopay_callback(db, _charge('APAY-OK')) is True

        record = await sub_crud.get_antilopay_subscription_by_order_id(db, order_id)
        assert (record.status, record.charges_success) == ('ACTIVE', 1)


async def test_history_sync_credits_missed_charge_once_and_skips_binding(monkeypatch):
    """Callback о списании после пробного периода потерялся — добиваем по истории рекуррента."""
    async with memory_session(monkeypatch, TABLES) as db:
        user, tariff, subscription = await _seed(db)
        agent, _ = _agent(monkeypatch)
        order_id = await _bind(db, agent, user, tariff, subscription)
        await agent.process_antilopay_callback(db, _binding(order_id))
        await db.refresh(subscription)
        end_after_trial = subscription.end_date
        record = await sub_crud.get_antilopay_subscription_by_order_id(db, order_id)

        history = [
            {'payment_id': PAYMENT_ID, 'order_id': order_id, 'status': 'SUCCESS', 'ctime': '2026-09-01 10:00:00'},
            {
                'payment_id': 'APAY-MISSED',
                'order_id': 'alp-other',
                'status': 'SUCCESS',
                'ctime': '2026-09-04 10:00:00',
                'amount': 100.0,
                'original_amount': 100.0,
            },
            {'payment_id': 'APAY-BAD', 'order_id': 'alp-x', 'status': 'FAIL', 'ctime': '2026-09-03 10:00:00'},
        ]

        assert await agent.sync_antilopay_charges_from_history(db, record.id, history) == 1
        await db.refresh(subscription)
        assert subscription.end_date - end_after_trial >= timedelta(days=29)
        assert subscription.is_trial is False
        await db.refresh(record)
        assert (record.status, record.charges_success, record.last_charge_external_id) == (
            'ACTIVE',
            1,
            'APAY-MISSED',
        )

        # Повторная сверка ничего не продлевает и не дублирует транзакцию
        end_after_charge = subscription.end_date
        assert await agent.sync_antilopay_charges_from_history(db, record.id, history) == 0
        await db.refresh(subscription)
        assert subscription.end_date == end_after_charge
        from sqlalchemy import select

        assert len((await db.execute(select(Transaction))).scalars().all()) == 1
