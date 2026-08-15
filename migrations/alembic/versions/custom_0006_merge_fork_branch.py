"""merge fork custom_* branch with upstream 0131

Revision ID: custom_0006
Revises: custom_0004, 0131
Create Date: 2026-10-15

No-op merge point joining our fork's custom_* chain (TikTok click-id map,
personalized landing purchases) with upstream's numeric chain.

The id ``custom_0006`` is deliberate: production databases were stamped with it
by an earlier fork head. Keeping a revision with this id lets
``alembic upgrade head`` resolve those databases; any tables left over from
that head are simply unused.
"""

from typing import Sequence, Union

revision: str = 'custom_0006'
down_revision: Union[str, tuple[str, ...], None] = ('custom_0004', '0131')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
