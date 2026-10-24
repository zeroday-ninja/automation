"""The judge's attestation of a deletion.

Revision ID: 042
Revises: 041
Create Date: 2026-10-24

A deletion may publish without a person, but not on its author's word: the
judge attests that each removal is dead, replaced, or covered, naming where.
Stored rather than derived, for the reason the rest of the audit columns are:
the conversation that produced it is deleted with its sandbox.

The runtime is stored with it because that is what the service checks. A model
favours its own output, so the signer must not be the author -- and a rotation
moves the builder between cycles, so the check has to read what actually
signed rather than what was configured.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "042"
down_revision: str | None = "041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("automation_improve_cycle") as batch:
        batch.add_column(sa.Column("judge_attestation", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("automation_improve_cycle") as batch:
        batch.drop_column("judge_attestation")
