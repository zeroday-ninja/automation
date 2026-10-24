"""Request and response schemas for the improvement-program API."""

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from openhands.automation.improve.config import ImproveProgramConfig
from openhands.automation.models import (
    AutomationImproveCycle,
    ImproveCycleStatus,
    ImproveVerdict,
)
from openhands.automation.utils.time import UtcDatetime


MAX_SOURCES = 40
#: Commands recorded per builder or reviewer. Evidence, not a transcript.
MAX_CHECKS = 40
MAX_REVIEWS = 8
MAX_SOUL_SUGGESTIONS = 10
MAX_SUMMARY = 20000


class CycleRepository(BaseModel):
    """The repository a cycle works on."""

    label: str
    url: str
    ref: str | None = None
    index: int = Field(..., description="Position in the rotation.")
    remote_id: uuid.UUID | None = Field(
        default=None,
        description="The Git Sync remote this entry came from, when it came from one.",
    )


class CitedSource(BaseModel):
    """A source the builder based its change on."""

    model_config = ConfigDict(extra="forbid")

    url: str = Field(..., min_length=1, max_length=2000)
    title: str | None = Field(default=None, max_length=500)
    note: str | None = Field(
        default=None,
        max_length=2000,
        description="What this source actually supports in the change.",
    )


class CycleCheck(BaseModel):
    """A command that was run, and what it returned.

    Typed rather than a sentence, because "the tests passed" in a summary is
    a claim and this is evidence. Anything a machine has to act on is a field.
    """

    model_config = ConfigDict(extra="forbid")

    command: str = Field(..., min_length=1, max_length=500)
    passed: bool | None = Field(
        default=None,
        description=(
            "None when the command ran but its outcome is not a pass/fail — "
            "a count, a diff. Absent is not the same as failed."
        ),
    )
    detail: str | None = Field(default=None, max_length=2000)


class CycleReview(BaseModel):
    """One runtime's adversarial review of a cycle's change."""

    model_config = ConfigDict(extra="forbid")

    profile_id: uuid.UUID | None = None
    profile_name: str | None = Field(default=None, max_length=255)
    runtime: str | None = Field(
        default=None,
        max_length=64,
        description=(
            "The runtime that reviewed, e.g. 'claude-code' or 'codex'. This is "
            "what makes the review independent of the builder."
        ),
    )
    conversation_id: str | None = Field(default=None, max_length=64)
    verdict: ImproveVerdict | None = None
    summary: str | None = Field(default=None, max_length=MAX_SUMMARY)
    concerns: list[str] = Field(default_factory=list, max_length=40)
    failed: bool = Field(
        default=False,
        description=(
            "True when this reviewer never produced a judgement — an "
            "unreachable server, a stuck conversation. Recorded and shown, "
            "but it does not vote on the cycle's verdict: a runtime that "
            "did not look must not outrank one that did."
        ),
    )
    updates_checked: str | None = Field(
        default=None,
        max_length=MAX_SUMMARY,
        description=(
            "What this reviewer checked for being out of date, and what it "
            "found — including an explicit 'nothing newer', which is a "
            "different statement from having said nothing."
        ),
    )
    stale_sources: list[str] = Field(
        default_factory=list,
        max_length=MAX_SOURCES,
        description=(
            "Sources the reviewer found outdated or superseded, which is the "
            "check that keeps an existing implementation from drifting."
        ),
    )
    verified_sources: list[str] = Field(
        default_factory=list,
        max_length=MAX_SOURCES,
        description=(
            "Sources the reviewer opened and confirmed say what the change "
            "claims they say. Without this the record can show evidence "
            "disputed but never evidence confirmed, which is a lopsided "
            "account of a review."
        ),
    )
    checks: list[CycleCheck] = Field(
        default_factory=list,
        max_length=MAX_CHECKS,
        description="Commands this reviewer ran, and what they returned.",
    )


class SoulSuggestion(BaseModel):
    """Something a cycle learned about how the user wants to be worked for.

    A proposal for the user's SOUL.md, never an edit to it: the document that
    tells every future run what the user wants is not something a run may
    rewrite on its own.
    """

    model_config = ConfigDict(extra="forbid")

    text: str = Field(
        ...,
        min_length=1,
        max_length=2000,
        description="The preference, written as the user would want it stated.",
    )
    why: str | None = Field(
        default=None,
        max_length=2000,
        description="What in this cycle suggested it, so the user can judge it.",
    )


class ClaimCycleRequest(BaseModel):
    """A run asking for the next repository in the rotation."""

    model_config = ConfigDict(extra="forbid")

    run_id: uuid.UUID | None = Field(
        default=None,
        description="The run claiming the cycle, so the dashboard can link them.",
    )


class UpdateCycleRequest(BaseModel):
    """A run reporting progress or results for a cycle it holds."""

    model_config = ConfigDict(extra="forbid")

    status: ImproveCycleStatus | None = None
    verdict: ImproveVerdict | None = None
    builder_runtime: str | None = Field(default=None, max_length=64)
    builder_conversation_id: str | None = Field(default=None, max_length=64)
    summary: str | None = Field(default=None, max_length=MAX_SUMMARY)
    branch: str | None = Field(default=None, max_length=255)
    pr_url: str | None = Field(default=None, max_length=2000)
    sources: list[CitedSource] | None = Field(default=None, max_length=MAX_SOURCES)
    reviews: list[CycleReview] | None = Field(default=None, max_length=MAX_REVIEWS)
    soul_suggestions: list[SoulSuggestion] | None = Field(
        default=None, max_length=MAX_SOUL_SUGGESTIONS
    )
    error: str | None = Field(default=None, max_length=MAX_SUMMARY)

    base_sha: str | None = Field(default=None, max_length=64)
    head_sha: str | None = Field(
        default=None,
        max_length=64,
        description=(
            "The commit this cycle produced. Without it there is no path from "
            "a merged change back to the prompt and the sources behind it."
        ),
    )
    changed_files: list[str] | None = Field(default=None, max_length=200)
    checks: list[CycleCheck] | None = Field(default=None, max_length=MAX_CHECKS)
    followups: list[str] | None = Field(default=None, max_length=40)
    risks: list[str] | None = Field(
        default=None,
        max_length=40,
        description=(
            "The weakest part of the change, in the builder's own words. "
            "Rendered into the reviewer's prompt and, now, kept — an admission "
            "that is not recorded cannot be checked against what happened."
        ),
    )
    prompt_digest: str | None = Field(
        default=None,
        max_length=71,
        description=(
            "Digest of the rendered prompt. The digest rather than the text: "
            "the prompt embeds the user's identity and the repository's."
        ),
    )
    handoff: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Where this cycle got to when its runtime ran out, for the next "
            "cycle on this repository to continue from. Rejected without a "
            "non-empty `next_steps`: a handoff with no next step is "
            "indistinguishable from a restart."
        ),
    )
    change_class: str | None = Field(
        default=None,
        max_length=16,
        description=(
            "Which class of change the diff is: 'additive', 'deleting' or "
            "'breaking'. Computed from the diff by code that ships with the "
            "program, never the builder's opinion of its own work."
        ),
    )
    claimed_change_class: str | None = Field(
        default=None,
        max_length=32,
        description=(
            "What the builder said the change was. Stored beside the computed "
            "class because a claim that understates it is itself a finding."
        ),
    )
    change_evidence: dict[str, Any] | None = Field(
        default=None,
        description=(
            "The rules that fired and the lines they fired on. A class with no "
            "evidence behind it cannot be disagreed with."
        ),
    )
    runtime_switches: list[dict[str, Any]] | None = Field(
        default=None,
        max_length=20,
        description=(
            "Every runtime this cycle's builder moved to, and why. A "
            "conversation changes runtime underneath itself when the SDK "
            "fails over, so `builder_runtime` is the runtime that actually "
            "produced the work and this is the route it took to get there."
        ),
    )
    runtime_observations: list[dict[str, Any]] | None = Field(
        default=None,
        max_length=20,
        description=(
            "What this run saw about each runtime's remaining usage. The "
            "runtimes expose no balance, so this is the only way the "
            "scheduler learns which subscriptions are spent."
        ),
    )
    verdict_rule_version: str | None = Field(
        default=None,
        max_length=32,
        description=(
            "Which rule turned these reviews into this verdict. The "
            "aggregation ships into the sandbox, so without this, editing the "
            "severity table reinterprets every past verdict."
        ),
    )

    @field_validator("pr_url")
    @classmethod
    def validate_pr_url(cls, v: str | None) -> str | None:
        """A link the dashboard renders must be http(s)."""
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None
        if not v.startswith(("https://", "http://")):
            raise ValueError("pr_url must be an http(s) URL")
        return v


class CycleResponse(BaseModel):
    """One recorded cycle."""

    id: uuid.UUID
    automation_id: uuid.UUID
    run_id: uuid.UUID | None
    status: ImproveCycleStatus
    verdict: ImproveVerdict | None
    repository: CycleRepository
    builder_profile_id: uuid.UUID | None
    builder_runtime: str | None
    runtime_switches: list[dict[str, Any]] | None = None
    change_class: str | None = None
    claimed_change_class: str | None = None
    change_evidence: dict[str, Any] | None = None
    builder_conversation_id: str | None
    summary: str | None
    branch: str | None
    pr_url: str | None
    sources: list[dict[str, Any]]
    reviews: list[dict[str, Any]]
    soul_suggestions: list[dict[str, Any]]
    error: str | None
    base_sha: str | None
    head_sha: str | None
    changed_files: list[str]
    checks: list[dict[str, Any]]
    followups: list[str]
    risks: list[str]
    prompt_digest: str | None
    verdict_rule_version: str | None
    reaped: bool
    # UtcDatetime, not datetime: SQLite returns naive values and a timestamp
    # serialized without an offset is read by JavaScript as *local* time.
    created_at: UtcDatetime
    updated_at: UtcDatetime
    completed_at: UtcDatetime | None

    @classmethod
    def of(cls, cycle: AutomationImproveCycle) -> "CycleResponse":
        """Build from an ORM row."""
        return cls(
            id=cycle.id,
            automation_id=cycle.automation_id,
            run_id=cycle.run_id,
            status=cycle.status,
            verdict=cycle.verdict,
            repository=CycleRepository(
                label=cycle.repo_label,
                url=cycle.repo_url,
                ref=cycle.repo_ref,
                index=cycle.repo_index,
                remote_id=cycle.remote_id,
            ),
            builder_profile_id=cycle.builder_profile_id,
            builder_runtime=cycle.builder_runtime,
            runtime_switches=cycle.runtime_switches,
            change_class=cycle.change_class,
            claimed_change_class=cycle.claimed_change_class,
            change_evidence=cycle.change_evidence,
            builder_conversation_id=cycle.builder_conversation_id,
            summary=cycle.summary,
            branch=cycle.branch,
            pr_url=cycle.pr_url,
            sources=cycle.sources or [],
            reviews=cycle.reviews or [],
            soul_suggestions=cycle.soul_suggestions or [],
            error=cycle.error,
            base_sha=cycle.base_sha,
            head_sha=cycle.head_sha,
            changed_files=cycle.changed_files or [],
            checks=cycle.checks or [],
            followups=cycle.followups or [],
            risks=cycle.risks or [],
            prompt_digest=cycle.prompt_digest,
            verdict_rule_version=cycle.verdict_rule_version,
            reaped=cycle.reaped,
            created_at=cycle.created_at,
            updated_at=cycle.updated_at,
            completed_at=cycle.completed_at,
        )


class HandoffResume(BaseModel):
    """Unfinished work this cycle is continuing.

    Present only when a previous cycle on the same repository ran out of
    runtime mid-change and wrote down where it had got to. ``do_not`` is the
    field that saves the most: it is what stops this cycle spending its whole
    budget rediscovering a dead end the last one already walked into.
    """

    model_config = ConfigDict(extra="forbid")

    from_cycle_id: uuid.UUID
    branch: str | None = None
    head_sha: str | None = None
    stage: str | None = None
    goal: str | None = None
    done: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)
    do_not: list[str] = Field(default_factory=list)
    files_touched: list[str] = Field(default_factory=list)
    written_by: str | None = None


class ClaimCycleResponse(BaseModel):
    """What a run needs to carry out the cycle it just claimed."""

    cycle: CycleResponse
    config: ImproveProgramConfig
    rotation: list[CycleRepository] = Field(
        ...,
        description="The whole rotation in order, so a run can report context.",
    )
    resume: HandoffResume | None = Field(
        default=None,
        description=(
            "Work left unfinished by a previous cycle on this repository. "
            "Absent when there is none, which is the usual case."
        ),
    )


class RotationResponse(BaseModel):
    """The program's rotation as it stands, for the dashboard."""

    repositories: list[CycleRepository]
    next_index: int | None = Field(
        default=None,
        description=(
            "Which entry the next run will take. Null when the rotation is "
            "empty, which is also why a program can have nothing to do."
        ),
    )
    last_cycle: CycleResponse | None = None
    config: ImproveProgramConfig


class CycleListResponse(BaseModel):
    """A page of cycle history, newest first."""

    cycles: list[CycleResponse]
    total: int
