"""Integration test for the single-repo -> remotes git sync migration (034).

Builds the schema through revision 033 in a temporary SQLite database, seeds
the per-org config rows and state rows the old layout used, and checks that
the upgrade turns each configured org's row into its default remote owning
every one of its state rows, while an empty row (the one local mode created on
every tick) is dropped and its states stay unassigned.
"""

import sqlite3
import uuid

import pytest

from tests.test_kv_migration import _alembic, _assert_revision, _current_migration_head


CONFIGURED_ORG = uuid.uuid4()
EMPTY_ORG = uuid.uuid4()


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "git-sync-migration.db")


def _hex(value: uuid.UUID) -> str:
    # SQLAlchemy's Uuid type stores CHAR(32) hex on SQLite.
    return value.hex


def _seed_pre_remotes_rows(db_path: str) -> dict[str, str]:
    automation_ids = {org: uuid.uuid4() for org in (CONFIGURED_ORG, EMPTY_ORG)}
    with sqlite3.connect(db_path) as connection:
        for org, automation_id in automation_ids.items():
            connection.execute(
                "INSERT INTO automations (id, user_id, org_id, name, trigger, "
                "tarball_path, entrypoint, enabled, state, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 1, 'ACTIVE', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (
                    _hex(automation_id),
                    _hex(uuid.uuid4()),
                    _hex(org),
                    f"auto-{org}",
                    '{"type": "cron", "schedule": "0 9 * * 1"}',
                    "https://example.com/a.tar.gz",
                    "python main.py",
                ),
            )
            connection.execute(
                "INSERT INTO automation_git_sync_state (automation_id, org_id, "
                "slug, dirty, created_at, updated_at) VALUES (?, ?, 'auto', 1, "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (_hex(automation_id), _hex(org)),
            )
        connection.execute(
            "INSERT INTO automation_git_sync_org_config (org_id, overrides, "
            "last_synced_commit, consecutive_auth_failures, created_at, "
            "updated_at) VALUES (?, ?, 'abc1234', 2, CURRENT_TIMESTAMP, "
            "CURRENT_TIMESTAMP)",
            (
                _hex(CONFIGURED_ORG),
                '{"git_sync_repo_url": "https://example.com/repo.git", '
                '"git_sync_interval_seconds": 300}',
            ),
        )
        connection.execute(
            "INSERT INTO automation_git_sync_org_config (org_id, overrides, "
            "consecutive_auth_failures, created_at, updated_at) "
            "VALUES (?, '{}', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (_hex(EMPTY_ORG),),
        )
        connection.commit()
    return {
        str(org): _hex(automation_id) for org, automation_id in automation_ids.items()
    }


def test_configured_org_becomes_a_default_remote_owning_its_states(db_path):
    db_url = f"sqlite:///{db_path}"
    _alembic(db_url, "upgrade", "033")
    automation_ids = _seed_pre_remotes_rows(db_path)

    _alembic(db_url, "upgrade", "head")
    _assert_revision(db_path, _current_migration_head())

    with sqlite3.connect(db_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "automation_git_sync_org_config" not in tables
        assert "automation_git_sync_removal" in tables

        remotes = connection.execute(
            "SELECT id, org_id, is_default, overrides, last_synced_commit, "
            "consecutive_auth_failures, label FROM automation_git_sync_remote"
        ).fetchall()
        assert len(remotes) == 1, "the empty org row must not become a remote"
        remote_id, org_id, is_default, overrides, commit, failures, label = remotes[0]
        assert org_id == _hex(CONFIGURED_ORG)
        assert is_default == 1
        assert '"git_sync_repo_url": "https://example.com/repo.git"' in overrides
        assert commit == "abc1234"
        assert failures == 2
        assert label == ""

        states = dict(
            connection.execute(
                "SELECT automation_id, remote_id FROM automation_git_sync_state"
            ).fetchall()
        )
        assert states[automation_ids[str(CONFIGURED_ORG)]] == remote_id
        assert states[automation_ids[str(EMPTY_ORG)]] is None

        indexes = {
            row[1]
            for row in connection.execute(
                "PRAGMA index_list(automation_git_sync_state)"
            )
        }
        assert "ix_automation_git_sync_state_remote_slug" in indexes
        assert "ix_automation_git_sync_state_org_slug" not in indexes


def test_downgrade_restores_the_default_remote_as_the_org_row(db_path):
    db_url = f"sqlite:///{db_path}"
    _alembic(db_url, "upgrade", "033")
    _seed_pre_remotes_rows(db_path)
    _alembic(db_url, "upgrade", "head")

    _alembic(db_url, "downgrade", "033")
    _assert_revision(db_path, "033")

    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT org_id, overrides, last_synced_commit "
            "FROM automation_git_sync_org_config"
        ).fetchall()
        assert rows == [
            (
                _hex(CONFIGURED_ORG),
                '{"git_sync_repo_url": "https://example.com/repo.git", '
                '"git_sync_interval_seconds": 300}',
                "abc1234",
            )
        ]
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(automation_git_sync_state)"
            )
        }
        assert "remote_id" not in columns
