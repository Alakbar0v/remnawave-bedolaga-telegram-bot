"""add paid trial fields: tariffs.trial_card_product_id, lava_subscriptions.free_days/trial_activated_at

Платный триал через Lava: привязка карты к продукту с freeDays в кабинете Lava
(1₽ верификация сейчас, полная сумма — автоматически после freeDays). У тарифа
нужен второй слот продукта (``tariffs.trial_card_product_id``), а в записи
привязки — скопированный ``freeDays`` и метка обработки вебхука ``activated``.

Revision ID: custom_0005
Revises: 0131, custom_merge_0127
"""

import sqlalchemy as sa
from alembic import op


revision = 'custom_0005'
down_revision = ('0131', 'custom_merge_0127')
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {c['name'] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    # Идемпотентно: миграция раньше имела ID 0128 и могла быть уже применена.
    if 'trial_card_product_id' not in _columns('tariffs'):
        with op.batch_alter_table('tariffs') as batch:
            batch.add_column(sa.Column('trial_card_product_id', sa.String(length=255), nullable=True))

    existing = _columns('lava_subscriptions')
    with op.batch_alter_table('lava_subscriptions') as batch:
        if 'free_days' not in existing:
            batch.add_column(sa.Column('free_days', sa.Integer(), nullable=False, server_default='0'))
        if 'trial_activated_at' not in existing:
            batch.add_column(sa.Column('trial_activated_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('lava_subscriptions') as batch:
        batch.drop_column('trial_activated_at')
        batch.drop_column('free_days')

    with op.batch_alter_table('tariffs') as batch:
        batch.drop_column('trial_card_product_id')
