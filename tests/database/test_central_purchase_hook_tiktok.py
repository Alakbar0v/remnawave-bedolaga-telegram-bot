"""Central TikTok Events purchase-fire hook in `create_transaction`.

Mirrors tests/database/test_central_purchase_hook.py (the Yandex analog), which
documents the full architectural background: every completed
SUBSCRIPTION_PAYMENT flows through `create_transaction` (commit=True) or
`emit_transaction_side_effects` (commit=False deferred path), and the purchase
event must fire exactly once per paid purchase.

The TikTok hook sits right next to the Yandex one at both call sites and takes
an extra `transaction.id` argument (used to build a stable `event_id` for
TikTok-side deduplication, since purchases have no `*_sent` DB flag — unlike
registration/trial/first-connected, every payment fires its own event).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.database.crud import transaction as tx_crud
from app.database.models import TransactionType


def _stub_db() -> SimpleNamespace:
    db = SimpleNamespace()
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    db.refresh = AsyncMock()
    return db


@pytest.fixture
def tiktok_spy(monkeypatch):
    """Patch both conversion services' hooks plus the other lazy side-effects."""
    spawn_mock = MagicMock()
    fire_mock = MagicMock()
    monkeypatch.setattr('app.services.tiktok_events_service.spawn_bg', spawn_mock)
    monkeypatch.setattr('app.services.tiktok_events_service.fire_purchase_bg', fire_mock)

    # Yandex hook lives at the same call sites — neutralise it too so this
    # test file stays focused on the TikTok contract.
    monkeypatch.setattr('app.services.yandex_offline_conv_service.spawn_bg', MagicMock())
    monkeypatch.setattr('app.services.yandex_offline_conv_service.fire_purchase_bg', MagicMock())

    monkeypatch.setattr('app.services.event_emitter.event_emitter.emit', AsyncMock(return_value=None))
    monkeypatch.setattr(
        'app.services.promo_group_assignment.maybe_assign_promo_group_by_total_spent',
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        'app.services.referral_contest_service.referral_contest_service.on_subscription_payment',
        AsyncMock(return_value=None),
    )
    return spawn_mock, fire_mock


# ── create_transaction (commit=True, inline path) ──────────────────────────


async def test_completed_subscription_payment_fires_once_with_transaction_id(tiktok_spy):
    spawn_mock, fire_mock = tiktok_spy
    db = _stub_db()

    tx = await tx_crud.create_transaction(
        db,
        user_id=42,
        type=TransactionType.SUBSCRIPTION_PAYMENT,
        amount_kopeks=29900,
        description='Подписка 30 дней',
        is_completed=True,
        commit=True,
    )

    fire_mock.assert_called_once_with(42, 29900, tx.id)
    spawn_mock.assert_called_once()


async def test_deposit_does_not_fire(tiktok_spy):
    spawn_mock, fire_mock = tiktok_spy
    db = _stub_db()

    await tx_crud.create_transaction(
        db,
        user_id=42,
        type=TransactionType.DEPOSIT,
        amount_kopeks=50000,
        description='Пополнение баланса',
        is_completed=True,
        commit=True,
    )

    fire_mock.assert_not_called()
    spawn_mock.assert_not_called()


async def test_not_completed_subscription_payment_does_not_fire_inline(tiktok_spy):
    spawn_mock, fire_mock = tiktok_spy
    db = _stub_db()

    await tx_crud.create_transaction(
        db,
        user_id=42,
        type=TransactionType.SUBSCRIPTION_PAYMENT,
        amount_kopeks=29900,
        description='Подписка (ожидает оплаты)',
        is_completed=False,
        commit=True,
    )

    fire_mock.assert_not_called()
    spawn_mock.assert_not_called()


async def test_commit_false_does_not_fire_inline(tiktok_spy):
    spawn_mock, fire_mock = tiktok_spy
    db = _stub_db()

    await tx_crud.create_transaction(
        db,
        user_id=42,
        type=TransactionType.SUBSCRIPTION_PAYMENT,
        amount_kopeks=29900,
        description='Подписка 30 дней',
        is_completed=True,
        commit=False,
    )

    fire_mock.assert_not_called()
    spawn_mock.assert_not_called()


async def test_negative_stored_amount_fires_positive_abs(tiktok_spy):
    spawn_mock, fire_mock = tiktok_spy
    db = _stub_db()

    tx = await tx_crud.create_transaction(
        db,
        user_id=7,
        type=TransactionType.SUBSCRIPTION_PAYMENT,
        amount_kopeks=79900,
        description='Подписка 90 дней',
        is_completed=True,
        commit=True,
    )

    fire_mock.assert_called_once_with(7, 79900, tx.id)
    assert fire_mock.call_args.args[1] > 0


# ── emit_transaction_side_effects (commit=False deferred path) ─────────────


async def test_deferred_subscription_payment_fires_once_with_transaction_id(tiktok_spy):
    spawn_mock, fire_mock = tiktok_spy
    db = _stub_db()
    fake_tx = SimpleNamespace(id=123)

    await tx_crud.emit_transaction_side_effects(
        db,
        fake_tx,
        amount_kopeks=29900,
        user_id=42,
        type=TransactionType.SUBSCRIPTION_PAYMENT,
        is_completed=True,
        description='Подписка 30 дней',
    )

    fire_mock.assert_called_once_with(42, 29900, 123)
    spawn_mock.assert_called_once()


async def test_deferred_deposit_does_not_fire(tiktok_spy):
    spawn_mock, fire_mock = tiktok_spy
    db = _stub_db()
    fake_tx = SimpleNamespace(id=124)

    await tx_crud.emit_transaction_side_effects(
        db,
        fake_tx,
        amount_kopeks=50000,
        user_id=42,
        type=TransactionType.DEPOSIT,
        is_completed=True,
        description='Пополнение',
    )

    fire_mock.assert_not_called()
    spawn_mock.assert_not_called()


# ── no double-fire across the two paths for a single transaction ───────────


async def test_single_transaction_does_not_double_fire(tiktok_spy):
    spawn_mock, fire_mock = tiktok_spy
    db = _stub_db()

    tx = await tx_crud.create_transaction(
        db,
        user_id=42,
        type=TransactionType.SUBSCRIPTION_PAYMENT,
        amount_kopeks=29900,
        description='Подписка 30 дней',
        is_completed=True,
        commit=False,
    )
    fire_mock.assert_not_called()

    await tx_crud.emit_transaction_side_effects(
        db,
        tx,
        amount_kopeks=29900,
        user_id=42,
        type=TransactionType.SUBSCRIPTION_PAYMENT,
        is_completed=True,
        description='Подписка 30 дней',
    )

    fire_mock.assert_called_once_with(42, 29900, tx.id)
    spawn_mock.assert_called_once()
