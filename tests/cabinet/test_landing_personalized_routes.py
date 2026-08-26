"""Tests for personalized (?tgid=) landing page routes.

Follows the house pattern for route tests: a route-registration smoke test on
the aggregate router plus direct handler calls with hand-built fakes
(`db=AsyncMock()`, `raw_request=MagicMock()`) — dependencies normally injected
by FastAPI (`get_cabinet_db`) are bypassed by passing the resolved argument.

Background — see also tests/services/test_guest_purchase_personalized_*.py.
A landing purchase driven by ?tgid= must:
  - show the SAME discount the bot would show the same user, not the
    landing's own campaign discount (personalization.status == 'ok');
  - refuse to personalize prices, and refuse the purchase, for an unknown
    tgid or a user who already has an active subscription;
  - never expose subscription_url / cabinet credentials on the post-payment
    status page for this mode (tgid is unsigned — anyone can pay for a
    victim's account, so the payer must not receive the victim's link).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError


def _landing(**overrides) -> SimpleNamespace:
    base = dict(
        id=10,
        slug='promo',
        title={'ru': 'Промо'},
        subtitle=None,
        features=[],
        footer_text=None,
        allowed_tariff_ids=[7],
        allowed_periods={},
        payment_methods=[{'method_id': 'yookassa', 'display_name': 'YooKassa'}],
        gift_enabled=False,
        custom_css=None,
        meta_title=None,
        meta_description=None,
        discount_percent=None,
        discount_overrides=None,
        discount_starts_at=None,
        discount_ends_at=None,
        discount_badge_text=None,
        background_config=None,
        sticky_pay_button=False,
        analytics_view_enabled=False,
        analytics_view_goal=None,
        analytics_click_enabled=False,
        analytics_click_goal=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _request() -> MagicMock:
    req = MagicMock()
    req.headers.get.return_value = None
    req.cookies.get.return_value = None
    return req


# --- Wiring ------------------------------------------------------------


def test_landing_routes_registered(registered_paths) -> None:
    assert registered_paths.get('/cabinet/landing/{slug}') == {'GET'}
    assert registered_paths.get('/cabinet/landing/{slug}/purchase') == {'POST'}
    assert registered_paths.get('/cabinet/landing/purchase/{token}') == {'GET'}


# --- _resolve_personalized_user — banned/deleted accounts ---------------


@pytest.mark.asyncio
async def test_resolve_personalized_user_excludes_blocked_account() -> None:
    """An admin-banned account (User.status = BLOCKED, set by
    admin_users.block_user — unrelated to Subscription.status) must not be a
    valid personalization target, otherwise anyone who knows the banned
    user's telegram_id can complete a full purchase for that account via the
    landing, bypassing the ban entirely."""
    from app.cabinet.routes import landing as landing_routes

    blocked_user = SimpleNamespace(id=1, telegram_id=999, status='blocked')
    with patch.object(landing_routes, 'get_user_by_telegram_id', AsyncMock(return_value=blocked_user)):
        result = await landing_routes._resolve_personalized_user(AsyncMock(), 999)

    assert result is None


@pytest.mark.asyncio
async def test_resolve_personalized_user_excludes_deleted_account() -> None:
    from app.cabinet.routes import landing as landing_routes

    deleted_user = SimpleNamespace(id=1, telegram_id=999, status='deleted')
    with patch.object(landing_routes, 'get_user_by_telegram_id', AsyncMock(return_value=deleted_user)):
        result = await landing_routes._resolve_personalized_user(AsyncMock(), 999)

    assert result is None


@pytest.mark.asyncio
async def test_resolve_personalized_user_allows_active_account() -> None:
    from app.cabinet.routes import landing as landing_routes

    active_user = SimpleNamespace(id=1, telegram_id=999, status='active')
    with patch.object(landing_routes, 'get_user_by_telegram_id', AsyncMock(return_value=active_user)):
        result = await landing_routes._resolve_personalized_user(AsyncMock(), 999)

    assert result is active_user


# --- GET /{slug} — personalization --------------------------------------


@pytest.mark.asyncio
async def test_get_landing_config_without_tgid_has_no_personalization() -> None:
    from app.cabinet.routes import landing as landing_routes

    landing = _landing()
    with (
        patch.object(landing_routes.RateLimitCache, 'is_ip_rate_limited', AsyncMock(return_value=False)),
        patch.object(landing_routes, 'get_active_landing_by_slug', AsyncMock(return_value=landing)),
        patch.object(landing_routes, '_load_landing_tariffs', AsyncMock(return_value=[])) as load_mock,
        patch.object(landing_routes, '_get_method_defaults', lambda: {}),
    ):
        response = await landing_routes.get_landing_config(
            raw_request=_request(), slug='promo', lang='ru', tgid=None, db=AsyncMock()
        )

    assert response.personalization is None
    load_mock.assert_awaited_once()
    assert load_mock.await_args.kwargs['user'] is None


@pytest.mark.asyncio
async def test_get_landing_config_tgid_not_found() -> None:
    from app.cabinet.routes import landing as landing_routes

    landing = _landing()
    with (
        patch.object(landing_routes.RateLimitCache, 'is_ip_rate_limited', AsyncMock(return_value=False)),
        patch.object(landing_routes.RateLimitCache, 'is_rate_limited', AsyncMock(return_value=False)),
        patch.object(landing_routes, 'get_active_landing_by_slug', AsyncMock(return_value=landing)),
        patch.object(landing_routes, 'get_user_by_telegram_id', AsyncMock(return_value=None)),
        patch.object(type(landing_routes.settings), 'get_bot_username', lambda self: 'testbot'),
        patch.object(landing_routes, '_get_method_defaults', lambda: {}),
    ):
        response = await landing_routes.get_landing_config(
            raw_request=_request(), slug='promo', lang='ru', tgid=123456789, db=AsyncMock()
        )

    assert response.personalization.status == 'user_not_found'
    assert response.personalization.can_purchase is False
    assert response.personalization.bot_link == 'https://t.me/testbot'
    assert response.tariffs == []


@pytest.mark.asyncio
async def test_get_landing_config_tgid_has_active_subscription() -> None:
    from app.cabinet.routes import landing as landing_routes

    landing = _landing()
    user = SimpleNamespace(id=42, telegram_id=123456789, status='active')
    active_sub = SimpleNamespace(end_date=None)
    with (
        patch.object(landing_routes.RateLimitCache, 'is_ip_rate_limited', AsyncMock(return_value=False)),
        patch.object(landing_routes.RateLimitCache, 'is_rate_limited', AsyncMock(return_value=False)),
        patch.object(landing_routes, 'get_active_landing_by_slug', AsyncMock(return_value=landing)),
        patch.object(landing_routes, 'get_user_by_telegram_id', AsyncMock(return_value=user)),
        patch.object(landing_routes, 'user_has_active_subscription', AsyncMock(return_value=True)),
        patch('app.database.crud.subscription.get_subscription_by_user_id', AsyncMock(return_value=active_sub)),
        patch.object(type(landing_routes.settings), 'get_bot_username', lambda self: 'testbot'),
        patch.object(landing_routes, '_get_method_defaults', lambda: {}),
        patch.object(landing_routes, '_load_landing_tariffs', AsyncMock()) as load_mock,
    ):
        response = await landing_routes.get_landing_config(
            raw_request=_request(), slug='promo', lang='ru', tgid=123456789, db=AsyncMock()
        )

    assert response.personalization.status == 'has_active_subscription'
    assert response.personalization.can_purchase is False
    assert response.tariffs == []
    load_mock.assert_not_called()


@pytest.mark.asyncio
async def test_get_landing_config_tgid_ok_uses_personal_prices() -> None:
    from app.cabinet.routes import landing as landing_routes

    landing = _landing()
    user = SimpleNamespace(id=42, telegram_id=123456789, status='active')
    with (
        patch.object(landing_routes.RateLimitCache, 'is_ip_rate_limited', AsyncMock(return_value=False)),
        patch.object(landing_routes.RateLimitCache, 'is_rate_limited', AsyncMock(return_value=False)),
        patch.object(landing_routes, 'get_active_landing_by_slug', AsyncMock(return_value=landing)),
        patch.object(landing_routes, 'get_user_by_telegram_id', AsyncMock(return_value=user)),
        patch.object(landing_routes, 'user_has_active_subscription', AsyncMock(return_value=False)),
        patch.object(landing_routes, '_load_landing_tariffs', AsyncMock(return_value=[])) as load_mock,
        patch.object(type(landing_routes.settings), 'get_bot_username', lambda self: 'testbot'),
        patch.object(landing_routes, '_get_method_defaults', lambda: {}),
    ):
        response = await landing_routes.get_landing_config(
            raw_request=_request(), slug='promo', lang='ru', tgid=123456789, db=AsyncMock()
        )

    assert response.personalization.status == 'ok'
    assert response.personalization.can_purchase is True
    load_mock.assert_awaited_once()
    assert load_mock.await_args.kwargs['user'] is user


# --- POST /{slug}/purchase ----------------------------------------------


def _purchase_request(**overrides) -> object:
    from app.cabinet.routes.landing import PurchaseRequest

    base = dict(tariff_id=7, period_days=90, payment_method='yookassa', telegram_id=999)
    base.update(overrides)
    return PurchaseRequest(**base)


@pytest.mark.asyncio
async def test_purchase_rejects_unknown_tgid() -> None:
    from app.cabinet.routes import landing as landing_routes

    landing = _landing()
    with (
        patch.object(landing_routes.RateLimitCache, 'is_ip_rate_limited', AsyncMock(return_value=False)),
        patch.object(landing_routes.RateLimitCache, 'is_rate_limited', AsyncMock(return_value=False)),
        patch.object(landing_routes, 'get_active_landing_by_slug', AsyncMock(return_value=landing)),
        patch.object(landing_routes, '_resolve_personalized_user', AsyncMock(return_value=None)),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await landing_routes.create_landing_purchase(
                slug='promo', body=_purchase_request(), raw_request=_request(), db=AsyncMock()
            )
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_purchase_rejects_active_subscription() -> None:
    from app.cabinet.routes import landing as landing_routes

    landing = _landing()
    user = SimpleNamespace(id=42)
    with (
        patch.object(landing_routes.RateLimitCache, 'is_ip_rate_limited', AsyncMock(return_value=False)),
        patch.object(landing_routes.RateLimitCache, 'is_rate_limited', AsyncMock(return_value=False)),
        patch.object(landing_routes, 'get_active_landing_by_slug', AsyncMock(return_value=landing)),
        patch.object(landing_routes, '_resolve_personalized_user', AsyncMock(return_value=user)),
        patch.object(landing_routes, 'user_has_active_subscription', AsyncMock(return_value=True)),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await landing_routes.create_landing_purchase(
                slug='promo', body=_purchase_request(), raw_request=_request(), db=AsyncMock()
            )
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_purchase_stores_numeric_contact_and_user_id() -> None:
    """The purchase record must be bound to the numeric telegram id (not a
    username) and to the resolved user_id at creation time — see
    create_landing_purchase's comment on why str(telegram_id), not
    user.username, goes into the NOT NULL contact_value column."""
    from app.cabinet.routes import landing as landing_routes

    landing = _landing()
    user = SimpleNamespace(id=42, username=None)
    tariff = SimpleNamespace(id=7, name='Personal', show_in_gift=True)
    purchase = SimpleNamespace(token='tok123', referrer=None)

    with (
        patch.object(landing_routes.RateLimitCache, 'is_ip_rate_limited', AsyncMock(return_value=False)),
        patch.object(landing_routes.RateLimitCache, 'is_rate_limited', AsyncMock(return_value=False)),
        patch.object(landing_routes, 'get_active_landing_by_slug', AsyncMock(return_value=landing)),
        patch.object(landing_routes, '_resolve_personalized_user', AsyncMock(return_value=user)),
        patch.object(landing_routes, 'user_has_active_subscription', AsyncMock(return_value=False)),
        patch.object(landing_routes, 'lock_user_for_pricing', AsyncMock(return_value=user)),
        patch.object(landing_routes, '_get_method_defaults', lambda: {}),
        patch.object(landing_routes, 'validate_and_calculate', AsyncMock(return_value=(tariff, 72000))) as vac_mock,
        patch.object(landing_routes, 'create_purchase', AsyncMock(return_value=purchase)) as create_mock,
        patch.object(
            landing_routes,
            'PaymentService',
            MagicMock(return_value=SimpleNamespace(create_guest_payment=AsyncMock(return_value={'payment_url': 'https://pay'}))),
        ),
    ):
        response = await landing_routes.create_landing_purchase(
            slug='promo', body=_purchase_request(), raw_request=_request(), db=AsyncMock()
        )

    assert response.payment_url == 'https://pay'
    assert response.purchase_token == 'tok123'

    vac_mock.assert_awaited_once()
    assert vac_mock.await_args.kwargs['user'] is user

    create_kwargs = create_mock.await_args.kwargs
    assert create_kwargs['contact_type'] == 'telegram'
    assert create_kwargs['contact_value'] == '999', 'must store the numeric telegram id, not a username'
    assert create_kwargs['personalized_telegram_id'] == 999
    assert create_kwargs['user_id'] == 42


# --- PurchaseRequest validator -------------------------------------------


def test_purchase_request_validator_rejects_contacts_with_telegram_id() -> None:
    from app.cabinet.routes.landing import PurchaseRequest

    with pytest.raises(ValidationError):
        PurchaseRequest(
            tariff_id=1, period_days=30, payment_method='yookassa',
            telegram_id=999, contact_type='email', contact_value='a@b.com',
        )


def test_purchase_request_validator_rejects_gift_with_telegram_id() -> None:
    from app.cabinet.routes.landing import PurchaseRequest

    with pytest.raises(ValidationError):
        PurchaseRequest(tariff_id=1, period_days=30, payment_method='yookassa', telegram_id=999, is_gift=True)


def test_purchase_request_validator_requires_contacts_without_telegram_id() -> None:
    from app.cabinet.routes.landing import PurchaseRequest

    with pytest.raises(ValidationError):
        PurchaseRequest(tariff_id=1, period_days=30, payment_method='yookassa')


def test_purchase_request_validator_accepts_plain_contacts() -> None:
    from app.cabinet.routes.landing import PurchaseRequest

    req = PurchaseRequest(
        tariff_id=1, period_days=30, payment_method='yookassa',
        contact_type='email', contact_value='a@b.com',
    )
    assert req.telegram_id is None


# --- GET /purchase/{token} — status suppression --------------------------


def test_status_response_personalized_hides_subscription_url_and_credentials() -> None:
    from app.cabinet.routes.landing import _build_purchase_status_response
    from datetime import UTC, datetime

    purchase = SimpleNamespace(
        tariff=SimpleNamespace(name='Personal'),
        delivered_at=datetime.now(UTC),
        subscription_url='https://vpn.example.com/sub/abc',
        subscription_crypto_link=None,
        is_gift=False,
        contact_value='999',
        gift_recipient_value=None,
        gift_message=None,
        gift_recipient_type=None,
        contact_type='telegram',
        status='delivered',
        paid_at=None,
        cabinet_password=None,
        auto_login_token=None,
        personalized_telegram_id=999,
        user=SimpleNamespace(telegram_id=999),
        period_days=90,
        token='t' * 64,
    )

    response = _build_purchase_status_response(purchase)

    assert response.mode == 'telegram'
    assert response.requires_activation is False
    assert response.subscription_url is None, 'tgid is unsigned; must never hand out someone else\'s connection link'
    assert response.subscription_crypto_link is None
    assert response.cabinet_email is None
    assert response.cabinet_password is None
    assert response.auto_login_token is None
    assert response.is_claimable is False


def test_status_response_anonymous_still_exposes_subscription_url() -> None:
    """Regression guard: the anonymous flow's existing within-TTL exposure of
    subscription_url must be unaffected by the personalized suppression."""
    from app.cabinet.routes.landing import _build_purchase_status_response
    from datetime import UTC, datetime

    purchase = SimpleNamespace(
        tariff=SimpleNamespace(name='Basic'),
        delivered_at=datetime.now(UTC),
        subscription_url='https://vpn.example.com/sub/abc',
        subscription_crypto_link=None,
        is_gift=False,
        contact_value='user@example.com',
        gift_recipient_value=None,
        gift_message=None,
        gift_recipient_type=None,
        contact_type='email',
        status='delivered',
        paid_at=None,
        cabinet_password=None,
        auto_login_token=None,
        personalized_telegram_id=None,
        user=None,
        period_days=30,
        token='t' * 64,
    )

    response = _build_purchase_status_response(purchase)

    assert response.mode == 'guest'
    assert response.subscription_url == 'https://vpn.example.com/sub/abc'
