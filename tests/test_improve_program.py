"""Tests for improvement programs: creation, rotation and cycle reporting.

An improvement program rotates through repositories, researching and
implementing one change per cycle and having a second agent runtime review it.
The behavior worth protecting here is the rotation: that it advances, that it
wraps, that it survives the repository list changing underneath it, and that
what the dashboard promises is what the next run actually takes.
"""

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import ClassVar

import pytest
from sqlalchemy import select

from openhands.automation.app import app
from openhands.automation.auth import authenticate_request
from openhands.automation.config import clear_config_cache
from openhands.automation.improve.rotation import next_builder
from openhands.automation.models import (
    Automation,
    AutomationImproveCycle,
    AutomationRun,
    AutomationRunStatus,
    ImproveCycleStatus,
    ImproveVerdict,
)
from openhands.automation.watchdog import reap_abandoned_cycles


# The org `async_client` authenticates as (see conftest.py).
ORG_ID = uuid.UUID("87654321-4321-8765-4321-876543218765")
USER_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")
OTHER_ORG_ID = uuid.UUID("11111111-2222-4333-8444-555555555555")

PRESET = "/api/automation/v1/preset/improve"
REMOTES = "/api/automation/v1/git-sync/remotes"
PARKED = "/api/automation/v1/improve/parked"
BUILDER = "0a1b2c3d-0000-4000-8000-00000000b111"
REVIEWER = "0a1b2c3d-0000-4000-8000-00000000c222"
THIRD_BUILDER = "0a1b2c3d-0000-4000-8000-00000000d333"
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


class TestWhoseTurnItIsToBuild:
    """A rotation alone does not fix a lopsided history.

    The program exists because one runtime built 31 of 33 cycles. Taking
    turns corrects that only in the limit: a runtime added to a lopsided
    history stays behind for as many cycles as the history is long. The plan
    asks for "a filter plus a headroom bias: among available runtimes, prefer
    the one with the fewest cycles today", and neither was implemented.
    """

    ONE = uuid.UUID("0a1b2c3d-0000-4000-8000-00000000b111")
    TWO = uuid.UUID("0a1b2c3d-0000-4000-8000-00000000c222")
    THREE = uuid.UUID("0a1b2c3d-0000-4000-8000-00000000d333")

    def _after(self, profile_id):
        cycle = AutomationImproveCycle(
            id=uuid.uuid4(),
            automation_id=uuid.uuid4(),
            org_id=ORG_ID,
            repo_index=0,
            repo_label="alpha",
            repo_url="https://github.com/acme/alpha",
            status=ImproveCycleStatus.COMPLETED,
        )
        cycle.builder_profile_id = profile_id
        return cycle

    def test_the_one_that_is_behind_gets_the_turn(self):
        # The whole point. Taking turns would hand this to TWO and leave
        # THREE behind for another three cycles; the bias gives it to the
        # runtime that is actually short.
        chosen = next_builder(
            [self.ONE, self.TWO, self.THREE],
            self._after(self.ONE),
            built_today={self.ONE: 9, self.TWO: 8, self.THREE: 0},
        )

        assert chosen == self.THREE

    def test_a_level_field_still_simply_takes_turns(self):
        # With everything equal this must behave exactly as it always did,
        # or the bias would be a second rotation quietly replacing the first.
        rotation = [self.ONE, self.TWO, self.THREE]
        level = {self.ONE: 2, self.TWO: 2, self.THREE: 2}

        assert next_builder(rotation, self._after(self.ONE), built_today=level) == (
            self.TWO
        )
        assert next_builder(rotation, self._after(self.TWO), built_today=level) == (
            self.THREE
        )
        assert next_builder(rotation, self._after(self.THREE), built_today=level) == (
            self.ONE
        )

    def test_a_spent_runtime_does_not_get_a_turn_to_waste(self):
        # In a bare rotation an exhausted runtime still takes its turn and
        # spends a cycle discovering it cannot build.
        chosen = next_builder(
            [self.ONE, self.TWO, self.THREE],
            self._after(self.ONE),
            unavailable={self.TWO},
        )

        assert chosen == self.THREE

    def test_the_turn_still_goes_round_when_every_builder_is_spent(self):
        # A cycle that runs and records the refusal says more than one that
        # never started: the ledger learns the reset, and the dashboard shows
        # why nothing happened.
        chosen = next_builder(
            [self.ONE, self.TWO],
            self._after(self.ONE),
            unavailable={self.ONE, self.TWO},
        )

        assert chosen == self.TWO

    def test_being_spent_outranks_being_behind(self):
        # Order matters: a runtime with nothing left and no cycles today is
        # the most "behind" of all, and giving it the turn on that basis
        # would be the bias defeating the filter.
        chosen = next_builder(
            [self.ONE, self.TWO, self.THREE],
            self._after(self.ONE),
            built_today={self.ONE: 5, self.TWO: 5, self.THREE: 0},
            unavailable={self.THREE},
        )

        assert chosen == self.TWO

    async def test_a_newly_added_runtime_is_not_treated_as_spent(
        self, async_client, async_session
    ):
        """A profile nobody has built with has no runtime on record.

        Reading unknown as unavailable would keep a newly added runtime out
        of the rotation forever, which is the opposite of what the rotation
        is for — and is exactly the state Antigravity was in the moment it
        was added.
        """
        # Arrange: two builders, only one of which has ever built.
        await add_remote(async_client, "https://github.com/acme/alpha", "alpha")
        program = await create_program(async_client, rotate_builder=[REVIEWER])
        first = (await claim(async_client, program["id"])).json()["cycle"]
        await async_client.patch(
            f"/api/automation/v1/improve/cycles/{first['id']}",
            json={"status": "COMPLETED", "builder_runtime": "claude-code"},
        )

        # Act
        second = (await claim(async_client, program["id"])).json()["cycle"]

        # Assert: the turn went to the one that has never built.
        assert second["builder_profile_id"] == REVIEWER


class TestTheBuilderShare:
    """The rotation's own measure, which nothing could read.

    The program exists because one runtime built 31 of 33 cycles. The plan
    asks for "builder share across the three runtimes within twenty percent of
    even over a week" — and the only number available was the ledger's daily
    counter, which rolls over at midnight and so says nothing about a rotation
    at all.
    """

    async def _cycle_built_by(
        self, async_session, program_id: str, runtime: str, *, days_ago: float
    ) -> None:
        cycle = AutomationImproveCycle(
            id=uuid.uuid4(),
            automation_id=uuid.UUID(program_id),
            org_id=ORG_ID,
            repo_index=0,
            repo_label="alpha",
            repo_url="https://github.com/acme/alpha",
            status=ImproveCycleStatus.COMPLETED,
            builder_runtime=runtime,
            created_at=datetime.now(UTC) - timedelta(days=days_ago),
        )
        async_session.add(cycle)
        await async_session.flush()

    async def _cycle(self, async_session, program_id: str, **fields):
        cycle = AutomationImproveCycle(
            id=uuid.uuid4(),
            automation_id=uuid.UUID(program_id),
            org_id=ORG_ID,
            repo_index=0,
            repo_label="alpha",
            repo_url=fields.pop("repo_url", "https://github.com/acme/alpha"),
            status=ImproveCycleStatus.COMPLETED,
            created_at=datetime.now(UTC) - timedelta(days=fields.pop("days_ago", 1)),
            **fields,
        )
        async_session.add(cycle)
        await async_session.flush()
        return cycle

    async def test_a_cycle_that_names_no_runtime_is_not_counted_as_one(
        self, async_client, async_session
    ):
        """ "custom" is not a runtime; it is the absence of a named one.

        The preset resolves a profile's runtime from its ACP server, so a
        profile with its own launch command records the literal "custom" and
        several different real runtimes land under the same word. Counted as
        a runtime it reports "a runtime called custom did some of the work",
        and — worse — widens the denominator for "even", so the measure moves
        with how a profile was configured rather than with who built.
        """
        # Arrange: two real runtimes and one cycle that names neither.
        program = await create_program(async_client)
        await self._cycle(async_session, program["id"], builder_runtime="claude-code")
        await self._cycle(async_session, program["id"], builder_runtime="codex")
        await self._cycle(async_session, program["id"], builder_runtime="custom")
        await async_session.commit()

        # Act
        response = await async_client.get(
            "/api/automation/v1/improve/builder-share?days=7"
        )

        # Assert: two runtimes, evenly split, and the third cycle visible as
        # a gap in the record rather than as a runtime.
        body = response.json()
        assert {r["runtime"] for r in body["runtimes"]} == {"claude-code", "codex"}
        assert body["cycles"] == 2
        assert body["unattributed_cycles"] == 1
        assert body["evenness_gap"] == 0.0

    async def test_the_acp_fallback_is_also_no_runtime(
        self, async_client, async_session
    ):
        # The other sentinel: when the profile detail lookup fails the preset
        # falls back to the profile's kind. "acp" says even less than
        # "custom" does.
        program = await create_program(async_client)
        await self._cycle(async_session, program["id"], builder_runtime="acp")
        await async_session.commit()

        response = await async_client.get(
            "/api/automation/v1/improve/builder-share?days=7"
        )

        body = response.json()
        assert body["runtimes"] == []
        assert body["unattributed_cycles"] == 1
        # Nothing attributable was built, so there is no share to judge.
        assert body["evenness_gap"] is None

    async def test_a_cycle_filed_under_the_wrong_runtime_is_counted(
        self, async_client, async_session
    ):
        """The plan says this one is wrong today, so it has to be counted.

        `builder_runtime` is reported twice — at claim time from the profile,
        and again with whatever ran. The switch trail is the authority; a
        cycle disagreeing with it has its share, its reviewer choice and its
        ledger entry all filed under a runtime that did not do the work.
        """
        # Arrange: one cycle that failed over and was recorded correctly, one
        # that failed over and was not.
        program = await create_program(async_client)
        await self._cycle(
            async_session,
            program["id"],
            builder_runtime="codex",
            runtime_switches=[{"from": "claude-code", "to": "codex"}],
        )
        await self._cycle(
            async_session,
            program["id"],
            builder_runtime="claude-code",
            runtime_switches=[{"from": "claude-code", "to": "codex"}],
        )
        await async_session.commit()

        # Act
        response = await async_client.get(
            "/api/automation/v1/improve/builder-share?days=7"
        )

        # Assert
        assert response.json()["misattributed_cycles"] == 1

    async def test_work_redone_after_a_handoff_is_told_from_work_still_waiting(
        self, async_client, async_session
    ):
        """Two different things, and only one of them is a fault.

        A handoff nobody has picked up yet is work waiting. A handoff
        overtaken by a fresh cycle on the same repository is work *redone* —
        the next cycle paid again for what the last one had already learned,
        which is the thing the plan asks to be zero.
        """
        # Arrange
        program = await create_program(async_client)
        handoff = {
            "status": "incomplete",
            "reason": "runtime_exhausted",
            "next_steps": ["Continue from the branch diff."],
        }
        # Overtaken: a later cycle on the same repository.
        await self._cycle(
            async_session,
            program["id"],
            builder_runtime="claude-code",
            handoff=handoff,
            days_ago=3,
        )
        await self._cycle(
            async_session, program["id"], builder_runtime="codex", days_ago=1
        )
        # Still waiting: nothing has come after it, on another repository.
        await self._cycle(
            async_session,
            program["id"],
            builder_runtime="codex",
            handoff=handoff,
            repo_url="https://github.com/acme/beta",
            days_ago=2,
        )
        await async_session.commit()

        # Act
        response = await async_client.get(
            "/api/automation/v1/improve/builder-share?days=7"
        )

        # Assert
        body = response.json()
        assert body["work_redone"] == 1
        assert body["handoffs_waiting"] == 1

    async def test_a_resumed_handoff_is_neither_redone_nor_waiting(
        self, async_client, async_session
    ):
        # The whole point of the handoff: picked up, so nothing was lost and
        # nothing is outstanding.
        program = await create_program(async_client)
        first = await self._cycle(
            async_session,
            program["id"],
            builder_runtime="claude-code",
            handoff={"status": "incomplete", "next_steps": ["Carry on."]},
            days_ago=2,
        )
        second = await self._cycle(
            async_session, program["id"], builder_runtime="codex", days_ago=1
        )
        first.resumed_by_cycle_id = second.id
        await async_session.commit()

        response = await async_client.get(
            "/api/automation/v1/improve/builder-share?days=7"
        )

        body = response.json()
        assert (body["work_redone"], body["handoffs_waiting"]) == (0, 0)

    async def test_a_handoff_that_saved_no_code_is_counted(
        self, async_client, async_session
    ):
        """`work_redone` read zero through four handoffs that lost their work.

        It counts handoffs nobody resumed. These were resumed, correctly and
        promptly — and their branch had never reached the remote, so the
        cycle that picked each one up rebuilt the change from nothing. A
        measure that cannot see that is a measure the plan's "zero cycles
        that redo work" can sit at zero behind.
        """
        # Arrange: resumed exactly as intended, with nothing to resume.
        program = await create_program(async_client)
        lost = await self._cycle(
            async_session,
            program["id"],
            builder_runtime="claude-code",
            handoff={
                "status": "incomplete",
                "branch": "improve/20261025-0100",
                "branch_pushed": False,
                "next_steps": ["Carry on."],
            },
            days_ago=2,
        )
        kept = await self._cycle(
            async_session,
            program["id"],
            builder_runtime="codex",
            handoff={
                "status": "incomplete",
                "branch": "improve/20261025-0200",
                "branch_pushed": True,
                "next_steps": ["Carry on."],
            },
            repo_url="https://github.com/acme/beta",
            days_ago=2,
        )
        picked_up = await self._cycle(
            async_session, program["id"], builder_runtime="codex", days_ago=1
        )
        lost.resumed_by_cycle_id = picked_up.id
        kept.resumed_by_cycle_id = picked_up.id
        await async_session.commit()

        # Act
        response = await async_client.get(
            "/api/automation/v1/improve/builder-share?days=7"
        )

        # Assert: the fault is visible even though the handover worked.
        body = response.json()
        assert body["handoffs_unsaved"] == 1
        assert (body["work_redone"], body["handoffs_waiting"]) == (0, 0)

    async def test_an_even_rotation_is_within_the_target(
        self, async_client, async_session
    ):
        # Arrange: three runtimes, nine cycles, spread over a full week.
        program = await create_program(async_client)
        for index, runtime in enumerate(["claude-code", "codex", "antigravity"] * 3):
            await self._cycle_built_by(
                async_session, program["id"], runtime, days_ago=6.5 - index * 0.7
            )
        await async_session.commit()

        # Act
        response = await async_client.get(
            "/api/automation/v1/improve/builder-share?days=7"
        )

        # Assert
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["cycles"] == 9
        assert {r["runtime"]: r["cycles"] for r in body["runtimes"]} == {
            "claude-code": 3,
            "codex": 3,
            "antigravity": 3,
        }
        assert body["evenness_gap"] == 0.0
        assert body["within_target"] is True

    async def test_the_lopsided_split_the_rotation_exists_to_fix_is_reported(
        self, async_client, async_session
    ):
        # Arrange: the shape the plan was written against — one runtime doing
        # almost all of it.
        program = await create_program(async_client)
        for index in range(9):
            await self._cycle_built_by(
                async_session, program["id"], "claude-code", days_ago=6.5 - index * 0.7
            )
        await self._cycle_built_by(async_session, program["id"], "codex", days_ago=0.2)
        await async_session.commit()

        # Act
        response = await async_client.get(
            "/api/automation/v1/improve/builder-share?days=7"
        )

        # Assert: 90/10 against an even 50, so 40 points out — twice the
        # threshold, and reported as failing rather than as a bare number.
        body = response.json()
        assert body["evenness_gap"] == 40.0
        assert body["within_target"] is False
        assert body["target_gap"] == 20.0

    async def test_a_week_asked_of_two_days_is_not_yet_rather_than_a_verdict(
        self, async_client, async_session
    ):
        # The honest answer to "is it even over a week" with two days of
        # history is "ask again later". Reporting False would say the rotation
        # failed; reporting True would be worse.
        program = await create_program(async_client)
        await self._cycle_built_by(
            async_session, program["id"], "claude-code", days_ago=1.0
        )
        await self._cycle_built_by(async_session, program["id"], "codex", days_ago=0.1)
        await async_session.commit()

        response = await async_client.get(
            "/api/automation/v1/improve/builder-share?days=7"
        )

        body = response.json()
        assert body["days_covered"] == 2
        assert body["window_days"] == 7
        assert body["within_target"] is None
        # The measurement is still reported; only the verdict is withheld.
        assert body["evenness_gap"] == 0.0

    async def test_one_programs_rotation_is_not_measured_by_the_whole_machine(
        self, async_client, async_session
    ):
        # Unscoped, the answer sweeps in disabled programs and test harnesses.
        # That is the right answer to "what has this machine been running" and
        # the wrong one to "is this program's rotation working".
        mine = await create_program(async_client)
        other = await create_program(async_client)
        for runtime in ("claude-code", "codex"):
            await self._cycle_built_by(async_session, mine["id"], runtime, days_ago=1.0)
        for _ in range(8):
            await self._cycle_built_by(
                async_session, other["id"], "claude-code", days_ago=1.0
            )
        await async_session.commit()

        everything = await async_client.get(
            "/api/automation/v1/improve/builder-share?days=7"
        )
        scoped = await async_client.get(
            f"/api/automation/v1/improve/builder-share?days=7&automation_id={mine['id']}"
        )

        assert everything.json()["cycles"] == 10
        assert scoped.json()["cycles"] == 2
        assert scoped.json()["automation_id"] == mine["id"]
        assert {r["runtime"] for r in scoped.json()["runtimes"]} == {
            "claude-code",
            "codex",
        }

    async def test_a_configured_runtime_that_never_builds_widens_the_gap(
        self, async_client, async_session
    ):
        """Even between two is not even when three were configured.

        This is the failure the rotation exists to fix, and measuring against
        only the runtimes that appear would score it as a perfect split: a
        program set to rotate three runtimes, quietly using two of them.
        """
        # Arrange: three builders configured, two of them actually building.
        program = await create_program(
            async_client, rotate_builder=[REVIEWER, THIRD_BUILDER]
        )
        for runtime in ("claude-code", "codex") * 4:
            await self._cycle_built_by(
                async_session, program["id"], runtime, days_ago=1.0
            )
        await async_session.commit()

        # Act
        response = await async_client.get(
            f"/api/automation/v1/improve/builder-share?days=7"
            f"&automation_id={program['id']}"
        )

        # Assert: 50/50 between two, measured against an even third each, so
        # 16.7 points out rather than the 0.0 the observed-only reading gives.
        body = response.json()
        assert body["configured_builders"] == 3
        assert body["evenness_gap"] == 16.7

    async def test_nothing_built_is_no_measurement_rather_than_a_perfect_one(
        self, async_client
    ):
        # An empty window has no widest share to be far from even, and
        # reporting a gap of zero would read as a flawless rotation.
        await create_program(async_client)

        response = await async_client.get(
            "/api/automation/v1/improve/builder-share?days=7"
        )

        body = response.json()
        assert body["cycles"] == 0
        assert body["runtimes"] == []
        assert body["evenness_gap"] is None
        assert body["within_target"] is None


class TestWhatIdentityCostTheCycle:
    """The budget was enforced and never recorded, so nobody could read it.

    Milestone 2's measure is that identity never exceeds its share of the
    cycle's context budget, *measured per cycle*. Enforcement alone cannot
    answer it, and cannot show the user which of their documents is about to
    be the one that gets cut.
    """

    async def test_the_cycle_records_what_identity_took(self, async_client):
        # Arrange
        await add_remote(async_client, "https://github.com/acme/alpha", "alpha")
        program = await create_program(async_client)
        cycle_id = (await claim(async_client, program["id"])).json()["cycle"]["id"]

        # Act: the shape the runner sends with its builder stage.
        response = await async_client.patch(
            f"/api/automation/v1/improve/cycles/{cycle_id}",
            json={
                "status": "REVIEWING",
                "identity_cost": {
                    "budget": 24000,
                    "used": 9100,
                    "share": 0.379,
                    "documents": {"soul": 8000, "style": 1100},
                    "trimmed": ["soul"],
                    "omitted": [],
                },
            },
        )

        # Assert: readable, including the document that did not fit whole —
        # which is the part a person can act on.
        assert response.status_code == 200, response.text
        cost = response.json()["identity_cost"]
        assert cost["share"] == 0.379
        assert cost["trimmed"] == ["soul"]

    async def test_a_cycle_that_reported_none_says_nothing_rather_than_zero(
        self, async_client
    ):
        # A runner from before this existed sends no cost. Reporting zero
        # would read as "identity took none of the prompt", which is a claim
        # about a cycle nobody measured.
        await add_remote(async_client, "https://github.com/acme/beta", "beta")
        program = await create_program(async_client)
        cycle_id = (await claim(async_client, program["id"])).json()["cycle"]["id"]

        response = await async_client.patch(
            f"/api/automation/v1/improve/cycles/{cycle_id}",
            json={"status": "REVIEWING"},
        )

        assert response.json()["identity_cost"] is None


class TestFollowingAMergedChangeBack:
    """The audit trail's own measure, which nothing could read.

    The trail exists to answer one question — somebody holding a commit on a
    default branch asks what produced it — and the plan sets a number on it:
    95% of merged cycles. Until now the only way to check was to read the rows
    by hand, which is not a measure anybody runs.
    """

    async def _merged(
        self, async_session, program_id: str, **overrides
    ) -> AutomationImproveCycle:
        fields = {
            "merged_at": datetime.now(UTC),
            "merge_commit_sha": uuid.uuid4().hex[:40],
            "pr_url": "https://github.com/acme/alpha/pull/1",
            "prompt_digest": "sha256:" + "a" * 64,
            "sources": [{"url": "https://example.test/doc", "title": "doc"}],
        }
        fields.update(overrides)
        cycle = AutomationImproveCycle(
            id=uuid.uuid4(),
            automation_id=uuid.UUID(program_id),
            org_id=ORG_ID,
            repo_index=0,
            repo_label="alpha",
            repo_url="https://github.com/acme/alpha",
            status=ImproveCycleStatus.COMPLETED,
            builder_runtime="claude-code",
            **fields,
        )
        async_session.add(cycle)
        await async_session.flush()
        return cycle

    async def test_a_merged_change_with_its_prompt_and_sources_resolves(
        self, async_client, async_session
    ):
        # Arrange
        program = await create_program(async_client)
        await self._merged(async_session, program["id"])
        await async_session.commit()

        # Act
        response = await async_client.get("/api/automation/v1/improve/provenance")

        # Assert
        assert response.status_code == 200, response.text
        body = response.json()
        assert (body["merged"], body["from_commit"], body["unresolved"]) == (1, 1, 0)
        assert body["coverage"] == 1.0
        assert body["within_target"] is True

    async def test_a_squash_merge_resolves_by_its_pull_request_and_says_so(
        self, async_client, async_session
    ):
        # A squash merge rewrites the commit, so the trail still leads back —
        # by a longer route. The plan asks for that to be reported rather than
        # hidden, which means it must not pass as the stronger answer.
        program = await create_program(async_client)
        await self._merged(async_session, program["id"], merge_commit_sha=None)
        await async_session.commit()

        response = await async_client.get("/api/automation/v1/improve/provenance")

        body = response.json()
        assert body["from_commit"] == 0
        assert body["from_pull_request"] == 1
        assert body["coverage"] == 1.0

    async def test_a_change_that_cited_nothing_is_a_broken_trail_not_a_pass(
        self, async_client, async_session
    ):
        # The commit is where a reader starts; the prompt and the sources are
        # what they came for. A merged change with neither leads nowhere.
        program = await create_program(async_client)
        await self._merged(async_session, program["id"], sources=None)
        await self._merged(async_session, program["id"], prompt_digest=None)
        await self._merged(async_session, program["id"])
        await async_session.commit()

        response = await async_client.get("/api/automation/v1/improve/provenance")

        body = response.json()
        assert body["merged"] == 3
        assert body["unresolved"] == 2
        assert body["missing"] == {"sources": 1, "prompt_digest": 1}
        # One of three, well under the plan's 95%.
        assert body["coverage"] == 0.3333
        assert body["within_target"] is False

    async def test_a_change_that_never_merged_is_not_counted_against_the_trail(
        self, async_client, async_session
    ):
        # Nothing for anybody to find in a `git log`, so asking it to be
        # traceable would flatter the number with work nobody has to audit.
        program = await create_program(async_client)
        await self._merged(
            async_session, program["id"], merged_at=None, merge_commit_sha=None
        )
        await async_session.commit()

        response = await async_client.get("/api/automation/v1/improve/provenance")

        body = response.json()
        assert body["merged"] == 0
        assert body["coverage"] is None
        assert body["within_target"] is None


class TestTheProgramsPackagedCode:
    """A program runs the code it was packaged with, and must say which.

    The runner is packaged once, when the program is created, so a preset fix
    reaches only programs created afterwards. Nothing recorded which code a
    program got, which made "this feature does not work for me" and "my
    program predates the feature" the same observation — including for reading
    the user's own corrections, which is meant to happen every cycle or not at
    all.
    """

    async def test_a_new_program_is_running_current_code(self, async_client):
        # Arrange / Act
        program = await create_program(async_client)
        response = await async_client.get(
            f"/api/automation/v1/improve/{program['id']}/rotation"
        )

        # Assert
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["preset_code_is_current"] is True
        assert body["preset_code_digest"] == body["service_preset_code_digest"]
        assert body["preset_code_digest"].startswith("sha256:")

    async def test_a_program_packaged_from_other_code_says_so(
        self, async_client, async_session
    ):
        # Arrange: a program whose recorded digest is not this build's — which
        # is every program created before the preset last changed.
        program = await create_program(async_client)
        automation = await async_session.get(Automation, uuid.UUID(program["id"]))
        automation.preset_metadata = {
            **automation.preset_metadata,
            "preset_code_digest": "sha256:" + "0" * 64,
        }
        await async_session.commit()

        # Act
        response = await async_client.get(
            f"/api/automation/v1/improve/{program['id']}/rotation"
        )

        # Assert: both sides reported, so the dashboard can show the pair
        # rather than only an assertion.
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["preset_code_is_current"] is False
        assert body["preset_code_digest"] == "sha256:" + "0" * 64
        assert body["service_preset_code_digest"] != body["preset_code_digest"]

    async def test_a_program_from_before_the_digest_existed_is_not_current(
        self, async_client, async_session
    ):
        # No digest at all means packaged by a build that did not record one,
        # which is strictly older than this one. Reporting it as current would
        # hide exactly the case this exists for.
        program = await create_program(async_client)
        automation = await async_session.get(Automation, uuid.UUID(program["id"]))
        automation.preset_metadata = {
            key: value
            for key, value in automation.preset_metadata.items()
            if key != "preset_code_digest"
        }
        await async_session.commit()

        response = await async_client.get(
            f"/api/automation/v1/improve/{program['id']}/rotation"
        )

        assert response.status_code == 200, response.text
        assert response.json()["preset_code_is_current"] is False
        assert response.json()["preset_code_digest"] is None

    async def test_refreshing_the_code_makes_it_current_again(
        self, async_client, async_session
    ):
        # Arrange: the stale program from above.
        program = await create_program(async_client)
        automation = await async_session.get(Automation, uuid.UUID(program["id"]))
        automation.preset_metadata = {
            **automation.preset_metadata,
            "preset_code_digest": "sha256:" + "0" * 64,
        }
        before = automation.tarball_path
        await async_session.commit()

        # Act
        refresh = await async_client.post(
            f"/api/automation/v1/improve/{program['id']}/refresh-code"
        )
        rotation = await async_client.get(
            f"/api/automation/v1/improve/{program['id']}/rotation"
        )

        # Assert: new tarball, and the digest recorded with it — not left
        # describing the code the program had before the call.
        assert refresh.status_code == 200, refresh.text
        assert refresh.json()["tarball_path"] != before
        assert rotation.json()["preset_code_is_current"] is True


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

    async def test_whether_the_change_landed_is_data_not_a_rationale_sentence(
        self, async_client
    ):
        """A merged cycle has to be identifiable without reading prose.

        The publish gate approving is a decision, and an authorised merge can
        still fail, so the gate log cannot answer "did this land". It used to
        be the only record: the merge appeared as git's own output inside a
        rationale, which means counting merged cycles, or following one to the
        commit on the default branch, meant parsing a sentence.
        """
        # Arrange
        program, cycle_id = await self._claimed_cycle(async_client)

        # Act
        response = await async_client.patch(
            f"/api/automation/v1/improve/cycles/{cycle_id}",
            json={
                "status": "COMPLETED",
                "verdict": "ACCEPTED",
                "pr_url": "https://github.com/acme/alpha/pull/95",
                "head_sha": "e595f52",
                "merged_at": "2026-10-24T18:01:17Z",
                "merge_commit_sha": "666481f3154c7f6ce6551a674b9d3205dd724297",
            },
        )

        # Assert
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["merged_at"] == "2026-10-24T18:01:17Z"
        assert body["merge_commit_sha"] == "666481f3154c7f6ce6551a674b9d3205dd724297"

    async def test_a_cycle_that_did_not_merge_says_so_by_omission(self, async_client):
        # An absent merge must read as absent, not as an empty string that a
        # caller counting merged cycles would have to know to exclude.
        program, cycle_id = await self._claimed_cycle(async_client)

        response = await async_client.patch(
            f"/api/automation/v1/improve/cycles/{cycle_id}",
            json={"status": "COMPLETED", "verdict": "CHANGES_REQUESTED"},
        )

        assert response.status_code == 200, response.text
        assert response.json()["merged_at"] is None
        assert response.json()["merge_commit_sha"] is None

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

    #: What a cycle that reached the publish gate has already shown. A real
    #: park happens *after* the build, the review and the readiness check, so
    #: a fixture without this evidence is a cycle that could never have got
    #: there — and a person approving it still cannot conjure a passing test.
    PUBLISHABLE = {
        "verdict": "ACCEPTED",
        "head_sha": "a1b2c3d4e5f6",
        "changed_files": ["server/socket.py"],
        "checks": [{"command": "pytest tests/", "passed": True}],
        "summary": "Renamed the event envelope and shipped both for a release.",
    }

    async def _parked(self, async_client, label: str, **evidence) -> str:
        await add_remote(async_client, f"https://github.com/acme/{label}", label)
        program = await create_program(async_client)
        cycle_id = (await claim(async_client, program["id"])).json()["cycle"]["id"]
        response = await async_client.patch(
            f"/api/automation/v1/improve/cycles/{cycle_id}",
            json={
                **evidence,
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
        listed = await async_client.get(PARKED)
        assert listed.status_code == 200, listed.text
        parked = listed.json()["cycles"]
        assert [c["id"] for c in parked] == [cycle_id]
        assert parked[0]["status"] == "PARKED"
        assert parked[0]["parked_plan"]["rollback"]
        assert parked[0]["parked_at"]
        assert [d["route"] for d in parked[0]["gate_decisions"]] == ["escalate"]

    async def test_resolving_records_the_answer_against_the_phase_it_stopped_at(
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

        # Assert: the answer kept, both decisions on the record, and the
        # phase to carry on from named on the decision rather than guessed.
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["resolution"].startswith("The client release")
        assert body["resolved_at"]
        assert [d["route"] for d in body["gate_decisions"]] == ["escalate", "approve"]
        assert body["gate_decisions"][-1]["decided_by"].startswith("user:")
        assert body["gate_decisions"][-1]["phase"] == "publish"
        # Still waiting — on a run now rather than on a person. Reading as in
        # flight is what let the janitor close it as abandoned.
        assert body["status"] == "PARKED"

    async def test_an_answer_is_not_something_the_janitor_may_discard(
        self, async_client, async_session_factory
    ):
        """Raise, resolve, resume — and the resume had nothing behind it.

        Resolving used to put the cycle back to the phase it stopped at so it
        read as in flight again. Three mechanisms believed that and all three
        were wrong: the janitor closed it as abandoned on its next pass,
        because the run that parked it had ended long before; the work graph
        counted it as work in flight, so clients polled for a cycle nobody
        was working on; and nothing ever picked it up, because the claim only
        opens a new cycle. The person's answer bought nothing at all.

        It stays PARKED — answered, and waiting on a run rather than on a
        person — which is the one status the janitor already leaves alone and
        the graph already draws as waiting.
        """
        # Arrange
        cycle_id = await self._parked(async_client, "zeta")

        # Act
        answered = await async_client.post(
            f"/api/automation/v1/improve/cycles/{cycle_id}/resolve",
            json={
                "route": "approve",
                "resolution": "The client release is out; publish it.",
            },
        )
        reaped = await reap_abandoned_cycles(async_session_factory)

        # Assert: still there, still answered, still offered.
        assert answered.status_code == 200, answered.text
        assert reaped == 0
        listed = await async_client.get(
            f"/api/automation/v1/improve/{answered.json()['automation_id']}/cycles"
        )
        held = next(c for c in listed.json()["cycles"] if c["id"] == cycle_id)
        assert held["reaped"] is False
        assert held["resolution"].startswith("The client release")

    async def test_an_answered_question_stops_waiting_on_the_person(self, async_client):
        # "What is waiting on me" is the whole purpose of this list, and a
        # question they have already answered is not. It stays PARKED so the
        # janitor leaves it alone; that must not leak into this surface.
        cycle_id = await self._parked(async_client, "kappa")
        assert [c["id"] for c in (await async_client.get(PARKED)).json()["cycles"]] == [
            cycle_id
        ]

        await async_client.post(
            f"/api/automation/v1/improve/cycles/{cycle_id}/resolve",
            json={"route": "approve", "resolution": "Consumers have moved."},
        )

        assert (await async_client.get(PARKED)).json()["cycles"] == []

    async def test_a_question_is_answered_once(self, async_client):
        # It stays PARKED until a run carries the answer out, so "is it
        # parked" stopped answering "is it waiting" — and a second answer
        # would overwrite the first and add another decision to the log that
        # records who decided what.
        cycle_id = await self._parked(async_client, "mu")
        first = await async_client.post(
            f"/api/automation/v1/improve/cycles/{cycle_id}/resolve",
            json={"route": "approve", "resolution": "Consumers have moved."},
        )

        second = await async_client.post(
            f"/api/automation/v1/improve/cycles/{cycle_id}/resolve",
            json={"route": "archive", "resolution": "Actually, no."},
        )

        assert first.status_code == 200, first.text
        assert second.status_code == 409
        assert "already answered" in second.text

    async def test_an_answered_cycle_is_handed_to_the_next_run_to_finish(
        self, async_client
    ):
        """The resume half of the rule, which is what makes parking worth it.

        The work is on a branch and a person has said to publish it, so the
        next run finishes *that* cycle. Claiming would open a new one against
        the rotation and rebuild a change that already exists — which is the
        "restarts" the exit clause rules out.
        """
        # Arrange
        cycle_id = await self._parked(async_client, "eta")
        resolved = await async_client.post(
            f"/api/automation/v1/improve/cycles/{cycle_id}/resolve",
            json={"route": "approve", "resolution": "Ship it; consumers moved."},
        )
        program_id = resolved.json()["automation_id"]

        # Act
        handed = await async_client.post(
            f"/api/automation/v1/improve/{program_id}/continue", json={}
        )

        # Assert: the same cycle, with what the person said and where they
        # sent it, and the program's current configuration to publish under.
        assert handed.status_code == 200, handed.text
        body = handed.json()
        assert body["cycle"]["id"] == cycle_id
        assert body["route"] == "approve"
        assert body["resolution"] == "Ship it; consumers moved."
        assert body["config"]["builder_profile_id"] == BUILDER

        # And exactly once: a second run gets nothing rather than publishing
        # the same change twice.
        again = await async_client.post(
            f"/api/automation/v1/improve/{program_id}/continue", json={}
        )
        assert again.status_code == 200
        assert again.json() is None

    async def test_sending_it_round_again_reaches_the_next_cycle_as_work(
        self, async_client
    ):
        """`cycle` and `return` mean "more work", which is a builder's cycle.

        Their answer is written as a handoff, because that is the channel a
        next cycle already continues from: it arrives as the next steps, with
        the branch the work is already on, instead of being a sentence in a
        column nothing reads.
        """
        # Arrange
        cycle_id = await self._parked(async_client, "lambda", **self.PUBLISHABLE)
        resolved = await async_client.post(
            f"/api/automation/v1/improve/cycles/{cycle_id}/resolve",
            json={
                "route": "return",
                "resolution": "Keep both envelopes for one release first.",
            },
        )
        program_id = resolved.json()["automation_id"]

        # Act: the next run claims as usual -- this is not a publish.
        assert (
            await async_client.post(
                f"/api/automation/v1/improve/{program_id}/continue", json={}
            )
        ).json() is None
        claimed = await claim(async_client, program_id)

        # Assert: their words are what the next cycle is told to do, and the
        # chain reads in both directions.
        assert claimed.status_code == 201, claimed.text
        assert claimed.json()["cycle"]["resumed_from_cycle_id"] == cycle_id
        resume = claimed.json()["resume"]
        assert "both envelopes" in resume["next_steps"][0]
        assert resume["branch"] == "improve/20261024"
        assert resume["branch_pushed"] is True
        # And it stops waiting: handed on is finished, and a cycle left
        # PARKED is one the janitor never closes and the graph draws as
        # waiting for somebody for good.
        history = await async_client.get(
            f"/api/automation/v1/improve/{program_id}/cycles"
        )
        handed = next(c for c in history.json()["cycles"] if c["id"] == cycle_id)
        assert handed["status"] == "COMPLETED"
        assert (await async_client.get(PARKED)).json()["cycles"] == []

    async def test_nothing_waiting_is_said_plainly_rather_than_as_an_error(
        self, async_client
    ):
        # The usual case, asked once per run. An error here would make every
        # ordinary run log a failure.
        await add_remote(async_client, "https://github.com/acme/theta", "theta")
        program = await create_program(async_client)

        response = await async_client.post(
            f"/api/automation/v1/improve/{program['id']}/continue", json={}
        )

        assert response.status_code == 200
        assert response.json() is None

    async def test_a_person_who_said_publish_outranks_the_class_that_parked_it(
        self, async_client
    ):
        """Otherwise resolving a park could only produce the same refusal.

        "A breaking change is never published from here" is a rule about
        agents — the whole point of escalating is that the thing being gated
        is not the thing that answers. The authority endpoint read only the
        class, so a person's answer could not change it and the work stayed
        parked forever.
        """
        # Arrange
        cycle_id = await self._parked(async_client, "iota", **self.PUBLISHABLE)
        refused = await async_client.get(
            f"/api/automation/v1/improve/cycles/{cycle_id}/authority"
        )

        # Act
        await async_client.post(
            f"/api/automation/v1/improve/cycles/{cycle_id}/resolve",
            json={"route": "approve", "resolution": "Both envelopes shipped."},
        )
        allowed = await async_client.get(
            f"/api/automation/v1/improve/cycles/{cycle_id}/authority"
        )

        # Assert
        assert refused.json()["authorised"] is False
        assert refused.json()["human_only"] is True
        assert allowed.json()["authorised"] is True
        assert allowed.json()["route"] == "approve"
        assert "person" in allowed.json()["reason"]

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


class TestJudgingADeletion:
    """The judge signs a removal off, and the service checks who signed."""

    async def _built_deletion(self, async_client, label: str) -> str:
        await add_remote(async_client, f"https://github.com/acme/{label}", label)
        program = await create_program(async_client)
        cycle_id = (await claim(async_client, program["id"])).json()["cycle"]["id"]
        response = await async_client.patch(
            f"/api/automation/v1/improve/cycles/{cycle_id}",
            json={
                "status": "REVIEWING",
                "builder_runtime": "claude-code",
                "change_class": "deleting",
                # The review comes first: a judge's signature answers "is this
                # removal safe to make", not "is this change worth making", so
                # a deletion the reviewers objected to is still objected to.
                "verdict": "ACCEPTED",
                "head_sha": "a1b2c3d",
                "changed_files": ["openhands/legacy.py"],
                "checks": [{"command": "pytest tests/", "passed": True}],
                "summary": "Removed the unused backend resolver.",
            },
        )
        assert response.status_code == 200, response.text
        return cycle_id

    async def test_the_runtime_that_built_it_cannot_attest_its_own_removals(
        self, async_client
    ):
        # Arrange
        cycle_id = await self._built_deletion(async_client, "alpha")

        # Act: the configuration is not consulted; what signed is.
        response = await async_client.post(
            f"/api/automation/v1/improve/cycles/{cycle_id}/attest",
            json={
                "attested": True,
                "runtime": "claude-code",
                "summary": "Looks dead to me.",
                "removals": [
                    {
                        "path": "openhands/legacy.py",
                        "finding": "dead",
                        "where": "grep returns only the definition",
                    }
                ],
            },
        )

        assert response.status_code == 409
        assert "never the author" in response.text

    async def test_an_attestation_from_another_runtime_authorises_the_merge(
        self, async_client
    ):
        # Arrange
        cycle_id = await self._built_deletion(async_client, "beta")
        before = await async_client.get(
            f"/api/automation/v1/improve/cycles/{cycle_id}/authority"
        )
        assert before.json()["authorised"] is False

        # Act
        attested = await async_client.post(
            f"/api/automation/v1/improve/cycles/{cycle_id}/attest",
            json={
                "attested": True,
                "runtime": "codex",
                "summary": "Nothing imports it; the replacement covers the case.",
                "removals": [
                    {
                        "path": "openhands/legacy.py",
                        "symbol": "resolve_backend",
                        "finding": "replaced",
                        "where": "launch/resolve.py does this now",
                    }
                ],
            },
        )

        # Assert: authorised, and the signature is on the gate log.
        assert attested.status_code == 200, attested.text
        assert [d["route"] for d in attested.json()["gate_decisions"]] == ["approve"]
        after = await async_client.get(
            f"/api/automation/v1/improve/cycles/{cycle_id}/authority"
        )
        assert after.json()["authorised"] is True
        assert "codex" in after.json()["reason"]

    async def test_attesting_a_set_of_removals_without_naming_them_is_refused(
        self, async_client
    ):
        cycle_id = await self._built_deletion(async_client, "gamma")

        response = await async_client.post(
            f"/api/automation/v1/improve/cycles/{cycle_id}/attest",
            json={"attested": True, "runtime": "codex", "summary": "all fine"},
        )

        assert response.status_code == 422
        assert "signing off nothing" in response.text

    async def test_a_breaking_change_is_never_authorised_from_here(self, async_client):
        # Arrange: complete evidence, and it still does not matter.
        await add_remote(async_client, "https://github.com/acme/delta", "delta")
        program = await create_program(async_client)
        cycle_id = (await claim(async_client, program["id"])).json()["cycle"]["id"]
        await async_client.patch(
            f"/api/automation/v1/improve/cycles/{cycle_id}",
            json={
                "builder_runtime": "claude-code",
                "change_class": "breaking",
                "head_sha": "a1b2c3d",
                "changed_files": ["openhands/schemas.py"],
                "checks": [{"command": "pytest tests/", "passed": True}],
                "summary": "Renamed the event envelope.",
            },
        )

        # Act
        response = await async_client.get(
            f"/api/automation/v1/improve/cycles/{cycle_id}/authority"
        )

        # Assert
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["authorised"] is False
        assert body["human_only"] is True
        assert body["route"] == "escalate"

    async def test_a_judge_that_also_builds_is_refused_in_the_config(
        self, async_client
    ):
        # Every profile in a rotation builds on some turn, so a judge drawn
        # from it would eventually sign off its own removals.
        response = await async_client.post(
            PRESET,
            json={
                "name": "Judge of its own work",
                "trigger": CRON,
                "improve": improve_config(
                    rotate_builder=[REVIEWER], judge_profile_id=REVIEWER
                ),
            },
        )

        assert response.status_code == 422
        assert "attesting its own deletions" in response.text


class TestCorrectionsBecomeLessons:
    """Answering a parked cycle is a correction, and it is kept as one."""

    LESSONS = "/api/automation/v1/lessons"

    async def _parked_breaking(
        self, async_client, label: str, *, remote: bool = True
    ) -> str:
        # The remote is added once per repository: a second configuration for
        # the same repository is refused, and rightly.
        if remote:
            await add_remote(async_client, f"https://github.com/acme/{label}", label)
        program = await create_program(async_client)
        cycle_id = (await claim(async_client, program["id"])).json()["cycle"]["id"]
        response = await async_client.patch(
            f"/api/automation/v1/improve/cycles/{cycle_id}",
            json={
                "status": "PARKED",
                "change_class": "breaking",
                "branch": "improve/envelope",
                "parked_phase": "publish",
                "parked_question": "Publish the rename now, or wait?",
                "injected_lesson_ids": [],
            },
        )
        assert response.status_code == 200, response.text
        return cycle_id

    async def test_what_a_cycle_was_told_survives_the_failure_that_follows(
        self, async_client
    ):
        """The one field that tells "ignored it" from "never saw it".

        The runner reports it as the builder starts, and the builder may then
        refuse. A failure update that carried no lessons must leave the ones
        already recorded alone — otherwise a cycle that was handed a
        correction and then ran out of usage looks exactly like one that was
        never given it, and the repeat diagnosis has nothing to work from.
        """
        # Arrange: a lesson in force, and a cycle that was handed it.
        captured = await async_client.post(
            self.LESSONS,
            json={
                "scope": "user",
                "trigger": "chat_instruction",
                "text": "Say what a command does before running it.",
            },
        )
        lesson_id = captured.json()["lesson"]["id"]
        await add_remote(async_client, "https://github.com/acme/delta", "delta")
        program = await create_program(async_client)
        claimed = await claim(async_client, program["id"])
        assert claimed.status_code == 201, claimed.text
        cycle_id = claimed.json()["cycle"]["id"]
        await async_client.patch(
            f"/api/automation/v1/improve/cycles/{cycle_id}",
            json={
                "status": "BUILDING",
                "branch": "improve/20261025",
                "injected_lesson_ids": [lesson_id],
            },
        )

        # Act: the builder runs out, which reports no lessons of its own.
        failed = await async_client.patch(
            f"/api/automation/v1/improve/cycles/{cycle_id}",
            json={
                "status": "FAILED",
                "error": "claude-code ran out of usage mid-change.",
            },
        )

        # Assert
        assert failed.status_code == 200, failed.text
        assert failed.json()["injected_lesson_ids"] == [lesson_id]

    async def test_an_answer_is_filed_without_being_asked_for(self, async_client):
        # Arrange
        cycle_id = await self._parked_breaking(async_client, "alpha")

        # Act
        resolved = await async_client.post(
            f"/api/automation/v1/improve/cycles/{cycle_id}/resolve",
            json={
                "route": "cycle",
                "resolution": (
                    "Wait for the client release before renaming a wire contract."
                ),
            },
        )
        assert resolved.status_code == 200, resolved.text

        # Assert: a correction the user *made* is recorded without asking
        # them. An agent's guess about their preferences stays a proposal.
        listed = await async_client.get(self.LESSONS)
        assert listed.status_code == 200, listed.text
        lessons = listed.json()["lessons"]
        assert len(lessons) == 1
        assert lessons[0]["trigger"] == "parked_resolution"
        assert lessons[0]["scope"] == "project"
        # Verbatim: paraphrasing a correction is how it comes back as
        # something they have to give again.
        assert lessons[0]["text"].startswith("Wait for the client release")
        assert lessons[0]["repeats"] == 0

    async def test_the_same_answer_twice_is_a_repeat_with_a_diagnosis(
        self, async_client
    ):
        # Arrange: the same correction, given on two cycles.
        first = await self._parked_breaking(async_client, "beta")
        await async_client.post(
            f"/api/automation/v1/improve/cycles/{first}/resolve",
            json={
                "route": "cycle",
                "resolution": (
                    "Wait for the client release before renaming a wire contract."
                ),
            },
        )
        second = await self._parked_breaking(async_client, "beta", remote=False)

        # Act: said differently, meant identically.
        await async_client.post(
            f"/api/automation/v1/improve/cycles/{second}/resolve",
            json={
                "route": "cycle",
                "resolution": (
                    "Do not rename a wire contract until the client release is out."
                ),
            },
        )

        # Assert: one lesson, not two, and the repeat is counted with the
        # stage that let it through named.
        listed = await async_client.get(self.LESSONS)
        lessons = listed.json()["lessons"]
        assert len(lessons) == 1
        assert lessons[0]["repeats"] == 1

        incidents = await async_client.get(f"{self.LESSONS}/incidents")
        assert incidents.status_code == 200, incidents.text
        body = incidents.json()
        assert body["total"] == 1
        # The cycle reported loading no lessons, so this is an injection
        # failure rather than the work ignoring something it had.
        assert body["incidents"][0]["stage"] == "not_injected"
        # Every stage, including the ones at zero: a count of zero is
        # information.
        assert set(body["by_stage"]) >= {"never_captured", "ignored"}

    async def test_an_acknowledgement_is_not_kept_as_a_correction(self, async_client):
        # Arrange
        cycle_id = await self._parked_breaking(async_client, "gamma")

        # Act
        await async_client.post(
            f"/api/automation/v1/improve/cycles/{cycle_id}/resolve",
            json={"route": "approve", "resolution": "Yes, go ahead."},
        )

        # Assert: storing these would bury the real corrections in a list a
        # person has to read.
        listed = await async_client.get(self.LESSONS)
        assert listed.json()["lessons"] == []

    async def test_mechanising_a_lesson_names_what_now_checks_it(self, async_client):
        # Arrange
        captured = await async_client.post(
            self.LESSONS,
            json={
                "scope": "user",
                "trigger": "chat_instruction",
                "text": "Never add a dependency without checking its age.",
            },
        )
        assert captured.status_code == 201, captured.text
        lesson_id = captured.json()["lesson"]["id"]

        # Act
        mechanised = await async_client.post(
            f"{self.LESSONS}/{lesson_id}/mechanise",
            json={"check_ref": "scripts/check-dependency-age.cjs"},
        )

        # Assert: the stage that actually ends repetition — a model that
        # skims the document still fails the check.
        assert mechanised.status_code == 200, mechanised.text
        assert mechanised.json()["mechanised"] is True
        assert mechanised.json()["check_ref"].endswith(".cjs")

    async def test_mechanised_with_nothing_named_is_refused(self, async_client):
        captured = await async_client.post(
            self.LESSONS,
            json={
                "scope": "user",
                "trigger": "comment",
                "text": "Keep commit subjects under seventy characters.",
            },
        )
        lesson_id = captured.json()["lesson"]["id"]

        response = await async_client.post(
            f"{self.LESSONS}/{lesson_id}/mechanise", json={"check_ref": ""}
        )

        assert response.status_code == 422

    async def test_a_retired_lesson_keeps_the_record_that_it_was_needed(
        self, async_client
    ):
        captured = await async_client.post(
            self.LESSONS,
            json={
                "scope": "user",
                "trigger": "comment",
                "text": "Stop asking before every additive change.",
            },
        )
        lesson_id = captured.json()["lesson"]["id"]

        retired = await async_client.post(f"{self.LESSONS}/{lesson_id}/retire")

        assert retired.status_code == 200, retired.text
        assert retired.json()["status"] == "retired"
        # Gone from what applies, still there as evidence this was needed.
        assert (await async_client.get(self.LESSONS)).json()["lessons"] == []
        all_of_them = await async_client.get(f"{self.LESSONS}?include_retired=true")
        assert len(all_of_them.json()["lessons"]) == 1


class TestTheHandoffIsVisible:
    """A handoff that no client can read is a handoff nobody acts on.

    The columns, the request field and the resume pointer all landed before
    the response did, so a cycle could be handed over and resumed from with
    every client seeing an ordinary failure. That is the one bug shape this
    whole feature cannot survive: it looks exactly like the feature not
    working.
    """

    FALLBACK: ClassVar[dict] = {
        "status": "incomplete",
        "reason": "runtime_exhausted",
        "branch": "improve/20261024",
        "branch_pushed": False,
        "stage": "unknown",
        "goal": "",
        "done": [],
        "next_steps": ["Read the branch diff and continue from it."],
        "do_not": [],
        "files_touched": [],
    }

    async def test_a_stored_handoff_comes_back_in_the_response(self, async_client):
        # Arrange
        await add_remote(async_client, "https://github.com/acme/alpha", "alpha")
        program = await create_program(async_client)
        cycle_id = (await claim(async_client, program["id"])).json()["cycle"]["id"]

        # Act: exactly what the preset sends when nothing was left to write
        # the handoff itself.
        response = await async_client.patch(
            f"/api/automation/v1/improve/cycles/{cycle_id}",
            json={
                "status": "FAILED",
                "builder_runtime": "claude-code",
                "error": "claude-code ran out of usage mid-change.",
                "handoff": self.FALLBACK,
            },
        )

        # Assert: stored, and re-aimed. The fallback's branch never reached
        # the remote, so "read the branch diff" is an instruction the next
        # cycle's fresh sandbox cannot carry out; the reasoning is what
        # survives and the record says to redo the work from it.
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["handoff"] is not None
        assert body["handoff"]["reason"] == "runtime_exhausted"
        assert body["handoff"]["branch"] is None
        assert "never pushed" in body["handoff"]["next_steps"][0]
        assert self.FALLBACK["next_steps"][0] in body["handoff"]["next_steps"]

    async def test_the_next_cycle_says_which_one_it_continues(self, async_client):
        # Arrange: one cycle hands over.
        await add_remote(async_client, "https://github.com/acme/beta", "beta")
        program = await create_program(async_client)
        first = (await claim(async_client, program["id"])).json()["cycle"]["id"]
        await async_client.patch(
            f"/api/automation/v1/improve/cycles/{first}",
            json={
                "status": "FAILED",
                "builder_runtime": "claude-code",
                "handoff": self.FALLBACK,
            },
        )

        # Act
        claimed = await claim(async_client, program["id"])

        # Assert: the chain reads in both directions, which is what lets a
        # reader see a handed-over cycle as a chain rather than as two
        # unrelated failures that happen to share a branch.
        assert claimed.status_code == 201, claimed.text
        second = claimed.json()["cycle"]
        assert second["resumed_from_cycle_id"] == first
        resume = claimed.json()["resume"]
        assert resume is not None
        assert self.FALLBACK["next_steps"][0] in resume["next_steps"]
        # Whether the branch is there to continue from, which the runner acts
        # on: the field was absent from `HandoffResume`, which forbids extras,
        # so both the runner and its context renderer fell back to assuming
        # it was pushed and continued a branch that did not exist.
        assert resume["branch_pushed"] is False

        history = await async_client.get(
            f"/api/automation/v1/improve/{program['id']}/cycles"
        )
        by_id = {c["id"]: c for c in history.json()["cycles"]}
        assert by_id[first]["resumed_by_cycle_id"] == second["id"]

    async def test_a_handoff_is_found_however_the_other_program_was_set_up(
        self, async_client
    ):
        """One repository, two configurations, one piece of saved work.

        "Same repository" was the remote's id when the claimant had one and
        the label otherwise — so a handoff written by a program that lists
        its repositories by hand was invisible to a git-synced program, and
        the next cycle restarted work that was already on a branch. Milestone
        4 counts that: "zero cycles that redo work a prior incomplete handoff
        already covered".
        """
        # Arrange: a hand-configured program hands over, and a git-synced
        # program is next to take the same repository.
        url = "https://github.com/acme/shared"
        listed = await create_program(
            async_client,
            repository_source="explicit",
            repositories=[{"label": "shared", "url": url, "ref": "main"}],
        )
        first = (await claim(async_client, listed["id"])).json()["cycle"]["id"]
        await async_client.patch(
            f"/api/automation/v1/improve/cycles/{first}",
            json={
                "status": "FAILED",
                "builder_runtime": "claude-code",
                "handoff": self.FALLBACK,
            },
        )
        await add_remote(async_client, url + ".git", "shared")
        synced = await create_program(async_client)

        # Act
        claimed = await claim(async_client, synced["id"])

        # Assert: it continues the work rather than starting again.
        assert claimed.status_code == 201, claimed.text
        assert claimed.json()["cycle"]["resumed_from_cycle_id"] == first
        assert self.FALLBACK["next_steps"][0] in claimed.json()["resume"]["next_steps"]

    async def test_a_handoff_that_saved_its_work_is_offered_as_a_branch(
        self, async_client
    ):
        """The other half of the same rule, so the cure cannot be worse.

        When the branch is on the remote there is a commit to carry on from,
        and re-aiming the record at redoing the work would throw away exactly
        what the handoff managed to preserve.
        """
        # Arrange
        await add_remote(async_client, "https://github.com/acme/gamma", "gamma")
        program = await create_program(async_client)
        first = (await claim(async_client, program["id"])).json()["cycle"]["id"]
        await async_client.patch(
            f"/api/automation/v1/improve/cycles/{first}",
            json={
                "status": "FAILED",
                "builder_runtime": "claude-code",
                "handoff": {
                    **self.FALLBACK,
                    "branch_pushed": True,
                    "head_sha": "a1b2c3d4e5f6",
                },
            },
        )

        # Act
        resume = (await claim(async_client, program["id"])).json()["resume"]

        # Assert
        assert resume["branch_pushed"] is True
        assert resume["branch"] == self.FALLBACK["branch"]
        assert resume["next_steps"] == self.FALLBACK["next_steps"]

    async def test_work_abandoned_to_a_timeout_is_resumed_like_any_other(
        self, async_client
    ):
        """A builder can stop mid-change for want of time as well as quota.

        Only a usage limit used to be handed over; a run that exceeded its
        time re-raised, and the branch it had already committed to was left
        for nobody — which is exactly the discarded work the handoff exists to
        prevent. The reason differs, what happens next does not.
        """
        # Arrange
        await add_remote(async_client, "https://github.com/acme/gamma", "gamma")
        program = await create_program(async_client)
        first = (await claim(async_client, program["id"])).json()["cycle"]["id"]
        await async_client.patch(
            f"/api/automation/v1/improve/cycles/{first}",
            json={
                "status": "FAILED",
                "builder_runtime": "claude-code",
                "error": "claude-code ran out of time mid-change.",
                "handoff": {**self.FALLBACK, "reason": "timed_out"},
            },
        )

        # Act
        claimed = await claim(async_client, program["id"])

        # Assert
        assert claimed.status_code == 201, claimed.text
        assert claimed.json()["cycle"]["resumed_from_cycle_id"] == first
        assert claimed.json()["resume"]["reason"] == "timed_out"

    async def test_a_timeout_does_not_report_the_runtime_as_spent(self, async_client):
        # Running out of time says nothing about a runtime's remaining quota.
        # Recording it as exhausted would park a healthy runtime and send the
        # next cycle to a different one for no reason.
        await add_remote(async_client, "https://github.com/acme/delta", "delta")
        program = await create_program(async_client)
        cycle_id = (await claim(async_client, program["id"])).json()["cycle"]["id"]

        await async_client.patch(
            f"/api/automation/v1/improve/cycles/{cycle_id}",
            json={
                "status": "FAILED",
                "builder_runtime": "claude-code",
                "handoff": {**self.FALLBACK, "reason": "timed_out"},
                "runtime_observations": [],
            },
        )
        usage = await async_client.get("/api/automation/v1/improve/runtime-usage")

        assert usage.status_code == 200, usage.text
        spent = [
            entry["runtime"]
            for entry in usage.json()["runtimes"]
            if not entry["available"]
        ]
        assert spent == []


class TestTheReviewRecordThePresetSends:
    """The review the runner builds must be the review the service accepts.

    `CycleReview` forbids extra fields, the runner assembles its review record
    in `sdk_main.py` — which cannot be imported by a test — and nothing
    compared the two. So a field added to the record and not to the schema
    made *every cycle with a reviewer* fail its final report: a 422 on the last
    PATCH left the cycle in REVIEWING, and the janitor closed it as FAILED
    with "its run ended as COMPLETED". The work was done, reviewed and
    accepted, and the record said it failed.
    """

    async def test_every_field_the_runner_sends_is_accepted(self, async_client):
        # Arrange
        await add_remote(async_client, "https://github.com/acme/alpha", "alpha")
        program = await create_program(async_client)
        cycle_id = (await claim(async_client, program["id"])).json()["cycle"]["id"]

        # Act: the shape `sdk_main.py` builds, field for field.
        response = await async_client.patch(
            f"/api/automation/v1/improve/cycles/{cycle_id}",
            json={
                "status": "COMPLETED",
                "verdict": "ACCEPTED",
                "reviews": [
                    {
                        "profile_id": REVIEWER,
                        "profile_name": "codex",
                        "runtime": "codex",
                        "runtime_switches": [
                            {
                                "from": "claude-code",
                                "to": "codex",
                                "reason": "usage_limit",
                                "at": "2026-10-24T17:00:00+00:00",
                            }
                        ],
                        "conversation_id": "conv-review",
                        "verdict": "ACCEPTED",
                        "failed": False,
                        "summary": "Additive only.",
                        "concerns": [],
                        "stale_sources": [],
                        "verified_sources": ["https://example.test/doc"],
                        "updates_checked": "Nothing supersedes it.",
                        "checks": [{"command": "pytest", "passed": True}],
                    }
                ],
                "gate_decisions": [
                    {
                        "phase": "publish",
                        "route": "approve",
                        "trigger": "asked the service",
                        "rationale": "additive publishes on its own account",
                        "decided_by": "service",
                    }
                ],
                "runtime_observations": [
                    {
                        "runtime": "codex",
                        "outcome": "ok",
                        "observed_at": "2026-10-24T17:30:00+00:00",
                    }
                ],
                "injected_lesson_ids": [],
            },
        )

        # Assert: accepted, and the cycle actually reaches COMPLETED — which is
        # what keeps the janitor from closing it as an abandonment.
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "COMPLETED"
        assert body["verdict"] == "ACCEPTED"
        assert body["reviews"][0]["runtime_switches"][0]["to"] == "codex"
        assert body["reaped"] is False

    async def test_a_reviewer_that_switched_is_recorded_as_the_one_that_finished(
        self, async_client
    ):
        # `runtime` is whichever finished, so without the route the record
        # says a runtime reviewed a change it only reviewed part of.
        await add_remote(async_client, "https://github.com/acme/beta", "beta")
        program = await create_program(async_client)
        cycle_id = (await claim(async_client, program["id"])).json()["cycle"]["id"]

        response = await async_client.patch(
            f"/api/automation/v1/improve/cycles/{cycle_id}",
            json={
                "reviews": [
                    {
                        "runtime": "codex",
                        "runtime_switches": [
                            {
                                "from": "claude-code",
                                "to": "codex",
                                "reason": "usage_limit",
                            }
                        ],
                        "verdict": "ACCEPTED",
                    }
                ]
            },
        )

        assert response.status_code == 200, response.text
        review = response.json()["reviews"][0]
        assert review["runtime"] == "codex"
        assert review["runtime_switches"][0]["from"] == "claude-code"

    async def test_a_rejected_report_can_still_be_landed_with_its_reason(
        self, async_client
    ):
        # The runner's recovery path after a rejection: drop everything that
        # could have caused it and land the terminal state with the reason, so
        # the cycle is a completed one with a gap rather than an abandoned one.
        await add_remote(async_client, "https://github.com/acme/gamma", "gamma")
        program = await create_program(async_client)
        cycle_id = (await claim(async_client, program["id"])).json()["cycle"]["id"]

        rejected = await async_client.patch(
            f"/api/automation/v1/improve/cycles/{cycle_id}",
            json={"reviews": [{"verdict": "ACCEPTED", "invented_field": 1}]},
        )
        recovery = await async_client.patch(
            f"/api/automation/v1/improve/cycles/{cycle_id}",
            json={
                "status": "COMPLETED",
                "verdict": "ACCEPTED",
                "error": "the service rejected its full report: 422",
            },
        )

        assert rejected.status_code == 422
        assert recovery.status_code == 200, recovery.text
        body = recovery.json()
        assert body["status"] == "COMPLETED"
        assert "rejected its full report" in body["error"]
