"""Which runtime actually ran, and every switch on the way to it.

Revision ID: 039
Revises: 038
Create Date: 2026-10-24

A conversation can change runtime underneath itself: the SDK fails over on a
usage limit and keeps going. Until now ``builder_runtime`` held the runtime the
cycle *began* on, so a change built by the fallback was recorded against the
runtime that ran out -- wrong in the audit trail and wrong in any measure of
which runtime does the work.

The switches are stored rather than derived because the conversation they came
from is deleted with its sandbox, and a route that can only be reconstructed
from a transcript that no longer exists is not an audit trail.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "039"
down_revision: str | None = "038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("automation_improve_cycle") as batch:
        batch.add_column(sa.Column("runtime_switches", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("automation_improve_cycle") as batch:
        batch.drop_column("runtime_switches")
