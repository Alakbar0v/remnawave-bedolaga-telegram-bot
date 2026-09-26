"""Бот: статус и отмена СБП-автопродления Antilopay
(``handle_antilopay_recurring_menu`` / ``_cancel`` и кнопка входа в меню автоплатежа).
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import app.handlers.subscription.autopay as autopay_mod
from app.config import settings


GET_ACTIVE = 'app.database.crud.antilopay_subscription.get_active_antilopay_subscription_by_subscription'


def _make_callback():
    cb = MagicMock()
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()
    return cb


def _make_user():
    user = MagicMock()
    user.id = 1
    user.language = 'ru'
    return user


def _callbacks(markup) -> list[str]:
    return [btn.callback_data for row in markup.inline_keyboard for btn in row if btn.callback_data]


def _record(**overrides):
    base = {
        'id': 5,
        'status': 'ACTIVE',
        'amount_kopeks': 10000,
        'next_charge_at': datetime(2026, 10, 1, tzinfo=UTC),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


async def test_menu_shows_status_amount_and_cancel_button(monkeypatch):
    cb, user, db = _make_callback(), _make_user(), AsyncMock()
    monkeypatch.setattr(autopay_mod, '_resolve_subscription', AsyncMock(return_value=(SimpleNamespace(id=10), 10)))
    monkeypatch.setattr(GET_ACTIVE, AsyncMock(return_value=_record()))

    await autopay_mod.handle_antilopay_recurring_menu(cb, user, db)

    args, kwargs = cb.message.edit_text.call_args
    assert 'активно' in args[0]
    assert settings.format_price(10000) in args[0]
    assert 'antilopay_recurring_cancel' in _callbacks(kwargs['reply_markup'])


async def test_menu_without_record_has_no_cancel_button(monkeypatch):
    cb, user, db = _make_callback(), _make_user(), AsyncMock()
    monkeypatch.setattr(autopay_mod, '_resolve_subscription', AsyncMock(return_value=(SimpleNamespace(id=10), 10)))
    monkeypatch.setattr(GET_ACTIVE, AsyncMock(return_value=None))

    await autopay_mod.handle_antilopay_recurring_menu(cb, user, db)

    _, kwargs = cb.message.edit_text.call_args
    assert 'antilopay_recurring_cancel' not in _callbacks(kwargs['reply_markup'])


async def test_cancel_cancels_record_and_returns_to_autopay_menu(monkeypatch):
    """Отмена не гейтится флагом ANTILOPAY_SBP_ENABLED — это операция безопасности."""
    monkeypatch.setattr(settings, 'ANTILOPAY_SBP_ENABLED', False, raising=False)
    cb, user, db = _make_callback(), _make_user(), AsyncMock()
    record = _record()
    monkeypatch.setattr(autopay_mod, '_resolve_subscription', AsyncMock(return_value=(SimpleNamespace(id=10), 10)))
    monkeypatch.setattr(GET_ACTIVE, AsyncMock(return_value=record))
    cancel = AsyncMock(return_value=True)
    monkeypatch.setattr(
        'app.services.payment.antilopay.AntilopayPaymentMixin.cancel_antilopay_subscription_record', cancel
    )
    back_to_menu = AsyncMock()
    monkeypatch.setattr(autopay_mod, 'handle_autopay_menu', back_to_menu)

    await autopay_mod.handle_antilopay_recurring_cancel(cb, user, db)

    cancel.assert_awaited_once()
    assert cancel.await_args.args[-1] is record
    cb.answer.assert_awaited_once()
    back_to_menu.assert_awaited_once()


async def test_cancel_failure_shows_alert_and_keeps_menu(monkeypatch):
    cb, user, db = _make_callback(), _make_user(), AsyncMock()
    monkeypatch.setattr(autopay_mod, '_resolve_subscription', AsyncMock(return_value=(SimpleNamespace(id=10), 10)))
    monkeypatch.setattr(GET_ACTIVE, AsyncMock(return_value=_record()))
    monkeypatch.setattr(
        'app.services.payment.antilopay.AntilopayPaymentMixin.cancel_antilopay_subscription_record',
        AsyncMock(side_effect=RuntimeError('db down')),
    )
    back_to_menu = AsyncMock()
    monkeypatch.setattr(autopay_mod, 'handle_autopay_menu', back_to_menu)

    await autopay_mod.handle_antilopay_recurring_cancel(cb, user, db)

    assert cb.answer.await_args.kwargs.get('show_alert') is True
    back_to_menu.assert_not_awaited()


async def test_autopay_menu_daily_shows_antilopay_entry_only_with_live_record(monkeypatch):
    subscription = SimpleNamespace(id=10, tariff=SimpleNamespace(is_daily=True))
    monkeypatch.setattr(autopay_mod, '_resolve_subscription', AsyncMock(return_value=(subscription, 10)))

    monkeypatch.setattr(GET_ACTIVE, AsyncMock(return_value=_record()))
    cb, user, db = _make_callback(), _make_user(), AsyncMock()
    await autopay_mod.handle_autopay_menu(cb, user, db)
    _, kwargs = cb.message.edit_text.call_args
    assert 'antilopay_recurring_menu' in _callbacks(kwargs['reply_markup'])

    monkeypatch.setattr(GET_ACTIVE, AsyncMock(return_value=None))
    cb, user, db = _make_callback(), _make_user(), AsyncMock()
    await autopay_mod.handle_autopay_menu(cb, user, db)
    _, kwargs = cb.message.edit_text.call_args
    assert 'antilopay_recurring_menu' not in _callbacks(kwargs['reply_markup'])
