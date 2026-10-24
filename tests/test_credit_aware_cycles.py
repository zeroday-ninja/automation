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
    note_attempt,
    record_observations,
    runtime_statuses,
)
from openhands.automation.models import (
    Automation,
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

    # Act / Assert: identity follows the remote when there is one, because a
    # rename would otherwise hand the same repository a second allowance —
    # which the live history shows happening.
    assert repository_identity(remote, "hazard") == repository_identity(
        remote, "HAZARD"
    )
    assert repository_identity(None, "hazard") != repository_identity(None, "pairwatch")


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
        timezone_name="UTC",
    )

    # Assert: three programs each taking their own allowance of one repository
    # is the runaway the cap exists to prevent.
    assert taken == 3


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
    # Arrange: the first choice has already done three cycles today.
    for _ in range(3):
        await note_attempt(async_session, ORG_ID, "claude-code")
    await note_attempt(async_session, ORG_ID, "codex")
    await async_session.commit()
    statuses = await runtime_statuses(async_session, ORG_ID, ["claude-code", "codex"])

    # Act
    chosen = choose_by_headroom(["claude-code", "codex"], statuses)

    # Assert: without this bias, a program with one builder configured spends
    # one subscription several times faster than the other — which is exactly
    # what the recorded history showed.
    assert chosen == "codex"


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
