"""Corrections: capturing them, reading them, and diagnosing a repeat.

Its own router rather than more endpoints on the improvement-program one,
because a lesson is not a property of a program. A correction given about one
repository applies to the next cycle on any program that touches it, and a
correction about how the user wants to be worked for applies to all of them.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from openhands.automation.auth import AuthenticatedUser, require_permission
from openhands.automation.db import get_session
from openhands.automation.improve.lessons import (
    LESSON_FAILURE_STAGES,
    LESSON_SCOPES,
    LESSON_TRIGGERS,
    diagnose_repeat,
    normalize_scope,
    normalize_trigger,
    resolution_is_a_correction,
)
from openhands.automation.models import AutomationLesson, AutomationLessonIncident
from openhands.automation.utils import utcnow
from openhands.automation.utils.time import UtcDatetime


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/lessons", tags=["Lessons"])

_require_view_automations = require_permission("view_automations")

#: Most lessons returned in one read. A cycle has a context budget, and a list
#: longer than this has stopped being a set of rules and become a document.
MAX_LESSONS = 100


class CaptureLessonRequest(BaseModel):
    """A correction that was actually given."""

    model_config = ConfigDict(extra="forbid")

    scope: str = Field(
        ...,
        description=(
            "Who it applies to: 'user' (everywhere), 'project' (one "
            "repository), 'runtime' (one agent runtime), 'surface' (one part "
            "of the interface)."
        ),
    )
    subject: str | None = Field(
        default=None,
        max_length=255,
        description=(
            "The repository, runtime or surface. Absent means every subject "
            "in this scope, which is what it means when the correction did "
            "not name one."
        ),
    )
    trigger: str = Field(
        ...,
        description=(
            "What produced it. Every trigger is something that already "
            "happens: nothing here asks the user to write a lesson."
        ),
    )
    text: str = Field(
        ...,
        min_length=1,
        max_length=4000,
        description=(
            "The user's words, verbatim. Paraphrasing a correction is how it "
            "comes back as something they have to give again."
        ),
    )
    why: str | None = Field(default=None, max_length=2000)
    source: dict[str, Any] | None = Field(
        default=None,
        description="What caused it: a cycle id, a pull request, a conversation.",
    )
    #: What the work that caused this actually had in front of it. The only
    #: thing that tells "ignored the lesson" apart from "never saw it".
    injected_lesson_ids: list[uuid.UUID] | None = Field(
        default=None, max_length=MAX_LESSONS
    )

    @field_validator("scope")
    @classmethod
    def _a_real_scope(cls, value: str) -> str:
        normalized = normalize_scope(value)
        if normalized is None:
            raise ValueError(
                f"{value!r} is not a scope; expected one of {', '.join(LESSON_SCOPES)}"
            )
        return normalized

    @field_validator("trigger")
    @classmethod
    def _a_real_trigger(cls, value: str) -> str:
        normalized = normalize_trigger(value)
        if normalized is None:
            raise ValueError(
                f"{value!r} is not a trigger; expected one of "
                f"{', '.join(LESSON_TRIGGERS)}"
            )
        return normalized


class LessonResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    scope: str
    subject: str | None
    trigger: str
    text: str
    why: str | None
    source: dict[str, Any] | None
    mechanised: bool
    check_ref: str | None
    status: str
    created_at: UtcDatetime
    #: How many times this correction has had to be given again. Zero is the
    #: number this whole mechanism exists to hold.
    repeats: int = 0

    @classmethod
    def of(cls, lesson: AutomationLesson, repeats: int = 0) -> LessonResponse:
        return cls(
            id=lesson.id,
            scope=lesson.scope,
            subject=lesson.subject,
            trigger=lesson.trigger,
            text=lesson.text,
            why=lesson.why,
            source=lesson.source,
            mechanised=lesson.mechanised,
            check_ref=lesson.check_ref,
            status=lesson.status,
            created_at=lesson.created_at,
            repeats=repeats,
        )


class CaptureLessonResponse(BaseModel):
    """What happened to the correction that was just given."""

    model_config = ConfigDict(extra="forbid")

    lesson: LessonResponse
    #: True when this correction had already been given. The lesson is the one
    #: that already existed; nothing new was stored.
    repeat: bool
    #: Which stage of the pipeline let it through, when it is a repeat.
    failed_stage: str | None = None
    failure_detail: str | None = None


class LessonListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lessons: list[LessonResponse]
    total: int


class MechaniseLessonRequest(BaseModel):
    """What now checks this lesson, so skimming the document is not enough."""

    model_config = ConfigDict(extra="forbid")

    check_ref: str = Field(
        ...,
        min_length=1,
        max_length=2000,
        description=(
            "The hook, lint rule, test or gate criterion that now fails when "
            "this lesson is broken. Required: 'mechanised' with nothing named "
            "is a claim, and the next repeat would be diagnosed against it."
        ),
    )


def _as_dicts(lessons: list[AutomationLesson]) -> list[dict]:
    return [
        {
            "id": str(lesson.id),
            "scope": lesson.scope,
            "subject": lesson.subject,
            "text": lesson.text,
            "mechanised": lesson.mechanised,
            "status": lesson.status,
        }
        for lesson in lessons
    ]


async def _active(session: AsyncSession, org_id: uuid.UUID) -> list[AutomationLesson]:
    return list(
        (
            await session.execute(
                select(AutomationLesson).where(
                    AutomationLesson.org_id == org_id,
                    AutomationLesson.status == "active",
                )
            )
        )
        .scalars()
        .all()
    )


async def _repeat_counts(
    session: AsyncSession, org_id: uuid.UUID
) -> dict[uuid.UUID, int]:
    rows = (
        await session.execute(
            select(AutomationLessonIncident.lesson_id).where(
                AutomationLessonIncident.org_id == org_id
            )
        )
    ).scalars()
    counts: dict[uuid.UUID, int] = {}
    for lesson_id in rows:
        counts[lesson_id] = counts.get(lesson_id, 0) + 1
    return counts


@router.post("", status_code=status.HTTP_201_CREATED)
async def capture_lesson(
    body: CaptureLessonRequest,
    user: AuthenticatedUser = Depends(_require_view_automations),
    session: AsyncSession = Depends(get_session),
) -> CaptureLessonResponse:
    """Record a correction that was given.

    A correction already given is not stored twice. It records an incident
    against the lesson that failed to prevent it, naming the stage, because a
    repeat is a defect of this pipeline and a defect with no diagnosis is a
    complaint.
    """
    existing = await _active(session, user.org_id)
    match = diagnose_repeat(
        body.text,
        scope=body.scope,
        subject=body.subject,
        existing=_as_dicts(existing),
        injected_lesson_ids=(
            [str(value) for value in body.injected_lesson_ids]
            if body.injected_lesson_ids is not None
            else None
        ),
    )

    if match is not None:
        lesson = next(item for item in existing if str(item.id) == match.lesson_id)
        session.add(
            AutomationLessonIncident(
                id=uuid.uuid4(),
                lesson_id=lesson.id,
                org_id=user.org_id,
                stage=match.stage,
                detail=match.detail,
                similarity=match.score,
                source=body.source,
            )
        )
        await session.flush()
        counts = await _repeat_counts(session, user.org_id)
        response = CaptureLessonResponse(
            lesson=LessonResponse.of(lesson, counts.get(lesson.id, 0)),
            repeat=True,
            failed_stage=match.stage,
            failure_detail=match.detail,
        )
        await session.commit()
        return response

    lesson = AutomationLesson(
        id=uuid.uuid4(),
        org_id=user.org_id,
        scope=body.scope,
        subject=body.subject,
        trigger=body.trigger,
        text=body.text,
        why=body.why,
        source=body.source,
    )
    session.add(lesson)
    await session.flush()
    await session.refresh(lesson)
    response = CaptureLessonResponse(lesson=LessonResponse.of(lesson), repeat=False)
    await session.commit()
    return response


@router.get("")
async def list_lessons(
    scope: str | None = Query(default=None),
    subject: str | None = Query(default=None),
    include_retired: bool = Query(default=False),
    user: AuthenticatedUser = Depends(_require_view_automations),
    session: AsyncSession = Depends(get_session),
) -> LessonListResponse:
    """Every correction in force, with how often it has had to be repeated.

    Scoping is done by the caller rather than here, because the caller knows
    what it is: a cycle filters by its own repository *and* takes every
    user-level lesson, which is not expressible as one equality.
    """
    conditions = [AutomationLesson.org_id == user.org_id]
    if not include_retired:
        conditions.append(AutomationLesson.status == "active")
    if scope is not None:
        normalized = normalize_scope(scope)
        if normalized is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"{scope!r} is not a scope",
            )
        conditions.append(AutomationLesson.scope == normalized)
    if subject is not None:
        conditions.append(AutomationLesson.subject == subject)

    rows = list(
        (
            await session.execute(
                select(AutomationLesson)
                .where(*conditions)
                .order_by(AutomationLesson.created_at.desc())
                .limit(MAX_LESSONS)
            )
        )
        .scalars()
        .all()
    )
    counts = await _repeat_counts(session, user.org_id)
    return LessonListResponse(
        lessons=[LessonResponse.of(row, counts.get(row.id, 0)) for row in rows],
        total=len(rows),
    )


@router.post("/{lesson_id}/mechanise")
async def mechanise_lesson(
    lesson_id: uuid.UUID,
    body: MechaniseLessonRequest,
    user: AuthenticatedUser = Depends(_require_view_automations),
    session: AsyncSession = Depends(get_session),
) -> LessonResponse:
    """Record what now checks this lesson.

    This is the stage that actually ends repetition: a model that skims the
    document still fails the check. Naming the check is required, because
    `mechanised` with nothing named would make the next repeat diagnose itself
    as "the check does not cover this case" when there is no check at all.
    """
    lesson = await _load(session, lesson_id, user)
    lesson.mechanised = True
    lesson.check_ref = body.check_ref
    lesson.updated_at = utcnow()
    await session.flush()
    await session.refresh(lesson)
    counts = await _repeat_counts(session, user.org_id)
    response = LessonResponse.of(lesson, counts.get(lesson.id, 0))
    await session.commit()
    return response


@router.post("/{lesson_id}/retire")
async def retire_lesson(
    lesson_id: uuid.UUID,
    user: AuthenticatedUser = Depends(_require_view_automations),
    session: AsyncSession = Depends(get_session),
) -> LessonResponse:
    """Stop applying a correction, without forgetting that it was given.

    Retired rather than deleted: its incidents are the record of how often it
    had to be repeated, and deleting the lesson would delete the evidence that
    this mechanism was needed.
    """
    lesson = await _load(session, lesson_id, user)
    lesson.status = "retired"
    lesson.updated_at = utcnow()
    await session.flush()
    await session.refresh(lesson)
    counts = await _repeat_counts(session, user.org_id)
    response = LessonResponse.of(lesson, counts.get(lesson.id, 0))
    await session.commit()
    return response


class LessonIncidentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lesson_id: uuid.UUID
    stage: str
    detail: str | None
    similarity: float | None
    created_at: UtcDatetime


class LessonIncidentListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    incidents: list[LessonIncidentResponse]
    total: int
    #: Every stage, so a reader can see which ones have never fired as well as
    #: which ones have. A count of zero is information.
    by_stage: dict[str, int]


@router.get("/incidents")
async def list_incidents(
    user: AuthenticatedUser = Depends(_require_view_automations),
    session: AsyncSession = Depends(get_session),
) -> LessonIncidentListResponse:
    """Every time a correction had to be given again, and what let it through.

    The number to watch: it should trend to zero, and every entry should name
    the stage that failed rather than only recording that something did.
    """
    rows = list(
        (
            await session.execute(
                select(AutomationLessonIncident)
                .where(AutomationLessonIncident.org_id == user.org_id)
                .order_by(AutomationLessonIncident.created_at.desc())
                .limit(MAX_LESSONS)
            )
        )
        .scalars()
        .all()
    )
    by_stage = {stage: 0 for stage in LESSON_FAILURE_STAGES}
    for row in rows:
        by_stage[row.stage] = by_stage.get(row.stage, 0) + 1
    return LessonIncidentListResponse(
        incidents=[
            LessonIncidentResponse(
                lesson_id=row.lesson_id,
                stage=row.stage,
                detail=row.detail,
                similarity=row.similarity,
                created_at=row.created_at,
            )
            for row in rows
        ],
        total=len(rows),
        by_stage=by_stage,
    )


async def _load(
    session: AsyncSession, lesson_id: uuid.UUID, user: AuthenticatedUser
) -> AutomationLesson:
    lesson = (
        await session.execute(
            select(AutomationLesson).where(
                AutomationLesson.id == lesson_id,
                AutomationLesson.org_id == user.org_id,
            )
        )
    ).scalar_one_or_none()
    if lesson is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Lesson not found")
    return lesson


async def capture_resolution_as_lesson(
    session: AsyncSession,
    cycle: Any,
    resolution: str,
    user: AuthenticatedUser,
) -> None:
    """File a person's answer to a parked cycle as a correction.

    Scoped to the repository, because that is what the question was about: a
    decision on one project's wire contract is not a standing preference about
    every project. The user can widen it afterwards; guessing wider here would
    apply an answer to work it was never about.

    Best-effort by design, and the *only* place in this module that is. A
    resolution the user has already given must not be lost because the lesson
    pipeline had a problem, so a failure here is reported and the resolution
    still stands.
    """
    if not resolution_is_a_correction(resolution):
        return
    try:
        existing = await _active(session, user.org_id)
        subject = cycle.remote_id and str(cycle.remote_id) or cycle.repo_label
        match = diagnose_repeat(
            resolution,
            scope="project",
            subject=subject,
            existing=_as_dicts(existing),
            # What the cycle reported having in front of it. None rather
            # than an empty list when it reported nothing at all: "it had no
            # corrections loaded" and "it never said" are different facts,
            # and only the first is grounds for blaming injection.
            injected_lesson_ids=(
                [str(value) for value in cycle.injected_lesson_ids]
                if cycle.injected_lesson_ids is not None
                else None
            ),
        )
        source = {
            "cycle_id": str(cycle.id),
            "repository": cycle.repo_label,
            "branch": cycle.branch,
            "pr_url": cycle.pr_url,
        }
        if match is not None:
            session.add(
                AutomationLessonIncident(
                    id=uuid.uuid4(),
                    lesson_id=uuid.UUID(match.lesson_id),
                    org_id=user.org_id,
                    stage=match.stage,
                    detail=match.detail,
                    similarity=match.score,
                    source=source,
                )
            )
            return
        session.add(
            AutomationLesson(
                id=uuid.uuid4(),
                org_id=user.org_id,
                scope="project",
                subject=subject,
                trigger="parked_resolution",
                text=resolution[:4000],
                why=(
                    "Answered a parked "
                    f"{cycle.change_class or 'change'} on {cycle.repo_label}."
                ),
                source=source,
            )
        )
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        logger.warning(
            "Could not file a resolution as a lesson: %s",
            exc,
            extra={"cycle_id": str(getattr(cycle, "id", None))},
        )
