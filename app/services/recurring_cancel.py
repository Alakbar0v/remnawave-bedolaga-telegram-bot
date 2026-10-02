"""Единая точка отмены провайдерских рекуррентов подписки.

Любой путь, который удаляет подписку или отзывает у неё доступ, обязан вызвать
``cancel_all_recurring_for_subscription_safe`` ДО удаления строки: локальная запись
привязки уходит каскадом вместе с подпиской, и после этого недошедшую до провайдера
отмену уже некому добить (реконсилер видит только локальные записи).
"""

from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncSession


logger = structlog.get_logger(__name__)


async def cancel_all_recurring_for_subscription_safe(
    db: AsyncSession,
    subscription_id: int,
    *,
    commit: bool = True,
) -> None:
    """Отменяет СБП/карточные рекурренты Platega, Lava, Antilopay и Cashera, привязанные к подписке.

    Best-effort и никогда не бросает: сбой одного провайдера не мешает отменить остальных
    и не блокирует удаление подписки. Идемпотентна. ``commit=False`` — вызывающий держит
    свою транзакцию (отмена войдёт в неё). Не гейтится флагами фич: отмена — операция
    безопасности, выключенная фича не останавливает списания на стороне провайдера.
    """
    from app.services.cashera_recurring_cancel import cancel_cashera_recurring_for_subscription_safe
    from app.services.payment.antilopay import cancel_antilopay_recurring_for_subscription_safe
    from app.services.payment.lava import cancel_lava_recurring_for_subscription_safe
    from app.services.payment.platega import cancel_platega_recurring_for_subscription_safe

    for name, cancel in (
        ('platega', cancel_platega_recurring_for_subscription_safe),
        ('lava', cancel_lava_recurring_for_subscription_safe),
        ('antilopay', cancel_antilopay_recurring_for_subscription_safe),
        ('cashera', cancel_cashera_recurring_for_subscription_safe),
    ):
        try:
            if commit:
                await cancel(db, subscription_id)
            else:
                await cancel(db, subscription_id, commit=False)
        except Exception as error:  # pragma: no cover - хелперы сами best-effort
            logger.warning(
                'Не удалось отменить рекуррент при удалении подписки',
                provider=name,
                subscription_id=subscription_id,
                error=str(error),
            )
