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
from openhands.automation.improve.authority import resolve_authority
from openhands.automation.improve.change_class import (
    CHANGE_CLASSES,
    normalize_change_class,
)
from openhands.automation.improve.config import ImproveProgramConfig
from openhands.automation.improve.handoff import parse_handoff
from openhands.automation.improve.lesson_router import (
    capture_resolution_as_lesson,
)
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
from openhands.automation.improve.routing import GateDecision, normalize_route
from openhands.automation.improve.schemas import (
    AttestCycleRequest,
    AuthorityResponse,
    BuilderShareEntry,
    BuilderShareResponse,
    ClaimCycleRequest,
    ClaimCycleResponse,
    CycleListResponse,
    CycleRepository,
    CycleResponse,
    HandoffResume,
    ProvenanceResponse,
    ResolveCycleRequest,
    RotationResponse,
    RuntimeUsageEntry,
    RuntimeUsageResponse,
    UpdateCycleRequest,
)
from openhands.automation.improve.usage import (
    RuntimeObservation,
    builder_share,
    earliest_reset,
    evenness_gap,
    last_observation_of,
    observed_runtimes,
    provenance_of_merged_cycles,
    record_observations,
    runtime_statuses,
)
from openhands.automation.models import (
    Automation,
    AutomationImproveCycle,
    AutomationRun,
    ImproveCycleStatus,
    ImproveVerdict,
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

#: Most gate decisions kept on one cycle. The log is append-only, so without a
#: bound a cycle that loops could grow its own row without limit.
MAX_GATE_DECISIONS = 200


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
        reason=stored.get("reason"),
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
        # Whether it landed, and as which commit. Reported rather than
        # inferred from the publish gate: the gate approving is a decision,
        # and an authorised merge can still fail.
        "merged_at",
        "merge_commit_sha",
        "identity_cost",
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
        "claimed_change_class",
        "injected_lesson_ids",
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
    if "gate_decisions" in updates and body.gate_decisions:
        # Appended, never replaced: a routing decision that was taken cannot
        # be un-taken by a later report, and the log is what answers "why did
        # this go that way" after the branch and its sandbox are gone.
        recorded = list(cycle.gate_decisions or [])
        for entry in body.gate_decisions:
            if not isinstance(entry, dict):
                continue
            accepted, problem = _accept_gate_decision(entry)
            if accepted is None:
                # Not dropped. A decision the service could not read is itself
                # something the audit trail has to show, or a client with a
                # bug looks like a client that never decided anything.
                recorded.append({"unreadable": True, "problem": problem, "raw": entry})
                continue
            recorded.append(accepted)
        cycle.gate_decisions = recorded[-MAX_GATE_DECISIONS:]
    for field in ("parked_question", "parked_phase", "parked_plan"):
        if field in updates:
            setattr(cycle, field, updates[field])
    if body.status is ImproveCycleStatus.PARKED and cycle.parked_at is None:
        cycle.parked_at = utcnow()
    if "change_class" in updates or "change_evidence" in updates:
        # The class decides who may publish, so it is not taken on the
        # reporter's word. The evidence it sent is re-read here: a report that
        # says "additive" while also reporting a removed export or a deleted
        # file has contradicted itself, and the service keeps the restrictive
        # reading. This is not a substitute for computing the class from the
        # diff -- it is the floor under a client that is wrong or lying.
        cycle.change_evidence = body.change_evidence
        cycle.change_class = _floor_change_class(
            body.change_class, body.change_evidence
        )
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


def _accept_gate_decision(entry: dict) -> tuple[dict | None, str | None]:
    """Validate one reported gate decision, or say why it could not be read.

    The rationale is required for the reason the type requires it: a route with
    no stated reason cannot be reviewed, and an unreviewable decision is how an
    invented stop criterion survives.
    """
    route = normalize_route(entry.get("route"))
    if route is None:
        return None, f"{str(entry.get('route'))[:40]!r} is not a route"
    try:
        decision = GateDecision(
            phase=str(entry.get("phase") or ""),
            route=route,
            trigger=str(entry.get("trigger") or "")[:300],
            rationale=str(entry.get("rationale") or ""),
            verdict=_optional_text(entry.get("verdict"), 32),
            change_class=_optional_text(entry.get("change_class"), 16),
            recommends=_optional_text(entry.get("recommends"), 500),
            decided_by=_optional_text(entry.get("decided_by"), 100),
            at=_optional_text(entry.get("at"), 40) or utcnow().isoformat(),
        )
    except ValueError as exc:
        return None, str(exc)[:200]
    return decision.as_dict(), None


def _optional_text(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()[:limit]
    return text or None


#: Which rules in a report's own evidence force a class, whatever it claimed.
#: Enforcement belongs here rather than in a prompt or in the sandbox: a
#: boundary that decides who may publish is not something the thing being
#: gated gets to decide.
_RULE_FLOOR: dict[str, str] = {
    "removed_export": "breaking",
    "contract_line_removed": "breaking",
    "route_removed": "breaking",
    "migration_drops_data": "breaking",
    "dependency_major_bump": "breaking",
    "file_deleted": "deleting",
    "deletions_above_threshold": "deleting",
    "file_renamed": "deleting",
}


def _floor_change_class(
    reported: str | None, evidence: dict[str, Any] | None
) -> str | None:
    """The reported class, raised to whatever its own evidence requires."""
    classes = [normalize_change_class(reported)]
    reasons = (evidence or {}).get("reasons")
    if isinstance(reasons, list):
        for reason in reasons:
            if not isinstance(reason, dict):
                continue
            classes.append(_RULE_FLOOR.get(str(reason.get("rule"))))
    known = [value for value in classes if value in CHANGE_CLASSES]
    if not known:
        return normalize_change_class(reported)
    return max(known, key=CHANGE_CLASSES.index)


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
        improve_preset_code_digest,
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
    # Recorded with the tarball it describes, or the program would still
    # report the code it had before this call.
    automation.preset_metadata = {
        **(automation.preset_metadata or {}),
        "preset_code_digest": improve_preset_code_digest(),
    }
    automation.updated_at = utcnow()
    await session.flush()
    await session.refresh(automation)
    response = AutomationResponse.model_validate(automation)
    await session.commit()
    return response


@router.get("/runtime-usage")
async def get_runtime_usage(
    user: AuthenticatedUser = Depends(_require_view_automations),
    session: AsyncSession = Depends(get_session),
) -> RuntimeUsageResponse:
    """Which runtimes have usage left, and what was seen that says so.

    The ledger existed from the start and nothing could read it: it was
    written by cycles and read only by the claim, against the list of
    runtimes one program happens to be configured with. A person asking "what
    is spent" has no such list, so this answers about every runtime that has
    actually been observed — a runtime nobody has used has nothing to report.

    Recovery is applied here, at read time, the same way the claim applies it:
    an exhaustion whose reset has passed reads as available without anything
    having been scheduled to flip it.

    The evidence travels with the verdict. A reader deciding whether to trust
    an exhaustion needs what was seen, not only the conclusion — and the
    conclusion for a runtime with no rows is "unknown", not "healthy".
    """
    runtimes = await observed_runtimes(session, user.org_id)
    statuses = await runtime_statuses(session, user.org_id, runtimes)
    evidence = await last_observation_of(session, user.org_id)
    return RuntimeUsageResponse(
        runtimes=[
            RuntimeUsageEntry(
                runtime=name,
                state=statuses[name].state.value,
                available=statuses[name].available,
                reset_at=statuses[name].reset_at,
                cycles_attempted=statuses[name].cycles_attempted,
                cycles_completed=statuses[name].cycles_completed,
                **(evidence.get(name) or {}),
            )
            for name in runtimes
        ],
        earliest_reset=earliest_reset(statuses),
    )


@router.get("/builder-share")
async def get_builder_share(
    days: int = 7,
    automation_id: uuid.UUID | None = None,
    user: AuthenticatedUser = Depends(_require_view_automations),
    session: AsyncSession = Depends(get_session),
) -> BuilderShareResponse:
    """How evenly the building was divided between runtimes, over a window.

    The rotation exists because one runtime built 31 of 33 cycles. The plan's
    measure for whether that is fixed is a *share* over a week — and nothing
    could read it: the ledger counts per day and rolls over, so the only
    number available was "today", which says nothing about a rotation.

    Counted from the cycles, where the recorded builder is the runtime that
    actually ran rather than the one the profile named, because a cycle that
    failed over mid-change belongs to whichever runtime finished it.

    ``days_covered`` travels with the answer. A week asked of two days of
    history is "not yet", not a result, and `within_target` is null rather
    than false while that is true: an unmet measure and an unmeasurable one
    are different answers.

    ``automation_id`` scopes it to one program, and is how to read the
    question the plan actually asks. Unscoped, the answer covers every program
    in the org -- disabled ones, and any test harness -- which is the right
    answer to "what has this machine been running" and the wrong one to "is
    this program's rotation working".

    Scoped, "even" is measured against the runtimes the program is
    *configured* to rotate, not only the ones that appear. A program set to
    rotate three and quietly using two would otherwise read as a perfect
    split, which is the exact failure the rotation exists to fix, scored as a
    pass.
    """
    window = max(1, min(days, 365))
    expected: int | None = None
    if automation_id is not None:
        _, config = await _load_program(session, automation_id, user)
        expected = len({config.builder_profile_id, *config.rotate_builder})
    shares, since, covered = await builder_share(
        session, user.org_id, days=window, automation_id=automation_id
    )
    gap = evenness_gap(shares, expected_runtimes=expected)
    within = None
    if covered >= window and gap is not None:
        within = gap <= 20.0
    return BuilderShareResponse(
        automation_id=automation_id,
        configured_builders=expected,
        window_days=window,
        since=since,
        days_covered=covered,
        cycles=sum(entry.cycles for entry in shares),
        runtimes=[
            BuilderShareEntry(
                runtime=entry.runtime,
                cycles=entry.cycles,
                share=round(entry.share, 4),
            )
            for entry in shares
        ],
        evenness_gap=gap,
        within_target=within,
    )


@router.get("/provenance")
async def get_provenance(
    days: int = 30,
    automation_id: uuid.UUID | None = None,
    user: AuthenticatedUser = Depends(_require_view_automations),
    session: AsyncSession = Depends(get_session),
) -> ProvenanceResponse:
    """How many merged changes can be followed back to what produced them.

    The audit trail exists to answer one question — somebody holding a commit
    on a default branch asks what produced it — and until now the only way to
    check whether it could was to read the rows by hand.

    Only merged cycles count. A change that never landed has nothing for
    anybody to find in a `git log`, and counting it would flatter the number
    with work nobody has to audit.

    A cycle whose merge commit was not recorded but whose pull request was is
    counted separately rather than as a miss *or* as a full resolution. That
    is the squash-merge case the plan asks to be reported rather than hidden:
    the trail still leads back, by a longer route.
    """
    window = max(1, min(days, 365))
    if automation_id is not None:
        await _load_program(session, automation_id, user)
    found, since = await provenance_of_merged_cycles(
        session, user.org_id, days=window, automation_id=automation_id
    )
    coverage = found.coverage
    return ProvenanceResponse(
        automation_id=automation_id,
        window_days=window,
        since=since,
        merged=found.merged,
        from_commit=found.from_commit,
        from_pull_request=found.from_pull_request,
        unresolved=found.unresolved,
        missing=found.missing,
        coverage=None if coverage is None else round(coverage, 4),
        within_target=None if coverage is None else coverage >= 0.95,
    )


@router.get("/parked")
async def list_parked_cycles(
    limit: int = 20,
    user: AuthenticatedUser = Depends(_require_view_automations),
    session: AsyncSession = Depends(get_session),
) -> CycleListResponse:
    """Everything waiting for a person, across every program in the org.

    Its own route rather than a filter on one program's history, because the
    question is never "which of this program's cycles are parked" -- it is
    "what is waiting on me", and an answer that needs the reader to know which
    program to look in is not an answer.
    """
    limit = max(1, min(limit, 100))
    rows = (
        (
            await session.execute(
                select(AutomationImproveCycle)
                .where(
                    AutomationImproveCycle.org_id == user.org_id,
                    AutomationImproveCycle.status == ImproveCycleStatus.PARKED,
                )
                .order_by(AutomationImproveCycle.parked_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return CycleListResponse(
        cycles=[CycleResponse.of(row) for row in rows],
        total=len(rows),
    )


@router.post("/cycles/{cycle_id}/attest")
async def attest_cycle(
    cycle_id: uuid.UUID,
    body: AttestCycleRequest,
    user: AuthenticatedUser = Depends(_require_view_automations),
    session: AsyncSession = Depends(get_session),
) -> CycleResponse:
    """Record the judge's verdict on this cycle's removals.

    The service refuses an attestation from the runtime that built the change
    rather than trusting the configuration to have kept them apart: a rotation
    moves the builder between cycles, and a model favours its own output.
    """
    cycle = await _load_cycle(session, cycle_id, user)
    if cycle.status in _TERMINAL_STATUSES:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"Cycle already {cycle.status.value.lower()}",
        )
    if cycle.builder_runtime and body.runtime == cycle.builder_runtime:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=(
                f"{body.runtime} built this change, so it cannot attest its "
                "own removals. The signer is never the author."
            ),
        )
    cycle.judge_attestation = {
        "attested": body.attested,
        "runtime": body.runtime,
        "summary": body.summary,
        "removals": [removal.model_dump(mode="json") for removal in body.removals],
        "profile_id": str(body.profile_id) if body.profile_id else None,
        "conversation_id": body.conversation_id,
        "at": utcnow().isoformat(),
    }
    decision = GateDecision(
        phase="judge",
        route="approve" if body.attested else "cycle",
        trigger=f"the judge examined {len(body.removals)} removal(s)",
        rationale=body.summary,
        change_class=cycle.change_class,
        decided_by=f"runtime:{body.runtime}",
        at=cycle.judge_attestation["at"],
    )
    cycle.gate_decisions = [*(cycle.gate_decisions or []), decision.as_dict()][
        -MAX_GATE_DECISIONS:
    ]
    cycle.updated_at = utcnow()
    await session.flush()
    await session.refresh(cycle)
    response = CycleResponse.of(cycle)
    await session.commit()
    return response


@router.get("/cycles/{cycle_id}/authority")
async def get_cycle_authority(
    cycle_id: uuid.UUID,
    user: AuthenticatedUser = Depends(_require_view_automations),
    session: AsyncSession = Depends(get_session),
) -> AuthorityResponse:
    """Whether this change may publish now, and what is standing in the way.

    Read-only and computed on demand, so it is the same answer whoever asks
    and whenever they ask. A caller that publishes without asking is still
    possible on a deployment where the run and the dashboard share one
    credential -- but the answer is recorded here, which makes doing so a
    detectable act rather than an invisible one.
    """
    cycle = await _load_cycle(session, cycle_id, user)
    authority = resolve_authority(
        change_class=cycle.change_class,
        verdict=cycle.verdict.value if cycle.verdict else None,
        head_sha=cycle.head_sha,
        changed_files=cycle.changed_files,
        checks=cycle.checks,
        summary=cycle.summary,
        builder_runtime=cycle.builder_runtime,
        judge_attestation=cycle.judge_attestation,
    )
    return AuthorityResponse(
        cycle_id=cycle.id,
        change_class=cycle.change_class,
        **authority.as_dict(),
    )


@router.post("/cycles/{cycle_id}/resolve")
async def resolve_cycle(
    cycle_id: uuid.UUID,
    body: ResolveCycleRequest,
    user: AuthenticatedUser = Depends(_require_manage_automations),
    session: AsyncSession = Depends(get_session),
) -> CycleResponse:
    """Answer what a parked cycle is waiting for, so it can carry on.

    A parked cycle is not a failed cycle: the work is intact on its branch and
    the cycle holds the phase it stopped at. Resolving records the answer and
    returns the cycle to that phase, so the next run continues it rather than
    rediscovering it.

    Requires ``manage_automations``, and deliberately so. This is the endpoint
    a breaking change waits at, and the whole point of escalating is that the
    thing being gated is not the thing that answers.
    """
    cycle = await _load_cycle(session, cycle_id, user)
    if cycle.status is not ImproveCycleStatus.PARKED:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=(
                f"Cycle is {cycle.status.value}, not PARKED; there is nothing "
                "waiting to be answered"
            ),
        )
    cycle.resolution = body.resolution
    cycle.resolved_at = utcnow()
    decision = GateDecision(
        phase=cycle.parked_phase or "publish",
        route=body.route,
        trigger="a person answered the parked question",
        rationale=body.resolution,
        change_class=cycle.change_class,
        decided_by=f"user:{user.user_id}",
        at=cycle.resolved_at.isoformat(),
    )
    cycle.gate_decisions = [*(cycle.gate_decisions or []), decision.as_dict()][
        -MAX_GATE_DECISIONS:
    ]
    # `archive` is the one route that ends here: the person has said no, and
    # recording that as COMPLETED with the verdict is more honest than a
    # FAILED row, which reads as something having gone wrong.
    if body.route == "archive":
        cycle.status = ImproveCycleStatus.COMPLETED
        cycle.verdict = ImproveVerdict.REJECTED
        cycle.completed_at = cycle.resolved_at
    else:
        # Back to the phase it stopped at. The run that picks it up resumes
        # from there; until then it is visibly in flight again rather than
        # sitting in a queue nobody is watching.
        cycle.status = (
            ImproveCycleStatus.REVIEWING
            if cycle.parked_phase in ("review", "judge", "publish")
            else ImproveCycleStatus.BUILDING
        )
    # A correction the user actually made, recorded without asking them for
    # it. This is the line that separates learning from remembering: an
    # agent's guess about their preferences is a soul_suggestion they have to
    # accept, while an answer they gave to a real question is already theirs.
    await capture_resolution_as_lesson(session, cycle, body.resolution, user)
    cycle.updated_at = utcnow()
    await session.flush()
    await session.refresh(cycle)
    response = CycleResponse.of(cycle)
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
    automation, config = await _load_program(session, automation_id, user)
    repositories = await resolve_rotation(session, user.org_id, config)
    previous = await latest_cycle(session, automation_id)
    try:
        upcoming: int | None = next_index(repositories, previous)
    except NoRepositoriesError:
        upcoming = None

    # Imported here for the reason the refresh endpoint does: `preset_router`
    # imports this module's router.
    from openhands.automation.preset_router import improve_preset_code_digest

    packaged = (automation.preset_metadata or {}).get("preset_code_digest")
    try:
        current: str | None = improve_preset_code_digest()
    except OSError as exc:
        # The preset sources are not readable. That is not "the program is
        # current": claiming either answer would be inventing one, so the
        # comparison is reported as unavailable and the dashboard says nothing.
        logger.warning("Could not digest the improve preset code: %s", exc)
        current = None

    return RotationResponse(
        repositories=_rotation_entries(repositories),
        next_index=upcoming,
        last_cycle=CycleResponse.of(previous) if previous is not None else None,
        config=config,
        preset_code_is_current=(
            True
            if current is None
            else isinstance(packaged, str) and packaged == current
        ),
        preset_code_digest=packaged if isinstance(packaged, str) else None,
        service_preset_code_digest=current,
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
