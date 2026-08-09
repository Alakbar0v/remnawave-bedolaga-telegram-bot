"""add ttp column to tiktok_click_id_map

Revision ID: 0106
Revises: 0105
Create Date: 2026-08-09
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0106'
down_revision: Union[str, None] = '0105'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'tiktok_click_id_map' AND column_name = 'ttp')"
        )
    )
    if not result.scalar():
        op.add_column('tiktok_click_id_map', sa.Column('ttp', sa.String(256), nullable=True))


def downgrade() -> None:
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'tiktok_click_id_map' AND column_name = 'ttp')"
        )
    )
    if result.scalar():
        op.drop_column('tiktok_click_id_map', 'ttp')
