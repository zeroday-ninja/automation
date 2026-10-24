"""Corrections, so the same one never has to be given twice.

Revision ID: 043
Revises: 042
Create Date: 2026-10-24

A document a model forgot to read is indistinguishable from a document that
does not exist, which is why memory alone does not stop repetition. These rows
are what make the documents *complete*: each is a correction that actually
happened, with the scope it applies to and a link to the work that caused it.

The incident table is the part that makes this a learning mechanism rather than
a memory one. A correction arriving twice is a defect of the pipeline, and the
row records which stage broke -- never captured, out of scope, in scope but not
injected, injected but ignored, or not mechanisable yet.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "043"
down_revision: str | None = "042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "automation_lesson",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), nullable=False, index=True),
        sa.Column("scope", sa.String(16), nullable=False),
        # Null means "every subject in this scope", which is what it means when
        # the correction did not name one.
        sa.Column("subject", sa.String(255), nullable=True),
        sa.Column("trigger", sa.String(32), nullable=False),
        # The user's words, verbatim. Paraphrasing a correction is how it comes
        # back as something they have to give again.
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("why", sa.Text(), nullable=True),
        # What caused it: a cycle, a pull request, a conversation.
        sa.Column("source", sa.JSON(), nullable=True),
        # A lesson that can be checked becomes a check, and a model that skims
        # the document still fails the check.
        sa.Column(
            "mechanised", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("check_ref", sa.Text(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
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
    )
    # The one question a cycle asks: what am I under, here.
    op.create_index(
        "ix_automation_lesson_scope",
        "automation_lesson",
        ["org_id", "status", "scope", "subject"],
    )
    op.create_table(
        "automation_lesson_incident",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "lesson_id",
            sa.Uuid(),
            sa.ForeignKey("automation_lesson.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("org_id", sa.Uuid(), nullable=False, index=True),
        sa.Column("stage", sa.String(32), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("similarity", sa.Float(), nullable=True),
        sa.Column("source", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )

    # What a cycle had in front of it. On the cycle rather than derived,
    # because it is what diagnoses a repeat and the conversation it came from
    # does not outlive its sandbox.
    with op.batch_alter_table("automation_improve_cycle") as batch:
        batch.add_column(sa.Column("injected_lesson_ids", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("automation_improve_cycle") as batch:
        batch.drop_column("injected_lesson_ids")
    op.drop_table("automation_lesson_incident")
    op.drop_index("ix_automation_lesson_scope", table_name="automation_lesson")
    op.drop_table("automation_lesson")
