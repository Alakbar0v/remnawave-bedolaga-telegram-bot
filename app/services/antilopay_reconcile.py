"""Реконсиляция СБП-рекуррентов Antilopay (зеркало реконсиляции Lava/Platega)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import AntilopaySubscription
from app.services.antilopay_service import antilopay_service
from app.services.antilopay_test_log import test_log  # TEST-LOG: временно, удалить
from app.services.panel_sync.db_session import rollback_quietly


logger = structlog.get_logger(__name__)


async def reconcile_antilopay_subscriptions(db: AsyncSession, bot: Any = None) -> None:
    """Safety net для СБП-рекуррентов Antilopay.

    Сверяет локальный статус с ``payment/recurrent/check`` (потерянные callback'и,
    зависший PENDING), подтягивает ``recurrent_id`` и дату следующего списания,
    добивает списания, чьи callback'и не дошли (по истории ``payments`` рекуррента —
    идемпотентно по ``payment_id``), и недошедшие отмены.

    ``bot`` нужен, чтобы уведомления о продлении/сбое списания доходили до пользователя.
    Не гейтится ANTILOPAY_SBP_ENABLED: выключение фичи не останавливает живые привязки.
    """
    from app.database.crud import antilopay_subscription as sub_crud
    from app.services.antilopay_recurrent import (
        antilopay_reconcile_decision,
        normalize_remote_status,
        parse_next_payment_date,
    )
    from app.services.payment.antilopay import _AntilopayRecurrentAgent

    if not settings.is_antilopay_enabled():
        return

    agent = _AntilopayRecurrentAgent()
    agent.bot = bot

    try:
        records = await sub_crud.list_antilopay_subscriptions_by_statuses(db, ['PENDING', 'ACTIVE', 'PAST_DUE'])
    except Exception as error:
        logger.warning('Ошибка реконсиляции Antilopay-подписок', error=error)
        return

    # Идентификаторы — заранее, запись перечитываем на каждом шаге: после отката (сбой соседней
    # записи) ORM-объекты списка протухают, и чтение полей упало бы MissingGreenlet.
    for record_id in [item.id for item in records]:
        try:
            record = await db.get(AntilopaySubscription, record_id, populate_existing=True)
            if record is None:
                continue
            remote_missing = True
            remote_status: str | None = None
            next_payment: datetime | None = None
            remote_payments: list[dict[str, Any]] = []

            try:
                if record.recurrent_id:
                    remote = await antilopay_service.check_recurrent(recurrent_id=record.recurrent_id)
                    if remote is not None:
                        remote_status = normalize_remote_status(remote.get('status'))
                        next_payment = parse_next_payment_date(remote.get('next_payment_date'))
                        remote_payments = remote.get('payments') or []
                        remote_missing = remote_status is None
                else:
                    # Callback привязки мог потеряться: смотрим инициирующий платёж.
                    payment_info = await antilopay_service.check_payment(order_id=record.order_id)
                    test_log('reconcile_check_payment', local_id=record.id, response=payment_info)  # TEST-LOG
                    if payment_info.get('code', 0) == 0:
                        pay_status = str(payment_info.get('status') or '').upper()
                        remote_missing = False
                        if pay_status in ('FAIL', 'CANCEL', 'EXPIRED'):
                            remote_status = 'failed'
                        elif pay_status == 'SUCCESS' and payment_info.get('recurrent_id'):
                            record.recurrent_id = str(payment_info['recurrent_id'])
                            await db.commit()
                            remote_status = 'active'
            except Exception:
                # Сбой сети/API — временная недоступность, PENDING хоронить рано.
                remote_missing = False

            # Списания, чьи callback'и потерялись (после пробного периода и далее), — по истории.
            # Делаем ДО решения о статусе: успешное списание возвращает запись в ACTIVE.
            if remote_payments and record.status in ('ACTIVE', 'PAST_DUE'):
                await agent.sync_antilopay_charges_from_history(db, record.id, remote_payments)
                await db.refresh(record)

            age_minutes = (
                (datetime.now(UTC) - record.created_at).total_seconds() / 60 if record.created_at is not None else 0.0
            )
            new_status = antilopay_reconcile_decision(
                record.status, remote_status, age_minutes, remote_missing=remote_missing
            )
            if new_status == 'PAST_DUE' and remote_payments and _has_charge_after_last_failure(record):
                # Свежее успешное списание из истории важнее устаревшего статуса ACTIVE_FAILED.
                new_status = None

            if new_status and new_status != record.status:
                previous_status = record.status
                if new_status == 'ACTIVE' and record.free_days > 0 and record.trial_activated_at is None:
                    # Потерянный callback привязки: без этого юзер привязал СБП, а триал не выдан.
                    # record прочитан без блокировки — перечитываем под FOR UPDATE, иначе при
                    # параллельном настоящем callback триал выдался бы дважды.
                    locked = await sub_crud.get_antilopay_subscription_by_id_for_update(db, record.id)
                    if locked and locked.trial_activated_at is None:
                        await agent._activate_antilopay_trial(db, locked)
                else:
                    fields: dict[str, Any] = {'status': new_status}
                    if new_status == 'PAST_DUE' and previous_status == 'ACTIVE':
                        # Списание не прошло, а callback о сбое не дошёл.
                        fields['charges_failed'] = record.charges_failed + 1
                    await sub_crud.update_antilopay_subscription(db, record, **fields)
                    if 'charges_failed' in fields:
                        await agent._notify_antilopay_recurring(db, record, 'failed')
                test_log(  # TEST-LOG
                    'reconcile_status_change',
                    local_id=record.id,
                    old_status=previous_status,
                    new_status=new_status,
                    remote_status=remote_status,
                    next_payment=str(next_payment),
                    remote_payments_count=len(remote_payments),
                )
                logger.info(
                    'Antilopay-подписка реконсилирована',
                    local_id=record.id,
                    recurrent_id=record.recurrent_id,
                    old_status=previous_status,
                    new_status=new_status,
                    remote_status=remote_status,
                )
            elif next_payment is not None and record.status in ('ACTIVE', 'PAST_DUE'):
                if record.next_charge_at is None or record.next_charge_at.date() != next_payment.date():
                    await sub_crud.update_antilopay_subscription(db, record, next_charge_at=next_payment)
        except Exception as record_error:
            await rollback_quietly(db)
            logger.warning(
                'Не удалось реконсилировать Antilopay-подписку',
                local_id=record_id,
                error=record_error,
            )

    # Контрольный свип недавних отмен: локальный CANCELLED мог не дойти до провайдера.
    try:
        cancelled = await sub_crud.list_recently_cancelled_antilopay_subscriptions(
            db, datetime.now(UTC) - timedelta(days=30)
        )
    except Exception as error:
        logger.warning('Ошибка свипа отменённых Antilopay-подписок', error=error)
        return

    # Идентификаторы — заранее, запись перечитываем на каждом шаге: после отката (сбой соседней
    # записи) ORM-объекты списка протухают, и чтение полей упало бы MissingGreenlet.
    for record_id in [item.id for item in cancelled]:
        try:
            record = await db.get(AntilopaySubscription, record_id, populate_existing=True)
            if record is None:
                continue
            remote = await antilopay_service.check_recurrent(recurrent_id=record.recurrent_id)
            remote_status = normalize_remote_status(remote.get('status')) if remote else None
            if remote_status in (None, 'cancelled', 'failed', 'expired'):
                continue
            await antilopay_service.cancel_recurrent(recurrent_id=record.recurrent_id)
            logger.warning(
                'Antilopay-подписка осталась активной после локальной отмены — повторил отмену',
                local_id=record.id,
                recurrent_id=record.recurrent_id,
                remote_status=remote_status,
            )
        except Exception as record_error:
            await rollback_quietly(db)
            logger.warning(
                'Не удалось досверить отменённую Antilopay-подписку',
                local_id=record_id,
                error=record_error,
            )


def _has_charge_after_last_failure(record: Any) -> bool:
    """Локальный ACTIVE после только что добитого успешного списания — PAST_DUE ставить нельзя."""
    return record.status == 'ACTIVE' and record.last_charge_at is not None
