"""Tests for improvement programs: creation, rotation and cycle reporting.

An improvement program rotates through repositories, researching and
implementing one change per cycle and having a second agent runtime review it.
The behavior worth protecting here is the rotation: that it advances, that it
wraps, that it survives the repository list changing underneath it, and that
what the dashboard promises is what the next run actually takes.
"""

import uuid
from dataclasses import replace

import pytest
from sqlalchemy import select

from openhands.automation.app import app
from openhands.automation.auth import authenticate_request
from openhands.automation.config import clear_config_cache
from openhands.automation.models import (
    Automation,
    AutomationImproveCycle,
    AutomationRun,
    AutomationRunStatus,
    ImproveCycleStatus,
    ImproveVerdict,
)


# The org `async_client` authenticates as (see conftest.py).
ORG_ID = uuid.UUID("87654321-4321-8765-4321-876543218765")
USER_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")
OTHER_ORG_ID = uuid.UUID("11111111-2222-4333-8444-555555555555")

PRESET = "/api/automation/v1/preset/improve"
REMOTES = "/api/automation/v1/git-sync/remotes"
BUILDER = "0a1b2c3d-0000-4000-8000-00000000b111"
REVIEWER = "0a1b2c3d-0000-4000-8000-00000000c222"
CRON = {"type": "cron", "schedule": "0 3 * * *", "timezone": "UTC"}


@pytest.fixture(autouse=True)
def local_mode_workspace(tmp_path, monkeypatch):
    """Local agent-server mode with a writable workspace.

    Local mode is what a self-hosted stack runs, and it is also the mode in
    which agent profiles are resolved by the agent server at run time rather
    than looked up over the network at creation time.
    """
    monkeypatch.setenv("AUTOMATION_AGENT_SERVER_URL", "http://localhost:3000")
    monkeypatch.setenv("AUTOMATION_WORKSPACE_BASE", str(tmp_path))
    monkeypatch.setenv("AUTOMATION_GIT_SYNC_SECRET", "test-wrapping-secret")
    clear_config_cache()
    yield
    clear_config_cache()


@pytest.fixture
def as_other_org(mock_authenticated_user):
    """Authenticate as a different organization."""

    def _switch():
        other = replace(
            mock_authenticated_user, org_id=OTHER_ORG_ID, user_id=uuid.uuid4()
        )

        async def override_authenticate():
            return other

        app.dependency_overrides[authenticate_request] = override_authenticate
        return other

    return _switch


def improve_config(**overrides) -> dict:
    config = {"builder_profile_id": BUILDER}
    config.update(overrides)
    return config


async def create_program(async_client, **config_overrides) -> dict:
    response = await async_client.post(
        PRESET,
        json={
            "name": "Nightly improvement",
            "trigger": CRON,
            "improve": improve_config(**config_overrides),
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def add_remote(async_client, repo_url: str, label: str) -> dict:
    response = await async_client.post(
        REMOTES, json={"repo_url": repo_url, "label": label}
    )
    assert response.status_code == 201, response.text
    return response.json()


async def claim(async_client, automation_id: str, run_id: str | None = None):
    return await async_client.post(
        f"/api/automation/v1/improve/{automation_id}/claim",
        json={"run_id": run_id} if run_id else {},
    )


async def claim_label(async_client, automation_id: str) -> str:
    """Take the next repository and return its label."""
    response = await claim(async_client, automation_id)
    assert response.status_code == 201, response.text
    return response.json()["cycle"]["repository"]["label"]


class TestCreateProgram:
    """POST /v1/preset/improve."""

    async def test_creates_a_cron_program_owned_by_its_builder_profile(
        self, async_client, async_session
    ):
        # Arrange / Act
        created = await create_program(async_client, reviewer_profile_ids=[REVIEWER])

        # Assert
        automation = (
            (
                await async_session.execute(
                    select(Automation).where(Automation.id == uuid.UUID(created["id"]))
                )
            )
            .scalars()
            .first()
        )
        assert automation is not None
        assert automation.preset_metadata["preset_type"] == "improve"
        assert automation.preset_metadata["improve"]["reviewer_profile_ids"] == [
            REVIEWER
        ]
        # The builder is the automation's profile, so existing per-automation
        # profile display and validation apply to a program too.
        assert str(automation.agent_profile_id) == BUILDER
        # A program has no single prompt: each cycle renders its own.
        assert automation.prompt is None
        assert automation.tarball_path
        assert automation.setup_script_path == "setup.sh"

    async def test_rejects_a_builder_that_reviews_its_own_work(self, async_client):
        response = await async_client.post(
            PRESET,
            json={
                "name": "Self review",
                "trigger": CRON,
                "improve": improve_config(reviewer_profile_ids=[BUILDER]),
            },
        )
        assert response.status_code == 422
        assert "not independent review" in response.text

    async def test_rejects_an_explicit_rotation_with_no_repositories(
        self, async_client
    ):
        response = await async_client.post(
            PRESET,
            json={
                "name": "Empty rotation",
                "trigger": CRON,
                "improve": improve_config(repository_source="explicit"),
            },
        )
        assert response.status_code == 422

    async def test_rejects_a_branch_prefix_git_would_refuse(self, async_client):
        response = await async_client.post(
            PRESET,
            json={
                "name": "Bad prefix",
                "trigger": CRON,
                "improve": improve_config(branch_prefix="improve branch~"),
            },
        )
        assert response.status_code == 422


class TestRotation:
    """Which repository a cycle takes, and what the dashboard promises."""

    async def test_rotates_through_git_sync_repositories_in_order_and_wraps(
        self, async_client
    ):
        # Arrange: three synced repositories, created in this order.
        for label in ("alpha", "beta", "gamma"):
            await add_remote(async_client, f"https://github.com/acme/{label}", label)
        program = await create_program(async_client)

        # Act: four cycles over three repositories.
        taken = [await claim_label(async_client, program["id"]) for _ in range(4)]

        # Assert: each run moves on, and the fourth comes back round.
        assert taken == ["alpha", "beta", "gamma", "alpha"]

    async def test_rotation_endpoint_predicts_what_the_next_claim_takes(
        self, async_client
    ):
        # Arrange
        for label in ("alpha", "beta"):
            await add_remote(async_client, f"https://github.com/acme/{label}", label)
        program = await create_program(async_client)
        await claim_label(async_client, program["id"])

        # Act
        rotation = await async_client.get(
            f"/api/automation/v1/improve/{program['id']}/rotation"
        )
        assert rotation.status_code == 200, rotation.text
        predicted = rotation.json()
        promised = predicted["repositories"][predicted["next_index"]]["label"]

        # Assert: the promise and the claim come from the same function.
        assert promised == await claim_label(async_client, program["id"])

    async def test_continues_from_the_repository_it_took_when_one_is_removed(
        self, async_client
    ):
        # Arrange: three repositories; a cycle takes the second.
        remotes = {}
        for label in ("alpha", "beta", "gamma"):
            remotes[label] = await add_remote(
                async_client, f"https://github.com/acme/{label}", label
            )
        program = await create_program(async_client)
        assert await claim_label(async_client, program["id"]) == "alpha"
        assert await claim_label(async_client, program["id"]) == "beta"

        # Act: the repository it just took is removed from the rotation.
        removed = await async_client.delete(f"{REMOTES}/{remotes['beta']['id']}")
        assert removed.status_code in (200, 204), removed.text

        # Assert: it carries on rather than restarting at the top.
        assert await claim_label(async_client, program["id"]) == "gamma"

    async def test_rotates_an_explicit_list_instead_of_git_sync(self, async_client):
        # Arrange: a synced repository the program must ignore.
        await add_remote(async_client, "https://github.com/acme/synced", "synced")
        program = await create_program(
            async_client,
            repository_source="explicit",
            repositories=[
                {"label": "one", "url": "https://github.com/acme/one"},
                {"label": "two", "url": "https://github.com/acme/two", "ref": "dev"},
            ],
        )

        # Act / Assert
        assert await claim_label(async_client, program["id"]) == "one"
        second = await claim(async_client, program["id"])
        repository = second.json()["cycle"]["repository"]
        assert (repository["label"], repository["ref"]) == ("two", "dev")

    async def test_rotates_only_the_labels_the_program_names(self, async_client):
        for label in ("alpha", "beta", "gamma"):
            await add_remote(async_client, f"https://github.com/acme/{label}", label)
        program = await create_program(
            async_client, repository_labels=["alpha", "gamma"]
        )

        taken = [await claim_label(async_client, program["id"]) for _ in range(3)]
        assert taken == ["alpha", "gamma", "alpha"]

    async def test_claiming_with_nothing_synced_is_a_conflict_not_a_crash(
        self, async_client
    ):
        program = await create_program(async_client)
        response = await claim(async_client, program["id"])
        assert response.status_code == 409
        assert "rotate over" in response.text

    async def test_claiming_a_plain_automation_is_a_conflict(
        self, async_client, async_session
    ):
        automation = Automation(
            user_id=USER_ID,
            org_id=ORG_ID,
            name="Not a program",
            prompt="do a thing",
            trigger=CRON,
            tarball_path="internal://not-a-program",
            entrypoint="python main.py",
        )
        async_session.add(automation)
        await async_session.flush()

        response = await claim(async_client, str(automation.id))
        assert response.status_code == 409
        assert "not an improvement program" in response.text.lower()

    async def test_does_not_carry_credentials_out_of_the_git_sync_config(
        self, async_client
    ):
        # Arrange: a repository URL with a credential embedded in it.
        await add_remote(
            async_client,
            "https://someone:s3cret@github.com/acme/alpha",
            "alpha",
        )
        program = await create_program(async_client)

        # Act
        response = await claim(async_client, program["id"])

        # Assert: the cycle records a clonable URL, not the secret.
        url = response.json()["cycle"]["repository"]["url"]
        assert url == "https://github.com/acme/alpha"
        assert "s3cret" not in response.text

    async def test_another_org_cannot_claim_a_cycle(self, async_client, as_other_org):
        await add_remote(async_client, "https://github.com/acme/alpha", "alpha")
        program = await create_program(async_client)

        as_other_org()
        response = await claim(async_client, program["id"])
        assert response.status_code == 404

    async def test_links_the_cycle_to_the_run_that_claimed_it(
        self, async_client, async_session
    ):
        await add_remote(async_client, "https://github.com/acme/alpha", "alpha")
        program = await create_program(async_client)
        run = AutomationRun(
            automation_id=uuid.UUID(program["id"]),
            status=AutomationRunStatus.RUNNING,
        )
        async_session.add(run)
        await async_session.flush()

        response = await claim(async_client, program["id"], run_id=str(run.id))
        assert response.status_code == 201, response.text
        assert response.json()["cycle"]["run_id"] == str(run.id)

    async def test_rejects_a_run_from_another_automation(
        self, async_client, async_session
    ):
        await add_remote(async_client, "https://github.com/acme/alpha", "alpha")
        program = await create_program(async_client)
        other = Automation(
            user_id=USER_ID,
            org_id=ORG_ID,
            name="Other",
            prompt="x",
            trigger=CRON,
            tarball_path="internal://other",
            entrypoint="python main.py",
        )
        async_session.add(other)
        await async_session.flush()
        stray = AutomationRun(
            automation_id=other.id, status=AutomationRunStatus.RUNNING
        )
        async_session.add(stray)
        await async_session.flush()

        response = await claim(async_client, program["id"], run_id=str(stray.id))
        assert response.status_code == 400

    async def test_a_failed_cycle_still_advances_the_rotation(
        self, async_client, async_session
    ):
        # Arrange: a cycle that took alpha and then failed.
        for label in ("alpha", "beta"):
            await add_remote(async_client, f"https://github.com/acme/{label}", label)
        program = await create_program(async_client)
        first = await claim(async_client, program["id"])
        cycle_id = first.json()["cycle"]["id"]
        failed = await async_client.patch(
            f"/api/automation/v1/improve/cycles/{cycle_id}",
            json={"status": "FAILED", "error": "clone failed"},
        )
        assert failed.status_code == 200, failed.text

        # Act / Assert: the next run moves on instead of retrying alpha
        # forever; alpha comes round again on the next lap.
        assert await claim_label(async_client, program["id"]) == "beta"


class TestRotatingTheBuilder:
    """Every runtime takes a turn building, so every runtime takes a turn reviewing."""

    async def test_each_builder_takes_a_turn_and_wraps(self, async_client):
        # Arrange: one repository, two builders. The repository is constant so
        # only the builder can vary.
        await add_remote(async_client, "https://github.com/acme/alpha", "alpha")
        program = await create_program(async_client, rotate_builder=[REVIEWER])

        # Act
        builders = []
        for _ in range(3):
            response = await claim(async_client, program["id"])
            assert response.status_code == 201, response.text
            builders.append(response.json()["cycle"]["builder_profile_id"])

        # Assert: with a fixed builder, that runtime would never review
        # anything. Taking turns is what puts each one on the other side.
        assert builders == [BUILDER, REVIEWER, BUILDER]

    async def test_the_rotation_can_name_itself_as_the_review_pool(self, async_client):
        # The only way to say "these runtimes, and no others": an empty
        # reviewer list draws from every profile on the server, so a local
        # model added for something else starts reviewing.
        response = await async_client.post(
            PRESET,
            json={
                "name": "Two runtimes, reviewing each other",
                "trigger": CRON,
                "improve": improve_config(
                    rotate_builder=[REVIEWER],
                    reviewer_profile_ids=[BUILDER, REVIEWER],
                ),
            },
        )

        # Each turn's builder is skipped from its own review, leaving the
        # other one — which is the whole point of the rotation.
        assert response.status_code == 201, response.text

    async def test_rejects_a_rotation_turn_that_nobody_would_review(self, async_client):
        # Reviewers = [REVIEWER] with REVIEWER in the rotation: on its own
        # turn it is skipped, and there is nobody left.
        response = await async_client.post(
            PRESET,
            json={
                "name": "A turn nobody reviews",
                "trigger": CRON,
                "improve": improve_config(
                    rotate_builder=[REVIEWER],
                    reviewer_profile_ids=[REVIEWER],
                ),
            },
        )
        assert response.status_code == 422
        assert "unreviewed" in response.text

    async def test_rejects_a_builder_listed_twice(self, async_client):
        response = await async_client.post(
            PRESET,
            json={
                "name": "Duplicate builder",
                "trigger": CRON,
                "improve": improve_config(rotate_builder=[BUILDER]),
            },
        )
        assert response.status_code == 422


class TestEditingAProgram:
    """PUT /v1/improve/{automation_id}/config."""

    async def test_changes_what_the_next_cycle_does(self, async_client):
        # Arrange: a program over three repositories, one cycle taken.
        for label in ("alpha", "beta", "gamma"):
            await add_remote(async_client, f"https://github.com/acme/{label}", label)
        program = await create_program(async_client)
        assert await claim_label(async_client, program["id"]) == "alpha"

        # Act: narrow the rotation and add a reviewer.
        response = await async_client.put(
            f"/api/automation/v1/improve/{program['id']}/config",
            json=improve_config(
                reviewer_profile_ids=[REVIEWER],
                repository_labels=["alpha", "gamma"],
                focus="Prefer dependency hygiene.",
            ),
        )

        # Assert: the response shows the new rotation, and the next cycle
        # follows it. alpha was taken last, so gamma is up next.
        assert response.status_code == 200, response.text
        body = response.json()
        assert [repo["label"] for repo in body["repositories"]] == ["alpha", "gamma"]
        assert body["config"]["focus"] == "Prefer dependency hygiene."
        assert body["repositories"][body["next_index"]]["label"] == "gamma"
        assert await claim_label(async_client, program["id"]) == "gamma"

    async def test_keeps_the_automations_profile_in_step_with_the_builder(
        self, async_client, async_session
    ):
        await add_remote(async_client, "https://github.com/acme/alpha", "alpha")
        program = await create_program(async_client)

        response = await async_client.put(
            f"/api/automation/v1/improve/{program['id']}/config",
            json={"builder_profile_id": REVIEWER},
        )
        assert response.status_code == 200, response.text

        await async_session.commit()
        automation = (
            (
                await async_session.execute(
                    select(Automation).where(Automation.id == uuid.UUID(program["id"]))
                )
            )
            .scalars()
            .first()
        )
        assert automation is not None
        assert str(automation.agent_profile_id) == REVIEWER

    async def test_rejects_a_configuration_it_would_not_have_accepted_at_creation(
        self, async_client
    ):
        await add_remote(async_client, "https://github.com/acme/alpha", "alpha")
        program = await create_program(async_client)

        response = await async_client.put(
            f"/api/automation/v1/improve/{program['id']}/config",
            json=improve_config(reviewer_profile_ids=[BUILDER]),
        )
        assert response.status_code == 422

    async def test_another_org_cannot_edit_a_program(self, async_client, as_other_org):
        await add_remote(async_client, "https://github.com/acme/alpha", "alpha")
        program = await create_program(async_client)

        as_other_org()
        response = await async_client.put(
            f"/api/automation/v1/improve/{program['id']}/config",
            json=improve_config(),
        )
        assert response.status_code == 404


class TestRepositoryUrls:
    """A credential never belongs in a stored repository URL."""

    async def test_drops_a_credential_from_an_explicit_repository_url(
        self, async_client, async_session
    ):
        # Arrange / Act: a repository URL submitted with a token in it.
        created = await create_program(
            async_client,
            repository_source="explicit",
            repositories=[
                {
                    "label": "one",
                    "url": "https://someone:s3cret@github.com/acme/one",
                }
            ],
        )

        # Assert: it is neither stored nor echoed. Storing it would export the
        # token to the Git Sync repository and show it in the dashboard.
        automation = (
            (
                await async_session.execute(
                    select(Automation).where(Automation.id == uuid.UUID(created["id"]))
                )
            )
            .scalars()
            .first()
        )
        assert automation is not None
        stored = automation.preset_metadata["improve"]["repositories"][0]["url"]
        assert stored == "https://github.com/acme/one"

        claimed = await claim(async_client, created["id"])
        assert claimed.status_code == 201, claimed.text
        assert "s3cret" not in claimed.text


class TestRefreshingProgramCode:
    """POST /v1/improve/{automation_id}/refresh-code."""

    async def test_repackages_without_touching_configuration_or_history(
        self, async_client, async_session
    ):
        # Arrange: a program with a cycle behind it, so the rotation cursor
        # has somewhere to be lost from.
        for label in ("alpha", "beta"):
            await add_remote(async_client, f"https://github.com/acme/{label}", label)
        program = await create_program(async_client, focus="Keep it small.")
        assert await claim_label(async_client, program["id"]) == "alpha"
        before = (
            (
                await async_session.execute(
                    select(Automation).where(Automation.id == uuid.UUID(program["id"]))
                )
            )
            .scalars()
            .first()
        )
        assert before is not None
        original_tarball = before.tarball_path

        # Act
        response = await async_client.post(
            f"/api/automation/v1/improve/{program['id']}/refresh-code"
        )

        # Assert: new code, same program. Recreating it instead would rewind
        # the rotation to the first repository, because the history is the
        # cursor.
        assert response.status_code == 200, response.text
        assert response.json()["tarball_path"] != original_tarball
        assert response.json()["preset_metadata"]["improve"]["focus"] == (
            "Keep it small."
        )
        assert await claim_label(async_client, program["id"]) == "beta"

    async def test_another_org_cannot_refresh(self, async_client, as_other_org):
        await add_remote(async_client, "https://github.com/acme/alpha", "alpha")
        program = await create_program(async_client)

        as_other_org()
        response = await async_client.post(
            f"/api/automation/v1/improve/{program['id']}/refresh-code"
        )
        assert response.status_code == 404


class TestRepositoryUrlSchemes:
    """Only URLs a cycle can actually clone are accepted."""

    @pytest.mark.parametrize("url", ["git://host/repo.git", "ssh://host/repo.git"])
    async def test_rejects_a_scheme_a_run_cannot_clone(self, async_client, url):
        # Accepting it would move the failure from here, where the user is
        # looking, to the middle of a cycle.
        response = await async_client.post(
            PRESET,
            json={
                "name": "Unclonable",
                "trigger": CRON,
                "improve": improve_config(
                    repository_source="explicit",
                    repositories=[{"label": "one", "url": url}],
                ),
            },
        )
        assert response.status_code == 422

    async def test_rejects_plain_http_rather_than_upgrading_it(self, async_client):
        response = await async_client.post(
            PRESET,
            json={
                "name": "Insecure",
                "trigger": CRON,
                "improve": improve_config(
                    repository_source="explicit",
                    repositories=[{"label": "one", "url": "http://host/repo.git"}],
                ),
            },
        )
        assert response.status_code == 422


class TestCycleReporting:
    """PATCH /v1/improve/cycles/{id} and the history the dashboard reads."""

    async def _claimed_cycle(self, async_client) -> tuple[dict, str]:
        await add_remote(async_client, "https://github.com/acme/alpha", "alpha")
        program = await create_program(async_client)
        response = await claim(async_client, program["id"])
        return program, response.json()["cycle"]["id"]

    async def test_the_runtime_that_ran_replaces_the_one_that_was_claimed(
        self, async_client, async_session
    ):
        # Arrange: claimed on one runtime, built by another after a failover.
        program, cycle_id = await self._claimed_cycle(async_client)

        # Act
        response = await async_client.patch(
            f"/api/automation/v1/improve/cycles/{cycle_id}",
            json={
                "status": "COMPLETED",
                "verdict": "ACCEPTED",
                "builder_runtime": "codex",
                "runtime_switches": [
                    {
                        "from": "claude-code",
                        "to": "codex",
                        "reason": "usage_limit",
                        "at": "2026-10-24T08:00:00+00:00",
                        "detail": "you've hit your usage limit",
                    }
                ],
            },
        )

        # Assert: the route is kept, because the conversation that holds the
        # switch events is deleted with its sandbox -- and the runtime of
        # record is the one that actually did the work.
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["builder_runtime"] == "codex"
        assert [
            (hop["from"], hop["to"], hop["reason"]) for hop in body["runtime_switches"]
        ] == [("claude-code", "codex", "usage_limit")]

    async def test_records_the_builder_stage_then_the_reviews(
        self, async_client, async_session
    ):
        # Arrange
        program, cycle_id = await self._claimed_cycle(async_client)

        # Act: the builder reports, then the reviewer does.
        build = await async_client.patch(
            f"/api/automation/v1/improve/cycles/{cycle_id}",
            json={
                "status": "BUILDING",
                "builder_runtime": "claude-code",
                "summary": "Pinned the parser to the patched release.",
                "branch": "improve/20261021-030000",
                "pr_url": "https://github.com/acme/alpha/pull/7",
                "sources": [
                    {
                        "url": "https://example.com/changelog",
                        "title": "Parser 3.2 changelog",
                        "note": "documents the fix this change relies on",
                    }
                ],
            },
        )
        assert build.status_code == 200, build.text

        reviewed = await async_client.patch(
            f"/api/automation/v1/improve/cycles/{cycle_id}",
            json={
                "status": "COMPLETED",
                "verdict": "CHANGES_REQUESTED",
                "reviews": [
                    {
                        "runtime": "codex",
                        "verdict": "CHANGES_REQUESTED",
                        "updates_checked": (
                            "Parser 3.2 is the latest release; nothing supersedes it."
                        ),
                        "summary": "The cited changelog covers a later version.",
                        "concerns": ["the pin does not match the cited release"],
                        "stale_sources": ["https://example.com/changelog"],
                    }
                ],
            },
        )

        # Assert
        assert reviewed.status_code == 200, reviewed.text
        body = reviewed.json()
        assert body["verdict"] == "CHANGES_REQUESTED"
        assert body["builder_runtime"] == "claude-code"
        assert body["sources"][0]["url"] == "https://example.com/changelog"
        assert body["reviews"][0]["runtime"] == "codex"
        # Recorded every cycle, including when the answer is "nothing newer":
        # saying nothing and having checked are different statements.
        assert "latest release" in body["reviews"][0]["updates_checked"]
        assert body["completed_at"] is not None

        stored = (
            (
                await async_session.execute(
                    select(AutomationImproveCycle).where(
                        AutomationImproveCycle.id == uuid.UUID(cycle_id)
                    )
                )
            )
            .scalars()
            .first()
        )
        assert stored is not None
        assert stored.status is ImproveCycleStatus.COMPLETED
        assert stored.verdict is ImproveVerdict.CHANGES_REQUESTED

    async def test_records_proposed_additions_to_the_users_soul(self, async_client):
        # Arrange
        program, cycle_id = await self._claimed_cycle(async_client)

        # Act: a cycle proposes a standing preference it had to guess at.
        response = await async_client.patch(
            f"/api/automation/v1/improve/cycles/{cycle_id}",
            json={
                "status": "COMPLETED",
                "verdict": "ACCEPTED",
                "soul_suggestions": [
                    {
                        "text": "Run the project's own lint before pushing.",
                        "why": "the repository enforces it and SOUL.md is silent",
                    }
                ],
            },
        )

        # Assert: it is recorded as a proposal for the user to decide on, and
        # the cycle does not claim to have changed SOUL.md itself.
        assert response.status_code == 200, response.text
        suggestions = response.json()["soul_suggestions"]
        assert len(suggestions) == 1
        assert suggestions[0]["why"].startswith("the repository enforces it")

    async def test_a_finished_cycle_cannot_be_rewritten(self, async_client):
        program, cycle_id = await self._claimed_cycle(async_client)
        first = await async_client.patch(
            f"/api/automation/v1/improve/cycles/{cycle_id}",
            json={"status": "COMPLETED", "verdict": "ACCEPTED"},
        )
        assert first.status_code == 200, first.text

        late = await async_client.patch(
            f"/api/automation/v1/improve/cycles/{cycle_id}",
            json={"status": "COMPLETED", "verdict": "REJECTED"},
        )
        assert late.status_code == 409

    async def test_rejects_a_pull_request_link_that_is_not_http(self, async_client):
        program, cycle_id = await self._claimed_cycle(async_client)
        response = await async_client.patch(
            f"/api/automation/v1/improve/cycles/{cycle_id}",
            json={"pr_url": "javascript:alert(1)"},
        )
        assert response.status_code == 422

    async def test_lists_history_newest_first(self, async_client):
        for label in ("alpha", "beta"):
            await add_remote(async_client, f"https://github.com/acme/{label}", label)
        program = await create_program(async_client)
        await claim_label(async_client, program["id"])
        await claim_label(async_client, program["id"])

        response = await async_client.get(
            f"/api/automation/v1/improve/{program['id']}/cycles"
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["total"] == 2
        assert [cycle["repository"]["label"] for cycle in body["cycles"]] == [
            "beta",
            "alpha",
        ]
        # Timestamps must carry an offset: a naive one is read by JavaScript's
        # Date as local time, so the dashboard would date every cycle wrongly.
        assert body["cycles"][0]["created_at"].endswith("Z") or body["cycles"][0][
            "created_at"
        ].endswith("+00:00")

    async def test_another_org_cannot_read_history(self, async_client, as_other_org):
        await add_remote(async_client, "https://github.com/acme/alpha", "alpha")
        program = await create_program(async_client)

        as_other_org()
        response = await async_client.get(
            f"/api/automation/v1/improve/{program['id']}/cycles"
        )
        assert response.status_code == 404


class TestTheServiceKeepsTheRestrictiveReading:
    """The floor under a client that is wrong or lying.

    The class is computed from the diff by code shipped with the program, so
    the service does not normally need to second-guess it. But the thing being
    gated must not be able to lower its own gate, so a report whose evidence
    contradicts its class is read the restrictive way.
    """

    async def test_a_report_that_contradicts_itself_is_read_restrictively(
        self, async_client
    ):
        # Arrange
        await add_remote(async_client, "https://github.com/acme/alpha", "alpha")
        program = await create_program(async_client)
        cycle_id = (await claim(async_client, program["id"])).json()["cycle"]["id"]

        # Act: "additive", with a removed export in its own evidence.
        response = await async_client.patch(
            f"/api/automation/v1/improve/cycles/{cycle_id}",
            json={
                "change_class": "additive",
                "claimed_change_class": "additive",
                "change_evidence": {
                    "change_class": "additive",
                    "reasons": [
                        {
                            "rule": "removed_export",
                            "path": "openhands/service.py",
                            "detail": "-def resolve_backend(name: str) -> str:",
                        }
                    ],
                },
            },
        )

        # Assert: the claim is kept as what was claimed, and the class is
        # raised to what the evidence requires.
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["change_class"] == "breaking"
        assert body["claimed_change_class"] == "additive"

    async def test_an_honest_report_is_stored_as_sent(self, async_client):
        # Arrange
        await add_remote(async_client, "https://github.com/acme/beta", "beta")
        program = await create_program(async_client)
        cycle_id = (await claim(async_client, program["id"])).json()["cycle"]["id"]

        # Act
        response = await async_client.patch(
            f"/api/automation/v1/improve/cycles/{cycle_id}",
            json={
                "change_class": "deleting",
                "claimed_change_class": "deleting",
                "change_evidence": {
                    "change_class": "deleting",
                    "reasons": [
                        {
                            "rule": "file_deleted",
                            "path": "openhands/_scratch.py",
                            "detail": "the whole file was removed",
                        }
                    ],
                },
            },
        )

        assert response.status_code == 200, response.text
        assert response.json()["change_class"] == "deleting"


class TestParkingAndResolving:
    """A cycle that waits for a person, and the answer that restarts it."""

    async def _parked(self, async_client, label: str) -> str:
        await add_remote(async_client, f"https://github.com/acme/{label}", label)
        program = await create_program(async_client)
        cycle_id = (await claim(async_client, program["id"])).json()["cycle"]["id"]
        response = await async_client.patch(
            f"/api/automation/v1/improve/cycles/{cycle_id}",
            json={
                "status": "PARKED",
                "change_class": "breaking",
                "branch": "improve/20261024",
                "parked_phase": "publish",
                "parked_question": (
                    "This renames the conversation socket's event envelope. "
                    "Publish now, or wait for the client release?"
                ),
                "parked_plan": {
                    "breaks": ["the 1.55 TypeScript client"],
                    "consumers": ["agent-canvas"],
                    "sequence": ["ship both envelopes", "move the client", "drop"],
                    "rollback": "revert the server commit; both envelopes stay",
                },
                "gate_decisions": [
                    {
                        "phase": "publish",
                        "route": "escalate",
                        "trigger": "the diff is a breaking change",
                        "rationale": (
                            "a wire contract changed, which is never mine to publish"
                        ),
                        "change_class": "breaking",
                    }
                ],
            },
        )
        assert response.status_code == 200, response.text
        return cycle_id

    async def test_a_breaking_change_waits_with_its_plan_and_is_not_a_failure(
        self, async_client
    ):
        # Act
        cycle_id = await self._parked(async_client, "alpha")

        # Assert: visible as waiting, across every program, with the plan the
        # question cannot be answered without.
        listed = await async_client.get("/api/automation/v1/improve/parked")
        assert listed.status_code == 200, listed.text
        parked = listed.json()["cycles"]
        assert [c["id"] for c in parked] == [cycle_id]
        assert parked[0]["status"] == "PARKED"
        assert parked[0]["parked_plan"]["rollback"]
        assert parked[0]["parked_at"]
        assert [d["route"] for d in parked[0]["gate_decisions"]] == ["escalate"]

    async def test_resolving_returns_the_cycle_to_the_phase_it_stopped_at(
        self, async_client
    ):
        # Arrange
        cycle_id = await self._parked(async_client, "beta")

        # Act
        response = await async_client.post(
            f"/api/automation/v1/improve/cycles/{cycle_id}/resolve",
            json={
                "route": "approve",
                "resolution": "The client release is out; publish it.",
            },
        )

        # Assert: back in flight rather than discarded, with the answer kept
        # and both decisions on the record.
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "REVIEWING"
        assert body["resolution"].startswith("The client release")
        assert body["resolved_at"]
        assert [d["route"] for d in body["gate_decisions"]] == ["escalate", "approve"]
        assert body["gate_decisions"][-1]["decided_by"].startswith("user:")

    async def test_archiving_ends_it_deliberately_rather_than_as_a_failure(
        self, async_client
    ):
        # Arrange
        cycle_id = await self._parked(async_client, "gamma")

        # Act
        response = await async_client.post(
            f"/api/automation/v1/improve/cycles/{cycle_id}/resolve",
            json={"route": "archive", "resolution": "Not this quarter."},
        )

        # Assert: a decision, not a malfunction.
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "COMPLETED"
        assert body["verdict"] == "REJECTED"

    async def test_escalating_an_escalation_is_refused(self, async_client):
        cycle_id = await self._parked(async_client, "delta")

        response = await async_client.post(
            f"/api/automation/v1/improve/cycles/{cycle_id}/resolve",
            json={"route": "escalate", "resolution": "Ask someone else."},
        )

        assert response.status_code == 422
        assert "escalation is answered" in response.text

    async def test_resolving_a_cycle_that_is_not_waiting_is_a_conflict(
        self, async_client
    ):
        await add_remote(async_client, "https://github.com/acme/eps", "eps")
        program = await create_program(async_client)
        cycle_id = (await claim(async_client, program["id"])).json()["cycle"]["id"]

        response = await async_client.post(
            f"/api/automation/v1/improve/cycles/{cycle_id}/resolve",
            json={"route": "approve", "resolution": "nothing to answer"},
        )

        assert response.status_code == 409

    async def test_a_decision_the_service_cannot_read_is_recorded_as_unreadable(
        self, async_client
    ):
        # Arrange
        await add_remote(async_client, "https://github.com/acme/zeta", "zeta")
        program = await create_program(async_client)
        cycle_id = (await claim(async_client, program["id"])).json()["cycle"]["id"]

        # Act: a route outside the closed set, and one with no rationale.
        response = await async_client.patch(
            f"/api/automation/v1/improve/cycles/{cycle_id}",
            json={
                "gate_decisions": [
                    {"phase": "review", "route": "merge-it", "rationale": "go on"},
                    {"phase": "review", "route": "approve", "rationale": ""},
                ]
            },
        )

        # Assert: not dropped. A client with a bug must not look like a client
        # that never decided anything.
        assert response.status_code == 200, response.text
        recorded = response.json()["gate_decisions"]
        assert len(recorded) == 2
        assert all(entry["unreadable"] for entry in recorded)
        assert "not a route" in recorded[0]["problem"]
        assert "rationale" in recorded[1]["problem"]
