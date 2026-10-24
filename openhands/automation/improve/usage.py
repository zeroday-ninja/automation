"""What is known about each runtime's remaining usage, and what to do with it.

The agent runtimes expose no credit balance. The protocol carries token counts
and a cost that is zero on a subscription, and nothing else: no remaining
quota, no reset time, no rate-limit header. So "is this runtime available"
cannot be read — it can only be *inferred* from what happened last time
something asked it, which is what this module keeps.

Two rules shape it:

- **Recovery is derived, never scheduled.** A row is written only when
  something is observed; whether a runtime is available again is decided
  against the clock when the row is read. A restart therefore cannot drop a
  transition, and a long outage cannot leave a runtime pinned to exhausted.
- **Silence is not health.** A runtime nobody has used is ``UNKNOWN``, not
  available. It is still *selectable* — you have to try something — but the
  difference is visible to anyone reading the ledger.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from openhands.automation.models import (
    AutomationImproveCycle,
    AutomationRuntimeUsage,
    RuntimeUsageState,
)


#: How long a runtime is presumed exhausted when nothing said otherwise.
#:
#: Deliberately shorter than any real window. The costs are asymmetric: too
#: long wastes a day of a runtime that already recovered, too short costs one
#: refused prompt and corrects itself on the next observation. Claude's real
#: windows are five hours and a week; Codex's are five hours and a week too.
DEFAULT_COOLDOWN = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class RuntimeObservation:
    """Something that was actually seen about a runtime."""

    runtime: str
    #: ``ok`` — a request succeeded. ``exhausted`` — a request was refused for
    #: usage reasons. ``failed_over_from`` — this runtime was abandoned
    #: mid-conversation, which is the honest meaning of "nearly out".
    outcome: str
    observed_at: datetime
    reset_at: datetime | None = None
    reset_source: str | None = None
    evidence: str | None = None
    conversation_id: str | None = None


@dataclass(frozen=True, slots=True)
class RuntimeStatus:
    """A runtime as it stands right now, read against the clock."""

    runtime: str
    state: RuntimeUsageState
    available: bool
    reset_at: datetime | None
    cycles_attempted: int
    cycles_completed: int


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def _row(
    session: AsyncSession, org_id: uuid.UUID, runtime: str
) -> AutomationRuntimeUsage:
    existing = (
        await session.execute(
            select(AutomationRuntimeUsage).where(
                AutomationRuntimeUsage.org_id == org_id,
                AutomationRuntimeUsage.runtime == runtime,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    created = AutomationRuntimeUsage(
        id=uuid.uuid4(),
        org_id=org_id,
        runtime=runtime,
        state=RuntimeUsageState.UNKNOWN,
        window_started_at=datetime.now(UTC),
    )
    session.add(created)
    await session.flush()
    return created


async def record_observations(
    session: AsyncSession,
    org_id: uuid.UUID,
    observations: Iterable[RuntimeObservation],
) -> None:
    """Write down what was seen. Never infers; only records.

    A success clears an exhaustion outright, including one whose estimated
    reset has not arrived: an estimate that disagrees with a request that just
    worked is simply wrong.
    """
    for observation in observations:
        row = await _row(session, org_id, observation.runtime)
        seen = _utc(observation.observed_at) or datetime.now(UTC)
        if observation.outcome == "ok":
            row.state = RuntimeUsageState.AVAILABLE
            row.observed_reset_at = None
            row.reset_source = None
            row.consecutive_exhaustions = 0
            row.last_success_at = seen
        elif observation.outcome in ("exhausted", "failed_over_from"):
            row.state = RuntimeUsageState.EXHAUSTED
            row.last_exhausted_at = seen
            row.consecutive_exhaustions += 1
            row.observed_reset_at = _utc(observation.reset_at) or (
                seen + DEFAULT_COOLDOWN
            )
            row.reset_source = observation.reset_source or "default_cooldown"
        row.last_observation = {
            "outcome": observation.outcome,
            "observed_at": seen.isoformat(),
            "evidence": (observation.evidence or "")[:500] or None,
            "conversation_id": observation.conversation_id,
            "reset_source": observation.reset_source,
        }
        row.updated_at = datetime.now(UTC)


def _rolled_over(row: AutomationRuntimeUsage, now: datetime) -> bool:
    """True when the row's counters belong to a day that has passed."""
    started = _utc(row.window_started_at) or now
    return started.date() != now.date()


async def runtime_statuses(
    session: AsyncSession,
    org_id: uuid.UUID,
    runtimes: Sequence[str],
    *,
    now: datetime | None = None,
) -> dict[str, RuntimeStatus]:
    """Each runtime as it stands, with recovery applied at read time."""
    moment = now or datetime.now(UTC)
    rows = {
        row.runtime: row
        for row in (
            await session.execute(
                select(AutomationRuntimeUsage).where(
                    AutomationRuntimeUsage.org_id == org_id,
                    AutomationRuntimeUsage.runtime.in_(list(runtimes)),
                )
            )
        )
        .scalars()
        .all()
    }

    statuses: dict[str, RuntimeStatus] = {}
    for runtime in runtimes:
        row = rows.get(runtime)
        if row is None:
            # Never observed. Selectable — something has to go first — but
            # recorded as unknown rather than dressed up as healthy.
            statuses[runtime] = RuntimeStatus(
                runtime=runtime,
                state=RuntimeUsageState.UNKNOWN,
                available=True,
                reset_at=None,
                cycles_attempted=0,
                cycles_completed=0,
            )
            continue

        reset_at = _utc(row.observed_reset_at)
        recovered = reset_at is not None and moment >= reset_at
        state = row.state
        if state is RuntimeUsageState.EXHAUSTED and recovered:
            state = RuntimeUsageState.AVAILABLE
            reset_at = None
        attempted = 0 if _rolled_over(row, moment) else row.cycles_attempted
        completed = 0 if _rolled_over(row, moment) else row.cycles_completed
        statuses[runtime] = RuntimeStatus(
            runtime=runtime,
            state=state,
            available=state is not RuntimeUsageState.EXHAUSTED,
            reset_at=reset_at,
            cycles_attempted=attempted,
            cycles_completed=completed,
        )
    return statuses


async def note_attempt(session: AsyncSession, org_id: uuid.UUID, runtime: str) -> None:
    """Count a cycle this runtime is about to build.

    Counted before the work rather than after, because the point of the count
    is to spread work across subscriptions, and a runtime that attempted and
    failed has still spent something.
    """
    row = await _row(session, org_id, runtime)
    now = datetime.now(UTC)
    if _rolled_over(row, now):
        row.window_started_at = now
        row.cycles_attempted = 0
        row.cycles_completed = 0
    row.cycles_attempted += 1
    row.updated_at = now


def choose_by_headroom(
    order: Sequence[str],
    statuses: dict[str, RuntimeStatus],
) -> str | None:
    """The runtime to use next: available, and least spent today.

    Preference order still decides ties, so a configured rotation is honoured
    — but between two available runtimes the one that has done less today goes
    first. Without that bias a program with one builder configured spends one
    subscription several times faster than the other, which is exactly what
    the live data showed before this existed.
    """
    candidates = [name for name in order if statuses[name].available]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda name: (statuses[name].cycles_attempted, order.index(name)),
    )


def earliest_reset(statuses: dict[str, RuntimeStatus]) -> datetime | None:
    """When the first exhausted runtime is expected back."""
    times = [
        status.reset_at for status in statuses.values() if status.reset_at is not None
    ]
    return min(times) if times else None


async def observed_runtimes(session: AsyncSession, org_id: uuid.UUID) -> list[str]:
    """Every runtime this org has a ledger row for.

    Needed because the ledger is only ever read against a *known* list of
    runtimes — the ones a program is configured to use — and a reader asking
    "what is spent" has no such list. A runtime nobody has observed has
    nothing to report, so this is the honest set to answer about.
    """
    return sorted(
        (
            await session.execute(
                select(AutomationRuntimeUsage.runtime).where(
                    AutomationRuntimeUsage.org_id == org_id
                )
            )
        )
        .scalars()
        .all()
    )


async def last_observation_of(
    session: AsyncSession, org_id: uuid.UUID
) -> dict[str, dict]:
    """What was last seen about each runtime, in its own words.

    The status says *whether* a runtime is spent; this says what was seen that
    made anyone think so. A reader deciding whether to trust an exhaustion
    needs the evidence, not only the verdict.
    """
    rows = (
        (
            await session.execute(
                select(AutomationRuntimeUsage).where(
                    AutomationRuntimeUsage.org_id == org_id
                )
            )
        )
        .scalars()
        .all()
    )
    return {
        row.runtime: {
            "last_observation": row.last_observation,
            "last_exhausted_at": _utc(row.last_exhausted_at),
            "last_success_at": _utc(row.last_success_at),
            "consecutive_exhaustions": row.consecutive_exhaustions,
            "reset_source": row.reset_source,
        }
        for row in rows
    }


@dataclass(frozen=True, slots=True)
class BuilderShare:
    """How the building was divided between runtimes over a window.

    The plan's measure for runtime rotation is a *share*, not a count: "within
    twenty percent of even", against a starting point where one runtime built
    31 of 33 cycles. A share needs a denominator and a window, and neither is
    the daily counter on the ledger -- that one rolls over, which is what makes
    it useless for answering "is the rotation actually working".
    """

    runtime: str
    cycles: int
    share: float


def evenness_gap(
    shares: list[BuilderShare], *, expected_runtimes: int | None = None
) -> float | None:
    """How far the widest share is from an even split, in percentage points.

    The gap, not the variance: the question is whether any one runtime is
    doing too much of the work, and a single runtime at 60% is the failure
    whatever the others are doing. ``None`` when nothing has been built, which
    is not a gap of zero -- it is no measurement at all.

    ``expected_runtimes`` is what makes "even" mean what the plan means by it.
    Measured against only the runtimes that *appear*, a program configured to
    rotate three and quietly using two reads as a perfect split -- which is
    the exact failure the rotation was built to fix, scored as a pass. The
    denominator is the larger of the two, so a configured runtime that never
    built widens the gap by its own absence, and a runtime that built without
    being configured still counts.
    """
    if not shares:
        return None
    even = 1.0 / max(len(shares), expected_runtimes or 0, 1)
    return round(max(abs(entry.share - even) for entry in shares) * 100, 1)


async def builder_share(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    days: int,
    automation_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> tuple[list[BuilderShare], datetime, int]:
    """Who built the cycles of the last ``days``, and what fraction each did.

    Counted from the cycles themselves rather than from the ledger, because
    the ledger's counters are per-day and roll over, and because the runtime
    of record on a cycle is the one that *actually ran* -- a cycle that failed
    over mid-change belongs to whichever runtime finished it, which is the
    only reading a share-of-work measurement can use.

    ``automation_id`` scopes it to one program. Unscoped, the answer covers
    every program in the org -- including disabled ones and any test harness
    -- which is the right answer to "what has this machine been running" and
    the wrong one to "is this program's rotation working".

    Returns the shares, the start of the window, and how many days of history
    actually exist inside it. The last of those matters: "even over a week"
    read off two days of data is not the measurement, and a caller that cannot
    tell the difference will report a number that means nothing.
    """
    moment = now or datetime.now(UTC)
    since = moment - timedelta(days=days)
    rows = (
        await session.execute(
            select(
                AutomationImproveCycle.builder_runtime,
                AutomationImproveCycle.created_at,
            ).where(
                AutomationImproveCycle.org_id == org_id,
                AutomationImproveCycle.created_at >= since,
                AutomationImproveCycle.builder_runtime.is_not(None),
                *(
                    [AutomationImproveCycle.automation_id == automation_id]
                    if automation_id is not None
                    else []
                ),
            )
        )
    ).all()

    counts: dict[str, int] = {}
    earliest: datetime | None = None
    for runtime, created_at in rows:
        counts[runtime] = counts.get(runtime, 0) + 1
        stamp = created_at if created_at.tzinfo else created_at.replace(tzinfo=UTC)
        if earliest is None or stamp < earliest:
            earliest = stamp

    total = sum(counts.values())
    shares = [
        BuilderShare(runtime=name, cycles=count, share=count / total)
        for name, count in sorted(counts.items(), key=lambda item: -item[1])
    ]
    covered = 0 if earliest is None else max(1, (moment - earliest).days + 1)
    return shares, since, min(covered, days)
