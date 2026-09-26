"""CRUD для СБП-рекуррентов Antilopay (зеркало lava_subscription)."""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import AntilopaySubscription


logger = structlog.get_logger(__name__)

_ACTIVE_STATUSES = ('PENDING', 'ACTIVE', 'PAST_DUE')


async def create_antilopay_subscription(
    db: AsyncSession,
    *,
    user_id: int,
    subscription_id: int,
    tariff_id: int | None,
    order_id: str,
    antilopay_payment_id: str | None,
    charge_days: int,
    amount_kopeks: int,
    redirect_url: str | None,
    free_days: int = 0,
    status: str = 'PENDING',
) -> AntilopaySubscription:
    record = AntilopaySubscription(
        user_id=user_id,
        subscription_id=subscription_id,
        tariff_id=tariff_id,
        order_id=order_id,
        antilopay_payment_id=antilopay_payment_id,
        charge_days=charge_days,
        amount_kopeks=amount_kopeks,
        redirect_url=redirect_url,
        free_days=free_days,
        status=status,
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)
    logger.info('Создана Antilopay-подписка', order_id=order_id, user_id=user_id)
    return record


async def get_antilopay_subscription_by_id(db: AsyncSession, sub_id: int) -> AntilopaySubscription | None:
    return await db.get(AntilopaySubscription, sub_id)


async def get_antilopay_subscription_by_id_for_update(db: AsyncSession, sub_id: int) -> AntilopaySubscription | None:
    result = await db.execute(
        select(AntilopaySubscription)
        .where(AntilopaySubscription.id == sub_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def get_antilopay_subscription_by_order_id(db: AsyncSession, order_id: str) -> AntilopaySubscription | None:
    result = await db.execute(select(AntilopaySubscription).where(AntilopaySubscription.order_id == order_id))
    return result.scalar_one_or_none()


async def get_antilopay_subscription_by_recurrent_id(
    db: AsyncSession, recurrent_id: str
) -> AntilopaySubscription | None:
    result = await db.execute(select(AntilopaySubscription).where(AntilopaySubscription.recurrent_id == recurrent_id))
    return result.scalar_one_or_none()


async def get_active_antilopay_subscription_by_subscription(
    db: AsyncSession, subscription_id: int
) -> AntilopaySubscription | None:
    result = await db.execute(
        select(AntilopaySubscription)
        .where(
            AntilopaySubscription.subscription_id == subscription_id,
            AntilopaySubscription.status.in_(_ACTIVE_STATUSES),
        )
        .order_by(AntilopaySubscription.id.desc())
    )
    return result.scalars().first()


async def update_antilopay_subscription(
    db: AsyncSession, record: AntilopaySubscription, **fields: Any
) -> AntilopaySubscription:
    for key, value in fields.items():
        setattr(record, key, value)
    await db.commit()
    await db.refresh(record)
    return record


async def list_antilopay_subscriptions_by_statuses(
    db: AsyncSession, statuses: list[str]
) -> list[AntilopaySubscription]:
    result = await db.execute(select(AntilopaySubscription).where(AntilopaySubscription.status.in_(statuses)))
    return list(result.scalars().all())


async def list_recently_cancelled_antilopay_subscriptions(
    db: AsyncSession, updated_after: Any
) -> list[AntilopaySubscription]:
    """Недавно отменённые локально записи с ``recurrent_id``.

    Локальная отмена могла не дойти до Antilopay (сеть) — провайдер продолжил бы списывать.
    """
    result = await db.execute(
        select(AntilopaySubscription).where(
            AntilopaySubscription.status == 'CANCELLED',
            AntilopaySubscription.recurrent_id.isnot(None),
            AntilopaySubscription.updated_at >= updated_after,
        )
    )
    return list(result.scalars().all())
