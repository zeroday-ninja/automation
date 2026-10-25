"""One repository, one lesson subject.

Revision ID: 046
Revises: 045
Create Date: 2026-10-25

A project-scope lesson was filed under whichever name the program that
captured it used -- a Git Sync remote id for a synced rotation, a label for a
hand-listed one. So a correction given about a repository was *out of scope*
for another program working on the same repository: never injected into its
cycles, and diagnosed on the repeat as "filed against project/<other name>,
so this work never had it in scope". Entirely true, and entirely wrong about
the repository.

Subjects are now the repository's own identity, computed in one place. This
rewrites the ones already stored, because a lesson nobody can match is a
correction the person has to give again -- which is the one thing the whole
mechanism exists to prevent.

Only project scope: a runtime is named by its runtime name and a surface by
its own id, and neither is a repository.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from openhands.automation.improve.caps import repository_identity


revision: str = "046"
down_revision: str | None = "045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT id, subject FROM automation_lesson "
            "WHERE scope = 'project' AND subject IS NOT NULL"
        )
    ).fetchall()
    for lesson_id, subject in rows:
        # The stored name is all there is to go on, so it is read as both the
        # label and the url. A url normalises; a bare name is not a url and
        # falls through to `label:`, which says plainly that nobody ever
        # recorded which repository it was — inventing a url-shaped identity
        # from a name would make it look matchable when it is not.
        identity = repository_identity(None, subject, subject)
        if identity == subject:
            continue
        bind.execute(
            sa.text("UPDATE automation_lesson SET subject = :subject WHERE id = :id"),
            {"subject": identity, "id": lesson_id},
        )


def downgrade() -> None:
    """Strip the kind prefix, which is as far back as this can go.

    The original spelling is not recoverable -- `url:github.com/o/r` could
    have been written four ways -- and a lesson still matches itself after
    this, so the data stays usable rather than pretending to be restored.
    """
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT id, subject FROM automation_lesson "
            "WHERE scope = 'project' AND subject IS NOT NULL"
        )
    ).fetchall()
    for lesson_id, subject in rows:
        for prefix in ("url:", "remote:", "label:"):
            if subject.startswith(prefix):
                bind.execute(
                    sa.text(
                        "UPDATE automation_lesson SET subject = :subject WHERE id = :id"
                    ),
                    {"subject": subject[len(prefix) :], "id": lesson_id},
                )
                break
