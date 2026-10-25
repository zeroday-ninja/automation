"""Resolving an improvement program's repository list and claiming the next one.

The rotation has no stored cursor. The next repository is the one *after* the
repository the program's newest cycle took, which keeps the cursor and the
visible history from ever disagreeing: there is only one fact, and the
dashboard reads the same rows the claim does.

Matching is by label first and position second. A program whose repository
list grows or shrinks between runs continues from the repository it actually
took last time, instead of jumping wherever a bare index happens to land.
"""

import logging
import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from openhands.automation.git_sync.config_override import effective_settings_for
from openhands.automation.improve.caps import (
    DEFAULT_DAILY_CYCLES_PER_REPOSITORY,
    cycles_today_by_repository,
    cycles_today_for_org,
    local_day_bounds,
    next_day_boundary,
    repository_identity,
)
from openhands.automation.improve.config import ImproveProgramConfig
from openhands.automation.improve.url import strip_credentials
from openhands.automation.improve.usage import runtime_statuses
from openhands.automation.models import (
    Automation,
    AutomationGitSyncRemote,
    AutomationImproveCycle,
    ImproveCycleStatus,
)
from openhands.automation.utils import utcnow


logger = logging.getLogger(__name__)


class RotationRepository:
    """One entry of a program's rotation, resolved for a given moment."""

    __slots__ = ("index", "label", "ref", "remote_id", "url")

    def __init__(
        self,
        index: int,
        label: str,
        url: str,
        ref: str | None,
        remote_id: uuid.UUID | None,
    ) -> None:
        self.index = index
        self.label = label
        self.url = url
        self.ref = ref
        self.remote_id = remote_id

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"RotationRepository(index={self.index}, label={self.label!r})"


class NoRepositoriesError(Exception):
    """The program's rotation resolved to nothing to work on."""


async def _git_sync_repositories(
    session: AsyncSession,
    org_id: uuid.UUID,
    labels: list[str],
) -> list[RotationRepository]:
    """The org's configured Git Sync remotes, in a stable rotation order.

    Ordered by creation, *not* default-first like the Git Sync page: which
    repository is the default changes as the user works, and the rotation must
    not reshuffle underneath a program because of it.

    A remote with no repository URL is skipped rather than failing the claim.
    Local mode creates such a row on its first tick, and a half-configured
    remote is not something a program should refuse to run over.
    """
    result = await session.execute(
        select(AutomationGitSyncRemote)
        .where(AutomationGitSyncRemote.org_id == org_id)
        .order_by(
            AutomationGitSyncRemote.created_at.asc(),
            AutomationGitSyncRemote.id.asc(),
        )
    )
    wanted = {label.strip() for label in labels if label.strip()}
    repositories: list[RotationRepository] = []
    for remote in result.scalars().all():
        settings = effective_settings_for(remote)
        url = strip_credentials(settings.git_sync_repo_url or "")
        if not url:
            continue
        label = (remote.label or "").strip() or url
        if wanted and label not in wanted:
            continue
        repositories.append(
            RotationRepository(
                index=len(repositories),
                label=label,
                url=url,
                ref=(settings.git_sync_branch or None),
                remote_id=remote.id,
            )
        )
    return repositories


async def resolve_rotation(
    session: AsyncSession,
    org_id: uuid.UUID,
    config: ImproveProgramConfig,
) -> list[RotationRepository]:
    """Every repository the program rotates over, in order."""
    if config.repository_source == "explicit":
        return [
            RotationRepository(
                index=index,
                label=repo.label.strip() or strip_credentials(repo.url),
                url=strip_credentials(repo.url),
                ref=repo.ref,
                remote_id=None,
            )
            for index, repo in enumerate(config.repositories)
        ]
    return await _git_sync_repositories(session, org_id, config.repository_labels)


async def latest_cycle(
    session: AsyncSession, automation_id: uuid.UUID
) -> AutomationImproveCycle | None:
    """The program's newest cycle, whatever its status.

    A failed or abandoned cycle still counts: it took its repository, and the
    next run should move on rather than retry the same one forever. A
    repository whose cycle failed comes round again on the next lap.
    """
    result = await session.execute(
        select(AutomationImproveCycle)
        .where(AutomationImproveCycle.automation_id == automation_id)
        .order_by(
            AutomationImproveCycle.created_at.desc(),
            AutomationImproveCycle.id.desc(),
        )
        .limit(1)
    )
    return result.scalars().first()


def next_builder(
    builders: list,
    previous: AutomationImproveCycle | None,
    *,
    built_today: dict | None = None,
    unavailable: set | None = None,
):
    """Whose turn it is to build.

    Three rules, in order, and the order is the point.

    **Skip what cannot build.** A runtime that is out of usage still has its
    turn in a bare rotation, and spends a cycle discovering that. Dropping it
    here is the filter the plan asks for -- and if every builder is spent the
    turn still goes round, because a cycle that runs and records the refusal
    says more than one that never started.

    **Then prefer whoever has built least today.** A rotation alone only
    converges in the limit: it takes turns, so a runtime added to a lopsided
    history stays lopsided for as many cycles as the history is long. The
    headroom bias is what actually corrects a 31-to-1 split, by giving the
    turn to the runtime that is behind until it has caught up.

    **Then take turns.** With everything level this is the old behaviour
    exactly -- derived from the last cycle's builder, the same way the
    repository is, so there is one fact and no second counter to drift.

    A builder removed from the rotation simply does not match, and the turn
    falls to the first.
    """
    if not builders:
        raise NoRepositoriesError("the program has no builder")

    spent = unavailable or set()
    candidates = [b for b in builders if b not in spent] or list(builders)

    counts = built_today or {}
    if counts:
        fewest = min(counts.get(b, 0) for b in candidates)
        candidates = [b for b in candidates if counts.get(b, 0) == fewest]

    if previous is None or previous.builder_profile_id is None:
        return candidates[0]
    try:
        position = builders.index(previous.builder_profile_id)
    except ValueError:
        return candidates[0]
    # Round-robin among whoever is still in the running: walk forward from
    # the last builder and take the first candidate met, so a level field
    # behaves exactly as it always did.
    for step in range(1, len(builders) + 1):
        nominee = builders[(position + step) % len(builders)]
        if nominee in candidates:
            return nominee
    return candidates[0]


async def _spent_builders(
    session: AsyncSession,
    automation: Automation,
    config: ImproveProgramConfig,
) -> set:
    """Builders whose runtime has nothing left, so the turn can skip them.

    A profile nobody has built with yet is *not* spent: it has no runtime on
    record, and treating unknown as unavailable would keep a newly added
    runtime out of the rotation forever -- which is the opposite of what the
    rotation is for.

    The runtime of record is the one that actually ran, read from the last
    cycle that profile built, because a failover moves it.
    """
    rotation = list(config.builder_rotation)
    if not rotation:
        return set()
    rows = (
        await session.execute(
            select(
                AutomationImproveCycle.builder_profile_id,
                AutomationImproveCycle.builder_runtime,
                AutomationImproveCycle.created_at,
            )
            .where(
                AutomationImproveCycle.org_id == automation.org_id,
                AutomationImproveCycle.builder_profile_id.in_(rotation),
                AutomationImproveCycle.builder_runtime.is_not(None),
            )
            .order_by(AutomationImproveCycle.created_at.desc())
            .limit(200)
        )
    ).all()
    runtime_of: dict = {}
    for profile_id, runtime, _created in rows:
        runtime_of.setdefault(profile_id, runtime)
    if not runtime_of:
        return set()
    statuses = await runtime_statuses(
        session, automation.org_id, sorted(set(runtime_of.values()))
    )
    return {
        profile_id
        for profile_id, runtime in runtime_of.items()
        if runtime in statuses and not statuses[runtime].available
    }


async def builders_today(
    session: AsyncSession,
    automation: Automation,
    *,
    timezone_name: str,
    now: datetime | None = None,
) -> dict:
    """How many cycles each builder has taken since the day began.

    The program's own day, not a rolling window: the cap it sits beside is
    counted that way, and two clocks would disagree at the boundary.

    Across the organization rather than per program, which is the lesson the
    cap beside it already learned. A runtime is one subscription however many
    programs use it, so a bias that asked "who has built least *for me*"
    let two programs hand the turn to the same runtime with neither of them
    knowing. Five programs contributed cycles on 2026-10-25 and the day came
    out even by luck.
    """
    start, _ = local_day_bounds(timezone_name, now)
    rows = (
        await session.execute(
            select(
                AutomationImproveCycle.builder_profile_id,
                func.count(),
            )
            .where(
                AutomationImproveCycle.org_id == automation.org_id,
                AutomationImproveCycle.created_at >= start,
                AutomationImproveCycle.builder_profile_id.is_not(None),
            )
            .group_by(AutomationImproveCycle.builder_profile_id)
        )
    ).all()
    return {profile_id: count for profile_id, count in rows}


def next_index(
    repositories: list[RotationRepository],
    previous: AutomationImproveCycle | None,
) -> int:
    """Where the rotation goes next.

    Pure so the dashboard's "up next" and the run's claim cannot disagree.
    """
    if not repositories:
        raise NoRepositoriesError("the program has no repositories to rotate over")
    if previous is None:
        return 0
    for repo in repositories:
        if repo.label == previous.repo_label:
            return (repo.index + 1) % len(repositories)
    # The repository it took last is no longer in the list, so it was removed
    # or relabelled. Whatever now sits at that position is the one that
    # followed it, so the rotation resumes there rather than restarting at the
    # top. No +1: the removal already shifted the successor into that slot.
    return previous.repo_index % len(repositories)


def _automation_timezone(automation: Automation) -> str | None:
    """The zone the program's own schedule is expressed in.

    The cap resets when the program's day does, not when UTC's does, so a
    nightly program and its allowance stay in step.
    """
    trigger = automation.trigger
    if isinstance(trigger, dict):
        value = trigger.get("timezone")
        if isinstance(value, str) and value:
            return value
    return None


class AllRepositoriesCappedError(Exception):
    """Every repository has taken its allowance for today."""

    def __init__(self, resumes_at: datetime) -> None:
        super().__init__("every repository has reached its daily cap")
        self.resumes_at = resumes_at


class DailyCeilingReachedError(Exception):
    """The organization has taken as much work as it allows in a day."""

    def __init__(self, resumes_at: datetime) -> None:
        super().__init__("the daily ceiling for this organization is reached")
        self.resumes_at = resumes_at


async def next_available_index(
    session: AsyncSession,
    automation: Automation,
    repositories: list[RotationRepository],
    previous: AutomationImproveCycle | None,
    *,
    daily_cap: int,
    timezone_name: str | None,
) -> int:
    """Where the rotation goes next, skipping repositories done for today.

    A bounded scan from wherever the rotation already was. Skipping rather
    than blocking, because one busy repository must not stall the others — and
    skipping needs no new state, since the cursor is derived from whichever
    repository was actually taken.
    """
    start = next_index(repositories, previous)
    # One read of the day for the whole scan, rather than one query per
    # candidate: identity is decided in Python, so the counts come back
    # already bucketed.
    counts = await cycles_today_by_repository(session, automation.org_id, timezone_name)
    for offset in range(len(repositories)):
        candidate = repositories[(start + offset) % len(repositories)]
        taken = counts[
            repository_identity(candidate.remote_id, candidate.label, candidate.url)
        ]
        if taken < daily_cap:
            return (start + offset) % len(repositories)
    raise AllRepositoriesCappedError(next_day_boundary(timezone_name))


async def _claim_handoff(
    session: AsyncSession,
    automation: Automation,
    chosen: RotationRepository,
    cycle: AutomationImproveCycle,
) -> AutomationImproveCycle | None:
    """The newest unfinished handoff for this repository, now taken.

    Matched on the repository rather than on the program, because the work was
    left on a branch of that repository and whoever picks it up next is the
    right one to continue it.
    """
    query = (
        select(AutomationImproveCycle)
        .where(
            AutomationImproveCycle.org_id == automation.org_id,
            AutomationImproveCycle.id != cycle.id,
            AutomationImproveCycle.resumed_by_cycle_id.is_(None),
            AutomationImproveCycle.handoff.is_not(None),
        )
        .order_by(AutomationImproveCycle.created_at.desc())
        .limit(20)
    )
    for candidate in (await session.execute(query)).scalars().all():
        # The same identity the cap uses, so a repository configured one way
        # in one program and another way in a second is one repository to
        # both. Matching on the remote id alone left a handoff written by a
        # hand-configured program invisible to a git-synced one, which is a
        # cycle redoing work that was already saved.
        same_repository = repository_identity(
            candidate.remote_id, candidate.repo_label, candidate.repo_url
        ) == repository_identity(chosen.remote_id, chosen.label, chosen.url)
        if not same_repository:
            continue
        stored = candidate.handoff
        if isinstance(stored, dict) and stored.get("status") == "incomplete":
            return candidate
    return None


async def claim_next_cycle(
    session: AsyncSession,
    automation: Automation,
    config: ImproveProgramConfig,
    run_id: uuid.UUID | None,
    *,
    daily_cap: int = DEFAULT_DAILY_CYCLES_PER_REPOSITORY,
    daily_ceiling: int | None = None,
) -> tuple[AutomationImproveCycle, list[RotationRepository]]:
    """Take the next repository in the rotation and record the cycle.

    The cycle row is written before any agent starts, so an interrupted run is
    still visible in the history and still advances the rotation. The caller
    locks the automation row first, which serializes two runs that overlap.

    Both brakes are applied here, before anything is spent: a per-repository
    allowance, and a ceiling for the whole organization that no program's
    configuration can talk its way past.
    """
    repositories = await resolve_rotation(session, automation.org_id, config)
    previous = await latest_cycle(session, automation.id)
    timezone_name = _automation_timezone(automation)

    if daily_ceiling is not None:
        taken = await cycles_today_for_org(session, automation.org_id, timezone_name)
        if taken >= daily_ceiling:
            raise DailyCeilingReachedError(next_day_boundary(timezone_name))

    index = await next_available_index(
        session,
        automation,
        repositories,
        previous,
        daily_cap=daily_cap,
        timezone_name=timezone_name,
    )
    chosen = repositories[index]

    cycle = AutomationImproveCycle(
        automation_id=automation.id,
        org_id=automation.org_id,
        run_id=run_id,
        repo_index=chosen.index,
        repo_label=chosen.label,
        repo_url=chosen.url,
        repo_ref=chosen.ref,
        remote_id=chosen.remote_id,
        status=ImproveCycleStatus.CLAIMED,
        builder_profile_id=next_builder(
            config.builder_rotation,
            previous,
            built_today=await builders_today(
                session, automation, timezone_name=timezone_name
            ),
            unavailable=await _spent_builders(session, automation, config),
        ),
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    session.add(cycle)
    await session.flush()

    # Pick up where a previous cycle on this repository left off, if one left
    # anything. Claimed inside the same transaction that chose the repository,
    # and marked as taken: that write is what makes the handoff exactly-once,
    # so two overlapping runs cannot both continue the same work.
    resumed = await _claim_handoff(session, automation, chosen, cycle)
    if resumed is not None:
        cycle.resumed_from_cycle_id = resumed.id
        resumed.resumed_by_cycle_id = cycle.id
        if resumed.status is ImproveCycleStatus.PARKED:
            # An answered park whose work has just been handed on is
            # finished. Left PARKED it would be a cycle the janitor never
            # closes and the graph draws as waiting for somebody for good --
            # and the person it was waiting for has already answered.
            resumed.status = ImproveCycleStatus.COMPLETED
            resumed.completed_at = utcnow()
            resumed.updated_at = utcnow()
        await session.flush()

    logger.info(
        "Improvement cycle claimed",
        extra={
            "automation_id": str(automation.id),
            "cycle_id": str(cycle.id),
            "repo_label": chosen.label,
            "repo_index": chosen.index,
            "repo_count": len(repositories),
        },
    )
    return cycle, repositories
