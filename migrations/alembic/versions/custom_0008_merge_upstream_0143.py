"""merge fork head custom_0007 with upstream 0143

Revision ID: custom_0008
Revises: custom_0007, 0143
Create Date: 2026-11-21

No-op merge point joining the fork's chain (custom_0007) with upstream's
numeric chain (0141..0143) so that ``alembic upgrade head`` has a single head.
"""

from typing import Sequence, Union

revision: str = 'custom_0008'
down_revision: Union[str, tuple[str, ...], None] = ('custom_0007', '0143')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
