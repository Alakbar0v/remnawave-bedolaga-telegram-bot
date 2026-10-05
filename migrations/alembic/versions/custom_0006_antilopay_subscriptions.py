"""add antilopay_subscriptions (СБП-рекуррент для платного триала)

Платный триал через СБП Antilopay: ``payment/create`` с ``recurrent.category=SUBSCRIPTION``
и ``delay`` = длительность триала. Продуктов у Antilopay нет — сумма и шаг продления
копируются из тарифа на момент оформления.

Revision ID: custom_0006
Revises: custom_0005
"""

import sqlalchemy as sa
from alembic import op


revision = 'custom_0006'
down_revision = 'custom_0005'
branch_labels = None
depends_on = None

_ALIVE = "('PENDING', 'ACTIVE', 'PAST_DUE')"


def upgrade() -> None:
    # Идемпотентно: миграция раньше имела ID 0129 и могла быть уже применена.
    if sa.inspect(op.get_bind()).has_table('antilopay_subscriptions'):
        return
    op.create_table(
        'antilopay_subscriptions',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column(
            'subscription_id', sa.Integer(), sa.ForeignKey('subscriptions.id', ondelete='CASCADE'), nullable=False
        ),
        sa.Column('tariff_id', sa.Integer(), sa.ForeignKey('tariffs.id'), nullable=True),
        sa.Column('order_id', sa.String(length=64), nullable=False),
        sa.Column('antilopay_payment_id', sa.String(length=128), nullable=True),
        sa.Column('recurrent_id', sa.String(length=128), nullable=True),
        sa.Column('charge_days', sa.Integer(), nullable=False),
        sa.Column('amount_kopeks', sa.Integer(), nullable=False),
        sa.Column('currency', sa.String(length=10), nullable=False, server_default='RUB'),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='PENDING'),
        sa.Column('redirect_url', sa.Text(), nullable=True),
        sa.Column('next_charge_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_charge_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_charge_external_id', sa.String(length=128), nullable=True),
        sa.Column('last_failed_external_id', sa.String(length=128), nullable=True),
        sa.Column('charges_success', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('charges_failed', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('free_days', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('trial_activated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('ix_antilopay_subscriptions_user_id', 'antilopay_subscriptions', ['user_id'])
    op.create_index('ix_antilopay_subscriptions_subscription_id', 'antilopay_subscriptions', ['subscription_id'])
    op.create_index('ix_antilopay_subscriptions_order_id', 'antilopay_subscriptions', ['order_id'], unique=True)
    op.create_index('ix_antilopay_subscriptions_recurrent_id', 'antilopay_subscriptions', ['recurrent_id'], unique=True)
    op.create_index('ix_antilopay_subscriptions_user_active', 'antilopay_subscriptions', ['user_id', 'status'])
    op.execute(
        sa.text(
            f"""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_antilopay_subscriptions_alive
            ON antilopay_subscriptions (subscription_id)
            WHERE status IN {_ALIVE}
            """
        )
    )


def downgrade() -> None:
    op.execute(sa.text('DROP INDEX IF EXISTS uq_antilopay_subscriptions_alive'))
    op.drop_table('antilopay_subscriptions')
