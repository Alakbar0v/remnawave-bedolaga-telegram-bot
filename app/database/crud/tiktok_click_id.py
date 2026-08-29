"""CRUD operations for tiktok_click_id_map table."""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import TikTokClickIdMap


logger = structlog.get_logger(__name__)


async def upsert_ttclid(
    db: AsyncSession,
    user_id: int,
    ttclid: str,
    source: str = 'telegram',
    ttp: str | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
) -> TikTokClickIdMap:
    """Insert or update TikTok click id (and optional `_ttp` cookie, client IP,
    and user agent) for a user.

    Race-safe via ON CONFLICT. `ttp`/`ip`/`user_agent` are only overwritten
    when a new value is given, so a later call without them doesn't blow away
    values captured earlier (e.g. at the first ad-click redirect).
    """
    now = datetime.now(UTC)
    values = {
        'ttclid': ttclid,
        'source': source,
        'updated_at': now,
    }
    if ttp:
        values['ttp'] = ttp
    if ip:
        values['ip'] = ip
    if user_agent:
        values['user_agent'] = user_agent

    stmt = (
        pg_insert(TikTokClickIdMap)
        .values(user_id=user_id, ttclid=ttclid, source=source, ttp=ttp, ip=ip, user_agent=user_agent)
        .on_conflict_do_update(index_elements=['user_id'], set_=values)
        .returning(TikTokClickIdMap)
    )

    result = await db.execute(stmt)
    await db.flush()
    return result.scalar_one()


async def get_ttclid(db: AsyncSession, user_id: int) -> TikTokClickIdMap | None:
    """Get TikTok click id mapping for a user."""
    result = await db.execute(select(TikTokClickIdMap).where(TikTokClickIdMap.user_id == user_id))
    return result.scalar_one_or_none()


async def mark_registration_sent(db: AsyncSession, user_id: int) -> None:
    """Mark registration event as sent for a user."""
    await db.execute(
        update(TikTokClickIdMap)
        .where(TikTokClickIdMap.user_id == user_id)
        .values(registration_sent=True, updated_at=datetime.now(UTC))
    )
    await db.flush()


async def mark_trial_sent(db: AsyncSession, user_id: int) -> None:
    """Mark trial event as sent for a user."""
    await db.execute(
        update(TikTokClickIdMap)
        .where(TikTokClickIdMap.user_id == user_id)
        .values(trial_sent=True, updated_at=datetime.now(UTC))
    )
    await db.flush()


async def mark_first_connected_sent(db: AsyncSession, user_id: int) -> None:
    """Mark first-VPN-connection event as sent for a user."""
    await db.execute(
        update(TikTokClickIdMap)
        .where(TikTokClickIdMap.user_id == user_id)
        .values(first_connected_sent=True, updated_at=datetime.now(UTC))
    )
    await db.flush()
