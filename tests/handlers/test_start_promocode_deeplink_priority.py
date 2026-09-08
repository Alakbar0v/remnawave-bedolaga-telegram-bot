"""/start <код> должен сначала проверять промокод и только потом реферальный код.

До этого изменения ``start_parameter`` в /start-диплинке трактовался как
реферальный код безусловно — промокоды через /start вообще не проверялись,
и уже зарегистрированный пользователь получал «уже зарегистрированы в системе,
реферальная ссылка не может быть применена» для любой нераспознанной ссылки,
включая валидный промокод.

Эти тесты закрепляют новый порядок в ``cmd_start`` (app/handlers/start.py):
1. Если ``start_parameter`` — валидный промокод, он активируется (сразу для уже
   зарегистрированного пользователя, через FSM state 'promocode' — для нового) и
   НЕ трактуется как реферальный код.
2. Если это не промокод — поведение как раньше (реферальный код / campaign).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat, Message, User as TgUser
from sqlalchemy import select

from app.database.models import (
    AdvertisingCampaign,
    DiscountOffer,
    GuestPurchase,
    MainMenuButton,
    PaymentMethodConfig,
    PinnedMessage,
    PromoCode,
    PromoCodeType,
    PromoCodeUse,
    PromoGroup,
    PromoOfferLog,
    SentNotification,
    ServerSquad,
    Subscription,
    SystemSetting,
    Tariff,
    TrafficPurchase,
    Transaction,
    User,
    UserPromoGroup,
    Webhook,
    WebhookDelivery,
    tariff_promo_groups,
)
from app.handlers.start import cmd_start
from tests.fixtures.sqlite_memory import memory_session


_TABLES = [
    SystemSetting.__table__,
    Tariff.__table__,
    PromoCode.__table__,
    PromoCodeUse.__table__,
    PromoGroup.__table__,
    tariff_promo_groups,
    UserPromoGroup.__table__,
    Subscription.__table__,
    User.__table__,
    GuestPurchase.__table__,
    Transaction.__table__,
    DiscountOffer.__table__,
    PromoOfferLog.__table__,
    ServerSquad.__table__,
    MainMenuButton.__table__,
    PinnedMessage.__table__,
    AdvertisingCampaign.__table__,
    TrafficPurchase.__table__,
    SentNotification.__table__,
    Webhook.__table__,
    WebhookDelivery.__table__,
    PaymentMethodConfig.__table__,
]


def _make_fsm_context(user_id: int) -> FSMContext:
    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    return FSMContext(storage=storage, key=key)


def _make_message(text: str, user_id: int, username: str = 'tester') -> Message:
    msg = MagicMock(spec=Message)
    msg.text = text
    msg.message_id = 1
    msg.from_user = TgUser(id=user_id, is_bot=False, first_name='Test', username=username)
    msg.chat = Chat(id=user_id, type='private')
    msg.answer = AsyncMock()
    msg.reply = AsyncMock()
    msg.bot = AsyncMock()
    return msg


@pytest.mark.asyncio
async def test_existing_user_promocode_activated_immediately_not_as_referral(monkeypatch):
    """Уже зарегистрированный юзер: /start <промокод> активирует код сразу и НЕ
    показывает «уже зарегистрированы, реферальная ссылка не может быть применена»."""
    # Админ-уведомление об активации промокода пишет SubscriptionEvent — эта таблица
    # не входит в минимальный набор _TABLES и не относится к проверяемому поведению
    # (само поведение отката сессии при сбое уведомления закреплено отдельным
    # unit-тестом в tests/handlers/test_promocode_notification_rollback.py).
    monkeypatch.setattr(
        'app.handlers.promocode.AdminNotificationService',
        lambda *args, **kwargs: MagicMock(send_promocode_activation_notification=AsyncMock()),
    )

    async with memory_session(monkeypatch, _TABLES) as db:
        user = User(telegram_id=22222, username='existing', balance_kopeks=0)
        db.add(user)
        await db.commit()
        await db.refresh(user)

        promocode = PromoCode(
            code='WELCOME100',
            type=PromoCodeType.BALANCE.value,
            balance_bonus_kopeks=10000,
            max_uses=5,
            is_active=True,
        )
        db.add(promocode)
        await db.commit()
        await db.refresh(promocode)

        msg = _make_message('/start WELCOME100', user_id=user.telegram_id)
        state = _make_fsm_context(user.telegram_id)

        await cmd_start(msg, state, db, db_user=user)

        await db.refresh(user)
        assert user.balance_kopeks == 10000

        use_res = await db.execute(
            select(PromoCodeUse).where(PromoCodeUse.user_id == user.id, PromoCodeUse.promocode_id == promocode.id)
        )
        assert use_res.scalars().first() is not None

        answered_texts = [call.args[0] for call in msg.answer.call_args_list if call.args]
        assert not any('уже зарегистрированы в системе' in txt for txt in answered_texts)
        assert any('Промокод активирован' in txt or '✅' in txt for txt in answered_texts)

        state_data = await state.get_data()
        assert not state_data.get('referral_code')


@pytest.mark.asyncio
async def test_new_user_promocode_deferred_to_state_not_treated_as_referral(monkeypatch):
    """Новый пользователь: /start <промокод> кладёт код в FSM state('promocode') для
    активации после регистрации, а не резолвится как referral_code."""
    async with memory_session(monkeypatch, _TABLES) as db:
        promocode = PromoCode(
            code='NEWUSER50',
            type=PromoCodeType.BALANCE.value,
            balance_bonus_kopeks=5000,
            max_uses=5,
            is_active=True,
        )
        db.add(promocode)
        await db.commit()

        new_tg_id = 55555
        msg = _make_message('/start NEWUSER50', user_id=new_tg_id, username='brandnew')
        state = _make_fsm_context(new_tg_id)

        # db_user=None и юзера в БД ещё нет — это первый /start нового пользователя.
        await cmd_start(msg, state, db, db_user=None)

        state_data = await state.get_data()
        assert state_data.get('promocode') == 'NEWUSER50'
        assert not state_data.get('referral_code')


@pytest.mark.asyncio
async def test_non_promocode_start_parameter_still_falls_back_to_referral(monkeypatch):
    """Параметр, который не резолвится как промокод, по-прежнему трактуется как
    реферальный код (старое поведение не сломано)."""
    async with memory_session(monkeypatch, _TABLES) as db:
        referrer = User(telegram_id=11111, username='ref', balance_kopeks=0, referral_code='REFCODE123')
        db.add(referrer)
        await db.commit()
        await db.refresh(referrer)

        new_tg_id = 66666
        msg = _make_message('/start REFCODE123', user_id=new_tg_id, username='newbie')
        state = _make_fsm_context(new_tg_id)

        await cmd_start(msg, state, db, db_user=None)

        state_data = await state.get_data()
        assert state_data.get('referral_code') == 'REFCODE123'
        assert not state_data.get('promocode')
