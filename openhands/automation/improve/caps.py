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
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

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


def repository_identity(remote_id: uuid.UUID | None, label: str) -> tuple[str, str]:
    """What counts as "the same repository" for the cap.

    The remote's id when there is one, because a repository can be relabelled
    and a label-only count would let a rename hand it a fresh allowance. The
    label is the fallback for a repository configured by hand.
    """
    if remote_id is not None:
        return ("remote", str(remote_id))
    return ("label", label)


async def cycles_today(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    remote_id: uuid.UUID | None,
    label: str,
    timezone_name: str | None,
    now: datetime | None = None,
) -> int:
    """How many cycles this repository has taken today, across the org.

    Across the organization rather than per program: three programs each
    taking their own allowance of the same repository is the runaway this is
    here to prevent.
    """
    start, end = local_day_bounds(timezone_name, now)
    kind, value = repository_identity(remote_id, label)
    query = (
        select(func.count())
        .select_from(AutomationImproveCycle)
        .where(
            AutomationImproveCycle.org_id == org_id,
            AutomationImproveCycle.created_at >= start,
            AutomationImproveCycle.created_at < end,
        )
    )
    if kind == "remote":
        query = query.where(AutomationImproveCycle.remote_id == uuid.UUID(value))
    else:
        query = query.where(
            AutomationImproveCycle.remote_id.is_(None),
            AutomationImproveCycle.repo_label == value,
        )
    return (await session.execute(query)).scalar_one()


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
