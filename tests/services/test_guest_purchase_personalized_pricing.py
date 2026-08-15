"""Regression tests for personalized (?tgid=) landing pricing.

Background
----------
Landing campaign discounts (LandingPage.discount_percent) and bot-side
promo-group / config period discounts (PromoGroup.period_discounts,
BASE_PROMO_GROUP_PERIOD_DISCOUNTS) are two independent mechanisms. Before this
feature, guest landing purchases only ever saw the campaign discount — the
promo-group period discount a bot user configured for e.g. 90/180/360 days
was invisible on the landing.

calculate_personal_tariff_price() / validate_and_calculate(user=...) fix this
by reusing pricing_engine.calculate_tariff_purchase_price() — the exact call
the bot uses for a tariff purchase — for personalized (?tgid=) requests. These
tests pin two invariants: (1) the personalized price matches what the bot
would charge the same user, and (2) it does NOT stack with the landing's own
campaign discount.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services.guest_purchase_service import calculate_personal_tariff_price, validate_and_calculate
from app.services.pricing_engine import RenewalPricing


def _tariff(tariff_id: int = 1) -> SimpleNamespace:
    def get_purchasable_periods() -> list[int]:
        return [30, 90, 180, 360]

    def get_purchasable_price_for_period(days: int) -> int | None:
        return {30: 30000, 90: 80000, 180: 150000, 360: 280000}.get(days)

    return SimpleNamespace(
        id=tariff_id,
        is_active=True,
        is_daily=False,
        get_purchasable_periods=get_purchasable_periods,
        get_purchasable_price_for_period=get_purchasable_price_for_period,
    )


def _landing(tariff_id: int = 1, discount_percent: int = 50, *, campaign_active: bool = False) -> SimpleNamespace:
    """A landing with a (deliberately large) campaign discount, so a bug that
    stacks it with the personal discount is easy to catch."""
    now = datetime.now(UTC)
    return SimpleNamespace(
        id=10,
        allowed_tariff_ids=[tariff_id],
        allowed_periods={},
        discount_percent=discount_percent,
        discount_starts_at=(now - timedelta(days=1)) if campaign_active else None,
        discount_ends_at=(now + timedelta(days=1)) if campaign_active else None,
        discount_overrides={},
    )


def _renewal_pricing(*, final_total: int, group_discount: int = 0, offer_discount: int = 0) -> RenewalPricing:
    base = final_total + group_discount + offer_discount
    return RenewalPricing(
        base_price=base,
        servers_price=0,
        traffic_price=0,
        devices_price=0,
        promo_group_discount=group_discount,
        promo_offer_discount=offer_discount,
        final_total=final_total,
        period_days=90,
        is_tariff_mode=True,
        breakdown={'group_discount_pct': {'period': 10, 'devices': 0}, 'offer_discount_pct': 0},
    )


@pytest.mark.asyncio
async def test_personalized_price_matches_pricing_engine() -> None:
    """calculate_personal_tariff_price must reuse PricingEngine verbatim —
    the price a personalized landing charges must equal what the bot charges
    the same user for the same tariff/period."""
    tariff = _tariff()
    user = SimpleNamespace(id=1)
    pricing = _renewal_pricing(final_total=72000, group_discount=8000)  # 10% off 80000

    with patch(
        'app.services.pricing_engine.pricing_engine.calculate_tariff_purchase_price',
        AsyncMock(return_value=pricing),
    ) as mocked:
        final, original, pct = await calculate_personal_tariff_price(tariff, 90, user)

    mocked.assert_awaited_once_with(tariff, 90, user=user)
    assert final == 72000
    assert original == 80000
    assert pct == 10


@pytest.mark.asyncio
async def test_personalized_mode_ignores_campaign_discount_entirely() -> None:
    """validate_and_calculate(user=...) must NOT stack the landing's campaign
    discount with the personal one — the campaign branch must not run at all."""
    tariff = _tariff()
    user = SimpleNamespace(id=1)
    # 10% group discount on the 80000 base -> 72000, campaign is 50% (would be
    # 40000, or ~36000 stacked) -- if either leaks in, this assertion catches it.
    pricing = _renewal_pricing(final_total=72000, group_discount=8000)

    db = AsyncMock()
    with (
        patch('app.services.guest_purchase_service.get_tariff_by_id', AsyncMock(return_value=tariff)),
        patch(
            'app.services.pricing_engine.pricing_engine.calculate_tariff_purchase_price',
            AsyncMock(return_value=pricing),
        ),
    ):
        resolved, price = await validate_and_calculate(
            db, _landing(discount_percent=50, campaign_active=True), tariff_id=1, period_days=90, user=user
        )

    assert resolved is tariff
    assert price == 72000, 'personalized price must equal the PricingEngine result, not a stacked discount'


@pytest.mark.asyncio
async def test_anonymous_path_unchanged_when_user_is_none() -> None:
    """Regression guard: passing user=None (the default) must keep the exact
    pre-existing campaign-discount behaviour."""
    tariff = _tariff()
    db = AsyncMock()

    with patch('app.services.guest_purchase_service.get_tariff_by_id', AsyncMock(return_value=tariff)):
        resolved, price = await validate_and_calculate(
            db, _landing(discount_percent=50, campaign_active=True), tariff_id=1, period_days=90
        )

    assert resolved is tariff
    assert price == 40000  # 80000 * (1 - 0.50)


@pytest.mark.asyncio
async def test_personalized_no_discount_returns_no_original_price() -> None:
    """A user with no applicable discount must show no strikethrough price."""
    tariff = _tariff()
    user = SimpleNamespace(id=1)
    pricing = _renewal_pricing(final_total=80000)  # no discount at all

    final, original, pct = None, None, None
    with patch(
        'app.services.pricing_engine.pricing_engine.calculate_tariff_purchase_price',
        AsyncMock(return_value=pricing),
    ):
        final, original, pct = await calculate_personal_tariff_price(tariff, 90, user)

    assert final == 80000
    assert original == 80000
    assert pct == 0


@pytest.mark.asyncio
async def test_personalized_hundred_percent_discount_clamps_to_one_kopek() -> None:
    """A 100%-off order must still charge 1 kopek — every payment provider
    rejects a 0 amount, and the webhook amount-equality check would then fail."""
    tariff = _tariff()
    user = SimpleNamespace(id=1)
    pricing = _renewal_pricing(final_total=0, group_discount=80000)

    with patch(
        'app.services.pricing_engine.pricing_engine.calculate_tariff_purchase_price',
        AsyncMock(return_value=pricing),
    ):
        final, original, pct = await calculate_personal_tariff_price(tariff, 90, user)

    assert final == 1
    assert original == 80000
    assert pct == 100
