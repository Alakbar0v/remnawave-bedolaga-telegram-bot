"""Real-DB boundary tests for user_has_active_subscription.

Background
----------
This predicate gates personalized (?tgid=) landing purchases: "does this user
already have a subscription that should block a new one." It must match
ACTIVE and DISABLED (gated — e.g. left the required channel;
extend/replace_subscription would silently reactivate it if this predicate
let a purchase through) with a future end_date, and must NOT match TRIAL or
LIMITED — someone who ran out of traffic (LIMITED) or is on a trial (TRIAL)
is exactly who should be able to buy here; blocking them would kill the
purchase this feature exists for — or EXPIRED/PENDING, or any status with a
past end_date.

Uses a real in-memory SQLite session (not a mock) because the thing actually
at risk is the SQL WHERE clause itself — a mocked db.execute can't catch a
wrong status list or a wrong end_date comparison.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.database.crud.subscription import user_has_active_subscription
from app.database.models import Subscription
from tests.fixtures.sqlite_memory import memory_session


async def _insert_subscription(db, *, user_id: int, status: str, end_date) -> None:
    db.add(
        Subscription(
            user_id=user_id,
            status=status,
            is_trial=(status == 'trial'),
            end_date=end_date,
            remnawave_short_id=f'sid{user_id}',
        )
    )
    await db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('status', 'end_offset_days', 'expected'),
    [
        ('active', 10, True),
        ('limited', 10, False),  # out of traffic, not days — must be allowed to buy again
        ('disabled', 10, True),  # channel-gate / admin sync — must still block (see module docstring)
        ('trial', 10, False),  # deliberately does not block — keeps the upgrade funnel open
        ('expired', 10, False),
        ('pending', 10, False),
        ('active', -1, False),  # future status but past end_date must not block
        ('disabled', -1, False),
    ],
)
async def test_user_has_active_subscription_status_boundary(
    monkeypatch, status: str, end_offset_days: int, expected: bool
) -> None:
    async with memory_session(monkeypatch, [Subscription.__table__]) as db:
        user_id = abs(hash((status, end_offset_days))) % 1_000_000 + 1
        await _insert_subscription(
            db,
            user_id=user_id,
            status=status,
            end_date=datetime.now(UTC) + timedelta(days=end_offset_days),
        )

        result = await user_has_active_subscription(db, user_id)

    assert result is expected


@pytest.mark.asyncio
async def test_user_has_active_subscription_false_when_no_rows(monkeypatch) -> None:
    async with memory_session(monkeypatch, [Subscription.__table__]) as db:
        assert await user_has_active_subscription(db, 999) is False


@pytest.mark.asyncio
async def test_user_has_active_subscription_ignores_other_users(monkeypatch) -> None:
    async with memory_session(monkeypatch, [Subscription.__table__]) as db:
        await _insert_subscription(
            db, user_id=1, status='active', end_date=datetime.now(UTC) + timedelta(days=10)
        )
        assert await user_has_active_subscription(db, 2) is False
