"""What class of change a cycle made, and what it claimed to make.

Revision ID: 040
Revises: 039
Create Date: 2026-10-24

Three classes decide who may publish: additive needs nobody's confirmation,
deleting needs a judge's attestation, breaking needs a human's. The class is
computed from the diff rather than claimed, and the claim is stored beside it
so a mismatch is reviewable after the fact rather than only at the moment it
was caught.

The evidence is stored with the class for the same reason the audit columns
exist: a class with no evidence behind it cannot be disagreed with, and the
branch it was read from will not outlive the sandbox.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "040"
down_revision: str | None = "039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("automation_improve_cycle") as batch:
        batch.add_column(sa.Column("change_class", sa.String(16), nullable=True))
        batch.add_column(
            sa.Column("claimed_change_class", sa.String(32), nullable=True)
        )
        batch.add_column(sa.Column("change_evidence", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("automation_improve_cycle") as batch:
        batch.drop_column("change_evidence")
        batch.drop_column("claimed_change_class")
        batch.drop_column("change_class")
