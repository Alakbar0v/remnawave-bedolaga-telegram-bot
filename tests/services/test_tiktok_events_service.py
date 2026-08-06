"""Tests for the TikTok Events API 2.0 server-side conversion service.

Mirrors tests/services/test_yandex_purchase_hook.py's mocking conventions.
Covers:
  1. `_is_enabled()` gating (enabled requires flag + pixel + token, all three).
  2. Events API 2.0 payload shape for each event type.
  3. `_post_event` success/failure semantics: a 2xx HTTP status is not enough —
     TikTok returns logical errors inside a 200 body (`code` != 0), which must
     NOT be retried (the request was understood, just rejected).
  4. `store_ttclid_and_fire_registration` — store-then-fire contract.
  5. `on_registration` / `on_trial` / `on_first_connected` dedup via their
     `*_sent` flags; `on_purchase` fires on every call (no dedup flag).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import tiktok_events_service as tiktok_events


# ── _is_enabled ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ('enabled', 'pixel', 'token', 'expected'),
    [
        (True, 'pixel123', 'token456', True),
        (False, 'pixel123', 'token456', False),
        (True, '', 'token456', False),
        (True, 'pixel123', '', False),
        (False, '', '', False),
    ],
)
def test_is_enabled_requires_all_three(enabled, pixel, token, expected) -> None:
    with (
        patch.object(tiktok_events.settings, 'TIKTOK_EVENTS_ENABLED', enabled),
        patch.object(tiktok_events.settings, 'TIKTOK_PIXEL_CODE', pixel),
        patch.object(tiktok_events.settings, 'TIKTOK_EVENTS_ACCESS_TOKEN', token),
    ):
        assert tiktok_events._is_enabled() is expected


# ── payload shape (Events API 2.0) ──────────────────────────────────────────


def test_event_payload_shape() -> None:
    with patch.object(tiktok_events.settings, 'TIKTOK_PIXEL_CODE', 'pixel123'):
        payload = tiktok_events._event_payload(
            'ttclid.abc123', tiktok_events.EVENT_REGISTRATION, 'CompleteRegistration_42', 42
        )

    assert payload['event_source'] == 'web'
    assert payload['event_source_id'] == 'pixel123'
    assert len(payload['data']) == 1
    entry = payload['data'][0]
    assert entry['event'] == 'CompleteRegistration'
    assert entry['event_id'] == 'CompleteRegistration_42'
    assert entry['user'] == {'ttclid': 'ttclid.abc123', 'external_id': tiktok_events._hash_external_id(42)}
    assert isinstance(entry['event_time'], int)
    assert 'properties' not in entry


def test_purchase_payload_includes_value_and_currency() -> None:
    with (
        patch.object(tiktok_events.settings, 'TIKTOK_PIXEL_CODE', 'pixel123'),
        patch.object(tiktok_events.settings, 'TIKTOK_EVENTS_CURRENCY', 'RUB'),
    ):
        payload = tiktok_events._purchase_payload('ttclid.abc123', 'purchase_555', 299.0, 42)

    entry = payload['data'][0]
    assert entry['event'] == 'CompletePayment'
    assert entry['event_id'] == 'purchase_555'
    assert entry['properties'] == {'currency': 'RUB', 'value': 299.0}
    assert entry['user']['external_id'] == tiktok_events._hash_external_id(42)


# ── _post_event ──────────────────────────────────────────────────────────────


def _fake_client(responses: list[SimpleNamespace]) -> MagicMock:
    client = MagicMock()
    client.post = AsyncMock(side_effect=responses)
    return client


@pytest.mark.asyncio
async def test_post_event_success_with_code_zero() -> None:
    resp = SimpleNamespace(status_code=200, json=lambda: {'code': 0, 'message': 'OK'}, text='{}')
    with patch.object(tiktok_events, '_get_client', return_value=_fake_client([resp])):
        ok = await tiktok_events._post_event({'x': 1}, 'registration', 'ttclid.abc123')
    assert ok is True


@pytest.mark.asyncio
async def test_post_event_200_with_nonzero_code_is_failure_not_retried() -> None:
    """TikTok can 200 an ill-formed event (e.g. bad pixel) with code != 0 —
    that's a logical rejection, not a transient error, so it must not retry."""
    resp = SimpleNamespace(status_code=200, json=lambda: {'code': 40002, 'message': 'Invalid parameter'}, text='{}')
    client = _fake_client([resp])
    with patch.object(tiktok_events, '_get_client', return_value=client):
        ok = await tiktok_events._post_event({'x': 1}, 'registration', 'ttclid.abc123')
    assert ok is False
    assert client.post.await_count == 1


@pytest.mark.asyncio
async def test_post_event_retries_on_5xx_then_succeeds() -> None:
    fail_resp = SimpleNamespace(status_code=503, text='service unavailable')
    ok_resp = SimpleNamespace(status_code=200, json=lambda: {'code': 0}, text='{}')
    client = _fake_client([fail_resp, ok_resp])
    with (
        patch.object(tiktok_events, '_get_client', return_value=client),
        patch.object(tiktok_events.asyncio, 'sleep', AsyncMock()),
    ):
        ok = await tiktok_events._post_event({'x': 1}, 'purchase', 'ttclid.abc123')
    assert ok is True
    assert client.post.await_count == 2


@pytest.mark.asyncio
async def test_post_event_exhausts_retries_on_persistent_5xx() -> None:
    fail_resp = SimpleNamespace(status_code=500, text='boom')
    client = _fake_client([fail_resp, fail_resp, fail_resp])
    with (
        patch.object(tiktok_events, '_get_client', return_value=client),
        patch.object(tiktok_events.asyncio, 'sleep', AsyncMock()),
    ):
        ok = await tiktok_events._post_event({'x': 1}, 'purchase', 'ttclid.abc123')
    assert ok is False
    assert client.post.await_count == tiktok_events.MAX_RETRIES


@pytest.mark.asyncio
async def test_post_event_4xx_fails_without_retry() -> None:
    resp = SimpleNamespace(status_code=401, text='unauthorized')
    client = _fake_client([resp])
    with patch.object(tiktok_events, '_get_client', return_value=client):
        ok = await tiktok_events._post_event({'x': 1}, 'purchase', 'ttclid.abc123')
    assert ok is False
    assert client.post.await_count == 1


# ── store_ttclid_and_fire_registration ──────────────────────────────────────


@pytest.mark.asyncio
async def test_store_and_fire_registration_passes_through() -> None:
    with (
        patch.object(tiktok_events, 'store_ttclid', AsyncMock(return_value=True)) as store_mock,
        patch.object(tiktok_events, 'spawn_bg') as spawn_mock,
        patch.object(tiktok_events, 'fire_registration_bg') as fire_mock,
        patch.object(tiktok_events, 'AsyncSessionLocal') as session_local,
    ):
        session_local.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
        session_local.return_value.__aexit__ = AsyncMock(return_value=None)

        await tiktok_events.store_ttclid_and_fire_registration(42, 'ttclid.abc123', source='telegram')

        store_mock.assert_awaited_once()
        assert store_mock.await_args.args[1] == 42
        assert store_mock.await_args.args[2] == 'ttclid.abc123'
        fire_mock.assert_called_once_with(42)
        spawn_mock.assert_called_once()


@pytest.mark.asyncio
async def test_store_and_fire_registration_noop_without_ttclid() -> None:
    with (
        patch.object(tiktok_events, 'store_ttclid', AsyncMock()) as store_mock,
        patch.object(tiktok_events, 'spawn_bg') as spawn_mock,
    ):
        await tiktok_events.store_ttclid_and_fire_registration(42, None)

        store_mock.assert_not_called()
        spawn_mock.assert_not_called()


@pytest.mark.asyncio
async def test_store_ttclid_only_noop_when_disabled() -> None:
    with (
        patch.object(tiktok_events, '_is_enabled', return_value=False),
        patch.object(tiktok_events, 'store_ttclid', AsyncMock()) as store_mock,
    ):
        await tiktok_events.store_ttclid_only(42, 'ttclid.abc123')
        store_mock.assert_not_called()


# ── on_registration / on_trial / on_first_connected dedup ──────────────────


@pytest.mark.asyncio
async def test_on_registration_skips_when_already_sent() -> None:
    row = SimpleNamespace(ttclid='ttclid.abc123', registration_sent=True)
    with (
        patch.object(tiktok_events, '_is_enabled', return_value=True),
        patch.object(tiktok_events, 'get_ttclid', AsyncMock(return_value=row)),
        patch.object(tiktok_events, '_post_event', AsyncMock()) as post_mock,
    ):
        await tiktok_events.on_registration(AsyncMock(), 42)
    post_mock.assert_not_called()


@pytest.mark.asyncio
async def test_on_registration_skips_without_ttclid_row() -> None:
    with (
        patch.object(tiktok_events, '_is_enabled', return_value=True),
        patch.object(tiktok_events, 'get_ttclid', AsyncMock(return_value=None)),
        patch.object(tiktok_events, '_post_event', AsyncMock()) as post_mock,
    ):
        await tiktok_events.on_registration(AsyncMock(), 42)
    post_mock.assert_not_called()


@pytest.mark.asyncio
async def test_on_registration_fires_and_marks_sent() -> None:
    row = SimpleNamespace(ttclid='ttclid.abc123', registration_sent=False)
    db = AsyncMock()
    with (
        patch.object(tiktok_events, '_is_enabled', return_value=True),
        patch.object(tiktok_events, 'get_ttclid', AsyncMock(return_value=row)),
        patch.object(tiktok_events, '_post_event', AsyncMock(return_value=True)) as post_mock,
        patch.object(tiktok_events, 'mark_registration_sent', AsyncMock()) as mark_mock,
    ):
        await tiktok_events.on_registration(db, 42)

    post_mock.assert_awaited_once()
    mark_mock.assert_awaited_once_with(db, 42)
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_on_purchase_fires_every_call_no_dedup_flag() -> None:
    """Unlike registration/trial/first-connected, purchases have no dedup flag —
    every completed payment should fire its own event."""
    row = SimpleNamespace(ttclid='ttclid.abc123')
    db = AsyncMock()
    with (
        patch.object(tiktok_events, '_is_enabled', return_value=True),
        patch.object(tiktok_events, 'get_ttclid', AsyncMock(return_value=row)),
        patch.object(tiktok_events, '_post_event', AsyncMock(return_value=True)) as post_mock,
    ):
        await tiktok_events.on_purchase(db, 42, 29900, 555)
        await tiktok_events.on_purchase(db, 42, 19900, 556)

    assert post_mock.await_count == 2


@pytest.mark.asyncio
async def test_on_purchase_noop_without_ttclid() -> None:
    with (
        patch.object(tiktok_events, '_is_enabled', return_value=True),
        patch.object(tiktok_events, 'get_ttclid', AsyncMock(return_value=None)),
        patch.object(tiktok_events, '_post_event', AsyncMock()) as post_mock,
    ):
        await tiktok_events.on_purchase(AsyncMock(), 42, 29900, 555)
    post_mock.assert_not_called()


# ── resolve_ttclid_token ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_ttclid_token_reads_cache_key() -> None:
    with patch.object(tiktok_events.cache, 'get', AsyncMock(return_value='ttclid.abc123')) as get_mock:
        result = await tiktok_events.resolve_ttclid_token('shorttok123')

    get_mock.assert_awaited_once_with('ttclid:token:shorttok123')
    assert result == 'ttclid.abc123'


@pytest.mark.asyncio
async def test_resolve_ttclid_token_returns_none_when_missing() -> None:
    with patch.object(tiktok_events.cache, 'get', AsyncMock(return_value=None)):
        result = await tiktok_events.resolve_ttclid_token('unknowntoken')
    assert result is None


@pytest.mark.asyncio
async def test_resolve_ttclid_token_empty_input() -> None:
    result = await tiktok_events.resolve_ttclid_token('')
    assert result is None
