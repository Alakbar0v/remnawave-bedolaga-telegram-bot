"""Tests for GET /cabinet/go/tiktok — the ttclid capture → bot deep-link redirect.

Telegram's /start deep link is capped at 64 chars (A-Za-z0-9_- only), but
TikTok's ttclid routinely exceeds that. This endpoint stashes the full ttclid
in the cache behind a short token and redirects to
`tg://resolve?domain=<bot>&start={campaign}_subid_tt_<token>` (or, with no campaign,
`_subid_tt_<token>` — an empty campaign head, which `_split_start_param_subid`
must treat as "no campaign").

Route function is called directly (matching the convention in
tests/cabinet/test_admin_legal_pages_routes.py) rather than through a full
ASGI TestClient, since the logic under test is entirely in the handler body.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.cabinet.routes import tiktok_redirect as tiktok_redirect_module
from app.cabinet.routes.tiktok_redirect import tiktok_redirect, tiktok_redirect_json


def _fake_request(ip: str = '203.0.113.5') -> SimpleNamespace:
    return SimpleNamespace(client=SimpleNamespace(host=ip), headers={})


def test_route_registered(registered_paths):
    assert '/cabinet/go/tiktok' in registered_paths
    assert 'GET' in registered_paths['/cabinet/go/tiktok']


def test_json_route_registered(registered_paths):
    assert '/cabinet/go/tiktok/json' in registered_paths
    assert 'GET' in registered_paths['/cabinet/go/tiktok/json']


@pytest.mark.asyncio
async def test_redirect_with_campaign_builds_expected_start_param():
    with (
        patch.object(tiktok_redirect_module.RateLimitCache, 'is_ip_rate_limited', AsyncMock(return_value=False)),
        patch.object(tiktok_redirect_module.settings, 'BOT_USERNAME', 'mybot'),
        patch.object(tiktok_redirect_module.cache, 'set', AsyncMock(return_value=True)) as cache_set_mock,
        patch.object(tiktok_redirect_module.secrets, 'token_urlsafe', return_value='shorttok1234'),
    ):
        response = await tiktok_redirect(_fake_request(), ttclid='ttclid.abcXYZ123', campaign='tiktok1', ttp=None)

    assert response.status_code == 302
    assert response.headers['location'] == 'tg://resolve?domain=mybot&start=tiktok1_subid_tt_shorttok1234'
    cache_set_mock.assert_awaited_once_with(
        'ttclid:token:shorttok1234',
        json.dumps({'ttclid': 'ttclid.abcXYZ123', 'ttp': None}),
        expire=86400,
    )


@pytest.mark.asyncio
async def test_redirect_caches_ttp_alongside_ttclid():
    with (
        patch.object(tiktok_redirect_module.RateLimitCache, 'is_ip_rate_limited', AsyncMock(return_value=False)),
        patch.object(tiktok_redirect_module.settings, 'BOT_USERNAME', 'mybot'),
        patch.object(tiktok_redirect_module.cache, 'set', AsyncMock(return_value=True)) as cache_set_mock,
        patch.object(tiktok_redirect_module.secrets, 'token_urlsafe', return_value='shorttok1234'),
    ):
        response = await tiktok_redirect(
            _fake_request(), ttclid='ttclid.abcXYZ123', campaign='tiktok1', ttp='ttp.cookie456'
        )

    assert response.status_code == 302
    cache_set_mock.assert_awaited_once_with(
        'ttclid:token:shorttok1234',
        json.dumps({'ttclid': 'ttclid.abcXYZ123', 'ttp': 'ttp.cookie456'}),
        expire=86400,
    )


@pytest.mark.asyncio
async def test_json_variant_returns_same_deep_link_as_redirect():
    """The background-prefetch endpoint must build the exact same deep link
    the 302 redirect would have — same helper, same cache-write side effect."""
    with (
        patch.object(tiktok_redirect_module.RateLimitCache, 'is_ip_rate_limited', AsyncMock(return_value=False)),
        patch.object(tiktok_redirect_module.settings, 'BOT_USERNAME', 'mybot'),
        patch.object(tiktok_redirect_module.cache, 'set', AsyncMock(return_value=True)) as cache_set_mock,
        patch.object(tiktok_redirect_module.secrets, 'token_urlsafe', return_value='shorttok1234'),
    ):
        result = await tiktok_redirect_json(
            _fake_request(), ttclid='ttclid.abcXYZ123', campaign='tiktok1', ttp=None
        )

    assert result == {'url': 'tg://resolve?domain=mybot&start=tiktok1_subid_tt_shorttok1234'}
    cache_set_mock.assert_awaited_once_with(
        'ttclid:token:shorttok1234',
        json.dumps({'ttclid': 'ttclid.abcXYZ123', 'ttp': None}),
        expire=86400,
    )


@pytest.mark.asyncio
async def test_json_variant_rate_limited_returns_429():
    with patch.object(tiktok_redirect_module.RateLimitCache, 'is_ip_rate_limited', AsyncMock(return_value=True)):
        with pytest.raises(HTTPException) as exc_info:
            await tiktok_redirect_json(_fake_request(), ttclid='ttclid.abcXYZ123', campaign=None, ttp=None)

    assert exc_info.value.status_code == 429


@pytest.mark.asyncio
async def test_redirect_without_campaign_uses_empty_head_format():
    """No campaign → `_subid_tt_<token>` (leading underscore = empty head),
    which _split_start_param_subid must resolve to (None, 'tt_<token>')."""
    with (
        patch.object(tiktok_redirect_module.RateLimitCache, 'is_ip_rate_limited', AsyncMock(return_value=False)),
        patch.object(tiktok_redirect_module.settings, 'BOT_USERNAME', 'mybot'),
        patch.object(tiktok_redirect_module.cache, 'set', AsyncMock(return_value=True)),
        patch.object(tiktok_redirect_module.secrets, 'token_urlsafe', return_value='shorttok1234'),
    ):
        response = await tiktok_redirect(_fake_request(), ttclid='ttclid.abcXYZ123', campaign=None, ttp=None)

    assert response.status_code == 302
    start_param = response.headers['location'].split('start=')[1]
    assert start_param == '_subid_tt_shorttok1234'

    from app.handlers.start import _split_start_param_subid

    assert _split_start_param_subid(start_param) == (None, 'tt_shorttok1234')


@pytest.mark.asyncio
async def test_redirect_at_the_64_char_boundary_succeeds():
    """campaign(42) + '_subid_tt_' (10) + token(12) == 64 exactly — must pass."""
    campaign = 'x' * 42
    with (
        patch.object(tiktok_redirect_module.RateLimitCache, 'is_ip_rate_limited', AsyncMock(return_value=False)),
        patch.object(tiktok_redirect_module.settings, 'BOT_USERNAME', 'mybot'),
        patch.object(tiktok_redirect_module.cache, 'set', AsyncMock(return_value=True)),
        patch.object(tiktok_redirect_module.secrets, 'token_urlsafe', return_value='shorttok1234'),
    ):
        response = await tiktok_redirect(_fake_request(), ttclid='ttclid.abcXYZ123', campaign=campaign, ttp=None)

    start_param = response.headers['location'].split('start=')[1]
    assert len(start_param) == 64


@pytest.mark.asyncio
async def test_redirect_rejects_campaign_too_long_for_deep_link():
    """One char over the 64-char boundary must be rejected with a clear 400,
    not a silently-broken deep link. Bypasses the Query() pattern validation
    (only enforced by FastAPI's own request layer) to pin the handler's own
    defensive length check."""
    too_long_campaign = 'x' * 43
    with (
        patch.object(tiktok_redirect_module.RateLimitCache, 'is_ip_rate_limited', AsyncMock(return_value=False)),
        patch.object(tiktok_redirect_module.settings, 'BOT_USERNAME', 'mybot'),
        patch.object(tiktok_redirect_module.cache, 'set', AsyncMock(return_value=True)),
        patch.object(tiktok_redirect_module.secrets, 'token_urlsafe', return_value='shorttok1234'),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await tiktok_redirect(_fake_request(), ttclid='ttclid.abcXYZ123', campaign=too_long_campaign, ttp=None)

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_redirect_missing_bot_username_returns_503():
    with (
        patch.object(tiktok_redirect_module.RateLimitCache, 'is_ip_rate_limited', AsyncMock(return_value=False)),
        patch.object(tiktok_redirect_module.settings, 'BOT_USERNAME', None),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await tiktok_redirect(_fake_request(), ttclid='ttclid.abcXYZ123', campaign=None, ttp=None)

    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_redirect_rate_limited_returns_429():
    with patch.object(tiktok_redirect_module.RateLimitCache, 'is_ip_rate_limited', AsyncMock(return_value=True)):
        with pytest.raises(HTTPException) as exc_info:
            await tiktok_redirect(_fake_request(), ttclid='ttclid.abcXYZ123', campaign=None, ttp=None)

    assert exc_info.value.status_code == 429


@pytest.mark.asyncio
async def test_redirect_rejects_campaign_containing_subid_delimiter():
    with (
        patch.object(tiktok_redirect_module.RateLimitCache, 'is_ip_rate_limited', AsyncMock(return_value=False)),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await tiktok_redirect(_fake_request(), ttclid='ttclid.abcXYZ123', campaign='foo_subid_bar', ttp=None)

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_redirect_survives_cache_failure():
    """Cache being down must not break the redirect — best-effort write."""
    with (
        patch.object(tiktok_redirect_module.RateLimitCache, 'is_ip_rate_limited', AsyncMock(return_value=False)),
        patch.object(tiktok_redirect_module.settings, 'BOT_USERNAME', 'mybot'),
        patch.object(tiktok_redirect_module.cache, 'set', AsyncMock(side_effect=RuntimeError('redis down'))),
        patch.object(tiktok_redirect_module.secrets, 'token_urlsafe', return_value='shorttok1234'),
    ):
        response = await tiktok_redirect(_fake_request(), ttclid='ttclid.abcXYZ123', campaign='tiktok1', ttp=None)

    assert response.status_code == 302
