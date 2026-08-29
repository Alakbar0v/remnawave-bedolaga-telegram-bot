"""add ip and user_agent columns to tiktok_click_id_map

Revision ID: custom_0003
Revises: custom_merge_0106
Create Date: 2026-08-29
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'custom_0003'
down_revision: Union[str, None] = 'custom_merge_0106'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    for column_name, column_type in (('ip', sa.String(64)), ('user_agent', sa.String(512))):
        result = conn.execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'tiktok_click_id_map' AND column_name = :column_name)"
            ),
            {'column_name': column_name},
        )
        if not result.scalar():
            op.add_column('tiktok_click_id_map', sa.Column(column_name, column_type, nullable=True))


def downgrade() -> None:
    conn = op.get_bind()
    for column_name in ('ip', 'user_agent'):
        result = conn.execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'tiktok_click_id_map' AND column_name = :column_name)"
            ),
            {'column_name': column_name},
        )
        if result.scalar():
            op.drop_column('tiktok_click_id_map', column_name)
