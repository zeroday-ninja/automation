"""Whether the change landed, and as which commit.

Revision ID: 044
Revises: 043
Create Date: 2026-10-24

The gate log already records that the publish gate approved. That is a
decision, not an outcome: a merge can be authorised and still fail, and a
cycle that merged recorded the fact only inside a rationale sentence carrying
git's own output. So "how many cycles merged" and "which commit is this
cycle's change on the default branch" both meant parsing prose.

Read from the forge after the merge rather than from the merge command's
output, which reports the local fast-forward and not the commit the forge
created.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "044"
down_revision: str | None = "043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("automation_improve_cycle") as batch:
        batch.add_column(
            sa.Column("merged_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch.add_column(
            sa.Column("merge_commit_sha", sa.String(length=64), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("automation_improve_cycle") as batch:
        batch.drop_column("merge_commit_sha")
        batch.drop_column("merged_at")
