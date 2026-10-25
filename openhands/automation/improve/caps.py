"""How much work one repository may take in a day.

Continuous improvement without a ceiling is how a repository takes thirteen
cycles in a day and nobody notices until they read the history. The cap is
enforced where the claim happens, which is the only place that is both
serialized and repository-aware, and it is enforced by *skipping* rather than
blocking: one busy repository must not stall the whole rotation.

Skipping needs no new cursor, which is the part worth keeping. The rotation's
position is derived from the newest cycle's label, so taking the next
available repository is self-representing — the next lap resumes after
whatever was actually taken.
"""

from __future__ import annotations

import uuid
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from openhands.automation.improve.url import normalise_repository_url
from openhands.automation.models import AutomationImproveCycle


#: Cycles one repository may take in a day when nothing says otherwise.
DEFAULT_DAILY_CYCLES_PER_REPOSITORY = 10


def local_day_bounds(
    timezone_name: str | None, now: datetime | None = None
) -> tuple[datetime, datetime]:
    """The day's start and end, in the automation's own timezone.

    The automation's cron timezone rather than UTC, so "the cap resets" and
    "the program fires" happen in the same frame. A zone the system does not
    know falls back to UTC rather than failing a claim over a name.
    """
    moment = now or datetime.now(UTC)
    try:
        zone = ZoneInfo(timezone_name) if timezone_name else UTC
    except (ZoneInfoNotFoundError, ValueError):
        zone = UTC
    local = moment.astimezone(zone)
    start_local = datetime.combine(local.date(), datetime.min.time(), tzinfo=zone)
    # Built from the next calendar date rather than by adding 24 hours, so a
    # day that is 23 or 25 hours long still has exactly one start.
    next_day = date.fromordinal(local.date().toordinal() + 1)
    end_local = datetime.combine(next_day, datetime.min.time(), tzinfo=zone)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


def repository_identity(
    remote_id: uuid.UUID | None, label: str, url: str | None
) -> tuple[str, str]:
    """What counts as "the same repository" for the cap.

    The url, normalised, because it is the only thing every configuration of
    the same repository agrees on. Identity used to be "the remote's id, or
    the label" — and those are disjoint buckets, so a repository reached
    through a git-sync remote by one program and listed by hand in another had
    **two** allowances. Live on 2026-10-25 that was `HAZARD` taking twenty
    cycles against a cap of ten.

    The remote's id and the label remain the fallbacks, in that order, for a
    row with no url to read — and ``url`` is a required argument rather than a
    defaulted one, because a caller that forgot it would silently count a
    different bucket, and a brake that silently reads zero is worse than the
    double allowance it is here to fix. A repository renamed at the forge does get a
    fresh allowance for one day; a redirect is indistinguishable from a move,
    and the alternative traded a real double allowance for a hypothetical one.
    """
    normalised = normalise_repository_url(url)
    if normalised is not None:
        return ("url", normalised)
    if remote_id is not None:
        return ("remote", str(remote_id))
    return ("label", label)


async def cycles_today_by_repository(
    session: AsyncSession,
    org_id: uuid.UUID,
    timezone_name: str | None,
    now: datetime | None = None,
) -> Counter[tuple[str, str]]:
    """Today's cycles, counted per repository, across the whole org.

    Across the organization rather than per program: three programs each
    taking their own allowance of the same repository is the runaway this is
    here to prevent.

    Counted here rather than in SQL because identity needs the url
    normalised, which no portable query expresses — and one day of one org's
    cycles is bounded by the global ceiling, so reading them is cheaper than
    the per-repository query it replaces.
    """
    start, end = local_day_bounds(timezone_name, now)
    rows = await session.execute(
        select(
            AutomationImproveCycle.remote_id,
            AutomationImproveCycle.repo_label,
            AutomationImproveCycle.repo_url,
        ).where(
            AutomationImproveCycle.org_id == org_id,
            AutomationImproveCycle.created_at >= start,
            AutomationImproveCycle.created_at < end,
        )
    )
    return Counter(
        repository_identity(remote_id, label, url) for remote_id, label, url in rows
    )


async def cycles_today(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    remote_id: uuid.UUID | None,
    label: str,
    url: str | None,
    timezone_name: str | None,
    now: datetime | None = None,
) -> int:
    """How many cycles this one repository has taken today."""
    counts = await cycles_today_by_repository(session, org_id, timezone_name, now)
    return counts[repository_identity(remote_id, label, url)]


async def cycles_today_for_org(
    session: AsyncSession,
    org_id: uuid.UUID,
    timezone_name: str | None,
    now: datetime | None = None,
) -> int:
    """Every cycle the organization has taken today.

    The ceiling this feeds is independent of any program's configuration, so
    a mis-edited program cannot talk its way past it.
    """
    start, end = local_day_bounds(timezone_name, now)
    return (
        await session.execute(
            select(func.count())
            .select_from(AutomationImproveCycle)
            .where(
                AutomationImproveCycle.org_id == org_id,
                AutomationImproveCycle.created_at >= start,
                AutomationImproveCycle.created_at < end,
            )
        )
    ).scalar_one()


def next_day_boundary(
    timezone_name: str | None, now: datetime | None = None
) -> datetime:
    """When the allowance comes back."""
    _, end = local_day_bounds(timezone_name, now)
    return end


def seconds_until(moment: datetime, now: datetime | None = None) -> int:
    """Whole seconds from now until ``moment``, never negative."""
    delta = moment - (now or datetime.now(UTC))
    return max(0, int(delta / timedelta(seconds=1)))
