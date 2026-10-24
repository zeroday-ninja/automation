"""Where each gate sent the work, and a cycle that is waiting for a person.

Revision ID: 041
Revises: 040
Create Date: 2026-10-24

Two things the audit trail could not answer: *why did this go that way*, and
*what is waiting on me*.

The gate log answers the first. A verdict says whether a change is good; it
never said what happened next, which is why a changes-requested cycle simply
stopped. Each decision is now a row in an append-only list with its route, its
trigger and its rationale.

Parking answers the second. A breaking change is never an agent's decision, and
until now the only way to stop was to fail -- which discards the work and reads
like something went wrong. A parked cycle is not a failed cycle: it holds its
phase and its question, and resolving it resumes from there.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "041"
down_revision: str | None = "040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("automation_improve_cycle") as batch:
        batch.add_column(sa.Column("gate_decisions", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("parked_phase", sa.String(32), nullable=True))
        batch.add_column(sa.Column("parked_question", sa.Text(), nullable=True))
        batch.add_column(sa.Column("parked_plan", sa.JSON(), nullable=True))
        batch.add_column(
            sa.Column("parked_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch.add_column(sa.Column("resolution", sa.Text(), nullable=True))
        batch.add_column(
            sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True)
        )
    # The dashboard's one question about parked work is "what is waiting on
    # me", across every program in the org.
    op.create_index(
        "ix_automation_improve_cycle_parked",
        "automation_improve_cycle",
        ["org_id", "status", "parked_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_automation_improve_cycle_parked",
        table_name="automation_improve_cycle",
    )
    with op.batch_alter_table("automation_improve_cycle") as batch:
        batch.drop_column("resolved_at")
        batch.drop_column("resolution")
        batch.drop_column("parked_at")
        batch.drop_column("parked_plan")
        batch.drop_column("parked_question")
        batch.drop_column("parked_phase")
        batch.drop_column("gate_decisions")
