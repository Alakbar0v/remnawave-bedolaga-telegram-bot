"""Регрессия: сбой админ-уведомления об активации промокода не должен оставлять
сессию БД «отравленной» для остатка запроса.

``activate_promocode_for_registration`` шлёт уведомление админу ПОСЛЕ того, как
сама активация промокода уже закоммичена (``PromoCodeService.activate_promocode``
делает ``await db.commit()`` перед возвратом успеха). Если запись уведомления
(``AdminNotificationService.send_promocode_activation_notification`` — она пишет
``SubscriptionEvent`` в той же сессии) падает посреди flush, простое
``except Exception: logger.error(...)`` ловит исключение, но НЕ откатывает
транзакцию — SQLAlchemy требует явный ``rollback()`` после неудачного flush,
иначе сессия остаётся в состоянии ``PendingRollbackError`` и следующий же запрос
в этой сессии (а вызывающие держат её открытой для остального обработчика,
например ``cmd_start``) падает тем же исключением ещё раз, хотя сама активация
промокода уже успешно сохранена.

Обнаружено при добавлении immediate-активации промокода по deep-link ``/start
<код>`` (app/handlers/start.py) — там сессия остаётся открытой для полноценной
регистрации/меню ПОСЛЕ активации, и порча сессии рушит весь остальной /start,
а не только уведомление.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.handlers.promocode import activate_promocode_for_registration


@pytest.mark.asyncio
async def test_notification_failure_rolls_back_session(monkeypatch):
    """Если отправка админ-уведомления падает, db.rollback() вызывается, а сама
    успешная активация (result) всё равно возвращается вызывающему."""
    db = AsyncMock()
    db.rollback = AsyncMock()

    promocode_data = {'code': 'TEST1'}
    success_result = {
        'success': True,
        'description': '✅ Промокод активирован',
        'promocode': promocode_data,
        'balance_before_kopeks': 0,
        'balance_after_kopeks': 10000,
    }

    fake_service = MagicMock()
    fake_service.activate_promocode = AsyncMock(return_value=success_result)
    monkeypatch.setattr('app.handlers.promocode.PromoCodeService', lambda: fake_service)

    fake_user = MagicMock(id=1)
    monkeypatch.setattr(
        'app.database.crud.user.get_user_by_id',
        AsyncMock(return_value=fake_user),
    )

    failing_notification_service = MagicMock()
    failing_notification_service.send_promocode_activation_notification = AsyncMock(
        side_effect=RuntimeError('PendingRollbackError-like failure mid-flush')
    )
    monkeypatch.setattr(
        'app.handlers.promocode.AdminNotificationService',
        lambda *args, **kwargs: failing_notification_service,
    )

    result = await activate_promocode_for_registration(db, user_id=1, code='TEST1', bot=AsyncMock())

    # Активация успешна и результат не теряется, несмотря на сбой уведомления.
    assert result['success'] is True
    assert result is success_result

    # Ключевая проверка регрессии: сессия должна быть откачена после сбоя,
    # иначе следующая операция в этой же сессии падает PendingRollbackError.
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_rollback_when_notification_succeeds(monkeypatch):
    """Sanity-check: если уведомление ушло успешно, лишнего rollback быть не должно."""
    db = AsyncMock()
    db.rollback = AsyncMock()

    success_result = {'success': True, 'description': 'ok', 'promocode': {'code': 'TEST2'}}

    fake_service = MagicMock()
    fake_service.activate_promocode = AsyncMock(return_value=success_result)
    monkeypatch.setattr('app.handlers.promocode.PromoCodeService', lambda: fake_service)

    fake_user = MagicMock(id=1)
    monkeypatch.setattr(
        'app.database.crud.user.get_user_by_id',
        AsyncMock(return_value=fake_user),
    )

    ok_notification_service = MagicMock()
    ok_notification_service.send_promocode_activation_notification = AsyncMock(return_value=True)
    monkeypatch.setattr(
        'app.handlers.promocode.AdminNotificationService',
        lambda *args, **kwargs: ok_notification_service,
    )

    result = await activate_promocode_for_registration(db, user_id=1, code='TEST2', bot=AsyncMock())

    assert result['success'] is True
    db.rollback.assert_not_awaited()
