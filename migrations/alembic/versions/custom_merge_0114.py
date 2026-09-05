"""merge custom fork branch with upstream 0114

Revision ID: custom_merge_0114
Revises: custom_0003, 0114
Create Date: 2026-09-06

No-op merge point joining our fork's custom_* branch (custom_0003, the
TikTok ip/user-agent migration) with upstream's numeric chain, which has
advanced to 0114 since the last merge point (custom_merge_0106). See
custom_merge_0106's docstring for the general convention: after each
upstream pull that advances the numeric chain, add a new custom_merge_*
revision here rather than renumbering our own migrations.
"""

from typing import Sequence, Union

revision: str = 'custom_merge_0114'
down_revision: Union[str, tuple[str, ...], None] = ('custom_0003', '0114')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
