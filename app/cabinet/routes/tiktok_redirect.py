"""TikTok click-id (ttclid) capture → Telegram bot deep-link redirect.

Telegram's `/start` deep link is capped at 64 chars, alphabet `A-Za-z0-9_-`
(https://core.telegram.org/bots/features#deep-linking). TikTok's `ttclid` is an
opaque token that routinely exceeds that budget, so it can never ride directly
on `/start`. This endpoint accepts the full `ttclid` from the TikTok click URL,
stashes it in the cache behind a short token, and redirects to the bot using
the existing `{campaign}_subid_{payload}` deep-link format (see
app/handlers/start.py `_split_start_param_subid`), with payload `tt_<token>`.
"""

from __future__ import annotations

import json
import re
import secrets

import structlog
from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse

from app.cabinet.ip_utils import get_client_ip
from app.config import settings
from app.utils.cache import RateLimitCache, cache


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/go', tags=['TikTok Redirect'])

_TTCLID_PATTERN = r'^[A-Za-z0-9._-]{1,512}$'
_TTP_PATTERN = r'^[A-Za-z0-9._-]{1,256}$'
_CAMPAIGN_PATTERN = r'^[A-Za-z0-9_-]{1,42}$'
_START_PARAM_RE = re.compile(r'^[A-Za-z0-9_-]+$')
_SUBID_DELIMITER = '_subid_'
_TOKEN_TTL_SECONDS = 86400
_MAX_START_PARAM_LEN = 64


async def _build_deep_link(
    request: Request,
    ttclid: str,
    campaign: str | None,
    ttp: str | None,
) -> str:
    """Capture a TikTok ttclid and return the bot's /start deep link URL."""
    client_ip = get_client_ip(request)
    # Logged at info level so a real/curl test click can be eyeballed in the
    # deploy logs to confirm CABINET_TRUSTED_PROXIES is resolving the real
    # visitor IP behind the reverse proxy, not the proxy's own address.
    logger.info(
        'tiktok redirect click received',
        client_ip=client_ip,
        peer_ip=request.client.host if request.client else None,
        campaign=campaign,
    )
    if await RateLimitCache.is_ip_rate_limited(client_ip, 'tiktok_redirect', limit=30, window=60, fail_closed=True):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail='Too many requests',
            headers={'Retry-After': '60'},
        )

    if campaign and _SUBID_DELIMITER in f'{campaign}_':
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Invalid campaign code')

    bot_username = settings.get_bot_username()
    if not bot_username:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail='Bot username is not configured')

    token = secrets.token_urlsafe(9)  # 12 url-safe chars, well within the Telegram deep-link alphabet

    # Captured here rather than later in the bot flow: this is the actual click
    # from the user's device on the TikTok ad, before it hops through Telegram's
    # /start deep link (which carries no IP/UA of its own) — the best-quality
    # signal for TikTok Events API's `user.ip` / `user.user_agent` match fields.
    user_agent = request.headers.get('user-agent', '')[:512] or None

    try:
        cached_value = json.dumps({'ttclid': ttclid, 'ttp': ttp, 'ip': client_ip, 'user_agent': user_agent})
        await cache.set(f'ttclid:token:{token}', cached_value, expire=_TOKEN_TTL_SECONDS)
    except Exception:
        logger.warning('Failed to cache ttclid token', token=token)

    payload = f'tt_{token}'
    start_param = f'{campaign}{_SUBID_DELIMITER}{payload}' if campaign else f'{_SUBID_DELIMITER}{payload}'

    if len(start_param) > _MAX_START_PARAM_LEN or not _START_PARAM_RE.match(start_param):
        logger.error('Generated /start param would overflow Telegram deep-link limits', campaign=campaign)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Campaign code too long for a TikTok deep link',
        )

    return f'tg://resolve?domain={bot_username}&start={start_param}'


@router.get('/tiktok', include_in_schema=True)
async def tiktok_redirect(
    request: Request,
    ttclid: str = Query(..., pattern=_TTCLID_PATTERN),
    campaign: str | None = Query(None, pattern=_CAMPAIGN_PATTERN),
    ttp: str | None = Query(None, pattern=_TTP_PATTERN),
) -> RedirectResponse:
    """No-JS fallback: capture ttclid and 302 straight to the bot's /start deep link."""
    deep_link = await _build_deep_link(request, ttclid, campaign, ttp)
    return RedirectResponse(url=deep_link, status_code=status.HTTP_302_FOUND)


@router.get('/tiktok/json', include_in_schema=True)
async def tiktok_redirect_json(
    request: Request,
    ttclid: str = Query(..., pattern=_TTCLID_PATTERN),
    campaign: str | None = Query(None, pattern=_CAMPAIGN_PATTERN),
    ttp: str | None = Query(None, pattern=_TTP_PATTERN),
) -> dict[str, str]:
    """JS variant: same capture logic, returned as JSON so the landing page can
    prefetch the deep link in the background and open it instantly on click,
    instead of navigating the browser through this endpoint (see `_build_deep_link`,
    and the frontend script in the ConsiliumVPN landing page)."""
    deep_link = await _build_deep_link(request, ttclid, campaign, ttp)
    return {'url': deep_link}
