"""Tests for the multi-repository git sync API: remotes and assignments.

The legacy single-repo endpoints are covered in test_git_sync_router.py; here
they appear only where they meet the default remote.
"""

import uuid
from dataclasses import replace

import pytest
from sqlalchemy import select

from openhands.automation.app import app
from openhands.automation.auth import authenticate_request
from openhands.automation.config import clear_config_cache
from openhands.automation.git_sync import SyncCycleResult
from openhands.automation.models import (
    Automation,
    AutomationGitSyncRemoval,
    AutomationGitSyncState,
)


# The org `async_client` authenticates as (see conftest.py).
ORG_ID = uuid.UUID("87654321-4321-8765-4321-876543218765")
OTHER_ORG_ID = uuid.UUID("11111111-2222-4333-8444-555555555555")

REMOTES = "/api/automation/v1/git-sync/remotes"
ASSIGNMENTS = "/api/automation/v1/git-sync/assignments"


@pytest.fixture(autouse=True)
def writable_workspace(tmp_path, monkeypatch):
    """A temp workspace and a wrapping secret, as in test_git_sync_router.py."""
    monkeypatch.setenv("AUTOMATION_WORKSPACE_BASE", str(tmp_path))
    monkeypatch.setenv("AUTOMATION_GIT_SYNC_SECRET", "test-wrapping-secret")
    clear_config_cache()
    yield
    clear_config_cache()


@pytest.fixture
def as_other_org(mock_authenticated_user):
    def _switch():
        other = replace(
            mock_authenticated_user, org_id=OTHER_ORG_ID, user_id=uuid.uuid4()
        )

        async def override_authenticate():
            return other

        app.dependency_overrides[authenticate_request] = override_authenticate
        return other

    return _switch


async def _create(async_client, repo_url: str, **extra) -> dict:
    response = await async_client.post(REMOTES, json={"repo_url": repo_url, **extra})
    assert response.status_code == 201, response.text
    return response.json()


async def _automation(async_session, name: str = "Nightly") -> Automation:
    automation = Automation(
        user_id=uuid.uuid4(),
        org_id=ORG_ID,
        name=name,
        trigger={"type": "cron", "schedule": "0 9 * * 1"},
        tarball_path="https://example.com/a.tar.gz",
        entrypoint="python main.py",
    )
    async_session.add(automation)
    await async_session.flush()
    return automation


class TestRemotes:
    async def test_first_remote_becomes_the_default(self, async_client):
        created = await _create(async_client, "https://example.com/first.git")

        assert created["is_default"] is True
        assert created["enabled"] is True
        assert created["repo_url"] == "https://example.com/first.git"
        assert created["label"] == ""
        assert created["automation_count"] == 0

        listed = (await async_client.get(REMOTES)).json()
        assert [r["id"] for r in listed["remotes"]] == [created["id"]]
        assert listed["unassigned_count"] == 0

    async def test_later_remotes_are_not_the_default_unless_asked(self, async_client):
        first = await _create(async_client, "https://example.com/first.git")
        second = await _create(async_client, "https://example.com/second.git")
        assert second["is_default"] is False

        third = await _create(
            async_client, "https://example.com/third.git", is_default=True
        )
        assert third["is_default"] is True

        remotes = (await async_client.get(REMOTES)).json()["remotes"]
        # Exactly one default, listed first.
        assert [r["is_default"] for r in remotes] == [True, False, False]
        assert remotes[0]["id"] == third["id"]
        assert {r["id"] for r in remotes} == {first["id"], second["id"], third["id"]}

    async def test_requires_a_repository_url(self, async_client):
        response = await async_client.post(REMOTES, json={"label": "No repo"})
        assert response.status_code == 422

    async def test_refuses_a_repository_another_remote_already_syncs(
        self, async_client
    ):
        await _create(async_client, "https://example.com/shared.git")

        same = await async_client.post(
            REMOTES, json={"repo_url": "https://example.com/shared.git"}
        )
        assert same.status_code == 409
        assert "already syncs this repository" in same.json()["detail"]

        # A different path in the same repository is a different directory
        # tree, so it is allowed.
        other_path = await async_client.post(
            REMOTES,
            json={"repo_url": "https://example.com/shared.git", "path": "ops"},
        )
        assert other_path.status_code == 201

    async def test_update_renames_promotes_and_keeps_a_default(self, async_client):
        first = await _create(async_client, "https://example.com/first.git")
        second = await _create(async_client, "https://example.com/second.git")

        renamed = await async_client.put(
            f"{REMOTES}/{second['id']}", json={"label": "  Project B  "}
        )
        assert renamed.status_code == 200
        assert renamed.json()["label"] == "Project B"

        promoted = await async_client.put(
            f"{REMOTES}/{second['id']}", json={"is_default": True}
        )
        assert promoted.json()["is_default"] is True
        demoted = (await async_client.get(f"{REMOTES}/{first['id']}")).json()
        assert demoted["is_default"] is False

        # An org never ends up without a default by unsetting it.
        unset = await async_client.put(
            f"{REMOTES}/{second['id']}", json={"is_default": False}
        )
        assert unset.status_code == 422

    async def test_update_reconfigures_the_remote(self, async_client):
        remote = await _create(async_client, "https://example.com/first.git")

        updated = await async_client.put(
            f"{REMOTES}/{remote['id']}",
            json={"branch": "develop", "interval_seconds": 120, "enabled": False},
        )
        body = updated.json()
        assert body["branch"] == "develop"
        assert body["interval_seconds"] == 120
        assert body["enabled"] is False

    async def test_remotes_are_scoped_to_the_callers_org(
        self, async_client, as_other_org
    ):
        remote = await _create(async_client, "https://example.com/first.git")
        missing = await async_client.get(f"{REMOTES}/{uuid.uuid4()}")
        assert missing.status_code == 404

        as_other_org()
        assert (await async_client.get(REMOTES)).json()["remotes"] == []
        assert (await async_client.get(f"{REMOTES}/{remote['id']}")).status_code == 404
        assert (
            await async_client.delete(f"{REMOTES}/{remote['id']}")
        ).status_code == 404

    async def test_delete_unassigns_its_automations(self, async_client, async_session):
        await _create(async_client, "https://example.com/first.git")
        second = await _create(async_client, "https://example.com/second.git")
        automation = await _automation(async_session)
        state = AutomationGitSyncState(
            automation_id=automation.id,
            org_id=ORG_ID,
            remote_id=uuid.UUID(second["id"]),
            slug="nightly",
            dirty=False,
        )
        async_session.add(state)
        await async_session.commit()

        deleted = await async_client.delete(f"{REMOTES}/{second['id']}")
        assert deleted.status_code == 204

        await async_session.refresh(state)
        assert state.remote_id is None
        assert state.dirty is True
        listed = (await async_client.get(REMOTES)).json()
        assert [r["id"] for r in listed["remotes"]] != [second["id"]]
        # Counted as the default's, which adopts it on its next cycle.
        assert listed["remotes"][0]["automation_count"] == 1
        assert listed["unassigned_count"] == 1

    async def test_manage_permission_is_required_to_change_remotes(
        self, readonly_client
    ):
        assert (await readonly_client.get(REMOTES)).status_code == 200
        create = await readonly_client.post(
            REMOTES, json={"repo_url": "https://example.com/first.git"}
        )
        assert create.status_code == 403


class TestChecksAndTriggers:
    async def test_check_for_a_new_remote_needs_a_url(self, async_client):
        response = await async_client.post(f"{REMOTES}/check", json={})
        assert response.status_code == 200
        assert response.json() == {
            "ok": False,
            "branch_exists": False,
            "detail": "No repository URL is configured.",
        }

    async def test_trigger_is_per_remote(self, async_client, monkeypatch):
        import openhands.automation.git_sync.router as router_module

        calls: list[dict] = []

        async def fake_run_sync_cycle(session_factory, org_id, settings, service, **kw):
            calls.append({"org_id": org_id, "repo": settings.git_sync_repo_url, **kw})
            return SyncCycleResult(head=None)

        monkeypatch.setattr(router_module, "run_sync_cycle", fake_run_sync_cycle)

        await _create(async_client, "https://example.com/first.git")
        second = await _create(async_client, "https://example.com/second.git")
        paused = await _create(
            async_client, "https://example.com/paused.git", enabled=False
        )

        refused = await async_client.post(f"{REMOTES}/{paused['id']}/sync")
        assert refused.status_code == 503

        accepted = await async_client.post(f"{REMOTES}/{second['id']}/sync")
        assert accepted.status_code == 202
        assert accepted.json() == {"triggered": True}
        for _ in range(50):
            if calls:
                break
            import asyncio

            await asyncio.sleep(0.02)
        assert calls == [
            {
                "org_id": ORG_ID,
                "repo": "https://example.com/second.git",
                "remote_id": uuid.UUID(second["id"]),
            }
        ]


class TestAssignments:
    async def test_unassigned_automations_follow_the_default(
        self, async_client, async_session
    ):
        default = await _create(async_client, "https://example.com/first.git")
        automation = await _automation(async_session)
        await async_session.commit()

        body = (await async_client.get(ASSIGNMENTS)).json()
        assert body["automations"] == [
            {
                "automation_id": str(automation.id),
                "name": "Nightly",
                "remote_id": None,
                "effective_remote_id": default["id"],
                "slug": None,
                "dirty": True,
            }
        ]
        listed = (await async_client.get(REMOTES)).json()
        assert listed["remotes"][0]["automation_count"] == 1
        assert listed["unassigned_count"] == 1

    async def test_moving_an_automation_leaves_a_removal_behind(
        self, async_client, async_session
    ):
        first = await _create(async_client, "https://example.com/first.git")
        second = await _create(async_client, "https://example.com/second.git")
        automation = await _automation(async_session)
        await async_session.commit()

        moved = await async_client.put(
            f"{ASSIGNMENTS}/{automation.id}", json={"remote_id": second["id"]}
        )
        assert moved.status_code == 200
        assert moved.json()["remote_id"] == second["id"]
        assert moved.json()["effective_remote_id"] == second["id"]
        assert moved.json()["slug"] == "nightly"
        # Never exported anywhere yet, so nothing to remove.
        assert (
            await async_session.execute(select(AutomationGitSyncRemoval))
        ).scalars().all() == []

        back = await async_client.put(
            f"{ASSIGNMENTS}/{automation.id}", json={"remote_id": first["id"]}
        )
        assert back.json()["remote_id"] == first["id"]
        removals = (
            (await async_session.execute(select(AutomationGitSyncRemoval)))
            .scalars()
            .all()
        )
        assert [(r.remote_id, r.slug) for r in removals] == [
            (uuid.UUID(second["id"]), "nightly")
        ]

        counts = {
            r["id"]: r["automation_count"]
            for r in (await async_client.get(REMOTES)).json()["remotes"]
        }
        assert counts == {first["id"]: 1, second["id"]: 0}

    async def test_moving_to_a_remote_that_already_uses_the_slug_renames_it(
        self, async_client, async_session
    ):
        await _create(async_client, "https://example.com/first.git")
        second = await _create(async_client, "https://example.com/second.git")
        taken = await _automation(async_session, name="Nightly")
        async_session.add(
            AutomationGitSyncState(
                automation_id=taken.id,
                org_id=ORG_ID,
                remote_id=uuid.UUID(second["id"]),
                slug="nightly",
            )
        )
        newcomer = await _automation(async_session, name="Nightly")
        await async_session.commit()

        moved = await async_client.put(
            f"{ASSIGNMENTS}/{newcomer.id}", json={"remote_id": second["id"]}
        )
        assert moved.status_code == 200
        assert moved.json()["slug"] == f"nightly-{newcomer.id.hex[:8]}"

    async def test_unknown_targets_are_404(self, async_client, async_session):
        remote = await _create(async_client, "https://example.com/first.git")
        automation = await _automation(async_session)
        await async_session.commit()

        no_remote = await async_client.put(
            f"{ASSIGNMENTS}/{automation.id}", json={"remote_id": str(uuid.uuid4())}
        )
        assert no_remote.status_code == 404
        no_automation = await async_client.put(
            f"{ASSIGNMENTS}/{uuid.uuid4()}", json={"remote_id": remote["id"]}
        )
        assert no_automation.status_code == 404

    async def test_assigning_needs_manage_permission(
        self, readonly_client, async_session
    ):
        automation = await _automation(async_session)
        await async_session.commit()

        assert (await readonly_client.get(ASSIGNMENTS)).status_code == 200
        response = await readonly_client.put(
            f"{ASSIGNMENTS}/{automation.id}", json={"remote_id": str(uuid.uuid4())}
        )
        assert response.status_code == 403


class TestLegacyEndpointsMeetTheDefaultRemote:
    async def test_status_without_remotes_reads_unconfigured(self, async_client):
        body = (await async_client.get("/api/automation/v1/git-sync/status")).json()
        assert body["enabled"] is False
        assert body["repo_url"] == ""
        assert (await async_client.get(REMOTES)).json()["remotes"] == []

    async def test_config_creates_and_edits_the_default_remote(self, async_client):
        saved = await async_client.put(
            "/api/automation/v1/git-sync/config",
            json={"repo_url": "https://example.com/legacy.git"},
        )
        assert saved.status_code == 200
        assert saved.json()["enabled"] is True

        remotes = (await async_client.get(REMOTES)).json()["remotes"]
        assert len(remotes) == 1
        assert remotes[0]["is_default"] is True
        assert remotes[0]["repo_url"] == "https://example.com/legacy.git"

        # The two surfaces edit the same row.
        await async_client.put(f"{REMOTES}/{remotes[0]['id']}", json={"branch": "dev"})
        status = (await async_client.get("/api/automation/v1/git-sync/status")).json()
        assert status["branch"] == "dev"

    async def test_status_follows_a_promoted_default(self, async_client):
        await _create(async_client, "https://example.com/first.git")
        second = await _create(
            async_client, "https://example.com/second.git", is_default=True
        )

        status = (await async_client.get("/api/automation/v1/git-sync/status")).json()
        assert status["repo_url"] == second["repo_url"]
