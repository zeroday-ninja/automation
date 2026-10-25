"""What the user's identity took of the cycle's prompt.

Revision ID: 045
Revises: 044
Create Date: 2026-10-25

The identity budget was enforced from the first cycle and never recorded, so
the question the milestone actually asks -- does identity crowd out the
repository the cycle is supposed to improve -- could only be answered by
reading the preset and guessing.

The failure it hides is worse than the unanswered question. A document that
has grown past its slot is trimmed silently: the agent sees `[truncated]`,
and the person who wrote the part that was cut sees nothing at all.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "045"
down_revision: str | None = "044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("automation_improve_cycle") as batch:
        batch.add_column(sa.Column("identity_cost", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("automation_improve_cycle") as batch:
        batch.drop_column("identity_cost")
