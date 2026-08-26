"""merge custom fork branch with upstream 0106

Revision ID: custom_merge_0106
Revises: 0106, custom_0003
Create Date: 2026-08-26

No-op merge point joining upstream's numeric migration chain with our
fork's custom_* branch. After each upstream pull that advances the
numeric chain, add a new custom_merge_* revision here rather than
renumbering our own migrations.
"""

from typing import Sequence, Union

revision: str = 'custom_merge_0106'
down_revision: Union[str, tuple[str, ...], None] = ('0106', 'custom_0003')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
