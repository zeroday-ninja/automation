"""What a cycle actually changed, so it can be audited.

Revision ID: 036
Revises: 035
Create Date: 2026-10-24

A cycle recorded its verdict and its reasoning but not its artifact: no
commit SHA, no list of changed files, no record of the checks it ran. So a
merged change could not be traced back to the prompt and the sources that
produced it, which is the one question an audit trail exists to answer.

``reaped`` is separate from the status enum on purpose. A cycle closed by the
janitor is an absence of evidence, not a verdict, and adding an enum member
would silently change the behaviour of every client that switches on it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "036"
down_revision: str | None = "035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_COLUMNS = (
    sa.Column("base_sha", sa.String(length=64), nullable=True),
    sa.Column("head_sha", sa.String(length=64), nullable=True),
    sa.Column("changed_files", sa.JSON(), nullable=True),
    sa.Column("checks", sa.JSON(), nullable=True),
    sa.Column("followups", sa.JSON(), nullable=True),
    sa.Column("risks", sa.JSON(), nullable=True),
    sa.Column("prompt_digest", sa.String(length=71), nullable=True),
    sa.Column("verdict_rule_version", sa.String(length=32), nullable=True),
)


def upgrade() -> None:
    # batch_alter_table so this works on SQLite, which this deployment runs
    # even though the service's comments claim PostgreSQL everywhere.
    with op.batch_alter_table("automation_improve_cycle") as batch:
        for column in _COLUMNS:
            batch.add_column(column)
        batch.add_column(
            sa.Column(
                "reaped",
                sa.Boolean(),
                nullable=False,
                # sa.false(), not a literal 0: SQLite takes either, and
                # PostgreSQL refuses an integer default on a boolean column
                # outright, so a literal breaks the migration on the engine
                # the service is actually written for.
                server_default=sa.false(),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("automation_improve_cycle") as batch:
        batch.drop_column("reaped")
        for column in reversed(_COLUMNS):
            batch.drop_column(column.name)
