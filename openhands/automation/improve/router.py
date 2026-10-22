"""The improvement-program API.

Two kinds of caller share it:

* a **run**, from inside its sandbox, claiming the next repository and
  reporting what it found. It authenticates with the same credential the run
  callbacks use, so these routes check ownership of the automation the same
  way ``complete_run`` does rather than trusting the path.
* the **dashboard**, reading the rotation and the history.
"""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from openhands.automation.auth import AuthenticatedUser, require_permission
from openhands.automation.db import get_session
from openhands.automation.git_sync import mark_git_sync_dirty
from openhands.automation.improve.config import ImproveProgramConfig
from openhands.automation.improve.rotation import (
    NoRepositoriesError,
    RotationRepository,
    claim_next_cycle,
    latest_cycle,
    next_index,
    resolve_rotation,
)
from openhands.automation.improve.schemas import (
    ClaimCycleRequest,
    ClaimCycleResponse,
    CycleListResponse,
    CycleRepository,
    CycleResponse,
    RotationResponse,
    UpdateCycleRequest,
)
from openhands.automation.models import (
    Automation,
    AutomationImproveCycle,
    AutomationRun,
    ImproveCycleStatus,
)
from openhands.automation.utils import utcnow
from openhands.automation.utils.model_profiles import ensure_agent_profile_exists


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/improve", tags=["Improvement programs"])

_require_view_automations = require_permission("view_automations")
_require_manage_automations = require_permission("manage_automations")

# A terminal cycle is history. Re-reporting one would let a late or retried
# callback overwrite a recorded result, so the write is refused instead.
_TERMINAL_STATUSES = frozenset(
    {ImproveCycleStatus.COMPLETED, ImproveCycleStatus.FAILED}
)


async def _load_program(
    session: AsyncSession,
    automation_id: uuid.UUID,
    user: AuthenticatedUser,
    *,
    for_update: bool = False,
) -> tuple[Automation, ImproveProgramConfig]:
    """The automation and its program configuration, or the right error.

    ``for_update`` locks the automation row, which is how two overlapping runs
    of the same program are stopped from claiming the same repository. SQLite
    ignores row locks; it also never runs two of these concurrently.
    """
    query = select(Automation).where(Automation.id == automation_id)
    if for_update:
        query = query.with_for_update()
    automation = (await session.execute(query)).scalars().first()
    if automation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Automation not found")
    if automation.user_id != user.user_id or automation.org_id != user.org_id:
        # Same shape as the run callbacks: do not confirm that someone else's
        # automation exists.
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Automation not found")
    try:
        config = ImproveProgramConfig.from_metadata(automation.preset_metadata)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"Not an improvement program: {exc}",
        ) from exc
    return automation, config


async def _load_cycle(
    session: AsyncSession, cycle_id: uuid.UUID, user: AuthenticatedUser
) -> AutomationImproveCycle:
    cycle = (
        (
            await session.execute(
                select(AutomationImproveCycle).where(
                    AutomationImproveCycle.id == cycle_id
                )
            )
        )
        .scalars()
        .first()
    )
    if cycle is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Cycle not found")
    # The cycle carries org_id, but ownership lives on the automation.
    await _load_program(session, cycle.automation_id, user)
    return cycle


def _rotation_entries(
    repositories: list[RotationRepository],
) -> list[CycleRepository]:
    return [
        CycleRepository(
            label=repo.label,
            url=repo.url,
            ref=repo.ref,
            index=repo.index,
            remote_id=repo.remote_id,
        )
        for repo in repositories
    ]


@router.post("/{automation_id}/claim", status_code=status.HTTP_201_CREATED)
async def claim_cycle(
    automation_id: uuid.UUID,
    body: ClaimCycleRequest,
    user: AuthenticatedUser = Depends(_require_view_automations),
    session: AsyncSession = Depends(get_session),
) -> ClaimCycleResponse:
    """Take the next repository in the rotation and open a cycle for it.

    Called once at the start of a run, before any agent work. The response
    carries the program's *current* configuration, read from the automation
    rather than from the run's packaged code, so editing a program takes
    effect on its next run without rebuilding its tarball.
    """
    automation, config = await _load_program(
        session, automation_id, user, for_update=True
    )

    if body.run_id is not None:
        run = (
            (
                await session.execute(
                    select(AutomationRun).where(AutomationRun.id == body.run_id)
                )
            )
            .scalars()
            .first()
        )
        if run is None or run.automation_id != automation.id:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail="run_id does not belong to this automation",
            )

    try:
        cycle, repositories = await claim_next_cycle(
            session, automation, config, body.run_id
        )
    except NoRepositoriesError as exc:
        # 409, not 500: the program is configured, there is simply nothing in
        # its rotation yet. The run reports this as a clean no-op.
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    return ClaimCycleResponse(
        cycle=CycleResponse.of(cycle),
        config=config,
        rotation=_rotation_entries(repositories),
    )


@router.patch("/cycles/{cycle_id}")
async def update_cycle(
    cycle_id: uuid.UUID,
    body: UpdateCycleRequest,
    user: AuthenticatedUser = Depends(_require_view_automations),
    session: AsyncSession = Depends(get_session),
) -> CycleResponse:
    """Record progress or results on a cycle.

    Every field is optional so a run can report the builder stage and the
    review stage separately. A cycle that already finished is immutable.
    """
    cycle = await _load_cycle(session, cycle_id, user)
    if cycle.status in _TERMINAL_STATUSES:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"Cycle already {cycle.status.value.lower()}",
        )

    updates = body.model_dump(exclude_unset=True, exclude_none=True)

    for field in (
        "builder_runtime",
        "builder_conversation_id",
        "summary",
        "branch",
        "pr_url",
        "error",
    ):
        if field in updates:
            setattr(cycle, field, updates[field])
    if "sources" in updates and body.sources is not None:
        cycle.sources = [source.model_dump(mode="json") for source in body.sources]
    if "reviews" in updates and body.reviews is not None:
        cycle.reviews = [review.model_dump(mode="json") for review in body.reviews]
    if "soul_suggestions" in updates and body.soul_suggestions is not None:
        cycle.soul_suggestions = [
            suggestion.model_dump(mode="json") for suggestion in body.soul_suggestions
        ]
    if body.verdict is not None:
        cycle.verdict = body.verdict
    if body.status is not None:
        cycle.status = body.status
        if body.status in _TERMINAL_STATUSES:
            cycle.completed_at = utcnow()
    cycle.updated_at = utcnow()

    await session.flush()
    await session.refresh(cycle)
    return CycleResponse.of(cycle)


@router.put("/{automation_id}/config")
async def replace_program_config(
    automation_id: uuid.UUID,
    body: ImproveProgramConfig,
    request: Request,
    user: AuthenticatedUser = Depends(_require_manage_automations),
    session: AsyncSession = Depends(get_session),
) -> RotationResponse:
    """Change what a program does from its next cycle onwards.

    A whole-config replacement rather than a patch: the configuration is small,
    its fields constrain each other (a repository source and its list, a
    builder and its reviewers), and a partial update would have to re-validate
    the merged result anyway.

    Takes effect on the next run and does not touch the program's packaged
    code, because a run reads its configuration from the claim response rather
    than from its tarball. A cycle already running is unaffected: it is working
    from the configuration it claimed with.

    The response is the new rotation, so a caller immediately sees which
    repository the change puts up next.
    """
    automation, _ = await _load_program(session, automation_id, user)

    # Validate the cast before scheduling anything with it, as creation does.
    await ensure_agent_profile_exists(body.builder_profile_id, request, user)
    for reviewer_profile_id in body.reviewer_profile_ids:
        await ensure_agent_profile_exists(reviewer_profile_id, request, user)

    # Replace the whole mapping rather than mutating in place: SQLAlchemy does
    # not track in-place changes to a JSON column.
    metadata = dict(automation.preset_metadata or {})
    metadata["improve"] = body.to_metadata()
    automation.preset_metadata = metadata
    # The builder is also the automation's profile; keep the two in step so the
    # existing per-automation profile display does not go stale.
    automation.agent_profile_id = body.builder_profile_id
    automation.updated_at = utcnow()
    await session.flush()
    await mark_git_sync_dirty(session, automation)

    repositories = await resolve_rotation(session, user.org_id, body)
    previous = await latest_cycle(session, automation_id)
    try:
        upcoming: int | None = next_index(repositories, previous)
    except NoRepositoriesError:
        upcoming = None
    return RotationResponse(
        repositories=_rotation_entries(repositories),
        next_index=upcoming,
        last_cycle=CycleResponse.of(previous) if previous is not None else None,
        config=body,
    )


@router.get("/{automation_id}/rotation")
async def get_rotation(
    automation_id: uuid.UUID,
    user: AuthenticatedUser = Depends(_require_view_automations),
    session: AsyncSession = Depends(get_session),
) -> RotationResponse:
    """The program's repositories in order, and which one is up next.

    ``next_index`` is computed by the same function the claim uses, so what
    the dashboard promises is what the next run actually takes.
    """
    _, config = await _load_program(session, automation_id, user)
    repositories = await resolve_rotation(session, user.org_id, config)
    previous = await latest_cycle(session, automation_id)
    try:
        upcoming: int | None = next_index(repositories, previous)
    except NoRepositoriesError:
        upcoming = None
    return RotationResponse(
        repositories=_rotation_entries(repositories),
        next_index=upcoming,
        last_cycle=CycleResponse.of(previous) if previous is not None else None,
        config=config,
    )


@router.get("/{automation_id}/cycles")
async def list_cycles(
    automation_id: uuid.UUID,
    limit: int = 20,
    offset: int = 0,
    user: AuthenticatedUser = Depends(_require_view_automations),
    session: AsyncSession = Depends(get_session),
) -> CycleListResponse:
    """Cycle history for one program, newest first."""
    await _load_program(session, automation_id, user)
    limit = max(1, min(limit, 100))
    offset = max(0, offset)

    total = (
        await session.execute(
            select(func.count())
            .select_from(AutomationImproveCycle)
            .where(AutomationImproveCycle.automation_id == automation_id)
        )
    ).scalar_one()

    rows = (
        (
            await session.execute(
                select(AutomationImproveCycle)
                .where(AutomationImproveCycle.automation_id == automation_id)
                .order_by(
                    AutomationImproveCycle.created_at.desc(),
                    AutomationImproveCycle.id.desc(),
                )
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return CycleListResponse(
        cycles=[CycleResponse.of(row) for row in rows], total=total
    )
