"""Improvement-program cycles.

Revision ID: 035
Revises: 034
Create Date: 2026-10-21

An improvement program is a cron automation that rotates through the
organization's git-sync remotes, researching and implementing one improvement
per run and then having a different agent runtime review it adversarially.
``automation_improve_cycle`` records each pass: the repository it took, its
position in the rotation, the builder's cited sources, and every reviewer's
verdict. The newest row's ``repo_index`` is also the rotation cursor, so the
history and the cursor cannot drift apart.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "035"
down_revision: str | None = "034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "automation_improve_cycle",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("automation_id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("repo_index", sa.Integer(), nullable=False),
        sa.Column("repo_label", sa.String(length=255), nullable=False),
        sa.Column("repo_url", sa.Text(), nullable=False),
        sa.Column("repo_ref", sa.String(length=255), nullable=True),
        sa.Column("remote_id", sa.Uuid(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "CLAIMED",
                "BUILDING",
                "REVIEWING",
                "COMPLETED",
                "FAILED",
                name="improvecyclestatus",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column(
            "verdict",
            sa.Enum(
                "ACCEPTED",
                "CHANGES_REQUESTED",
                "REJECTED",
                "INCONCLUSIVE",
                "SINGLE_RUNTIME",
                "NO_CHANGE",
                name="improveverdict",
                native_enum=False,
                length=20,
            ),
            nullable=True,
        ),
        sa.Column("builder_profile_id", sa.Uuid(), nullable=True),
        sa.Column("builder_runtime", sa.String(length=64), nullable=True),
        sa.Column("builder_conversation_id", sa.String(length=64), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("branch", sa.String(length=255), nullable=True),
        sa.Column("pr_url", sa.Text(), nullable=True),
        sa.Column("sources", sa.JSON(), nullable=True),
        sa.Column("reviews", sa.JSON(), nullable=True),
        sa.Column("soul_suggestions", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["automation_id"], ["automations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["automation_runs.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["remote_id"],
            ["automation_git_sync_remote.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_automation_improve_cycle_automation_id",
        "automation_improve_cycle",
        ["automation_id"],
    )
    op.create_index(
        "ix_automation_improve_cycle_org_id",
        "automation_improve_cycle",
        ["org_id"],
    )
    op.create_index(
        "ix_automation_improve_cycle_run_id",
        "automation_improve_cycle",
        ["run_id"],
    )
    op.create_index(
        "ix_automation_improve_cycle_status",
        "automation_improve_cycle",
        ["status"],
    )
    op.create_index(
        "ix_automation_improve_cycle_automation_created",
        "automation_improve_cycle",
        ["automation_id", "created_at", "id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_automation_improve_cycle_automation_created",
        table_name="automation_improve_cycle",
    )
    op.drop_index(
        "ix_automation_improve_cycle_status", table_name="automation_improve_cycle"
    )
    op.drop_index(
        "ix_automation_improve_cycle_run_id", table_name="automation_improve_cycle"
    )
    op.drop_index(
        "ix_automation_improve_cycle_org_id", table_name="automation_improve_cycle"
    )
    op.drop_index(
        "ix_automation_improve_cycle_automation_id",
        table_name="automation_improve_cycle",
    )
    op.drop_table("automation_improve_cycle")
