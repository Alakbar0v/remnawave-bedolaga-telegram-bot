"""add paid trial fields: tariffs.trial_card_product_id, lava_subscriptions.free_days/trial_activated_at

Платный триал через Lava: привязка карты к продукту с freeDays в кабинете Lava
(1₽ верификация сейчас, полная сумма — автоматически после freeDays). У тарифа
нужен второй слот продукта (``tariffs.trial_card_product_id``), а в записи
привязки — скопированный ``freeDays`` и метка обработки вебхука ``activated``.

Revision ID: 0128
Revises: custom_merge_0127
"""

import sqlalchemy as sa
from alembic import op


revision = '0128'
down_revision = 'custom_merge_0127'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('tariffs') as batch:
        batch.add_column(sa.Column('trial_card_product_id', sa.String(length=255), nullable=True))

    with op.batch_alter_table('lava_subscriptions') as batch:
        batch.add_column(sa.Column('free_days', sa.Integer(), nullable=False, server_default='0'))
        batch.add_column(sa.Column('trial_activated_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('lava_subscriptions') as batch:
        batch.drop_column('trial_activated_at')
        batch.drop_column('free_days')

    with op.batch_alter_table('tariffs') as batch:
        batch.drop_column('trial_card_product_id')
