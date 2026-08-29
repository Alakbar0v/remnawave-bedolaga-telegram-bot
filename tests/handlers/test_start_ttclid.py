"""Tests for the TikTok ``ttclid`` half of the ``{campaign}_subid_{payload}``
/start deeplink mechanism.

Companion to tests/handlers/test_start_subid.py (parser) and
tests/handlers/test_start_subid_drain.py (Keitaro subid drain). Covers:
  1. ``_split_start_param_subid`` correctly yields an empty/None head for the
     no-campaign TikTok redirect format (``_subid_tt_<token>``).
  2. ``_persist_pending_ttclid_after_registration`` — the drain that stores the
     resolved ttclid and, for brand-new registrations, fires the
     CompleteRegistration event via its own DB session (never the caller's
     open transaction — see the docstring in start.py for the race it avoids).
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.handlers import start as start_module
from app.handlers.start import _TTCLID_PREFIX, _persist_pending_ttclid_after_registration, _split_start_param_subid


# ── parser: no-campaign TikTok redirect format ──────────────────────────────


class TestSplitStartParamSubidForTikTok:
    def test_no_campaign_ttclid_payload_yields_none_head(self) -> None:
        token = f'{_TTCLID_PREFIX}abc123XYZ90'
        assert _split_start_param_subid(f'_subid_{token}') == (None, token)

    def test_campaign_plus_ttclid_payload(self) -> None:
        token = f'{_TTCLID_PREFIX}abc123XYZ90'
        assert _split_start_param_subid(f'tiktok1_subid_{token}') == ('tiktok1', token)

    def test_ttclid_tail_prefix_is_preserved_verbatim(self) -> None:
        # The parser itself doesn't know about `tt_` — prefix inspection happens
        # at the call site in cmd_start. Pin that the tail is untouched.
        head, tail = _split_start_param_subid('promo_subid_tt_shorttok123')
        assert head == 'promo'
        assert tail == 'tt_shorttok123'
        assert tail.startswith(_TTCLID_PREFIX)


# ── drain: _persist_pending_ttclid_after_registration ───────────────────────


@pytest.mark.anyio('asyncio')
async def test_drain_fires_registration_for_new_user(monkeypatch: pytest.MonkeyPatch) -> None:
    """New-registration call sites pass fire_registration=True — the drain must
    delegate to store_ttclid_and_fire_registration (own session + commit + fire),
    not touch the caller's still-open transaction."""
    user = SimpleNamespace(id=42)
    state = SimpleNamespace(get_data=AsyncMock(return_value={'pending_ttclid': 'ttclid.abc123'}))

    store_and_fire_mock = AsyncMock()
    store_only_mock = AsyncMock()
    monkeypatch.setattr('app.services.tiktok_events_service.store_ttclid_and_fire_registration', store_and_fire_mock)
    monkeypatch.setattr('app.services.tiktok_events_service.store_ttclid_only', store_only_mock)

    await _persist_pending_ttclid_after_registration(state, user, fire_registration=True)

    store_and_fire_mock.assert_awaited_once_with(
        42, 'ttclid.abc123', source='telegram', ttp=None, ip=None, user_agent=None
    )
    store_only_mock.assert_not_called()


@pytest.mark.anyio('asyncio')
async def test_drain_passes_through_pending_ttp(monkeypatch: pytest.MonkeyPatch) -> None:
    """The `_ttp` cookie value captured alongside ttclid must ride along into
    store_ttclid_and_fire_registration for TikTok Events API advanced matching."""
    user = SimpleNamespace(id=42)
    state = SimpleNamespace(
        get_data=AsyncMock(return_value={'pending_ttclid': 'ttclid.abc123', 'pending_ttp': 'ttp.cookie456'})
    )

    store_and_fire_mock = AsyncMock()
    monkeypatch.setattr('app.services.tiktok_events_service.store_ttclid_and_fire_registration', store_and_fire_mock)

    await _persist_pending_ttclid_after_registration(state, user, fire_registration=True)

    store_and_fire_mock.assert_awaited_once_with(
        42, 'ttclid.abc123', source='telegram', ttp='ttp.cookie456', ip=None, user_agent=None
    )


@pytest.mark.anyio('asyncio')
async def test_drain_passes_through_pending_ip_and_user_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    """The click IP/UA captured at the TikTok ad-click redirect must ride along
    into store_ttclid_and_fire_registration for TikTok Events API matching."""
    user = SimpleNamespace(id=42)
    state = SimpleNamespace(
        get_data=AsyncMock(
            return_value={
                'pending_ttclid': 'ttclid.abc123',
                'pending_ttclid_ip': '203.0.113.5',
                'pending_ttclid_ua': 'Mozilla/5.0',
            }
        )
    )

    store_and_fire_mock = AsyncMock()
    monkeypatch.setattr('app.services.tiktok_events_service.store_ttclid_and_fire_registration', store_and_fire_mock)

    await _persist_pending_ttclid_after_registration(state, user, fire_registration=True)

    store_and_fire_mock.assert_awaited_once_with(
        42, 'ttclid.abc123', source='telegram', ttp=None, ip='203.0.113.5', user_agent='Mozilla/5.0'
    )


@pytest.mark.anyio('asyncio')
async def test_drain_stores_only_for_existing_user(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cmd_start fast-path (user already registered) passes
    fire_registration=False — must persist the ttclid without re-firing
    CompleteRegistration."""
    user = SimpleNamespace(id=42)
    state = SimpleNamespace(get_data=AsyncMock(return_value={'pending_ttclid': 'ttclid.abc123'}))

    store_and_fire_mock = AsyncMock()
    store_only_mock = AsyncMock()
    monkeypatch.setattr('app.services.tiktok_events_service.store_ttclid_and_fire_registration', store_and_fire_mock)
    monkeypatch.setattr('app.services.tiktok_events_service.store_ttclid_only', store_only_mock)

    await _persist_pending_ttclid_after_registration(state, user, fire_registration=False)

    store_only_mock.assert_awaited_once_with(42, 'ttclid.abc123', source='telegram', ttp=None, ip=None, user_agent=None)
    store_and_fire_mock.assert_not_called()


@pytest.mark.anyio('asyncio')
async def test_drain_is_noop_when_no_pending_ttclid(monkeypatch: pytest.MonkeyPatch) -> None:
    user = SimpleNamespace(id=42)
    state = SimpleNamespace(get_data=AsyncMock(return_value={}))

    store_and_fire_mock = AsyncMock()
    monkeypatch.setattr('app.services.tiktok_events_service.store_ttclid_and_fire_registration', store_and_fire_mock)

    await _persist_pending_ttclid_after_registration(state, user, fire_registration=True)

    store_and_fire_mock.assert_not_called()


@pytest.mark.anyio('asyncio')
async def test_drain_is_noop_when_get_data_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    user = SimpleNamespace(id=42)
    state = SimpleNamespace(get_data=AsyncMock(return_value=None))

    store_and_fire_mock = AsyncMock()
    monkeypatch.setattr('app.services.tiktok_events_service.store_ttclid_and_fire_registration', store_and_fire_mock)

    await _persist_pending_ttclid_after_registration(state, user, fire_registration=True)

    store_and_fire_mock.assert_not_called()


@pytest.mark.anyio('asyncio')
async def test_drain_swallows_store_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A DB/network hiccup while storing the ttclid must never crash the
    registration-completion flow — matches the Keitaro subid drain's contract."""
    user = SimpleNamespace(id=42)
    state = SimpleNamespace(get_data=AsyncMock(return_value={'pending_ttclid': 'ttclid.abc123'}))

    failing_mock = AsyncMock(side_effect=RuntimeError('cache down'))
    monkeypatch.setattr('app.services.tiktok_events_service.store_ttclid_and_fire_registration', failing_mock)

    await _persist_pending_ttclid_after_registration(state, user, fire_registration=True)

    failing_mock.assert_awaited_once()


def test_drain_function_imported_from_start_module() -> None:
    assert callable(start_module._persist_pending_ttclid_after_registration)
