"""Удаление/отзыв подписки отменяет привязанные к ней рекурренты провайдеров (Platega, Lava, Antilopay)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select

import app.handlers  # noqa: F401  (порядок импорта: иначе циклический импорт app.services.payment)
from app.config import settings
from app.database.crud import antilopay_subscription as sub_crud
from app.database.crud.subscription import wipe_trial_subscriptions
from app.database.models import (
    AntilopaySubscription,
    Base,
    Subscription,
    SubscriptionStatus,
    Tariff,
    User,
    UserStatus,
)
from app.services import recurring_cancel
from app.services.payment import antilopay as antilopay_module, lava as lava_module, platega as platega_module
from app.services.subscription_deletion_service import delete_subscription_record
from app.utils.subscription_utils import cleanup_duplicate_subscriptions
from tests.fixtures.sqlite_memory import memory_session


async def _seed(db, *, telegram_id=901, is_trial=True, with_record=True, order_id='alp-del-1', tariff=None):
    user = User(telegram_id=telegram_id, username=f'u{telegram_id}', status=UserStatus.ACTIVE.value, language='ru')
    db.add(user)
    await db.commit()
    if tariff is None:
        tariff = Tariff(name='T', is_active=True, device_limit=1, traffic_limit_gb=0, period_prices={'30': 10000})
        db.add(tariff)
        await db.commit()
    now = datetime.now(UTC)
    subscription = Subscription(
        user_id=user.id,
        tariff_id=tariff.id,
        status=SubscriptionStatus.ACTIVE.value,
        is_trial=is_trial,
        start_date=now,
        end_date=now + timedelta(days=3),
        remnawave_short_id=f'short{telegram_id}',
    )
    db.add(subscription)
    await db.commit()
    record = None
    if with_record:
        record = await sub_crud.create_antilopay_subscription(
            db,
            user_id=user.id,
            subscription_id=subscription.id,
            tariff_id=tariff.id,
            order_id=order_id,
            antilopay_payment_id=f'APAY-{telegram_id}',
            charge_days=30,
            amount_kopeks=10000,
            redirect_url=None,
            free_days=3,
            status='ACTIVE',
        )
        record.recurrent_id = f'rec-{telegram_id}'
        await db.commit()
    return user, tariff, subscription, record


def _antilopay(monkeypatch):
    service = SimpleNamespace(cancel_recurrent=AsyncMock(return_value=True))
    monkeypatch.setattr(antilopay_module, 'antilopay_service', service)
    return service


async def test_cancel_all_calls_every_provider_and_survives_a_failing_one(monkeypatch):
    platega, lava, antilopay = AsyncMock(), AsyncMock(side_effect=RuntimeError('lava down')), AsyncMock()
    monkeypatch.setattr(platega_module, 'cancel_platega_recurring_for_subscription_safe', platega)
    monkeypatch.setattr(lava_module, 'cancel_lava_recurring_for_subscription_safe', lava)
    monkeypatch.setattr(antilopay_module, 'cancel_antilopay_recurring_for_subscription_safe', antilopay)

    await recurring_cancel.cancel_all_recurring_for_subscription_safe(object(), 42, commit=False)

    platega.assert_awaited_once()
    lava.assert_awaited_once()
    # Сбой Lava не мешает отменить Antilopay
    antilopay.assert_awaited_once()
    assert antilopay.await_args.args[1] == 42
    assert antilopay.await_args.kwargs == {'commit': False}


async def test_cancel_all_cancels_antilopay_binding(monkeypatch):
    async with memory_session(monkeypatch, Base.metadata.sorted_tables) as db:
        _, _, subscription, record = await _seed(db)
        service = _antilopay(monkeypatch)

        await recurring_cancel.cancel_all_recurring_for_subscription_safe(db, subscription.id)

        service.cancel_recurrent.assert_awaited_once()
        await db.refresh(record)
        assert record.status == 'CANCELLED'


async def test_delete_subscription_record_cancels_antilopay_recurrent(monkeypatch):
    """Удаление подписки (админка/кабинет): рекуррент Antilopay гасится до удаления строки."""
    async with memory_session(monkeypatch, Base.metadata.sorted_tables) as db:
        _, _, subscription, record = await _seed(db)
        subscription_id = subscription.id
        service = _antilopay(monkeypatch)

        await delete_subscription_record(db, subscription, deleted_by='test')

        service.cancel_recurrent.assert_awaited_once()
        assert await db.get(Subscription, subscription_id) is None


async def test_wipe_trial_subscriptions_cancels_recurrent_before_delete(monkeypatch):
    """Сброс триала удаляет строку (и привязку каскадом) — рекуррент должен уйти у провайдера раньше."""
    async with memory_session(monkeypatch, Base.metadata.sorted_tables) as db:
        _, _, subscription, _ = await _seed(db)
        subscription_id = subscription.id
        service = _antilopay(monkeypatch)

        assert await wipe_trial_subscriptions(db, [subscription]) == 1
        await db.commit()

        service.cancel_recurrent.assert_awaited_once()
        assert await db.get(Subscription, subscription_id) is None


async def test_cleanup_duplicate_subscriptions_cancels_recurrent_of_removed_one(monkeypatch):
    async with memory_session(monkeypatch, Base.metadata.sorted_tables) as db:
        monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)
        user, _, old_subscription, _ = await _seed(db)
        old_id = old_subscription.id
        # Вторая, более свежая подписка того же пользователя на другой тариф — останется
        other_tariff = Tariff(name='T2', is_active=True, device_limit=1, traffic_limit_gb=0, period_prices={'30': 1})
        db.add(other_tariff)
        await db.commit()
        now = datetime.now(UTC)
        db.add(
            Subscription(
                user_id=user.id,
                tariff_id=other_tariff.id,
                status=SubscriptionStatus.ACTIVE.value,
                is_trial=False,
                start_date=now,
                end_date=now + timedelta(days=30),
                remnawave_short_id='shortnewer',
                created_at=now + timedelta(minutes=5),
            )
        )
        await db.commit()
        service = _antilopay(monkeypatch)

        assert await cleanup_duplicate_subscriptions(db) == 1

        service.cancel_recurrent.assert_awaited_once()
        assert await db.get(Subscription, old_id) is None
        rows = (await db.execute(select(AntilopaySubscription))).scalars().all()
        assert all(row.status != 'ACTIVE' for row in rows)
