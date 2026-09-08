"""merge custom fork branch with upstream 0116

Revision ID: custom_merge_0116
Revises: custom_merge_0114, 0116
Create Date: 2026-09-08

No-op merge point joining our fork's custom_* branch (custom_merge_0114)
with upstream's numeric chain, which has advanced to 0116 (reachability
tables/batches) since the last merge point. See custom_merge_0106's
docstring for the general convention: after each upstream pull that
advances the numeric chain, add a new custom_merge_* revision here rather
than renumbering our own migrations.
"""

from typing import Sequence, Union

revision: str = 'custom_merge_0116'
down_revision: Union[str, tuple[str, ...], None] = ('custom_merge_0114', '0116')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
