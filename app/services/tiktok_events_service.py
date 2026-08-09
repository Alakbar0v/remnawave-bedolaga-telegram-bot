"""TikTok Events API 2.0 server-side conversions.

Sends events (registration, trial-start, purchase, first VPN connection) to
business-api.tiktok.com/open_api/v1.3/event/track/ using the ttclid captured
via the /start deep-link redirect flow (see app/cabinet/routes/tiktok_redirect.py
and the `tt_` prefix handling in app/handlers/start.py).

Structurally mirrors app/services/yandex_offline_conv_service.py, but unlike
that service, the registration/trial hooks here are wired directly into the
bot handlers (not only the web cabinet) — see start.py and
handlers/subscription/purchase.py — so bot-only funnels are tracked.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.tiktok_click_id import (
    get_ttclid,
    mark_first_connected_sent,
    mark_registration_sent,
    mark_trial_sent,
    upsert_ttclid,
)
from app.database.database import AsyncSessionLocal
from app.utils.cache import cache


logger = structlog.get_logger(__name__)

API_URL = 'https://business-api.tiktok.com/open_api/v1.3/event/track/'
TIMEOUT = 10.0
MAX_RETRIES = 3
RETRY_DELAY = 1.0

EVENT_REGISTRATION = 'CompleteRegistration'
EVENT_TRIAL = 'StartTrial'
EVENT_PURCHASE = 'CompletePayment'

_TTCLID_RE = re.compile(r'^[A-Za-z0-9._-]{1,512}$')
_TTP_RE = re.compile(r'^[A-Za-z0-9._-]{1,256}$')
_http_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=TIMEOUT)
    return _http_client


def _is_enabled() -> bool:
    return bool(
        settings.TIKTOK_EVENTS_ENABLED
        and settings.TIKTOK_PIXEL_CODE
        and settings.TIKTOK_EVENTS_ACCESS_TOKEN
    )


def _normalize_ttclid(ttclid: str | None) -> str | None:
    if not isinstance(ttclid, str):
        return None
    ttclid = ttclid.strip()
    if not ttclid or not _TTCLID_RE.match(ttclid):
        return None
    return ttclid


def _normalize_ttp(ttp: str | None) -> str | None:
    if not isinstance(ttp, str):
        return None
    ttp = ttp.strip()
    if not ttp or not _TTP_RE.match(ttp):
        return None
    return ttp


def _mask_ttclid(ttclid: str) -> str:
    if len(ttclid) <= 4:
        return '****'
    return '*' * (len(ttclid) - 4) + ttclid[-4:]


def _hash_external_id(user_id: int) -> str:
    """SHA-256 our internal user_id for TikTok's `external_id` match parameter."""
    return hashlib.sha256(str(user_id).encode('utf-8')).hexdigest()


def _event_payload(ttclid: str, event: str, event_id: str, user_id: int, ttp: str | None = None) -> dict:
    user = {'ttclid': ttclid, 'external_id': _hash_external_id(user_id)}
    normalized_ttp = _normalize_ttp(ttp)
    if normalized_ttp:
        user['ttp'] = normalized_ttp
    payload = {
        'event_source': 'web',
        'event_source_id': settings.TIKTOK_PIXEL_CODE,
        'data': [
            {
                'event': event,
                'event_time': int(time.time()),
                'event_id': event_id,
                'user': user,
            }
        ],
    }
    if settings.TIKTOK_EVENTS_TEST_CODE:
        payload['test_event_code'] = settings.TIKTOK_EVENTS_TEST_CODE
    return payload


def _purchase_payload(ttclid: str, event_id: str, amount_rubles: float, user_id: int, ttp: str | None = None) -> dict:
    payload = _event_payload(ttclid, EVENT_PURCHASE, event_id, user_id, ttp=ttp)
    payload['data'][0]['properties'] = {
        'currency': settings.TIKTOK_EVENTS_CURRENCY or 'RUB',
        'value': amount_rubles,
    }
    return payload


async def _post_event(payload: dict, kind: str, ttclid: str) -> bool:
    """POST to the TikTok Events API with retries. Returns True on success.

    A 2xx HTTP status is not sufficient — TikTok returns logical errors inside
    a 200 response body (``code`` != 0), which is not retried since the request
    was well-formed and understood.
    """
    masked = _mask_ttclid(ttclid)
    headers = {
        'Access-Token': settings.TIKTOK_EVENTS_ACCESS_TOKEN,
        'Content-Type': 'application/json',
    }
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            client = _get_client()
            resp = await client.post(API_URL, json=payload, headers=headers)

            if 200 <= resp.status_code < 300:
                try:
                    body = resp.json()
                except Exception:
                    body = {}
                if isinstance(body, dict) and body.get('code') == 0:
                    logger.info('tiktok event sent', kind=kind, ttclid=masked, status=resp.status_code)
                    return True
                logger.error(
                    'tiktok event rejected', kind=kind, ttclid=masked, status=resp.status_code, body=str(body)[:200]
                )
                return False

            if 500 <= resp.status_code < 600 and attempt < MAX_RETRIES:
                logger.warning(
                    'tiktok event server error',
                    kind=kind,
                    attempt=attempt,
                    max=MAX_RETRIES,
                    ttclid=masked,
                    status=resp.status_code,
                )
                await asyncio.sleep(RETRY_DELAY)
                continue

            logger.error(
                'tiktok event http error', kind=kind, ttclid=masked, status=resp.status_code, body=resp.text[:200]
            )
            return False

        except Exception as exc:
            logger.warning(
                'tiktok event request error', kind=kind, attempt=attempt, max=MAX_RETRIES, ttclid=masked, error=str(exc)
            )
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_DELAY)
                continue
            return False

    return False


# --- Background task helpers ---

_background_tasks: set[asyncio.Task] = set()


def _task_done(task):
    """Log errors from background conversion tasks."""
    _background_tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        logger.error('TikTokEvents background task failed', error=str(exc))


def spawn_bg(coro) -> None:
    """Spawn a background TikTok conversion task with proper reference tracking.

    Checks _is_enabled() early so callers don't need to.
    """
    if not _is_enabled():
        # Close the coroutine to avoid RuntimeWarning
        coro.close()
        return
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_task_done)


async def _fire_bg(event_name: str, event_fn, user_id: int, **kwargs) -> None:
    """Generic background wrapper: opens a session, calls event_fn, logs errors."""
    try:
        async with AsyncSessionLocal() as db:
            await event_fn(db, user_id, **kwargs)
    except Exception as exc:
        logger.warning('TikTokEvents background event failed', event=event_name, user_id=user_id, error=str(exc))


async def fire_registration_bg(user_id: int) -> None:
    """Fire registration event in background with its own DB session."""
    await _fire_bg('registration', on_registration, user_id)


async def fire_trial_bg(user_id: int) -> None:
    """Fire trial event in background with its own DB session."""
    await _fire_bg('trial', on_trial, user_id)


async def fire_purchase_bg(user_id: int, amount_kopeks: int, transaction_id: int) -> None:
    """Fire purchase event in background with its own DB session."""
    await _fire_bg('purchase', on_purchase, user_id, amount_kopeks=amount_kopeks, transaction_id=transaction_id)


async def fire_first_connected_bg(user_id: int) -> None:
    """Fire first-VPN-connection event in background with its own DB session."""
    await _fire_bg('first_connected', on_first_connected, user_id)


# --- Public API ---


async def store_ttclid(
    db: AsyncSession,
    user_id: int,
    ttclid: str | None,
    *,
    source: str = 'telegram',
    ttp: str | None = None,
) -> bool:
    """Store TikTok click id (and optional `_ttp` cookie) for a user. Returns True if stored."""
    normalized = _normalize_ttclid(ttclid)
    if not normalized:
        return False

    try:
        await upsert_ttclid(db, user_id, normalized, source=source, ttp=_normalize_ttp(ttp))
        logger.info('stored ttclid', user_id=user_id, source=source, has_ttp=bool(ttp))
        return True
    except Exception as exc:
        logger.error('failed to store ttclid', user_id=user_id, error=str(exc))
        return False


async def store_ttclid_and_fire_registration(
    user_id: int,
    ttclid: str | None,
    *,
    source: str = 'telegram',
    ttp: str | None = None,
) -> None:
    """Store ttclid and fire registration conversion in background (best-effort).

    Opens its own DB session so it never interferes with the caller's transaction.
    """
    if not ttclid:
        return
    try:
        async with AsyncSessionLocal() as db:
            stored = await store_ttclid(db, user_id, ttclid, source=source, ttp=ttp)
            if stored:
                await db.commit()
                spawn_bg(fire_registration_bg(user_id))
    except Exception as exc:
        logger.warning('Failed to store ttclid and fire registration', user_id=user_id, error=str(exc))


async def store_ttclid_only(
    user_id: int,
    ttclid: str | None,
    *,
    source: str = 'telegram',
    ttp: str | None = None,
) -> None:
    """Persist a freshly-provided ttclid WITHOUT firing a registration event."""
    if not _is_enabled() or not ttclid:
        return
    try:
        async with AsyncSessionLocal() as db:
            stored = await store_ttclid(db, user_id, ttclid, source=source, ttp=ttp)
            if stored:
                await db.commit()
    except Exception as exc:
        logger.warning('Failed to store ttclid', user_id=user_id, error=str(exc))


async def on_registration(db: AsyncSession, user_id: int) -> None:
    """Fire registration event (once per user)."""
    if not _is_enabled():
        return

    try:
        row = await get_ttclid(db, user_id)
        if not row or row.registration_sent or not row.ttclid:
            return

        success = await _post_event(
            _event_payload(row.ttclid, EVENT_REGISTRATION, f'{EVENT_REGISTRATION}_{user_id}', user_id, ttp=row.ttp),
            'registration',
            row.ttclid,
        )
        if success:
            await mark_registration_sent(db, user_id)
            await db.commit()
            logger.info('tiktok registration event sent', user_id=user_id)
    except Exception as exc:
        logger.error('tiktok registration event failed', user_id=user_id, error=str(exc))


async def on_trial(db: AsyncSession, user_id: int) -> None:
    """Fire trial-start event (once per user)."""
    if not _is_enabled():
        return

    try:
        row = await get_ttclid(db, user_id)
        if not row or row.trial_sent or not row.ttclid:
            return

        success = await _post_event(
            _event_payload(row.ttclid, EVENT_TRIAL, f'{EVENT_TRIAL}_{user_id}', user_id, ttp=row.ttp),
            'trial',
            row.ttclid,
        )
        if success:
            await mark_trial_sent(db, user_id)
            await db.commit()
            logger.info('tiktok trial event sent', user_id=user_id)
    except Exception as exc:
        logger.error('tiktok trial event failed', user_id=user_id, error=str(exc))


async def on_first_connected(db: AsyncSession, user_id: int) -> None:
    """Fire first-VPN-connection event (once per user)."""
    if not _is_enabled():
        return

    try:
        row = await get_ttclid(db, user_id)
        if not row or row.first_connected_sent or not row.ttclid:
            return

        event = settings.TIKTOK_EVENT_FIRST_CONNECTED
        success = await _post_event(
            _event_payload(row.ttclid, event, f'{event}_{user_id}', user_id, ttp=row.ttp),
            'first_connected',
            row.ttclid,
        )
        if success:
            await mark_first_connected_sent(db, user_id)
            await db.commit()
            logger.info('tiktok first-connected event sent', user_id=user_id)
    except Exception as exc:
        logger.error('tiktok first-connected event failed', user_id=user_id, error=str(exc))


async def on_purchase(db: AsyncSession, user_id: int, amount_kopeks: int, transaction_id: int) -> None:
    """Fire purchase event (every payment, no dedup)."""
    if not _is_enabled():
        return

    try:
        row = await get_ttclid(db, user_id)
        if not row or not row.ttclid:
            return

        amount_rubles = amount_kopeks / 100
        payload = _purchase_payload(row.ttclid, f'purchase_{transaction_id}', amount_rubles, user_id, ttp=row.ttp)
        success = await _post_event(payload, 'purchase', row.ttclid)
        if success:
            logger.info('tiktok purchase event sent', user_id=user_id, amount=amount_rubles)
    except Exception as exc:
        logger.error('tiktok purchase event failed', user_id=user_id, error=str(exc))


async def resolve_ttclid_token(short_token: str) -> tuple[str | None, str | None]:
    """Resolve a short redirect token (see tiktok_redirect.py) back to (ttclid, ttp).

    Reads the cache key written by GET /cabinet/go/tiktok, which stores a JSON
    blob of ``{"ttclid": ..., "ttp": ...}``. Also accepts a bare ttclid string
    for tokens cached by an older version of that endpoint (pre-ttp), which
    may still be alive within the 24h TTL right after a deploy. Fail-soft:
    returns ``(None, None)`` on any cache error or expired/unknown token.
    """
    if not short_token:
        return None, None
    try:
        value = await cache.get(f'ttclid:token:{short_token}')
    except Exception as exc:
        logger.warning('Failed to resolve ttclid token', token=short_token, error=str(exc))
        return None, None

    if value is None:
        return None, None

    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return _normalize_ttclid(value), None

    if not isinstance(parsed, dict):
        return None, None
    return _normalize_ttclid(parsed.get('ttclid')), _normalize_ttp(parsed.get('ttp'))
