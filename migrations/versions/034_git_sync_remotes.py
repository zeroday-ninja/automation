"""Git sync to any number of repositories per organization.

Revision ID: 034
Revises: 033
Create Date: 2026-10-21

Git sync used to be one repository per organization: its config and status
lived on ``automation_git_sync_org_config``, keyed by org, and every automation
of the org was exported to that one repo. An organization now has any number
of *remotes* (``automation_git_sync_remote``), each its own repository, branch
and path with its own config, status and lease, and each automation syncs to
at most one of them (``automation_git_sync_state.remote_id``). The old row
becomes the org's default remote and every existing state row is assigned to
it, so a configured deployment keeps syncing exactly as before. Rows that
never held a configuration or a cycle result (the empty row local mode
creates on every tick) are dropped: their states stay unassigned and are
adopted by whichever remote becomes the default.
"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "034"
down_revision: str | None = "033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_org_config = sa.table(
    "automation_git_sync_org_config",
    sa.column("org_id", sa.Uuid()),
    sa.column("overrides", sa.Text()),
    sa.column("configured_by_user_id", sa.Uuid()),
    sa.column("last_synced_commit", sa.String(64)),
    sa.column("last_synced_path", sa.String(255)),
    sa.column("last_run_at", sa.DateTime(timezone=True)),
    sa.column("last_error", sa.Text()),
    sa.column("last_error_at", sa.DateTime(timezone=True)),
    sa.column("consecutive_auth_failures", sa.Integer()),
    sa.column("created_at", sa.DateTime(timezone=True)),
    sa.column("updated_at", sa.DateTime(timezone=True)),
)
_remote = sa.table(
    "automation_git_sync_remote",
    sa.column("id", sa.Uuid()),
    sa.column("org_id", sa.Uuid()),
    sa.column("label", sa.String(255)),
    sa.column("is_default", sa.Boolean()),
    sa.column("overrides", sa.Text()),
    sa.column("configured_by_user_id", sa.Uuid()),
    sa.column("last_synced_commit", sa.String(64)),
    sa.column("last_synced_path", sa.String(255)),
    sa.column("last_run_at", sa.DateTime(timezone=True)),
    sa.column("last_error", sa.Text()),
    sa.column("last_error_at", sa.DateTime(timezone=True)),
    sa.column("sync_started_at", sa.DateTime(timezone=True)),
    sa.column("consecutive_auth_failures", sa.Integer()),
    sa.column("created_at", sa.DateTime(timezone=True)),
    sa.column("updated_at", sa.DateTime(timezone=True)),
)
_state = sa.table(
    "automation_git_sync_state",
    sa.column("org_id", sa.Uuid()),
    sa.column("remote_id", sa.Uuid()),
)

_EMPTY_OVERRIDES = ("", "{}")


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def _default_remote_id(org_id: uuid.UUID) -> uuid.UUID:
    """A deterministic id for the remote an org's single config becomes, so a
    re-run of this migration (a restored database) maps to the same id."""
    return uuid.uuid5(uuid.NAMESPACE_DNS, f"openhands-git-sync-remote:{org_id}")


def upgrade() -> None:
    op.create_table(
        "automation_git_sync_remote",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("org_id", sa.Uuid, nullable=False),
        sa.Column("label", sa.String(255), nullable=False, server_default=""),
        sa.Column("is_default", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("overrides", sa.Text, nullable=False, server_default="{}"),
        sa.Column("configured_by_user_id", sa.Uuid, nullable=True),
        sa.Column("last_synced_commit", sa.String(64), nullable=True),
        sa.Column("last_synced_path", sa.String(255), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sync_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "consecutive_auth_failures",
            sa.Integer,
            nullable=False,
            server_default="0",
        ),
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
    op.create_index(
        "ix_automation_git_sync_remote_org_id",
        "automation_git_sync_remote",
        ["org_id"],
    )
    # At most one default per org, enforced by the database rather than by
    # code that could race.
    op.create_index(
        "ix_automation_git_sync_remote_org_default",
        "automation_git_sync_remote",
        ["org_id"],
        unique=True,
        postgresql_where=sa.text("is_default"),
        sqlite_where=sa.text("is_default"),
    )

    op.create_table(
        "automation_git_sync_removal",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column(
            "remote_id",
            sa.Uuid,
            sa.ForeignKey("automation_git_sync_remote.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("slug", sa.String(255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_automation_git_sync_removal_remote_id",
        "automation_git_sync_removal",
        ["remote_id"],
    )

    # Before the batch step, so SQLite's table recreate doesn't carry over an
    # index that is about to be replaced. Slugs are unique per remote now.
    op.drop_index(
        "ix_automation_git_sync_state_org_slug", table_name="automation_git_sync_state"
    )
    # Batch mode is what lets SQLite add a foreign key (it recreates the
    # table); on PostgreSQL it is a plain ALTER TABLE.
    with op.batch_alter_table("automation_git_sync_state") as batch:
        batch.add_column(sa.Column("remote_id", sa.Uuid, nullable=True))
        batch.create_foreign_key(
            "fk_automation_git_sync_state_remote_id",
            "automation_git_sync_remote",
            ["remote_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.create_index(
        "ix_automation_git_sync_state_remote_id",
        "automation_git_sync_state",
        ["remote_id"],
    )
    op.create_index(
        "ix_automation_git_sync_state_remote_slug",
        "automation_git_sync_state",
        ["remote_id", "slug"],
        unique=True,
    )

    _move_org_configs_to_default_remotes()

    op.drop_table("automation_git_sync_org_config")

    if _is_sqlite():
        return

    op.execute(
        "COMMENT ON TABLE automation_git_sync_remote IS "
        "'One git repository an organization syncs automations to and from: "
        "runtime config overrides (JSON, secrets wrapped at rest), last synced "
        "commit/run/error, the sync lease, and whether it is the org''s "
        "default (where unassigned automations go).'"
    )
    op.execute(
        "COMMENT ON TABLE automation_git_sync_removal IS "
        "'Directories a remote''s next export must delete from its repository, "
        "left behind by automations moved to another remote.'"
    )
    op.execute(
        "COMMENT ON COLUMN automation_git_sync_state.remote_id IS "
        "'The remote this automation syncs to; NULL means unassigned, adopted "
        "by the org''s default remote on its next cycle.'"
    )


def _move_org_configs_to_default_remotes() -> None:
    """Each org's single config becomes its default remote, owning every one
    of the org's existing state rows."""
    bind = op.get_bind()
    rows = bind.execute(sa.select(_org_config)).mappings().all()
    for row in rows:
        has_config = (row["overrides"] or "").strip() not in _EMPTY_OVERRIDES
        has_history = any(
            row[column] is not None
            for column in ("last_synced_commit", "last_run_at", "last_error")
        )
        if not has_config and not has_history:
            continue
        remote_id = _default_remote_id(row["org_id"])
        bind.execute(
            _remote.insert().values(
                id=remote_id,
                org_id=row["org_id"],
                label="",
                is_default=True,
                overrides=row["overrides"] or "{}",
                configured_by_user_id=row["configured_by_user_id"],
                last_synced_commit=row["last_synced_commit"],
                last_synced_path=row["last_synced_path"],
                last_run_at=row["last_run_at"],
                last_error=row["last_error"],
                last_error_at=row["last_error_at"],
                # A lease held by a process that no longer exists.
                sync_started_at=None,
                consecutive_auth_failures=row["consecutive_auth_failures"] or 0,
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
        )
        bind.execute(
            _state.update()
            .where(_state.c.org_id == row["org_id"])
            .values(remote_id=remote_id)
        )


def downgrade() -> None:
    """Best-effort reverse.

    Only each org's default remote goes back to its single config row; other
    remotes are dropped with the table. Re-creating the per-org slug index
    fails if two of an org's remotes share a slug.
    """
    op.create_table(
        "automation_git_sync_org_config",
        sa.Column("org_id", sa.Uuid, primary_key=True),
        sa.Column("overrides", sa.Text, nullable=False, server_default="{}"),
        sa.Column("configured_by_user_id", sa.Uuid, nullable=True),
        sa.Column("last_synced_commit", sa.String(64), nullable=True),
        sa.Column("last_synced_path", sa.String(255), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sync_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "consecutive_auth_failures",
            sa.Integer,
            nullable=False,
            server_default="0",
        ),
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
    bind = op.get_bind()
    defaults = (
        bind.execute(sa.select(_remote).where(_remote.c.is_default.is_(True)))
        .mappings()
        .all()
    )
    for row in defaults:
        bind.execute(
            _org_config.insert().values(
                org_id=row["org_id"],
                overrides=row["overrides"] or "{}",
                configured_by_user_id=row["configured_by_user_id"],
                last_synced_commit=row["last_synced_commit"],
                last_synced_path=row["last_synced_path"],
                last_run_at=row["last_run_at"],
                last_error=row["last_error"],
                last_error_at=row["last_error_at"],
                consecutive_auth_failures=row["consecutive_auth_failures"] or 0,
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
        )

    op.drop_index(
        "ix_automation_git_sync_state_remote_slug",
        table_name="automation_git_sync_state",
    )
    op.drop_index(
        "ix_automation_git_sync_state_remote_id", table_name="automation_git_sync_state"
    )
    with op.batch_alter_table("automation_git_sync_state") as batch:
        batch.drop_constraint(
            "fk_automation_git_sync_state_remote_id", type_="foreignkey"
        )
        batch.drop_column("remote_id")
    op.create_index(
        "ix_automation_git_sync_state_org_slug",
        "automation_git_sync_state",
        ["org_id", "slug"],
        unique=True,
    )

    op.drop_index(
        "ix_automation_git_sync_removal_remote_id",
        table_name="automation_git_sync_removal",
    )
    op.drop_table("automation_git_sync_removal")
    op.drop_index(
        "ix_automation_git_sync_remote_org_default",
        table_name="automation_git_sync_remote",
    )
    op.drop_index(
        "ix_automation_git_sync_remote_org_id", table_name="automation_git_sync_remote"
    )
    op.drop_table("automation_git_sync_remote")
