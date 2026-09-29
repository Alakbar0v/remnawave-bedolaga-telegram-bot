"""Mixin для интеграции с Antilopay (lk.antilopay.com)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from importlib import import_module
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import PaymentMethod, TransactionType
from app.services.antilopay_service import antilopay_service
from app.services.antilopay_test_log import test_log  # TEST-LOG: временно, удалить
from app.utils.payment_logger import payment_logger as logger
from app.utils.user_utils import format_referrer_info


# Маппинг статусов Antilopay -> internal
ANTILOPAY_STATUS_MAP: dict[str, tuple[str, bool]] = {
    'PENDING': ('pending', False),
    'SUCCESS': ('success', True),
    'FAIL': ('failed', False),
    'CANCEL': ('cancelled', False),
    'EXPIRED': ('expired', False),
    'CHARGEBACK': ('chargeback', False),
    'REVERSED': ('reversed', False),
}


# Статусы неуспешного регулярного списания (callback и история ``recurrent/check``)
ANTILOPAY_CHARGE_FAILED_STATUSES = ('FAIL', 'CANCEL', 'EXPIRED')

# metadata.type платежа-привязки СБП-рекуррента под платный триал
TRIAL_SBP_SUBSCRIPTION_TYPE = 'trial_sbp_subscription'
# Шаг и цена рекуррента: тариф за 30 дней, списание раз в MONTH
TRIAL_CHARGE_DAYS = 30
# Сколько раз произвести списание (включая первичный платёж) — фактически «пока не отменят»
TRIAL_RECURRENT_PAYMENT_COUNT = 120


class AntilopayPaymentMixin:
    """Mixin для работы с платежами Antilopay."""

    async def create_antilopay_payment(
        self,
        db: AsyncSession,
        *,
        user_id: int | None,
        amount_kopeks: int,
        description: str = 'Пополнение баланса',
        email: str | None = None,
        language: str = 'ru',
        payment_method_type: str | None = None,
        return_url: str | None = None,
    ) -> dict[str, Any] | None:
        """
        Создает платеж Antilopay.

        Returns:
            Словарь с данными платежа или None при ошибке
        """
        if not settings.is_antilopay_enabled():
            logger.error('Antilopay не настроен')
            return None

        # Валидация лимитов
        if amount_kopeks < settings.ANTILOPAY_MIN_AMOUNT_KOPEKS:
            logger.warning(
                'Antilopay: сумма меньше минимальной',
                amount_kopeks=amount_kopeks,
                ANTILOPAY_MIN_AMOUNT_KOPEKS=settings.ANTILOPAY_MIN_AMOUNT_KOPEKS,
            )
            return None

        if amount_kopeks > settings.ANTILOPAY_MAX_AMOUNT_KOPEKS:
            logger.warning(
                'Antilopay: сумма больше максимальной',
                amount_kopeks=amount_kopeks,
                ANTILOPAY_MAX_AMOUNT_KOPEKS=settings.ANTILOPAY_MAX_AMOUNT_KOPEKS,
            )
            return None

        # Получаем telegram_id пользователя для order_id
        payment_module = import_module('app.services.payment_service')
        if user_id is not None:
            user = await payment_module.get_user_by_id(db, user_id)
            tg_id = user.telegram_id if user else user_id
        else:
            user = None
            tg_id = 'guest'

        # Генерируем уникальный order_id с telegram_id для удобного поиска
        order_id = f'alp{tg_id}_{uuid.uuid4().hex[:6]}'
        amount_rubles = amount_kopeks / 100
        currency = settings.ANTILOPAY_CURRENCY

        # Метаданные
        metadata = {
            'user_id': user_id,
            'amount_kopeks': amount_kopeks,
            'description': description,
            'language': language,
            'type': 'balance_topup',
        }

        try:
            # Определяем prefer_methods по типу подметода
            prefer_methods: list[str] | None = None
            if payment_method_type == 'sbp':
                prefer_methods = ['SBP']
            elif payment_method_type == 'card':
                prefer_methods = ['CARD_RU']
            elif payment_method_type == 'sberpay':
                prefer_methods = ['SBER_PAY']

            # Формируем success/fail URL
            result_url = return_url or settings.ANTILOPAY_RETURN_URL

            # merchant_extra — строка до 255 символов для callback
            merchant_extra = order_id

            # Создаем платеж через API
            api_result = await antilopay_service.create_payment(
                amount_rubles=amount_rubles,
                order_id=order_id,
                product_name=settings.ANTILOPAY_PRODUCT_NAME,
                product_type=settings.ANTILOPAY_PRODUCT_TYPE,
                description=description,
                customer_email=email,
                prefer_methods=prefer_methods,
                success_url=result_url,
                fail_url=result_url,
                merchant_extra=merchant_extra,
            )

            payment_id = api_result.get('payment_id')
            payment_url = api_result.get('payment_url')

            logger.info(
                'Antilopay: получен ответ API',
                order_id=order_id,
                payment_id=payment_id,
                payment_url=payment_url,
            )

            lifetime = settings.ANTILOPAY_PAYMENT_LIFETIME_MINUTES
            expires_at = datetime.now(UTC) + timedelta(minutes=lifetime)

            # Сохраняем в БД
            antilopay_crud = import_module('app.database.crud.antilopay')
            local_payment = await antilopay_crud.create_antilopay_payment(
                db=db,
                user_id=user_id,
                order_id=order_id,
                amount_kopeks=amount_kopeks,
                currency=currency,
                description=description,
                payment_url=payment_url,
                payment_method=payment_method_type,
                antilopay_payment_id=payment_id,
                expires_at=expires_at,
                metadata_json=metadata,
            )

            logger.info(
                'Antilopay: создан платеж',
                order_id=order_id,
                user_id=user_id,
                amount_rubles=amount_rubles,
                currency=currency,
            )

            return {
                'order_id': order_id,
                'amount_kopeks': amount_kopeks,
                'amount_rubles': amount_rubles,
                'currency': currency,
                'payment_url': payment_url,
                'payment_id': payment_id,
                'expires_at': expires_at.isoformat(),
                'local_payment_id': local_payment.id,
            }

        except Exception as e:
            logger.exception('Antilopay: ошибка создания платежа', error=e)
            return None

    async def process_antilopay_callback(
        self,
        db: AsyncSession,
        payload: dict[str, Any],
    ) -> bool:
        """
        Обрабатывает callback от Antilopay.

        Подпись проверяется в webserver/payments.py до вызова этого метода.

        Args:
            db: Сессия БД
            payload: JSON тело callback (signature проверена в webserver)

        Returns:
            True если платеж успешно обработан
        """
        test_log('callback_in', payload=payload)  # TEST-LOG: сырой callback от Antilopay
        try:
            callback_type = payload.get('type')
            if callback_type != 'payment':
                logger.info('Antilopay callback: неизвестный тип', callback_type=callback_type)
                return True  # Не наш тип — не ошибка

            antilopay_payment_id = payload.get('payment_id')
            antilopay_status = payload.get('status')
            our_order_id = payload.get('order_id')

            if not our_order_id or not antilopay_status:
                logger.warning('Antilopay callback: отсутствуют обязательные поля', payload=payload)
                return False

            # Определяем is_paid по статусу
            is_confirmed = antilopay_status == 'SUCCESS'

            # Ищем платеж по order_id
            antilopay_crud = import_module('app.database.crud.antilopay')
            payment = await antilopay_crud.get_antilopay_payment_by_order_id(db, our_order_id)

            # СБП-рекуррент (платный триал): и сам callback привязки, и последующие
            # списания по recurrent_id (у них order_id может быть неизвестен нам).
            payment_kind = ((payment.metadata_json or {}).get('type') if payment else None) or None
            if payment_kind == TRIAL_SBP_SUBSCRIPTION_TYPE or (not payment and payload.get('recurrent_id')):
                test_log(  # TEST-LOG
                    'callback_routed_to_subscription',
                    order_id=our_order_id,
                    payment_id=antilopay_payment_id,
                    recurrent_id=payload.get('recurrent_id'),
                    local_payment_found=payment is not None,
                    payment_kind=payment_kind,
                )
                return await self.process_antilopay_subscription_callback(db, payload, payment=payment)

            if not payment:
                logger.warning(
                    'Antilopay callback: платеж не найден',
                    order_id=our_order_id,
                )
                return False

            # Lock payment row immediately to prevent concurrent webhook processing (TOCTOU race)
            locked = await antilopay_crud.get_antilopay_payment_by_id_for_update(db, payment.id)
            if not locked:
                logger.error('Antilopay: не удалось заблокировать платёж', payment_id=payment.id)
                return False
            payment = locked

            # Проверка дублирования (re-check from locked row)
            if payment.is_paid:
                logger.info('Antilopay callback: платеж уже обработан', order_id=payment.order_id)
                return True

            # Маппинг статуса
            status_info = ANTILOPAY_STATUS_MAP.get(antilopay_status, ('pending', False))
            internal_status, is_paid = status_info

            # Если статус SUCCESS, принудительно считаем оплаченным
            if is_confirmed:
                is_paid = True
                internal_status = 'success'

            callback_payload = {
                'antilopay_payment_id': antilopay_payment_id,
                'status': antilopay_status,
                'amount': payload.get('amount'),
                'original_amount': payload.get('original_amount'),
                'fee': payload.get('fee'),
                'currency': payload.get('currency'),
                'pay_method': payload.get('pay_method'),
                'pay_data': payload.get('pay_data'),
                'customer': payload.get('customer'),
                'merchant_extra': payload.get('merchant_extra'),
            }

            # Проверка суммы ДО обновления статуса
            if is_paid:
                original_amount = payload.get('original_amount')
                if original_amount is not None:
                    # original_amount в РУБЛЯХ (float), конвертируем в копейки
                    received_kopeks = round(float(original_amount) * 100)
                    if abs(received_kopeks - payment.amount_kopeks) > 1:
                        logger.error(
                            'Antilopay amount mismatch',
                            expected_kopeks=payment.amount_kopeks,
                            received_kopeks=received_kopeks,
                            order_id=payment.order_id,
                        )
                        await antilopay_crud.update_antilopay_payment_status(
                            db=db,
                            payment=payment,
                            status='amount_mismatch',
                            is_paid=False,
                            callback_payload=callback_payload,
                        )
                        return False

            # Финализируем платеж если оплачен — без промежуточного commit
            if is_paid:
                # Inline field assignments to keep FOR UPDATE lock intact
                payment.status = internal_status
                payment.is_paid = True
                payment.paid_at = datetime.now(UTC)
                payment.antilopay_payment_id = str(antilopay_payment_id) if antilopay_payment_id else None
                payment.callback_payload = callback_payload
                payment.updated_at = datetime.now(UTC)
                await db.flush()
                return await self._finalize_antilopay_payment(db, payment, trigger='webhook')

            # Для не-success статусов можно безопасно коммитить
            payment = await antilopay_crud.update_antilopay_payment_status(
                db=db,
                payment=payment,
                status=internal_status,
                is_paid=False,
                callback_payload=callback_payload,
            )

            return True

        except Exception as e:
            logger.exception('Antilopay callback: ошибка обработки', error=e)
            return False

    async def _finalize_antilopay_payment(
        self,
        db: AsyncSession,
        payment: Any,
        *,
        trigger: str,
    ) -> bool:
        """Создаёт транзакцию, начисляет баланс и отправляет уведомления.

        FOR UPDATE lock must be acquired by the caller before invoking this method.
        """
        payment_module = import_module('app.services.payment_service')
        antilopay_crud = import_module('app.database.crud.antilopay')

        # FOR UPDATE lock already acquired by caller — just check idempotency
        if payment.transaction_id:
            logger.info(
                'Antilopay платеж уже связан с транзакцией',
                order_id=payment.order_id,
                transaction_id=payment.transaction_id,
                trigger=trigger,
            )
            return True

        # Read fresh metadata AFTER lock to avoid stale data
        metadata = dict(getattr(payment, 'metadata_json', {}) or {})

        # --- Guest purchase flow ---
        from app.services.payment.common import try_fulfill_guest_purchase

        guest_result = await try_fulfill_guest_purchase(
            db,
            metadata=metadata,
            payment_amount_kopeks=payment.amount_kopeks,
            provider_payment_id=payment.order_id,
            provider_name='antilopay',
        )
        if guest_result is not None:
            return True

        # Ensure paid fields are set (idempotent — caller may have already set them)
        if not payment.is_paid:
            payment.status = 'success'
            payment.is_paid = True
            payment.paid_at = datetime.now(UTC)
            payment.updated_at = datetime.now(UTC)

        balance_already_credited = bool(metadata.get('balance_credited'))

        user = await payment_module.get_user_by_id(db, payment.user_id)
        if not user:
            logger.error('Пользователь не найден для Antilopay', user_id=payment.user_id)
            return False

        # Загружаем промогруппы в асинхронном контексте
        await db.refresh(user, attribute_names=['promo_group', 'user_promo_groups'])
        for user_promo_group in getattr(user, 'user_promo_groups', []):
            await db.refresh(user_promo_group, attribute_names=['promo_group'])

        promo_group = user.get_primary_promo_group()
        subscription = getattr(user, 'subscription', None)
        referrer_info = format_referrer_info(user)

        transaction_external_id = payment.order_id

        # Проверяем дупликат транзакции
        existing_transaction = None
        if transaction_external_id:
            existing_transaction = await payment_module.get_transaction_by_external_id(
                db,
                transaction_external_id,
                PaymentMethod.ANTILOPAY,
            )

        display_name = settings.get_antilopay_display_name()
        description = f'Пополнение через {display_name}'

        transaction = existing_transaction
        created_transaction = False

        if not transaction:
            transaction = await payment_module.create_transaction(
                db,
                user_id=payment.user_id,
                type=TransactionType.DEPOSIT,
                amount_kopeks=payment.amount_kopeks,
                description=description,
                payment_method=PaymentMethod.ANTILOPAY,
                external_id=transaction_external_id,
                is_completed=True,
                created_at=getattr(payment, 'created_at', None),
                commit=False,
            )
            created_transaction = True

        await antilopay_crud.link_antilopay_payment_to_transaction(db, payment=payment, transaction_id=transaction.id)

        should_credit_balance = created_transaction or not balance_already_credited

        if not should_credit_balance:
            logger.info('Antilopay платеж уже зачислил баланс ранее', order_id=payment.order_id)
            return True

        # Lock user row to prevent concurrent balance race conditions
        from app.database.crud.user import lock_user_for_update

        user = await lock_user_for_update(db, user)

        old_balance = user.balance_kopeks
        was_first_topup = not user.has_made_first_topup

        user.balance_kopeks += payment.amount_kopeks
        user.updated_at = datetime.now(UTC)
        await db.commit()
        await db.refresh(user)

        # Emit deferred side-effects after atomic commit
        from app.database.crud.transaction import emit_transaction_side_effects

        await emit_transaction_side_effects(
            db,
            transaction,
            amount_kopeks=payment.amount_kopeks,
            user_id=payment.user_id,
            type=TransactionType.DEPOSIT,
            payment_method=PaymentMethod.ANTILOPAY,
            external_id=transaction_external_id,
        )

        topup_status = '\U0001f195 Первое пополнение' if was_first_topup else '\U0001f504 Пополнение'

        try:
            from app.services.referral_service import process_referral_topup

            await process_referral_topup(
                db,
                user.id,
                payment.amount_kopeks,
                getattr(self, 'bot', None),
            )
        except Exception as error:
            logger.error('Ошибка обработки реферального пополнения Antilopay', error=error)

        if was_first_topup and not user.has_made_first_topup and not user.referred_by_id:
            user.has_made_first_topup = True
            await db.commit()
            await db.refresh(user)

        if getattr(self, 'bot', None):
            try:
                from app.services.admin_notification_service import AdminNotificationService

                notification_service = AdminNotificationService(self.bot)
                await notification_service.send_balance_topup_notification(
                    user,
                    transaction,
                    old_balance,
                    topup_status=topup_status,
                    referrer_info=referrer_info,
                    subscription=subscription,
                    promo_group=promo_group,
                    db=db,
                )
            except Exception as error:
                logger.error('Ошибка отправки админ уведомления Antilopay', error=error)

        if getattr(self, 'bot', None) and user.telegram_id and settings.is_notifications_enabled():
            try:
                keyboard = await self.build_topup_success_keyboard(user)
                await self.bot.send_message(
                    user.telegram_id,
                    (
                        '\u2705 <b>Пополнение успешно!</b>\n\n'
                        f'\U0001f4b0 Сумма: {settings.format_price(payment.amount_kopeks)}\n'
                        f'\U0001f4b3 Способ: {display_name}\n'
                        f'\U0001f194 Транзакция: {transaction.id}\n\n'
                        'Баланс пополнен автоматически!'
                    ),
                    parse_mode='HTML',
                    reply_markup=keyboard,
                )
            except Exception as error:
                logger.error('Ошибка отправки уведомления пользователю Antilopay', error=error)

        try:
            from app.services.payment.common import send_cart_notification_after_topup

            await send_cart_notification_after_topup(user, payment.amount_kopeks, db, getattr(self, 'bot', None))
        except Exception as error:
            logger.error(
                'Ошибка при работе с сохраненной корзиной для пользователя',
                user_id=payment.user_id,
                error=error,
                exc_info=True,
            )

        metadata['balance_change'] = {
            'old_balance': old_balance,
            'new_balance': user.balance_kopeks,
            'credited_at': datetime.now(UTC).isoformat(),
        }
        metadata['balance_credited'] = True
        payment.metadata_json = metadata
        await db.commit()

        logger.info(
            'Обработан Antilopay платеж',
            order_id=payment.order_id,
            user_id=payment.user_id,
            trigger=trigger,
        )

        return True

    async def create_antilopay_trial_subscription(
        self,
        db: AsyncSession,
        *,
        user: Any,
        tariff: Any,
        subscription: Any,
    ) -> dict[str, Any]:
        """Оформляет СБП-рекуррент Antilopay под платный триал.

        ``recurrent.category=SUBSCRIPTION`` + ``delay`` = длительность триала: доступ выдаётся
        по callback привязки, полная цена тарифа за 30 дней списывается автоматически после
        ``delay`` и далее раз в месяц. Продуктов у Antilopay нет — цену берём из тарифа.
        """
        from sqlalchemy.exc import IntegrityError

        from app.database.crud import antilopay_subscription as sub_crud

        price_kopeks = tariff.get_price_for_period(TRIAL_CHARGE_DAYS) or 0
        if price_kopeks <= 0:
            raise ValueError(f'Для тарифа не задана цена за {TRIAL_CHARGE_DAYS} дней — СБП-триал недоступен')

        free_days = int(settings.TRIAL_DURATION_DAYS or 0)
        if free_days <= 0:
            raise ValueError('Длительность пробного периода не настроена')

        order_id = f'alp{user.telegram_id or user.id}_{uuid.uuid4().hex[:6]}'
        description = f'Бесплатный пробный период на {free_days} дн.'
        result_url = settings.ANTILOPAY_RETURN_URL

        api_result = await antilopay_service.create_payment(
            amount_rubles=price_kopeks / 100,
            order_id=order_id,
            product_name=settings.ANTILOPAY_PRODUCT_NAME,
            product_type=settings.ANTILOPAY_PRODUCT_TYPE,
            description=description,
            customer_email=getattr(user, 'email', None),
            prefer_methods=['SBP'],
            success_url=result_url,
            fail_url=result_url,
            merchant_extra=order_id,
            recurrent={
                'type': 'MONTH',
                'payment_count': TRIAL_RECURRENT_PAYMENT_COUNT,
                'category': 'SUBSCRIPTION',
                'delay': free_days,
                'delay_type': 'DAY',
            },
        )
        payment_id = api_result.get('payment_id')
        payment_url = api_result.get('payment_url')

        antilopay_crud = import_module('app.database.crud.antilopay')
        await antilopay_crud.create_antilopay_payment(
            db=db,
            user_id=user.id,
            order_id=order_id,
            amount_kopeks=price_kopeks,
            currency=settings.ANTILOPAY_CURRENCY,
            description=description,
            payment_url=payment_url,
            payment_method='sbp',
            antilopay_payment_id=payment_id,
            expires_at=datetime.now(UTC) + timedelta(minutes=settings.ANTILOPAY_PAYMENT_LIFETIME_MINUTES),
            metadata_json={
                'type': TRIAL_SBP_SUBSCRIPTION_TYPE,
                'user_id': user.id,
                'subscription_id': subscription.id,
                'amount_kopeks': price_kopeks,
            },
        )

        try:
            record = await sub_crud.create_antilopay_subscription(
                db,
                user_id=user.id,
                subscription_id=subscription.id,
                tariff_id=getattr(tariff, 'id', None),
                order_id=order_id,
                antilopay_payment_id=payment_id,
                charge_days=TRIAL_CHARGE_DAYS,
                amount_kopeks=price_kopeks,
                redirect_url=payment_url,
                free_days=free_days,
            )
        except IntegrityError:
            await db.rollback()
            winner = await sub_crud.get_active_antilopay_subscription_by_subscription(db, subscription.id)
            if not winner:
                raise
            return {'local_id': winner.id, 'payment_url': winner.redirect_url, 'order_id': winner.order_id}

        # Рекуррент провайдера и баланс-автосписание — взаимоисключающие движки продления.
        subscription.autopay_enabled = False
        await db.commit()

        return {
            'local_id': record.id,
            'payment_url': payment_url,
            'payment_id': payment_id,
            'order_id': order_id,
            'amount_kopeks': price_kopeks,
        }

    async def process_antilopay_subscription_callback(
        self,
        db: AsyncSession,
        payload: dict[str, Any],
        *,
        payment: Any | None = None,
    ) -> bool:
        """Callback по СБП-рекурренту Antilopay: привязка (триал) и последующие списания.

        Callback привязки — это платёж с нашим ``order_id`` и ``payment_id`` инициирующего
        платежа: выдаём триал на ``free_days``. Любой другой успешный платёж по тому же
        ``recurrent_id`` — регулярное списание: продлеваем на ``charge_days``. Идемпотентность
        списаний — по ``payment_id`` (Antilopay ретраит callback каждые 3 минуты в течение часа).
        """
        from app.database.crud import antilopay_subscription as sub_crud

        order_id = payload.get('order_id')
        recurrent_id = payload.get('recurrent_id')
        payment_id = payload.get('payment_id')
        status = payload.get('status')

        found = None
        if order_id:
            found = await sub_crud.get_antilopay_subscription_by_order_id(db, str(order_id))
        if not found and recurrent_id:
            found = await sub_crud.get_antilopay_subscription_by_recurrent_id(db, str(recurrent_id))
        if not found:
            logger.warning(
                'Antilopay subscription callback: подписка не найдена',
                order_id=order_id,
                recurrent_id=recurrent_id,
            )
            return False

        # Блокировка строки: конкурентные ретраи callback не должны продлевать дважды.
        record = await sub_crud.get_antilopay_subscription_by_id_for_update(db, found.id)
        if not record:
            return False

        if recurrent_id and not record.recurrent_id:
            record.recurrent_id = str(recurrent_id)

        is_binding = str(order_id or '') == record.order_id and (
            not payment_id or not record.antilopay_payment_id or str(payment_id) == record.antilopay_payment_id
        )
        # TEST-LOG: ключевой вопрос — отличается ли payment_id списания от payment_id привязки
        test_log(
            'subscription_callback_decision',
            local_id=record.id,
            record_status=record.status,
            callback_status=status,
            callback_order_id=order_id,
            record_order_id=record.order_id,
            callback_payment_id=payment_id,
            record_binding_payment_id=record.antilopay_payment_id,
            recurrent_id=recurrent_id,
            same_payment_id_as_binding=bool(payment_id) and str(payment_id) == (record.antilopay_payment_id or ''),
            same_order_id_as_binding=str(order_id or '') == record.order_id,
            treated_as='binding' if is_binding else 'recurrent_charge',
        )

        if is_binding:
            if payment is not None:
                payment.callback_payload = {
                    'antilopay_payment_id': payment_id,
                    'status': status,
                    'amount': payload.get('amount'),
                    'original_amount': payload.get('original_amount'),
                    'recurrent_id': recurrent_id,
                    'pay_method': payload.get('pay_method'),
                }
                payment.status = ANTILOPAY_STATUS_MAP.get(status or '', ('pending', False))[0]
                if status == 'SUCCESS' and not payment.is_paid:
                    payment.is_paid = True
                    payment.paid_at = datetime.now(UTC)
            if status == 'SUCCESS':
                # Сумма привязки в документации не описана (при delay>0 может быть 0 или
                # верификационной) — логируем как есть, не сверяем с ценой тарифа.
                logger.info(
                    'Antilopay: привязка СБП подтверждена',
                    order_id=order_id,
                    amount=payload.get('amount'),
                    original_amount=payload.get('original_amount'),
                    recurrent_id=recurrent_id,
                )
                await db.commit()
                await self._activate_antilopay_trial(db, record)
                return True
            if status in ('FAIL', 'CANCEL', 'EXPIRED'):
                # Брошенная/неуспешная привязка: гасим запись, чтобы не висела «живой».
                if record.status == 'PENDING':
                    record.status = 'CANCELLED'
            await db.commit()
            return True

        return await self._process_antilopay_recurrent_charge(db, record, payload)

    async def _process_antilopay_recurrent_charge(
        self,
        db: AsyncSession,
        record: Any,
        payload: dict[str, Any],
    ) -> bool:
        """Регулярное списание по СБП-рекурренту: продлевает подписку на ``charge_days``."""
        from sqlalchemy import select as sa_select

        from app.database.crud.subscription import _lock_subscription_row, reconcile_tariff_traffic_limit
        from app.database.crud.transaction import create_transaction, emit_transaction_side_effects
        from app.database.models import Subscription, Transaction
        from app.services.grace_access_echo import undo_grace_overlay_echo

        status = payload.get('status')
        charge_id = payload.get('payment_id')

        if status in ANTILOPAY_CHARGE_FAILED_STATUSES:
            failed_id = str(charge_id) if charge_id else None
            # Antilopay ретраит callback каждые 3 минуты в течение часа: одно и то же
            # неудачное списание считаем и уведомляем ровно один раз.
            if failed_id is not None:
                if record.last_failed_external_id == failed_id:
                    return True
            elif record.status == 'PAST_DUE':
                return True
            test_log('charge_failed', local_id=record.id, status=status, failed_id=failed_id)  # TEST-LOG
            record.charges_failed += 1
            if failed_id is not None:
                record.last_failed_external_id = failed_id
            if record.status != 'CANCELLED':
                record.status = 'PAST_DUE'
            await db.commit()
            await self._notify_antilopay_recurring(db, record, 'failed')
            return True

        if status != 'SUCCESS':
            logger.info('Antilopay subscription callback: промежуточный статус', status=status)
            await db.commit()
            return True

        if not charge_id:
            test_log('charge_rejected_no_payment_id', local_id=record.id, payload=payload)  # TEST-LOG
            # Без payment_id идемпотентность не работает — не рискуем двойным продлением.
            logger.warning('Antilopay subscription callback: SUCCESS без payment_id', order_id=record.order_id)
            return False

        charge_id = str(charge_id)
        if record.last_charge_external_id == charge_id:
            test_log('charge_skipped_duplicate', local_id=record.id, charge_id=charge_id)  # TEST-LOG
            return True
        duplicate_tx = (
            await db.execute(
                sa_select(Transaction.id).where(
                    Transaction.external_id == charge_id,
                    Transaction.payment_method == PaymentMethod.ANTILOPAY.value,
                )
            )
        ).scalar_one_or_none()
        if duplicate_tx is not None:
            test_log('charge_skipped_duplicate_tx', local_id=record.id, charge_id=charge_id)  # TEST-LOG
            return True

        original_amount = payload.get('original_amount')
        charged_kopeks = round(float(original_amount) * 100) if original_amount is not None else 0
        if charged_kopeks > 0 and abs(charged_kopeks - record.amount_kopeks) > 1:
            logger.warning(
                'Antilopay: сумма списания отличается от сохранённой — обновляем запись',
                order_id=record.order_id,
                stored_kopeks=record.amount_kopeks,
                charged_kopeks=charged_kopeks,
            )
            record.amount_kopeks = charged_kopeks

        subscription = await db.get(Subscription, record.subscription_id)
        if subscription is None:
            # Продлевать нечего, а деньги уже списаны — останавливаем дальнейшие списания.
            logger.error(
                'Antilopay subscription callback: подписка удалена — останавливаем рекуррент',
                subscription_id=record.subscription_id,
            )
            await self.cancel_antilopay_subscription_record(db, record)
            return False

        await _lock_subscription_row(db, subscription)
        await undo_grace_overlay_echo(db, subscription)
        test_log(  # TEST-LOG
            'charge_extending_subscription',
            local_id=record.id,
            subscription_id=subscription.id,
            charge_id=charge_id,
            charge_days=record.charge_days,
            end_date_before=str(subscription.end_date),
            charged_kopeks=charged_kopeks,
        )
        subscription.extend_subscription(record.charge_days)
        await reconcile_tariff_traffic_limit(db, subscription)

        # Первое полное списание превращает триал в обычную оплаченную подписку.
        if record.free_days > 0 and getattr(subscription, 'is_trial', False):
            subscription.is_trial = False

        was_cancelled = record.status == 'CANCELLED'
        charged_at = datetime.now(UTC)
        record.last_charge_external_id = charge_id
        if not was_cancelled:
            record.status = 'ACTIVE'
            record.next_charge_at = charged_at + timedelta(days=record.charge_days)
        record.last_charge_at = charged_at
        record.charges_success += 1

        description = 'Автопродление через СБП'
        tx = await create_transaction(
            db,
            user_id=record.user_id,
            type=TransactionType.SUBSCRIPTION_PAYMENT,
            amount_kopeks=record.amount_kopeks,
            description=description,
            payment_method=PaymentMethod.ANTILOPAY,
            external_id=charge_id,
            commit=False,
        )
        await db.commit()
        test_log(  # TEST-LOG
            'charge_committed', local_id=record.id, charge_id=charge_id, end_date_after=str(subscription.end_date)
        )

        await emit_transaction_side_effects(
            db,
            tx,
            amount_kopeks=record.amount_kopeks,
            user_id=record.user_id,
            type=TransactionType.SUBSCRIPTION_PAYMENT,
            payment_method=PaymentMethod.ANTILOPAY,
            external_id=charge_id,
            description=description,
        )
        await self._notify_antilopay_recurring(db, record, 'confirmed')

        if was_cancelled:
            # Списание по локально отменённой записи = удалённая отмена не дошла: повторяем.
            logger.error('Antilopay: списание по отменённой подписке — повторяем удалённую отмену')
            await self._cancel_antilopay_remote(record)

        subscription_id_for_log = subscription.id
        try:
            from app.services.subscription_service import SubscriptionService

            await SubscriptionService().update_remnawave_user(
                db,
                subscription,
                reset_traffic=settings.RESET_TRAFFIC_ON_PAYMENT,
                reset_reason='Автопродление Antilopay',
            )
        except Exception as sync_error:  # best-effort: продление уже в БД
            logger.warning(
                'Синк панели после автопродления Antilopay не удался',
                error=str(sync_error),
                subscription_id=subscription_id_for_log,
            )
        return True

    async def sync_antilopay_charges_from_history(
        self,
        db: AsyncSession,
        record_id: int,
        remote_payments: list[dict[str, Any]] | None,
    ) -> int:
        """Добивает списания, чьи callback'и не дошли: история ``payment/recurrent/check``.

        Берём только успешные платежи, кроме инициирующего (привязки). Идемпотентность
        та же, что у callback'а, — по ``payment_id`` (``Transaction.external_id``), поэтому
        повторный вызов ничего не продлевает. Возвращает число обработанных списаний.
        """
        from app.database.crud import antilopay_subscription as sub_crud

        if not remote_payments:
            return 0

        processed = 0
        test_log('history_sync', local_id=record_id, remote_count=len(remote_payments))  # TEST-LOG
        ordered = sorted(
            (p for p in remote_payments if isinstance(p, dict)),
            key=lambda p: str(p.get('ctime') or ''),
        )
        for remote in ordered:
            if str(remote.get('status') or '').upper() != 'SUCCESS' or not remote.get('payment_id'):
                continue
            record = await sub_crud.get_antilopay_subscription_by_id_for_update(db, record_id)
            if record is None:
                break
            payment_id = str(remote['payment_id'])
            if payment_id == record.antilopay_payment_id or str(remote.get('order_id') or '') == record.order_id:
                continue  # платёж привязки — его выдача триала идёт отдельным путём
            if record.last_charge_external_id == payment_id:
                await db.rollback()
                continue
            payload = {
                'type': 'payment',
                'payment_id': payment_id,
                'order_id': remote.get('order_id'),
                'status': 'SUCCESS',
                'amount': remote.get('amount'),
                'original_amount': remote.get('original_amount'),
                'recurrent_id': record.recurrent_id,
            }
            before = record.charges_success
            await self._process_antilopay_recurrent_charge(db, record, payload)
            await db.refresh(record)
            if record.charges_success > before:
                processed += 1
                logger.warning(
                    'Antilopay: списание добито по истории рекуррента (callback не дошёл)',
                    local_id=record.id,
                    payment_id=payment_id,
                )
        return processed

    async def _activate_antilopay_trial(self, db: AsyncSession, record: Any) -> bool:
        """Выдаёт триальный доступ по callback привязки СБП (зеркало ``_activate_lava_trial``).

        Идемпотентно по ``trial_activated_at``. Транзакция не создаётся: в момент привязки
        оплаты подписки нет, первое списание придёт отдельным callback после ``free_days``.
        """
        if record.free_days <= 0 or record.trial_activated_at is not None or record.charges_success > 0:
            test_log(  # TEST-LOG
                'trial_activation_skipped',
                local_id=record.id,
                free_days=record.free_days,
                trial_activated_at=str(record.trial_activated_at),
                charges_success=record.charges_success,
            )
            return False

        from app.database.crud.subscription import _lock_subscription_row, reconcile_tariff_traffic_limit
        from app.database.models import Subscription, SubscriptionStatus
        from app.services.grace_access_echo import undo_grace_overlay_echo

        subscription = await db.get(Subscription, record.subscription_id)
        if subscription is None:
            logger.error('Antilopay: подписка не найдена при активации триала', order_id=record.order_id)
            return False

        from app.database.crud.subscription import get_other_alive_subscription_for_tariff

        alive_duplicate = await get_other_alive_subscription_for_tariff(db, subscription)
        if alive_duplicate is not None:
            # Повторный клик оформил вторую привязку, пока первая уже выдала триал:
            # активация упала бы на uq_subscriptions_user_tariff_active и (без отката)
            # ломала мониторинг. Гасим лишнюю привязку, в том числе у провайдера.
            logger.warning(
                'Antilopay: у пользователя уже есть живая подписка на тариф — отменяем лишнюю привязку СБП',
                order_id=record.order_id,
                subscription_id=subscription.id,
                alive_subscription_id=alive_duplicate.id,
            )
            await self.cancel_antilopay_subscription_record(db, record)
            return False

        await _lock_subscription_row(db, subscription)
        await undo_grace_overlay_echo(db, subscription)

        subscription.extend_subscription(record.free_days)
        await reconcile_tariff_traffic_limit(db, subscription)
        # Черновик триала создаётся в PENDING — статус выставляем явно.
        subscription.status = SubscriptionStatus.ACTIVE.value

        activated_at = datetime.now(UTC)
        record.trial_activated_at = activated_at
        record.status = 'ACTIVE'
        record.next_charge_at = activated_at + timedelta(days=record.free_days)
        await db.commit()
        test_log(  # TEST-LOG
            'trial_activated',
            local_id=record.id,
            free_days=record.free_days,
            next_charge_at=str(record.next_charge_at),
            end_date=str(subscription.end_date),
        )

        await self._notify_antilopay_recurring(db, record, 'trial_started')

        bot = getattr(self, 'bot', None)
        if bot is not None:
            try:
                from app.database.models import User
                from app.services.admin_notification_service import AdminNotificationService

                trial_user = await db.get(User, record.user_id)
                if trial_user is not None:
                    await AdminNotificationService(bot).send_trial_activation_notification(db, trial_user, subscription)
            except Exception as notify_error:  # best-effort: активация уже в БД
                logger.warning(
                    'Не удалось отправить админ-уведомление об активации триала Antilopay',
                    error=str(notify_error),
                    subscription_id=subscription.id,
                )

        subscription_id_for_log = subscription.id
        user_id_for_log = record.user_id
        panel_user = None
        try:
            from app.services.subscription_service import SubscriptionService

            # Новый пользователь ещё не заведён в панели — нужен create, а не update.
            panel_user = await SubscriptionService().create_remnawave_user(db, subscription)
        except Exception as sync_error:  # best-effort: активация уже в БД
            logger.error(
                'Не удалось создать пользователя RemnaWave после активации триала Antilopay',
                error=str(sync_error),
                subscription_id=subscription_id_for_log,
            )
        if panel_user is None:
            # create_remnawave_user глотает ошибки панели и возвращает None — тоже ретраим.
            from app.services.remnawave_retry_queue import remnawave_retry_queue

            remnawave_retry_queue.enqueue(
                subscription_id=subscription_id_for_log,
                user_id=user_id_for_log,
                action='create',
            )
            # Сбой create откатывает сессию и «протухают» ORM-объекты — вызывающим
            # (реконсилер) они нужны дальше, иначе MissingGreenlet при чтении полей.
            try:
                await db.refresh(record)
                await db.refresh(subscription)
            except Exception:  # best-effort: активация уже в БД
                pass
        return True

    async def _notify_antilopay_recurring(self, db: AsyncSession, record: Any, kind: str) -> None:
        """Best-effort уведомление пользователя о событии СБП-рекуррента; никогда не бросает."""
        bot = getattr(self, 'bot', None)
        if not bot or not settings.is_notifications_enabled():
            return
        try:
            from app.database.models import User
            from app.localization.texts import get_texts

            user = await db.get(User, record.user_id)
            if not user or not user.telegram_id:
                return
            texts = get_texts(user.language)
            messages = {
                'trial_started': texts.t('TRIAL_SBP_NOTIFY_STARTED', '🎁 СБП привязан, пробный период активирован.'),
                'confirmed': texts.t(
                    'ANTILOPAY_RECURRING_NOTIFY_CONFIRMED', '✅ Подписка продлена автосписанием по СБП.'
                ),
                'failed': texts.t(
                    'ANTILOPAY_RECURRING_NOTIFY_FAILED', '⚠️ Не удалось списать оплату по автопродлению через СБП.'
                ),
                'cancelled': texts.t(
                    'ANTILOPAY_RECURRING_NOTIFY_CANCELLED',
                    'ℹ️ Автопродление через СБП отключено: цена тарифа изменилась. Подключите его заново.',
                ),
            }
            text = messages.get(kind)
            if text:
                reply_markup = None
                if kind in ('trial_started', 'confirmed'):
                    # Отмена автопродления должна быть под рукой сразу, а не только в глубине меню.
                    from aiogram import types

                    reply_markup = types.InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                types.InlineKeyboardButton(
                                    text=texts.t('ANTILOPAY_RECURRING_MENU_BUTTON', '⚡ СБП-автопродление (Antilopay)'),
                                    callback_data='antilopay_recurring_menu',
                                )
                            ]
                        ]
                    )
                await bot.send_message(chat_id=user.telegram_id, text=text, reply_markup=reply_markup)
        except Exception as error:  # pragma: no cover - best-effort notify
            logger.warning('Не удалось отправить уведомление об автопродлении Antilopay', error=str(error), kind=kind)

    @staticmethod
    async def _cancel_antilopay_remote(record: Any) -> None:
        """Удалённая отмена рекуррента; best-effort — сбой не мешает локальной отмене."""
        if not record.recurrent_id and not record.antilopay_payment_id:
            return
        try:
            await antilopay_service.cancel_recurrent(
                recurrent_id=record.recurrent_id,
                transaction_id=None if record.recurrent_id else record.antilopay_payment_id,
            )
        except Exception as error:  # pragma: no cover - network errors
            logger.warning(
                'Не удалось отменить СБП-рекуррент Antilopay на стороне провайдера',
                order_id=record.order_id,
                error=str(error),
            )

    async def cancel_antilopay_subscription_record(self, db: AsyncSession, record: Any, *, commit: bool = True) -> bool:
        """Отменяет СБП-рекуррент: удалённо (best-effort) и локально. Идемпотентно."""
        if record.status == 'CANCELLED':
            return True
        await self._cancel_antilopay_remote(record)
        record.status = 'CANCELLED'
        if commit:
            await db.commit()
        else:
            await db.flush()
        return True

    async def check_antilopay_payment_status(
        self,
        db: AsyncSession,
        order_id: str,
    ) -> dict[str, Any] | None:
        """Проверяет статус платежа через API Antilopay."""
        try:
            result = await antilopay_service.check_payment(order_id=order_id)
            return result
        except Exception as e:
            logger.error('Antilopay: ошибка проверки статуса', order_id=order_id, error=e)
            return None


class _AntilopayRecurrentAgent(AntilopayPaymentMixin):
    """Минимальный носитель ``AntilopayPaymentMixin`` для модульных точек входа
    СБП-рекуррента (без полного ``PaymentService``)."""


async def start_antilopay_trial_sbp(
    db: AsyncSession,
    *,
    user: Any,
    tariff: Any,
) -> dict[str, Any]:
    """Платный триал через привязку СБП-рекуррента Antilopay.

    Создаёт PENDING-черновик триала и платёж-привязку с ``delay`` = ``TRIAL_DURATION_DAYS``.
    Доступ выдаётся по callback привязки, см. ``AntilopayPaymentMixin._activate_antilopay_trial``.
    """
    if not settings.is_antilopay_sbp_enabled():
        raise RuntimeError('Antilopay SBP is disabled')

    if user.is_trial_already_used():
        raise ValueError('Пробный период уже использован')

    # tariff приходит из callback_data пользователя — разрешён только тариф из набора триальных.
    from app.database.crud.tariff import resolve_trial_tariffs

    trial_tariffs = await resolve_trial_tariffs(db)
    if getattr(tariff, 'id', None) not in {t.id for t in trial_tariffs}:
        raise ValueError('Тариф недоступен для пробного периода')

    if (tariff.get_price_for_period(TRIAL_CHARGE_DAYS) or 0) <= 0:
        raise ValueError(f'Для тарифа не задана цена за {TRIAL_CHARGE_DAYS} дней — СБП-триал недоступен')

    from app.database.crud.subscription import create_trial_draft_subscription

    subscription = await create_trial_draft_subscription(db, user.id, tariff)
    result = await _AntilopayRecurrentAgent().create_antilopay_trial_subscription(
        db, user=user, tariff=tariff, subscription=subscription
    )
    return {**result, 'subscription_id': subscription.id}


async def cancel_antilopay_recurring_for_subscription_safe(
    db: AsyncSession,
    subscription_id: int,
    *,
    commit: bool = True,
) -> None:
    """Точка входа для путей удаления/отзыва подписки: отменяет СБП-рекуррент Antilopay.

    Best-effort, не бросает исключений. НЕ гейтится флагами намеренно — отмена это операция
    безопасности: выключение фичи не останавливает списания на стороне провайдера.
    """
    try:
        from app.database.crud import antilopay_subscription as sub_crud

        record = await sub_crud.get_active_antilopay_subscription_by_subscription(db, subscription_id)
        if record:
            await _AntilopayRecurrentAgent().cancel_antilopay_subscription_record(db, record, commit=commit)
    except Exception as error:  # pragma: no cover - defensive
        logger.warning(
            'Не удалось отменить СБП-рекуррент Antilopay при удалении подписки',
            error=str(error),
            subscription_id=subscription_id,
        )


async def get_antilopay_recurring_status(db: AsyncSession, subscription_id: int) -> dict[str, Any] | None:
    """Состояние активной привязки СБП Antilopay для UI (бот/кабинет) либо None."""
    from app.database.crud import antilopay_subscription as sub_crud

    record = await sub_crud.get_active_antilopay_subscription_by_subscription(db, subscription_id)
    if not record:
        return None
    return {
        'local_id': record.id,
        'recurrent_id': record.recurrent_id,
        'status': record.status,
        'amount_kopeks': record.amount_kopeks,
        'charge_days': record.charge_days,
        'redirect_url': record.redirect_url,
        'next_charge_at': record.next_charge_at,
        'last_charge_at': record.last_charge_at,
        'charges_success': record.charges_success,
        'charges_failed': record.charges_failed,
    }


async def cancel_antilopay_recurring_by_local_id(db: AsyncSession, local_id: int) -> bool:
    """Отмена привязки по локальному id (кабинет/бот). Идемпотентна."""
    from app.database.crud import antilopay_subscription as sub_crud

    record = await sub_crud.get_antilopay_subscription_by_id(db, local_id)
    if record is None:
        return False
    return await _AntilopayRecurrentAgent().cancel_antilopay_subscription_record(db, record)
