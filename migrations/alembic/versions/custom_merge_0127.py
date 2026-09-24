"""merge custom fork branch with upstream 0127

Revision ID: custom_merge_0127
Revises: custom_merge_0118, 0127
Create Date: 2026-09-24

No-op merge point joining our fork's custom_* branch (custom_merge_0118)
with upstream's numeric chain, which has advanced to 0127 (reminders tables)
since the last merge point. See custom_merge_0106's docstring for the
general convention.
"""

from typing import Sequence, Union

revision: str = 'custom_merge_0127'
down_revision: Union[str, tuple[str, ...], None] = ('custom_merge_0118', '0127')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
