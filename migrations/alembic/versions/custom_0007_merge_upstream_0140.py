"""merge fork head custom_0006 with upstream 0140

Revision ID: custom_0007
Revises: custom_0006, 0140
Create Date: 2026-11-09

No-op merge point joining the fork's chain (custom_0006) with upstream's
numeric chain (0132..0140) so that ``alembic upgrade head`` has a single head.
"""

from typing import Sequence, Union

revision: str = 'custom_0007'
down_revision: Union[str, tuple[str, ...], None] = ('custom_0006', '0140')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
