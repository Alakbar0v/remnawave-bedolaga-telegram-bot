"""Чистая логика СБП-рекуррентов Antilopay (без сети и БД).

Статусы ``payment/recurrent/check``: CREATED, WAIT_CONFIRM, ACTIVE, ACTIVE_FAILED,
PROCESSING, COMPLETE, CANCEL, PROVIDER_CANCEL, ERROR.
"""

from __future__ import annotations

from datetime import UTC, date, datetime


LOCAL_ACTIVE_STATUSES = ('PENDING', 'ACTIVE', 'PAST_DUE')


def normalize_remote_status(raw: str | None) -> str | None:
    """Статус рекуррента Antilopay -> нормализованный (как у Lava/Platega).

    Неизвестные значения возвращаются в нижнем регистре: reconciler на них
    не реагирует — лучше бездействие, чем ложный перевод статуса.
    """
    if raw is None:
        return None
    value = str(raw).strip().upper()
    if not value:
        return None
    if value in ('CREATED', 'WAIT_CONFIRM'):
        return 'pending'
    if value in ('ACTIVE', 'PROCESSING'):
        return 'active'
    if value == 'ACTIVE_FAILED':
        return 'past_due'
    if value in ('CANCEL', 'PROVIDER_CANCEL'):
        return 'cancelled'
    if value == 'COMPLETE':
        return 'expired'
    if value == 'ERROR':
        return 'failed'
    return value.lower()


def antilopay_reconcile_decision(
    local_status: str,
    remote_status: str | None,
    age_minutes: float,
    *,
    remote_missing: bool = True,
) -> str | None:
    """Новый локальный статус по данным Antilopay, либо None — не трогать.

    ``remote_missing=False`` — транспортный сбой: хоронить зависший PENDING рано.
    """
    if remote_status == 'active' and local_status in ('PENDING', 'PAST_DUE'):
        return 'ACTIVE'
    if remote_status == 'cancelled' and local_status != 'CANCELLED':
        return 'CANCELLED'
    if remote_status in ('failed', 'expired') and local_status not in ('FAILED', 'CANCELLED'):
        return 'FAILED'
    if remote_status == 'past_due' and local_status not in ('PAST_DUE', 'CANCELLED'):
        return 'PAST_DUE'
    if remote_status is None and remote_missing and local_status == 'PENDING' and age_minutes > 30:
        return 'FAILED'
    return None


def parse_next_payment_date(raw: str | None) -> datetime | None:
    """``next_payment_date`` (YYYY-MM-DD) -> aware datetime (полночь UTC)."""
    if not raw:
        return None
    try:
        parsed = date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None
    return datetime(parsed.year, parsed.month, parsed.day, tzinfo=UTC)
