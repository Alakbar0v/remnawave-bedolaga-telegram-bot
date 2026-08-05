"""add tiktok_click_id_map table

Revision ID: 0105
Revises: 0104
Create Date: 2026-08-05
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0105'
down_revision: Union[str, None] = '0104'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()

    result = conn.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'tiktok_click_id_map')")
    )
    if not result.scalar():
        op.create_table(
            'tiktok_click_id_map',
            sa.Column('id', sa.Integer, primary_key=True, autoincrement=True),
            sa.Column(
                'user_id',
                sa.Integer,
                sa.ForeignKey('users.id', ondelete='CASCADE'),
                unique=True,
                nullable=False,
            ),
            sa.Column('ttclid', sa.String(512), nullable=False),
            sa.Column('source', sa.String(20), nullable=False, server_default='telegram'),
            sa.Column(
                'registration_sent',
                sa.Boolean,
                nullable=False,
                server_default=sa.text('false'),
            ),
            sa.Column(
                'trial_sent',
                sa.Boolean,
                nullable=False,
                server_default=sa.text('false'),
            ),
            sa.Column(
                'first_connected_sent',
                sa.Boolean,
                nullable=False,
                server_default=sa.text('false'),
            ),
            sa.Column(
                'created_at',
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
            ),
            sa.Column(
                'updated_at',
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
            ),
        )


def downgrade() -> None:
    conn = op.get_bind()
    result = conn.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'tiktok_click_id_map')")
    )
    if result.scalar():
        op.drop_table('tiktok_click_id_map')
