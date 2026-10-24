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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from openhands.automation.git_sync.config_override import effective_settings_for
from openhands.automation.improve.caps import (
    DEFAULT_DAILY_CYCLES_PER_REPOSITORY,
    cycles_today,
    cycles_today_for_org,
    next_day_boundary,
)
from openhands.automation.improve.config import ImproveProgramConfig
from openhands.automation.improve.url import strip_credentials
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
):
    """Whose turn it is to build.

    Derived from the last cycle's builder, the same way the repository is
    derived from the last cycle's repository: one fact, no second counter to
    drift. A builder that has been removed from the rotation simply does not
    match, and the turn falls to the first.

    Rotating the builder is what lets *every* runtime review. With one fixed
    builder, that runtime only ever writes and never attacks anyone else's
    work.
    """
    if not builders:
        raise NoRepositoriesError("the program has no builder")
    if previous is None or previous.builder_profile_id is None:
        return builders[0]
    try:
        position = builders.index(previous.builder_profile_id)
    except ValueError:
        return builders[0]
    return builders[(position + 1) % len(builders)]


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
    for offset in range(len(repositories)):
        candidate = repositories[(start + offset) % len(repositories)]
        taken = await cycles_today(
            session,
            automation.org_id,
            remote_id=candidate.remote_id,
            label=candidate.label,
            timezone_name=timezone_name,
        )
        if taken < daily_cap:
            return (start + offset) % len(repositories)
    raise AllRepositoriesCappedError(next_day_boundary(timezone_name))


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
        builder_profile_id=next_builder(config.builder_rotation, previous),
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    session.add(cycle)
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
