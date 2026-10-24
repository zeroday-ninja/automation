"""What a cycle left for the next one when it ran out of runtime.

Revision ID: 038
Revises: 037
Create Date: 2026-10-24

A cycle whose runtime is exhausted mid-change has done real work and can
publish none of it. Restarting from nothing is the expensive answer; the cheap
one is to spend what is left of that runtime writing down where it got to.

The structured record goes here because the claim needs a pointer it can
follow without guessing a branch name, and the pointer is bidirectional so the
history reads as a chain rather than as two unrelated cycles sharing a branch.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "038"
down_revision: str | None = "037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("automation_improve_cycle") as batch:
        batch.add_column(sa.Column("handoff", sa.JSON(), nullable=True))
        batch.add_column(
            sa.Column("resumed_from_cycle_id", sa.Uuid(), nullable=True)
        )
        batch.add_column(sa.Column("resumed_by_cycle_id", sa.Uuid(), nullable=True))
    op.create_index(
        "ix_automation_improve_cycle_resumed_by",
        "automation_improve_cycle",
        ["resumed_by_cycle_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_automation_improve_cycle_resumed_by",
        table_name="automation_improve_cycle",
    )
    with op.batch_alter_table("automation_improve_cycle") as batch:
        batch.drop_column("resumed_by_cycle_id")
        batch.drop_column("resumed_from_cycle_id")
        batch.drop_column("handoff")
