"""Caps, the runtime ledger, and the guard that stops a program stacking runs.

Continuous improvement is only safe to leave running if the brakes are
provably there. These are the brakes.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from openhands.automation.config import clear_config_cache
from openhands.automation.improve.caps import (
    cycles_today,
    local_day_bounds,
    repository_identity,
)
from openhands.automation.improve.usage import (
    DEFAULT_COOLDOWN,
    RuntimeObservation,
    choose_by_headroom,
    observed_runtimes,
    record_observations,
    runtime_statuses,
)
from openhands.automation.models import (
    Automation,
    AutomationGitSyncRemote,
    AutomationImproveCycle,
    AutomationRun,
    AutomationRunStatus,
    ImproveCycleStatus,
    RuntimeUsageState,
)
from openhands.automation.scheduler import poll_and_schedule


ORG_ID = uuid.UUID("87654321-4321-8765-4321-876543218765")
USER_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")


@pytest.fixture(autouse=True)
def local_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOMATION_AGENT_SERVER_URL", "http://localhost:3000")
    monkeypatch.setenv("AUTOMATION_WORKSPACE_BASE", str(tmp_path))
    clear_config_cache()
    yield
    clear_config_cache()


# --- the daily cap ----------------------------------------------------------


def test_a_day_is_the_programs_own_day_not_utcs() -> None:
    # Arrange: three in the morning UTC, which is the previous evening in New
    # York — so a nightly program there is still inside yesterday.
    moment = datetime(2026, 10, 24, 3, 0, tzinfo=UTC)

    # Act
    start, end = local_day_bounds("America/New_York", moment)

    # Assert: the cap resets when the program's day does, not when UTC's does.
    assert start == datetime(2026, 10, 23, 4, 0, tzinfo=UTC)
    assert end == datetime(2026, 10, 24, 4, 0, tzinfo=UTC)


def test_a_zone_nobody_knows_falls_back_rather_than_failing_a_claim() -> None:
    # Act
    start, _ = local_day_bounds("Mars/Olympus", datetime(2026, 10, 24, 3, tzinfo=UTC))

    # Assert: a claim must not fail over the spelling of a timezone.
    assert start == datetime(2026, 10, 24, 0, 0, tzinfo=UTC)


def test_a_relabelled_repository_does_not_get_a_fresh_allowance() -> None:
    # Arrange
    remote = uuid.uuid4()
    url = "https://github.com/zeroday-ninja/hazard"

    # Act / Assert: a label is a display name and the live history shows one
    # repository carrying two of them in a day, so identity must not follow it.
    assert repository_identity(remote, "hazard", url) == repository_identity(
        remote, "HAZARD", url
    )
    # With no url to read, the remote's id is what is left — and it is still
    # better than the label for the same reason.
    assert repository_identity(remote, "hazard", None) == repository_identity(
        remote, "HAZARD", None
    )
    assert repository_identity(None, "hazard", None) != repository_identity(
        None, "pairwatch", None
    )


@pytest.mark.asyncio
async def test_cycles_are_counted_across_programs_not_per_program(
    async_session,
) -> None:
    # Arrange: two programs took the same repository today.
    programs = []
    for name in ("Nightly", "Hourly"):
        program = Automation(
            id=uuid.uuid4(),
            org_id=ORG_ID,
            user_id=USER_ID,
            name=name,
            trigger={"type": "cron", "schedule": "0 3 * * *", "timezone": "UTC"},
            enabled=True,
            tarball_path="uploads/test.tar.gz",
            entrypoint="python main.py",
        )
        async_session.add(program)
        programs.append(program)
    await async_session.flush()
    for index in range(3):
        async_session.add(
            AutomationImproveCycle(
                id=uuid.uuid4(),
                automation_id=programs[index % 2].id,
                org_id=ORG_ID,
                repo_index=0,
                repo_label="hazard",
                repo_url="https://example.invalid/hazard",
                status=ImproveCycleStatus.COMPLETED,
            )
        )
    await async_session.commit()

    # Act
    taken = await cycles_today(
        async_session,
        ORG_ID,
        remote_id=None,
        label="hazard",
        url="https://example.invalid/hazard",
        timezone_name="UTC",
    )

    # Assert: three programs each taking their own allowance of one repository
    # is the runaway the cap exists to prevent.
    assert taken == 3


@pytest.mark.asyncio
async def test_one_repository_is_one_allowance_however_it_was_configured(
    async_session,
) -> None:
    """Two spellings of the same repository are not two repositories.

    Identity was "the remote's id, or the label" — and those are disjoint
    buckets, because the label branch only counts rows whose remote id is
    null. A repository reached through a git-sync remote by one program and
    listed by hand in another therefore had **two** allowances.

    Observed live on 2026-10-25: `HAZARD` took 20 cycles against a cap of 10
    — ten under `remote_id b97fd366…` from the rotating program and ten under
    the label `HAZARD` from a program configured explicitly. Milestone 4's
    exit says no repository exceeds its cap.
    """
    # Arrange: the same repository, reached both ways, five cycles each.
    program = Automation(
        id=uuid.uuid4(),
        org_id=ORG_ID,
        user_id=USER_ID,
        name="Rotating",
        trigger={"type": "cron", "schedule": "0 3 * * *", "timezone": "UTC"},
        enabled=True,
        tarball_path="uploads/test.tar.gz",
        entrypoint="python main.py",
    )
    async_session.add(program)
    synced = uuid.uuid4()
    async_session.add(
        AutomationGitSyncRemote(id=synced, org_id=ORG_ID, is_default=True)
    )
    await async_session.flush()
    for index in range(10):
        from_git_sync = index % 2 == 0
        async_session.add(
            AutomationImproveCycle(
                id=uuid.uuid4(),
                automation_id=program.id,
                org_id=ORG_ID,
                repo_index=0,
                # The spellings the two configuration styles actually produce:
                # git-sync stores the remote's url and uses its own id, an
                # explicit entry carries a label and no id.
                repo_label=(
                    "https://github.com/zeroday-ninja/HAZARD"
                    if from_git_sync
                    else "HAZARD"
                ),
                repo_url=(
                    "https://github.com/zeroday-ninja/HAZARD"
                    if from_git_sync
                    else "https://github.com/zeroday-ninja/HAZARD.git"
                ),
                remote_id=synced if from_git_sync else None,
                status=ImproveCycleStatus.COMPLETED,
            )
        )
    await async_session.commit()

    # Act: asked as either program would ask.
    as_synced = await cycles_today(
        async_session,
        ORG_ID,
        remote_id=synced,
        label="https://github.com/zeroday-ninja/HAZARD",
        url="https://github.com/zeroday-ninja/HAZARD",
        timezone_name="UTC",
    )
    as_listed = await cycles_today(
        async_session,
        ORG_ID,
        remote_id=None,
        label="HAZARD",
        url="git@github.com:zeroday-ninja/HAZARD.git",
        timezone_name="UTC",
    )

    # Assert: one repository, one day's allowance, whichever program asks.
    assert as_synced == 10
    assert as_listed == 10


def test_the_same_repository_written_four_ways_is_one_repository() -> None:
    # The spellings a configuration actually arrives in, including a url
    # carrying a token — which git-sync stores and which must not split a
    # repository's allowance in two.
    same = {
        repository_identity(None, "HAZARD", url)
        for url in (
            "https://github.com/zeroday-ninja/HAZARD",
            "https://github.com/zeroday-ninja/HAZARD.git",
            "http://github.com/zeroday-ninja/hazard/",
            "git@github.com:zeroday-ninja/HAZARD.git",
            "ssh://git@github.com:22/zeroday-ninja/HAZARD",
            "https://x-access-token:ghs_secret@github.com/zeroday-ninja/HAZARD",
        )
    }

    assert len(same) == 1
    # And a different repository on the same host stays different.
    assert repository_identity(None, "a", "https://github.com/o/pairwatch") not in same


# --- the runtime ledger -----------------------------------------------------


@pytest.mark.asyncio
async def test_a_runtime_nobody_has_used_is_unknown_not_healthy(
    async_session,
) -> None:
    # Act
    statuses = await runtime_statuses(async_session, ORG_ID, ["codex"])

    # Assert: still selectable — something has to go first — but silence is
    # recorded as silence.
    assert statuses["codex"].state is RuntimeUsageState.UNKNOWN
    assert statuses["codex"].available is True


@pytest.mark.asyncio
async def test_an_exhausted_runtime_comes_back_by_itself_at_its_reset(
    async_session,
) -> None:
    # Arrange
    seen = datetime.now(UTC)
    await record_observations(
        async_session,
        ORG_ID,
        [
            RuntimeObservation(
                runtime="claude-code",
                outcome="exhausted",
                observed_at=seen,
                reset_at=seen + timedelta(hours=3),
                reset_source="notice_text",
            )
        ],
    )
    await async_session.commit()

    # Act: before and after the reset, read against the clock.
    before = await runtime_statuses(
        async_session, ORG_ID, ["claude-code"], now=seen + timedelta(hours=1)
    )
    after = await runtime_statuses(
        async_session, ORG_ID, ["claude-code"], now=seen + timedelta(hours=4)
    )

    # Assert: recovery is derived at read time, so a restart cannot drop it
    # and an outage cannot leave the runtime pinned.
    assert before["claude-code"].available is False
    assert after["claude-code"].available is True


@pytest.mark.asyncio
async def test_without_a_stated_reset_a_short_cooldown_is_assumed(
    async_session,
) -> None:
    # Arrange: a refusal that named no time.
    seen = datetime.now(UTC)
    await record_observations(
        async_session,
        ORG_ID,
        [RuntimeObservation(runtime="codex", outcome="exhausted", observed_at=seen)],
    )
    await async_session.commit()

    # Act
    statuses = await runtime_statuses(
        async_session, ORG_ID, ["codex"], now=seen + DEFAULT_COOLDOWN
    )

    # Assert: short on purpose. Guessing too long wastes a day of a runtime
    # that already recovered; too short costs one refused prompt.
    assert statuses["codex"].available is True


@pytest.mark.asyncio
async def test_a_request_that_worked_beats_an_estimated_reset(
    async_session,
) -> None:
    # Arrange: exhausted, with a reset hours away.
    seen = datetime.now(UTC)
    await record_observations(
        async_session,
        ORG_ID,
        [
            RuntimeObservation(
                runtime="codex",
                outcome="exhausted",
                observed_at=seen,
                reset_at=seen + timedelta(hours=5),
            ),
            RuntimeObservation(
                runtime="codex", outcome="ok", observed_at=seen + timedelta(minutes=5)
            ),
        ],
    )
    await async_session.commit()

    # Act
    statuses = await runtime_statuses(
        async_session, ORG_ID, ["codex"], now=seen + timedelta(minutes=10)
    )

    # Assert: an estimate that disagrees with a request that just worked is
    # simply wrong.
    assert statuses["codex"].available is True
    assert statuses["codex"].state is RuntimeUsageState.AVAILABLE


@pytest.mark.asyncio
async def test_the_least_spent_available_runtime_goes_next(
    async_session,
) -> None:
    """The headroom bias, against the cycles that actually ran.

    This used to seed the counts with `note_attempt`, which no production
    path calls — so it proved the bias worked on numbers nothing produced.
    Today's counts come from the cycle table now, which is what a real
    rotation leaves behind.
    """
    # Arrange: the first choice has already done three cycles today.
    await _cycles_built_by(async_session, ["claude-code"] * 3 + ["codex"])
    statuses = await runtime_statuses(async_session, ORG_ID, ["claude-code", "codex"])

    # Act
    chosen = choose_by_headroom(["claude-code", "codex"], statuses)

    # Assert: without this bias, a program with one builder configured spends
    # one subscription several times faster than the other — which is exactly
    # what the recorded history showed.
    assert chosen == "codex"


async def _cycles_built_by(
    session, runtimes, *, status=ImproveCycleStatus.COMPLETED
) -> Automation:
    """Cycles on one program, one per runtime named, created now."""
    automation = Automation(
        id=uuid.uuid4(),
        user_id=USER_ID,
        org_id=ORG_ID,
        name="rotating",
        trigger={"type": "cron", "schedule": "* * * * *", "timezone": "UTC"},
        enabled=True,
        tarball_path="uploads/test.tar.gz",
        entrypoint="python main.py",
    )
    session.add(automation)
    await session.flush()
    for runtime in runtimes:
        session.add(
            AutomationImproveCycle(
                id=uuid.uuid4(),
                automation_id=automation.id,
                org_id=ORG_ID,
                repo_index=0,
                repo_label="hazard",
                repo_url="https://example.invalid/hazard",
                status=status,
                builder_runtime=runtime,
            )
        )
    await session.commit()
    return automation


@pytest.mark.asyncio
async def test_cycles_today_counts_what_ran_not_what_was_written_down(
    async_session,
) -> None:
    """`0/0 cycles today`, for every runtime, always.

    The ledger's two counters were incremented by `note_attempt`, which has no
    production caller, and `cycles_completed` had no writer at all — so the
    one surface a person asks "how much has this runtime done today" from
    could only ever answer zero. The cycle table already knew.
    """
    # Arrange: two finished, one still going.
    await _cycles_built_by(async_session, ["codex", "codex"])
    await _cycles_built_by(
        async_session, ["codex"], status=ImproveCycleStatus.REVIEWING
    )

    # Act
    statuses = await runtime_statuses(async_session, ORG_ID, ["codex"])

    # Assert: attempted counts the one in flight, because it has already
    # spent something; completed does not, because it has not finished.
    assert statuses["codex"].cycles_attempted == 3
    assert statuses["codex"].cycles_completed == 2


@pytest.mark.asyncio
async def test_a_runtime_that_names_nothing_is_not_offered_as_available(
    async_session,
) -> None:
    """`custom available 0/0 cycles today` is an offer you cannot take.

    A profile with a custom launch command records the literal "custom", so
    the ledger grows a row for it. It is already kept out of the share for
    the same reason: it is what a lookup says when it could not tell, and
    several real runtimes land under the one word.
    """
    # Arrange
    seen = datetime.now(UTC)
    await record_observations(
        async_session,
        ORG_ID,
        [
            RuntimeObservation(runtime=name, outcome="ok", observed_at=seen)
            for name in ("codex", "custom")
        ],
    )
    await async_session.commit()

    # Act
    observed = await observed_runtimes(async_session, ORG_ID)

    # Assert
    assert observed == ["codex"]


@pytest.mark.asyncio
async def test_an_exhausted_runtime_is_never_chosen(async_session) -> None:
    # Arrange
    seen = datetime.now(UTC)
    await record_observations(
        async_session,
        ORG_ID,
        [
            RuntimeObservation(
                runtime="codex",
                outcome="exhausted",
                observed_at=seen,
                reset_at=seen + timedelta(hours=5),
            )
        ],
    )
    await async_session.commit()
    statuses = await runtime_statuses(async_session, ORG_ID, ["codex", "claude-code"])

    # Act
    chosen = choose_by_headroom(["codex", "claude-code"], statuses)

    # Assert
    assert chosen == "claude-code"


@pytest.mark.asyncio
async def test_when_everything_is_exhausted_nothing_is_chosen(
    async_session,
) -> None:
    # Arrange
    seen = datetime.now(UTC)
    await record_observations(
        async_session,
        ORG_ID,
        [
            RuntimeObservation(
                runtime=name,
                outcome="exhausted",
                observed_at=seen,
                reset_at=seen + timedelta(hours=5),
            )
            for name in ("codex", "claude-code")
        ],
    )
    await async_session.commit()
    statuses = await runtime_statuses(async_session, ORG_ID, ["codex", "claude-code"])

    # Assert: the caller has to handle "nobody can work right now" rather than
    # being handed a runtime that will refuse.
    assert choose_by_headroom(["codex", "claude-code"], statuses) is None


# --- the overlap guard ------------------------------------------------------


@pytest.mark.asyncio
async def test_a_program_with_a_run_in_flight_does_not_start_another(
    async_session, async_session_factory
) -> None:
    # Arrange: a program due every minute, with a run already going.
    automation = Automation(
        id=uuid.uuid4(),
        org_id=ORG_ID,
        user_id=USER_ID,
        name="Continuous improvement",
        trigger={"type": "cron", "schedule": "* * * * *", "timezone": "UTC"},
        enabled=True,
        tarball_path="uploads/test.tar.gz",
        entrypoint="python main.py",
        created_at=datetime.now(UTC) - timedelta(hours=1),
    )
    async_session.add(automation)
    async_session.add(
        AutomationRun(
            id=uuid.uuid4(),
            automation_id=automation.id,
            status=AutomationRunStatus.RUNNING,
            trigger_source="cron",
        )
    )
    await async_session.commit()

    # Act
    await poll_and_schedule(async_session_factory)

    # Assert: a cycle takes tens of minutes, so without this a program firing
    # every minute stacks runs on itself until something gives way. And the
    # cursor must not advance for a tick that created nothing.
    runs = (
        await async_session.execute(
            AutomationRun.__table__.select().where(
                AutomationRun.automation_id == automation.id
            )
        )
    ).all()
    assert len(runs) == 1
    await async_session.refresh(automation)
    assert automation.last_triggered_at is None


# --- the handoff, through the claim -----------------------------------------


@pytest.mark.asyncio
async def test_exactly_one_cycle_picks_up_a_handoff(async_session) -> None:
    """Two overlapping runs must not both continue the same work."""
    from openhands.automation.improve.config import ImproveProgramConfig
    from openhands.automation.improve.rotation import claim_next_cycle

    # Arrange: a program with one repository, and a cycle that left work.
    automation = Automation(
        id=uuid.uuid4(),
        org_id=ORG_ID,
        user_id=USER_ID,
        name="Nightly improvement",
        trigger={"type": "cron", "schedule": "0 3 * * *", "timezone": "UTC"},
        enabled=True,
        tarball_path="uploads/test.tar.gz",
        entrypoint="python main.py",
    )
    async_session.add(automation)
    await async_session.flush()
    abandoned = AutomationImproveCycle(
        id=uuid.uuid4(),
        automation_id=automation.id,
        org_id=ORG_ID,
        repo_index=0,
        repo_label="hazard",
        repo_url="https://example.invalid/hazard",
        status=ImproveCycleStatus.FAILED,
        handoff={
            "status": "incomplete",
            "branch": "improve/20261024",
            "branch_pushed": True,
            "head_sha": "a1b2c3d",
            "next_steps": ["finish the regression test"],
        },
    )
    async_session.add(abandoned)
    await async_session.flush()

    config = ImproveProgramConfig(
        builder_profile_id=uuid.uuid4(),
        repository_source="explicit",
        repositories=[{"url": "https://example.invalid/hazard", "label": "hazard"}],
    )

    # Act: two claims in a row, as two overlapping runs would do.
    first, _ = await claim_next_cycle(async_session, automation, config, None)
    second, _ = await claim_next_cycle(async_session, automation, config, None)
    await async_session.commit()

    # Assert: the first takes it and the second does not. The write that
    # marks it taken is what makes the handoff exactly-once.
    assert first.resumed_from_cycle_id == abandoned.id
    assert second.resumed_from_cycle_id is None
    await async_session.refresh(abandoned)
    assert abandoned.resumed_by_cycle_id == first.id


class TestReadingTheLedger:
    """GET /v1/improve/runtime-usage — what a person asking "what is spent" gets.

    The ledger existed from the start and nothing could read it: it was
    written by cycles and read only by the claim, against the runtimes one
    program happens to be configured with. A person has no such list.
    """

    USAGE = "/api/automation/v1/improve/runtime-usage"

    async def test_a_ledger_nobody_has_written_to_is_empty_not_wrong(
        self, async_client
    ):
        response = await async_client.get(self.USAGE)

        assert response.status_code == 200, response.text
        assert response.json() == {"runtimes": [], "earliest_reset": None}

    async def test_an_exhaustion_comes_back_with_its_evidence(
        self, async_client, async_session
    ):
        # Arrange: what a cycle reports when a runtime refuses.
        await record_observations(
            async_session,
            ORG_ID,
            [
                RuntimeObservation(
                    runtime="claude-code",
                    outcome="exhausted",
                    observed_at=datetime.now(UTC),
                    reset_at=datetime.now(UTC) + timedelta(hours=5),
                    reset_source="resets 3pm (America/New_York)",
                    evidence="You've hit your limit",
                    conversation_id="conv-1",
                ),
                RuntimeObservation(
                    runtime="codex",
                    outcome="ok",
                    observed_at=datetime.now(UTC),
                ),
            ],
        )
        await async_session.commit()

        # Act
        response = await async_client.get(self.USAGE)

        # Assert: a reader deciding whether to trust an exhaustion needs what
        # was seen, not only the conclusion.
        assert response.status_code == 200, response.text
        body = response.json()
        by_runtime = {entry["runtime"]: entry for entry in body["runtimes"]}
        spent = by_runtime["claude-code"]
        assert spent["available"] is False
        assert spent["state"] == "EXHAUSTED"
        assert spent["last_observation"]["evidence"] == "You've hit your limit"
        assert spent["reset_source"] == "resets 3pm (America/New_York)"
        assert spent["consecutive_exhaustions"] == 1
        assert by_runtime["codex"]["available"] is True
        # So a caller can say "nothing until then" rather than making the
        # reader compare timestamps.
        assert body["earliest_reset"] == spent["reset_at"]

    async def test_a_reset_that_has_passed_reads_as_available(
        self, async_client, async_session
    ):
        # Recovery is derived at read time, so a restart cannot drop the
        # transition and nothing has to be scheduled to flip it.
        await record_observations(
            async_session,
            ORG_ID,
            [
                RuntimeObservation(
                    runtime="claude-code",
                    outcome="exhausted",
                    observed_at=datetime.now(UTC) - timedelta(hours=9),
                    reset_at=datetime.now(UTC) - timedelta(hours=1),
                )
            ],
        )
        await async_session.commit()

        response = await async_client.get(self.USAGE)

        entry = response.json()["runtimes"][0]
        assert entry["available"] is True
        assert entry["reset_at"] is None
        # The exhaustion itself is still on the record: it happened.
        assert entry["last_exhausted_at"] is not None
