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
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from openhands.automation.auth import AuthenticatedUser, require_permission
from openhands.automation.config import get_config
from openhands.automation.db import get_session
from openhands.automation.git_sync import mark_git_sync_dirty
from openhands.automation.improve.config import ImproveProgramConfig
from openhands.automation.improve.handoff import parse_handoff
from openhands.automation.improve.rotation import (
    AllRepositoriesCappedError,
    DailyCeilingReachedError,
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
    HandoffResume,
    RotationResponse,
    UpdateCycleRequest,
)
from openhands.automation.improve.usage import (
    RuntimeObservation,
    record_observations,
)
from openhands.automation.models import (
    Automation,
    AutomationImproveCycle,
    AutomationRun,
    ImproveCycleStatus,
    TarballUpload,
    UploadStatus,
)
from openhands.automation.schemas import AutomationResponse
from openhands.automation.storage import FileStore, get_file_store
from openhands.automation.utils import utcnow
from openhands.automation.utils.model_profiles import ensure_agent_profile_exists
from openhands.automation.utils.tarball_validation import build_internal_url


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


def _parse_instant(value: Any) -> datetime | None:
    """An instant from whatever a run sent, or ``None``.

    Unreadable rather than invalid: a run that reports a time this service
    cannot parse has still reported the exhaustion, and losing that because
    of a format is worse than losing the time.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _observed_at(value: Any) -> datetime:
    """When something was seen, defaulting to now."""
    return _parse_instant(value) or datetime.now(UTC)


async def _resume_for(
    session: AsyncSession, cycle: AutomationImproveCycle
) -> HandoffResume | None:
    """Unfinished work this cycle was handed, if any.

    Read from the cycle the claim already pointed at, so the run is told what
    to continue without having to go looking for a branch by name.
    """
    if cycle.resumed_from_cycle_id is None:
        return None
    previous = await session.get(AutomationImproveCycle, cycle.resumed_from_cycle_id)
    stored = previous.handoff if previous is not None else None
    if not isinstance(stored, dict):
        return None
    return HandoffResume(
        from_cycle_id=cycle.resumed_from_cycle_id,
        branch=stored.get("branch"),
        head_sha=stored.get("head_sha"),
        stage=stored.get("stage"),
        goal=stored.get("goal"),
        done=stored.get("done") or [],
        next_steps=stored.get("next_steps") or [],
        do_not=stored.get("do_not") or [],
        files_touched=stored.get("files_touched") or [],
        written_by=stored.get("written_by"),
    )


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
        sandbox_cfg = get_config().sandbox
        cycle, repositories = await claim_next_cycle(
            session,
            automation,
            config,
            body.run_id,
            # The stricter of the two, so a program can tighten its own
            # allowance but never widen it.
            daily_cap=min(
                config.daily_cycles_per_repository
                or sandbox_cfg.improve_daily_cycles_per_repository,
                sandbox_cfg.improve_daily_cycles_per_repository,
            ),
            daily_ceiling=sandbox_cfg.improve_global_daily_ceiling,
        )
    except NoRepositoriesError as exc:
        # 409, not 500: the program is configured, there is simply nothing in
        # its rotation yet. The run reports this as a clean no-op.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"code": "no_repositories", "message": str(exc)},
        ) from exc
    except AllRepositoriesCappedError as exc:
        # Also a clean no-op, and a *different* one. Collapsing "nothing is
        # configured", "everything is done for today" and "every runtime is
        # out" into one prose conflict leaves the only visible result as
        # nothing happening, with no way to tell which it was.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": "all_repositories_capped",
                "message": str(exc),
                "resumes_at": exc.resumes_at.isoformat(),
            },
        ) from exc
    except DailyCeilingReachedError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": "daily_ceiling_reached",
                "message": str(exc),
                "resumes_at": exc.resumes_at.isoformat(),
            },
        ) from exc

    response = ClaimCycleResponse(
        cycle=CycleResponse.of(cycle),
        config=config,
        rotation=_rotation_entries(repositories),
        resume=await _resume_for(session, cycle),
    )
    # Commit before answering. `get_session` commits only *after* the response
    # is sent, and the caller is a run that reports against this cycle id as
    # its next act — on SQLite that next request can beat the commit and get a
    # 404 for a cycle it was just handed. Other handlers that hand out an
    # identifier commit here for the same reason.
    await session.commit()
    return response


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
        # Audit: what the change was, what was run, and which rule judged it.
        # Every one is optional, because a run's code is packaged into a
        # tarball at program-creation time and an older tarball will not send
        # them. A cycle that reports less than this build can store is a
        # cycle with less evidence, not a failed request.
        "base_sha",
        "head_sha",
        "changed_files",
        "followups",
        "risks",
        "prompt_digest",
        "verdict_rule_version",
        # The route the builder's runtime actually took. `builder_runtime` is
        # reported twice on purpose: once at claim time from the profile, and
        # again here with whatever ran, which is the only one an audit or a
        # share-of-work measurement can use.
        "runtime_switches",
    ):
        if field in updates:
            setattr(cycle, field, updates[field])
    if "checks" in updates and body.checks is not None:
        cycle.checks = [check.model_dump(mode="json") for check in body.checks]
    if "handoff" in updates and body.handoff is not None:
        parsed = parse_handoff(body.handoff, written_by=cycle.builder_runtime)
        # A handoff with no next step is indistinguishable from a restart, so
        # one without is treated as absent rather than stored and half-
        # trusted by whatever picks it up.
        cycle.handoff = parsed.as_dict() if parsed is not None else None
    if "runtime_observations" in updates and body.runtime_observations:
        await record_observations(
            session,
            cycle.org_id,
            [
                RuntimeObservation(
                    runtime=str(entry.get("runtime") or ""),
                    outcome=str(entry.get("outcome") or ""),
                    observed_at=_observed_at(entry.get("observed_at")),
                    reset_at=_parse_instant(entry.get("reset_at")),
                    reset_source=entry.get("reset_source"),
                    evidence=entry.get("evidence"),
                    conversation_id=entry.get("conversation_id"),
                )
                for entry in body.runtime_observations
                if isinstance(entry, dict) and entry.get("runtime")
            ],
        )
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
    response = CycleResponse.of(cycle)
    # Committed before answering, for the reason given in `claim_cycle`: the
    # caller reports the stages of one cycle in sequence.
    await session.commit()
    return response


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
    response = RotationResponse(
        repositories=_rotation_entries(repositories),
        next_index=upcoming,
        last_cycle=CycleResponse.of(previous) if previous is not None else None,
        config=body,
    )
    # Committed before answering, as `claim_cycle` explains: a caller may
    # claim against the configuration it has just set.
    await session.commit()
    return response


@router.post("/{automation_id}/refresh-code")
async def refresh_program_code(
    automation_id: uuid.UUID,
    user: AuthenticatedUser = Depends(_require_manage_automations),
    session: AsyncSession = Depends(get_session),
    file_store: FileStore = Depends(get_file_store),
) -> AutomationResponse:
    """Repackage the program's runner from the preset code on disk.

    A program's code is packaged when it is created, so a fix to the preset
    reaches only programs created afterwards. That turned every preset fix into
    "delete your programs and build them again", which costs the user their
    cycle history — and the history *is* the rotation cursor, so rebuilding a
    program silently rewinds it to the first repository.

    This repackages in place. Configuration is untouched (a run reads it from
    the claim), and the next cycle runs the new code. A cycle already running
    is unaffected: its code was unpacked when it started.
    """
    automation, _ = await _load_program(session, automation_id, user)

    # Imported here: `preset_router` imports this module's router, so a
    # module-level import would close the cycle.
    from openhands.automation.preset_router import (
        _build_storage_path,
        _bytes_to_async_iter,
        _generate_improve_tarball,
    )

    upload_id = uuid.uuid4()
    storage_path = _build_storage_path(user.org_id, user.user_id, upload_id)
    upload = TarballUpload(
        id=upload_id,
        user_id=user.user_id,
        org_id=user.org_id,
        name=f"improve-program-refresh-{str(automation.id)[:8]}",
        description="Repackaged improvement-program runner",
        status=UploadStatus.UPLOADING,
        storage_path=storage_path,
    )
    session.add(upload)
    await session.flush()

    try:
        upload.size_bytes = await file_store.write_stream(
            path=storage_path,
            stream=_bytes_to_async_iter(_generate_improve_tarball()),
            content_type="application/x-tar",
        )
        upload.status = UploadStatus.COMPLETED
    except Exception as exc:
        upload.status = UploadStatus.FAILED
        upload.error_message = f"Upload failed: {exc!s}"
        await session.flush()
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to upload tarball: {exc!s}",
        ) from exc

    automation.tarball_path = build_internal_url(upload_id)
    automation.updated_at = utcnow()
    await session.flush()
    await session.refresh(automation)
    response = AutomationResponse.model_validate(automation)
    await session.commit()
    return response


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
