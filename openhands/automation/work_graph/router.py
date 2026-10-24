"""``/v1/work-graph`` — what the agents are doing, and what they did.

One request per backend per poll, rather than the twenty-odd a client would
need to assemble the same picture itself: a list of automations, then runs for
each, then cycles for each improvement program, with no org-wide cycle feed to
shortcut any of it. This service's connection pool is small enough that a
second surface doing that fan-out would starve the first.

The response carries ``in_flight`` so a client can stop polling entirely when
nothing is running, which is the cheapest the surface can possibly be.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from openhands.automation.auth import AuthenticatedUser, require_permission
from openhands.automation.db import get_session
from openhands.automation.models import (
    Automation,
    AutomationImproveCycle,
    AutomationRun,
)
from openhands.automation.work_graph.builder import build_graph, count_in_flight
from openhands.automation.work_graph.schemas import WorkGraphResponse


router = APIRouter(prefix="/v1/work-graph", tags=["Work graph"])

_require_view_automations = require_permission("view_automations")

#: How far back a cold fetch reaches, and the most cycles it returns.
#:
#: Bounded because an organization that has been running for months would
#: otherwise pay for its whole history on every page load, and almost none of
#: it is what anyone opened the page to see.
DEFAULT_WINDOW_DAYS = 7
DEFAULT_LIMIT = 200
MAX_LIMIT = 1000


def _service_id(user: AuthenticatedUser) -> str:
    """This deployment's own name for itself.

    Returned so a client can key work by the service rather than by whatever
    local id a browser happens to have for it — a localStorage UUID does not
    survive the trip to another machine.
    """
    return str(user.org_id) if user.org_id else "local"


@router.get("", response_model=WorkGraphResponse)
async def get_work_graph(
    since: str | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
    limit: int = DEFAULT_LIMIT,
    user: AuthenticatedUser = Depends(_require_view_automations),
    session: AsyncSession = Depends(get_session),
) -> WorkGraphResponse:
    """The organization's work, as nodes and edges.

    ``since`` is a watermark over cycle activity: pass back the ``cursor`` from
    a previous response and only work that has changed since is returned. It
    is advisory — a client that passes nothing gets the whole window, which is
    what a cold load wants.
    """
    limit = max(1, min(limit, MAX_LIMIT))
    window_days = max(1, min(window_days, 365))
    cutoff = datetime.now(UTC) - timedelta(days=window_days)

    watermark: datetime | None = None
    if since:
        try:
            watermark = datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError:
            # An unreadable cursor is treated as no cursor rather than an
            # error: the worst case is one full window, and refusing to
            # answer would leave the page empty over a formatting detail.
            watermark = None

    automations = list(
        (
            await session.execute(
                select(Automation).where(
                    Automation.org_id == user.org_id,
                    Automation.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    automation_ids = [automation.id for automation in automations]

    cycle_query = (
        select(AutomationImproveCycle)
        .where(AutomationImproveCycle.org_id == user.org_id)
        .order_by(AutomationImproveCycle.updated_at.desc())
        .limit(limit + 1)
    )
    cycle_query = cycle_query.where(
        AutomationImproveCycle.updated_at
        >= (watermark if watermark is not None else cutoff)
    )
    cycle_rows = list((await session.execute(cycle_query)).scalars().all())
    truncated = len(cycle_rows) > limit
    cycles = cycle_rows[:limit]

    run_ids = {cycle.run_id for cycle in cycles if cycle.run_id is not None}
    runs: list[AutomationRun] = []
    if automation_ids:
        runs = list(
            (
                await session.execute(
                    select(AutomationRun).where(
                        AutomationRun.automation_id.in_(automation_ids),
                        AutomationRun.created_at >= cutoff,
                    )
                )
            )
            .scalars()
            .all()
        )
        known = {run.id for run in runs}
        missing = run_ids - known
        if missing:
            runs.extend(
                (
                    await session.execute(
                        select(AutomationRun).where(AutomationRun.id.in_(missing))
                    )
                )
                .scalars()
                .all()
            )

    graph = build_graph(automations, runs, cycles, truncated=truncated)
    newest = max(
        (cycle.updated_at for cycle in cycles),
        default=None,
    )
    cursor = None
    if newest is not None:
        stamped = newest if newest.tzinfo is not None else newest.replace(tzinfo=UTC)
        cursor = stamped.isoformat()

    return WorkGraphResponse(
        service_id=_service_id(user),
        org_id=str(user.org_id) if user.org_id else None,
        generated_at=datetime.now(UTC),
        cursor=cursor,
        has_more=truncated,
        in_flight=count_in_flight(graph),
        nodes=sorted(graph.nodes.values(), key=lambda node: node.key),
        edges=sorted(
            graph.edges.values(),
            key=lambda edge: (edge.kind.value, edge.from_key, edge.to_key),
        ),
        omissions=graph.omissions,
    )
