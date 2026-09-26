"""ВРЕМЕННОЕ тестовое логирование Antilopay (рекуррент/СБП). УДАЛИТЬ после проверки на живых платежах.

Как искать в логах:  grep "ANTILOPAY_TEST_LOG"
Как убрать: удалить этот файл и все вызовы ``test_log(...)`` (искать по ``# TEST-LOG``).

Что хотим выяснить на реальном рекуррентном платеже (delay = 1 день):
  1. Совпадает ли ``payment_id`` регулярного списания с ``payment_id`` привязки
     (код исходит из того, что НЕ совпадает; поле ``same_as_binding`` в событии recurrent_charge).
  2. Реальная структура ответа ``payment/recurrent/check`` (событие api_response check_recurrent).
  3. Какие ``amount``/``original_amount`` приходят при привязке (delay > 0).
"""

from __future__ import annotations

from typing import Any

import structlog


logger = structlog.get_logger('antilopay.test_log')

TAG = 'ANTILOPAY_TEST_LOG'
_SENSITIVE_KEYS = frozenset({'customer', 'pay_data'})


def redact(data: Any) -> Any:
    """Копия dict без персональных данных (customer, pay_data)."""
    if isinstance(data, dict):
        return {k: ('<redacted>' if k in _SENSITIVE_KEYS else redact(v)) for k, v in data.items()}
    if isinstance(data, list):
        return [redact(v) for v in data]
    return data


def test_log(event: str, **fields: Any) -> None:
    """Одна строка ``ANTILOPAY_TEST_LOG <event>`` с полями. Никогда не бросает исключений."""
    try:
        logger.info(f'{TAG} {event}', **{k: redact(v) for k, v in fields.items()})
    except Exception:  # логирование не должно ломать платёжный поток
        pass
