"""What is known about each agent runtime's remaining usage.

Revision ID: 037
Revises: 036
Create Date: 2026-10-24

Scheduling memory: which subscription is spent, and until when. It spans
cycles, programs and days, so it sits beside the scheduler rather than inside
any one run, and it is org-scoped because several programs share two or three
runtimes — a per-automation record would let one program spend the usage
another thinks it still has.

No index beyond the uniqueness constraint: this table holds one row per
runtime per organization, which is a handful.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "037"
down_revision: str | None = "036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "automation_runtime_usage",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), nullable=False, index=True),
        sa.Column("runtime", sa.String(length=64), nullable=False),
        sa.Column(
            "state",
            sa.Enum(
                "AVAILABLE",
                "EXHAUSTED",
                "UNKNOWN",
                name="runtimeusagestate",
                native_enum=False,
                length=16,
            ),
            nullable=False,
            server_default="UNKNOWN",
        ),
        sa.Column("last_exhausted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("observed_reset_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reset_source", sa.String(length=32), nullable=True),
        sa.Column(
            "consecutive_exhaustions",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "window_started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("cycles_attempted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cycles_completed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_observation", sa.JSON(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint("org_id", "runtime", name="uq_runtime_usage_org_runtime"),
    )


def downgrade() -> None:
    op.drop_table("automation_runtime_usage")
