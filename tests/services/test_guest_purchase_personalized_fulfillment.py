"""Regression tests for personalized (?tgid=) landing purchase fulfillment.

Background
----------
The anonymous guest-purchase flow resolves the recipient by contact form
(_find_or_create_user) and, if that user already has an active subscription,
holds the purchase in PENDING_ACTIVATION for a manual "activate" step
(protective, since identity there is only confirmed by an email/username the
buyer typed in).

A personalized (?tgid=) purchase already knows the buyer's account at
creation time (bound via purchase.user_id / personalized_telegram_id), so
fulfill_purchase must skip both: no _find_or_create_user call, no
PENDING_ACTIVATION hold — deliver immediately. If the user already has a
TRIAL or LIMITED subscription (not blocked upstream — see
user_has_active_subscription — since "on a trial" / "ran out of traffic" is
exactly who this feature is for), it extends and resets traffic, same as a
normal renewal. An ACTIVE subscription reaching here is a genuine race (the
purchase should have been blocked upstream); it also extends rather than
holding for manual activation, since the money is already taken and this
mode has no PENDING_ACTIVATION recovery step. DISABLED must NOT extend
(would silently reactivate a gated account) — it falls through to replace,
same as an expired subscription.
"""

from __future__ import annotations

from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.database.models import GuestPurchaseStatus
from app.services.guest_purchase_service import GuestPurchaseError, activate_purchase, fulfill_purchase


def _purchase(**overrides) -> SimpleNamespace:
    base = dict(
        id=1,
        token='t' * 64,
        status=GuestPurchaseStatus.PAID.value,
        personalized_telegram_id=555,
        user_id=42,
        tariff_id=7,
        period_days=90,
        amount_kopeks=72000,
        payment_id='prov-123',
        payment_method='yookassa',
        is_gift=False,
        contact_type='telegram',
        contact_value='555',
        subid=None,
        referrer=None,
        cabinet_password=None,
        auto_login_token=None,
        subscription_url=None,
        subscription_crypto_link=None,
        delivered_at=None,
        landing=None,
        buyer=None,
        user=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _tariff() -> SimpleNamespace:
    return SimpleNamespace(
        id=7,
        name='Personal',
        allowed_squads=['squad-1'],
        traffic_limit_gb=100,
        device_limit=3,
        get_price_for_period=lambda days: 72000,
    )


def _user() -> SimpleNamespace:
    return SimpleNamespace(id=42, telegram_id=555, language='ru')


def _db_with_purchase(purchase) -> AsyncMock:
    db = AsyncMock()
    db.execute = AsyncMock(
        return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: purchase))
    )
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.flush = AsyncMock()
    db.refresh = AsyncMock()
    return db


def _patched_fulfillment(
    *,
    existing_subscription=None,
    subscription_service_cls: MagicMock | None = None,
):
    """ExitStack of patches covering everything fulfill_purchase touches other
    than the subscription-creation call under test (extend/replace/create),
    so each test only has to assert on the one call it cares about."""
    stack = ExitStack()
    m = 'app.services.guest_purchase_service.'

    stack.enter_context(patch(m + 'get_tariff_by_id', AsyncMock(return_value=_tariff())))
    stack.enter_context(patch(m + 'get_user_by_id', AsyncMock(return_value=_user())))
    stack.enter_context(patch(m + 'get_user_by_telegram_id', AsyncMock(return_value=None)))
    find_or_create_mock = AsyncMock()
    stack.enter_context(patch(m + '_find_or_create_user', find_or_create_mock))
    stack.enter_context(patch(m + 'get_subscription_by_user_id', AsyncMock(return_value=existing_subscription)))

    extend_mock = AsyncMock(return_value=SimpleNamespace(subscription_url='sub-url', subscription_crypto_link=None))
    replace_mock = AsyncMock(return_value=SimpleNamespace(subscription_url='sub-url', subscription_crypto_link=None))
    create_mock = AsyncMock(return_value=SimpleNamespace(subscription_url='sub-url', subscription_crypto_link=None))
    stack.enter_context(patch(m + 'extend_subscription', extend_mock))
    stack.enter_context(patch(m + 'replace_subscription', replace_mock))
    stack.enter_context(patch(m + 'create_paid_subscription', create_mock))

    subscription_service_cls = subscription_service_cls or MagicMock(
        return_value=SimpleNamespace(create_remnawave_user=AsyncMock())
    )
    stack.enter_context(patch(m + 'SubscriptionService', subscription_service_cls))

    transaction_mock = AsyncMock(return_value=SimpleNamespace(receipt_uuid=None))
    stack.enter_context(patch(m + 'create_transaction', transaction_mock))
    stack.enter_context(patch(m + '_create_nalogo_receipt_for_purchase', AsyncMock()))
    stack.enter_context(patch(m + '_send_admin_notification', AsyncMock()))
    stack.enter_context(patch(m + 'send_guest_notification', AsyncMock()))

    consume_offer_mock = AsyncMock(return_value=False)
    stack.enter_context(patch('app.utils.promo_offer.consume_user_promo_offer', consume_offer_mock))
    stack.enter_context(patch('app.utils.cache.cache.get', AsyncMock(return_value=None)))
    stack.enter_context(patch('app.database.crud.yandex_client_id.get_subid', AsyncMock(return_value=None)))

    return stack, {
        'find_or_create': find_or_create_mock,
        'extend': extend_mock,
        'replace': replace_mock,
        'create': create_mock,
        'create_transaction': transaction_mock,
        'consume_offer': consume_offer_mock,
    }


@pytest.mark.asyncio
async def test_personalized_purchase_skips_find_or_create_user() -> None:
    purchase = _purchase()
    db = _db_with_purchase(purchase)

    stack, mocks = _patched_fulfillment()
    with stack:
        result = await fulfill_purchase(db, purchase.token)

    mocks['find_or_create'].assert_not_called()
    mocks['create'].assert_awaited_once()
    assert result.status == GuestPurchaseStatus.DELIVERED.value


@pytest.mark.asyncio
async def test_personalized_purchase_no_subscription_creates_new() -> None:
    purchase = _purchase()
    db = _db_with_purchase(purchase)

    stack, mocks = _patched_fulfillment(existing_subscription=None)
    with stack:
        result = await fulfill_purchase(db, purchase.token)

    mocks['create'].assert_awaited_once()
    mocks['extend'].assert_not_called()
    mocks['replace'].assert_not_called()
    assert result.status == GuestPurchaseStatus.DELIVERED.value
    assert result.status != GuestPurchaseStatus.PENDING_ACTIVATION.value


@pytest.mark.asyncio
async def test_personalized_purchase_expired_subscription_replaces() -> None:
    expired = SimpleNamespace(tariff_id=1, status='expired', end_date=datetime.now(UTC) - timedelta(days=1))
    purchase = _purchase()
    db = _db_with_purchase(purchase)

    stack, mocks = _patched_fulfillment(existing_subscription=expired)
    with stack:
        result = await fulfill_purchase(db, purchase.token)

    mocks['replace'].assert_awaited_once()
    mocks['extend'].assert_not_called()
    mocks['create'].assert_not_called()
    assert result.status == GuestPurchaseStatus.DELIVERED.value


@pytest.mark.asyncio
async def test_personalized_purchase_never_holds_for_activation() -> None:
    """The race case: the user already has a LIVE subscription by the time the
    webhook fires (e.g. bought again in the bot in the meantime). Anonymous
    purchases would hold this for manual activation; personalized purchases
    must never do that — extend instead, since money is already taken and
    there is no activate step in this mode."""
    live = SimpleNamespace(
        tariff_id=99, status='active', end_date=datetime.now(UTC) + timedelta(days=10), is_active=True
    )
    purchase = _purchase()
    db = _db_with_purchase(purchase)

    stack, mocks = _patched_fulfillment(existing_subscription=live)
    with stack:
        result = await fulfill_purchase(db, purchase.token)

    mocks['extend'].assert_awaited_once()
    mocks['replace'].assert_not_called()
    mocks['create'].assert_not_called()
    assert result.status == GuestPurchaseStatus.DELIVERED.value
    assert result.status != GuestPurchaseStatus.PENDING_ACTIVATION.value


@pytest.mark.asyncio
async def test_personalized_purchase_limited_subscription_extends_and_resets_traffic() -> None:
    """LIMITED (out of traffic, days still remaining) is NOT blocked upstream
    by user_has_active_subscription — this is the expected, common path for
    "ran out of traffic, buys more," not a race. It must extend (which resets
    traffic_used_gb and converts trial→paid where relevant), same as a normal
    renewal — not silently drop the purchase or replace it."""
    limited = SimpleNamespace(tariff_id=7, status='limited', end_date=datetime.now(UTC) + timedelta(days=5))
    purchase = _purchase()
    db = _db_with_purchase(purchase)

    stack, mocks = _patched_fulfillment(existing_subscription=limited)
    with stack:
        result = await fulfill_purchase(db, purchase.token)

    mocks['extend'].assert_awaited_once()
    mocks['replace'].assert_not_called()
    mocks['create'].assert_not_called()
    assert result.status == GuestPurchaseStatus.DELIVERED.value


@pytest.mark.asyncio
async def test_personalized_purchase_disabled_subscription_replaces_not_extends() -> None:
    """A DISABLED subscription (e.g. channel-membership gate) with a future
    end_date must NOT take the "race" extend path — extend_subscription
    unconditionally flips status back to ACTIVE, which would silently bypass
    whatever disabled it. It should fall through to the same replace path
    used for an expired subscription (this state should already be blocked
    upstream by user_has_active_subscription; this pins the fulfillment-side
    fallback for the residual race window)."""
    disabled = SimpleNamespace(
        tariff_id=1, status='disabled', end_date=datetime.now(UTC) + timedelta(days=10)
    )
    purchase = _purchase()
    db = _db_with_purchase(purchase)

    stack, mocks = _patched_fulfillment(existing_subscription=disabled)
    with stack:
        result = await fulfill_purchase(db, purchase.token)

    mocks['replace'].assert_awaited_once()
    mocks['extend'].assert_not_called()
    mocks['create'].assert_not_called()
    assert result.status == GuestPurchaseStatus.DELIVERED.value


@pytest.mark.asyncio
async def test_personalized_purchase_creates_transaction() -> None:
    """create_transaction must still run for personalized purchases — it
    drives promo-group auto-assignment, contest tracking, and the central
    purchase analytics hook."""
    purchase = _purchase()
    db = _db_with_purchase(purchase)

    stack, mocks = _patched_fulfillment()
    with stack:
        await fulfill_purchase(db, purchase.token)

    mocks['create_transaction'].assert_awaited_once()
    _, kwargs = mocks['create_transaction'].call_args
    assert kwargs['user_id'] == 42
    assert kwargs['amount_kopeks'] == 72000


@pytest.mark.asyncio
async def test_personalized_purchase_consumes_promo_offer() -> None:
    """A one-shot promo offer must be consumed on fulfillment — the landing
    has no subtract_user_balance() step to do it implicitly, unlike the bot."""
    purchase = _purchase()
    db = _db_with_purchase(purchase)

    stack, mocks = _patched_fulfillment()
    with stack:
        await fulfill_purchase(db, purchase.token)

    mocks['consume_offer'].assert_awaited_once_with(db, 42)


@pytest.mark.asyncio
async def test_personalized_purchase_marks_failed_when_user_vanished() -> None:
    """purchase.user_id and personalized_telegram_id both fail to resolve to a
    live User -> mark FAILED rather than raising or crashing the webhook."""
    purchase = _purchase(user_id=None, personalized_telegram_id=555)
    db = _db_with_purchase(purchase)

    m = 'app.services.guest_purchase_service.'
    with (
        patch(m + 'get_user_by_id', AsyncMock(return_value=None)),
        patch(m + 'get_user_by_telegram_id', AsyncMock(return_value=None)),
    ):
        result = await fulfill_purchase(db, purchase.token)

    assert result.status == GuestPurchaseStatus.FAILED.value


@pytest.mark.asyncio
async def test_activate_purchase_rejects_personalized_purchase() -> None:
    """A personalized purchase should never reach PENDING_ACTIVATION, but the
    activate endpoint must reject it explicitly regardless (defense in
    depth — it must not be possible to drive a replace/extend through the
    unauthenticated activate endpoint for a tgid-bound purchase)."""
    purchase = _purchase(personalized_telegram_id=555)
    db = _db_with_purchase(purchase)

    with pytest.raises(GuestPurchaseError) as exc_info:
        await activate_purchase(db, purchase.token)

    assert exc_info.value.status_code == 400
