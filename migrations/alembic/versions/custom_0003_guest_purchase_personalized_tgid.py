"""add personalized_telegram_id to guest_purchases

Revision ID: custom_0003
Revises: custom_0002
Create Date: 2026-08-16
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = 'custom_0003'
down_revision: Union[str, None] = 'custom_0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'guest_purchases' AND column_name = 'personalized_telegram_id')"
        )
    )
    if not result.scalar():
        op.add_column('guest_purchases', sa.Column('personalized_telegram_id', sa.BigInteger(), nullable=True))

    index_exists = conn.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM pg_indexes "
            "WHERE tablename = 'guest_purchases' AND indexname = 'ix_guest_purchases_personalized_tgid')"
        )
    )
    if not index_exists.scalar():
        op.create_index(
            'ix_guest_purchases_personalized_tgid',
            'guest_purchases',
            ['personalized_telegram_id'],
        )


def downgrade() -> None:
    conn = op.get_bind()
    index_exists = conn.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM pg_indexes "
            "WHERE tablename = 'guest_purchases' AND indexname = 'ix_guest_purchases_personalized_tgid')"
        )
    )
    if index_exists.scalar():
        op.drop_index('ix_guest_purchases_personalized_tgid', table_name='guest_purchases')

    result = conn.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'guest_purchases' AND column_name = 'personalized_telegram_id')"
        )
    )
    if result.scalar():
        op.drop_column('guest_purchases', 'personalized_telegram_id')
