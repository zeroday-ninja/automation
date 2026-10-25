"""Request and response schemas for the improvement-program API."""

import uuid
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from openhands.automation.improve.config import ImproveProgramConfig
from openhands.automation.improve.routing import ROUTES, normalize_route
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
    #: Where this reviewer's runtime moved mid-review, if it did. A reviewer
    #: can run out of usage and fail over just as a builder can, and
    #: `runtime` above is then the one that *finished* — so without this the
    #: record says a runtime reviewed the change that did not review all of it.
    runtime_switches: list[dict[str, Any]] = Field(default_factory=list, max_length=20)
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
    merged_at: UtcDatetime | None = Field(
        default=None,
        description=(
            "When the forge merged this cycle's change. The publish gate "
            "approving is a decision; this is the outcome."
        ),
    )
    merge_commit_sha: str | None = Field(default=None, max_length=64)
    identity_cost: dict[str, Any] | None = Field(
        default=None,
        description=(
            "What the user's identity took of this cycle's prompt: the "
            "budget, what was spent, and which documents did not fit whole."
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
    injected_lesson_ids: list[uuid.UUID] | None = Field(
        default=None,
        max_length=100,
        description=(
            "Which corrections this cycle actually had in front of it. "
            "Reported rather than inferred: when the same correction arrives "
            "again, this is the only thing that tells 'the work ignored it' "
            "apart from 'the work never saw it'."
        ),
    )
    gate_decisions: list[dict[str, Any]] | None = Field(
        default=None,
        max_length=40,
        description=(
            "Where each gate sent this work and why. Appended to whatever is "
            "already recorded, never replacing it: a routing decision that "
            "was taken cannot be un-taken by a later report."
        ),
    )
    parked_question: str | None = Field(
        default=None,
        max_length=4000,
        description=(
            "What this cycle needs a person to answer. Sending it with status "
            "PARKED is what stops the cycle without failing it."
        ),
    )
    parked_phase: str | None = Field(
        default=None,
        max_length=32,
        description="The phase to resume from once the question is answered.",
    )
    parked_plan: dict[str, Any] | None = Field(
        default=None,
        description=(
            "For a breaking change: what breaks, who consumes it, the sequence "
            "that keeps both sides working during the transition, and the "
            "rollback. A breaking change with no changeover plan is a question "
            "nobody can answer."
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


class RuntimeUsageEntry(BaseModel):
    """One runtime's remaining usage, and the evidence behind that reading."""

    model_config = ConfigDict(extra="forbid")

    runtime: str
    #: ``unknown`` for a runtime observed but never resolved either way. Kept
    #: distinct from ``available``: nothing having gone wrong is not the same
    #: as something having gone right.
    state: str
    available: bool
    reset_at: UtcDatetime | None = None
    cycles_attempted: int = 0
    cycles_completed: int = 0
    #: What was last seen, in the runtime's own words where it said anything.
    last_observation: dict[str, Any] | None = None
    last_exhausted_at: UtcDatetime | None = None
    last_success_at: UtcDatetime | None = None
    #: How many times running it has refused in a row. A rising count is the
    #: signal that a reset estimate is wrong rather than early.
    consecutive_exhaustions: int = 0
    #: Where the reset time came from: the runtime's own notice, or a default
    #: cooldown because it gave none.
    reset_source: str | None = None


class RuntimeUsageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runtimes: list[RuntimeUsageEntry]
    #: When the first exhausted runtime is expected back, so a caller can say
    #: "nothing until then" rather than making the reader compare timestamps.
    earliest_reset: UtcDatetime | None = None


class BuilderShareEntry(BaseModel):
    """One runtime's share of the building over the window."""

    runtime: str
    cycles: int
    #: 0..1. The fraction, not the count: the measure is a share, and a count
    #: means nothing without the denominator beside it.
    share: float


class BuilderShareResponse(BaseModel):
    """How evenly the work was divided, and whether that can be trusted yet.

    The plan's measure for runtime rotation is "builder share across the three
    runtimes within twenty percent of even over a week", against a starting
    point where one runtime built 31 of 33 cycles. It had no meter: the ledger
    counts per day and rolls over, so nothing could answer it.

    ``days_covered`` is reported beside the answer because the honest result
    of asking for a week on two days of history is "not yet", and a caller
    that cannot tell the difference reports a number that means nothing.
    """

    #: The program this covers, or null for every program in the org.
    automation_id: uuid.UUID | None = None
    #: How many runtimes the program is configured to rotate. Null when the
    #: answer is not scoped to one program, because there is then no single
    #: configuration to measure "even" against.
    configured_builders: int | None = None
    window_days: int
    since: UtcDatetime
    days_covered: int
    cycles: int
    runtimes: list[BuilderShareEntry]
    #: How far the busiest runtime is from an even split, in percentage
    #: points. Null when nothing was built -- which is not a gap of zero.
    evenness_gap: float | None = None
    #: The plan's threshold, carried with the answer so a reader does not have
    #: to know it to interpret the number.
    target_gap: float = 20.0
    #: Null, not False, while the window is not covered: an unmet measure and
    #: an unmeasurable one are different answers.
    within_target: bool | None = None
    #: Cycles whose recorded builder is not the runtime that finished the
    #: work. The plan asks for this to be zero and says "which today it does
    #: not" -- so it is counted, not assumed.
    misattributed_cycles: int = 0
    #: Handoffs written, never resumed, and then overtaken by a fresh cycle
    #: on the same repository. The plan asks for zero of these too: the next
    #: cycle redid from nothing what the last one had already paid to learn.
    work_redone: int = 0
    #: Handoffs still waiting. Not a fault -- nobody has had the chance yet --
    #: but reported, because a number that only counts failures cannot show
    #: work quietly piling up.
    handoffs_waiting: int = 0


class ProvenanceResponse(BaseModel):
    """Whether published changes can be followed back to what produced them.

    The plan's measure for the audit trail: "provenance resolves a merged
    commit back to its prompt and sources for at least 95% of merged cycles,
    with squash-merge misses falling back to the pull request and reported
    rather than hidden". It had no meter, so the only way to answer it was to
    read the rows by hand.
    """

    automation_id: uuid.UUID | None = None
    window_days: int
    since: UtcDatetime
    merged: int
    #: Reachable from the commit the change became, which is where a reader
    #: holding a line of `git log` actually starts.
    from_commit: int
    #: Reachable only through the pull request, because the commit was not
    #: recorded -- a squash merge rewrites it. A weaker answer, counted
    #: separately so it cannot pass as the stronger one.
    from_pull_request: int
    #: Merged and leading nowhere.
    unresolved: int
    #: Which part the unresolved ones are missing, counted. A bare total says
    #: the trail is broken; this says where.
    missing: dict[str, int] = Field(default_factory=dict)
    #: 0..1, or null when nothing merged -- which is not full coverage.
    coverage: float | None = None
    #: The plan's threshold, carried so a reader need not know it.
    target_coverage: float = 0.95
    #: Null, not False, when nothing merged: an unmet measure and an
    #: unmeasurable one are different answers.
    within_target: bool | None = None


class AuthorityResponse(BaseModel):
    """Whether a change may publish, and what is standing in the way."""

    model_config = ConfigDict(extra="forbid")

    cycle_id: uuid.UUID
    change_class: str | None
    authorised: bool
    route: str
    reason: str
    missing: list[str] = Field(default_factory=list)
    human_only: bool = Field(
        default=False,
        description=(
            "True when no caller can ever be authorised, however complete the "
            "evidence -- kept distinct from 'not yet', because one is a wait "
            "and the other is a different act by a different person."
        ),
    )


class AttestedRemoval(BaseModel):
    """One removal the judge looked at, and what it found."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(..., max_length=500)
    symbol: str | None = Field(default=None, max_length=200)
    finding: str = Field(
        ...,
        description=(
            "'dead' (nothing called it), 'replaced' (something else does the "
            "job now) or 'covered' (a test or caller proves the behaviour "
            "survives). Anything else is not an attestation."
        ),
    )
    where: str = Field(
        ...,
        min_length=1,
        max_length=1000,
        description=(
            "Where the finding can be checked -- the search that came back "
            "empty, the replacement, the test. An attestation with no 'where' "
            "is an assertion."
        ),
    )

    @field_validator("finding")
    @classmethod
    def _one_of_three(cls, value: str) -> str:
        folded = value.strip().casefold()
        if folded not in ("dead", "replaced", "covered"):
            raise ValueError(
                f"{value!r} is not a finding; expected dead, replaced or covered"
            )
        return folded


class AttestCycleRequest(BaseModel):
    """The judge's verdict on a cycle's removals."""

    model_config = ConfigDict(extra="forbid")

    attested: bool = Field(
        ...,
        description=(
            "Whether every removal in this change is accounted for. False is "
            "a legitimate answer and sends the work back round; it is not an "
            "error."
        ),
    )
    runtime: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description=(
            "The runtime that judged it. Checked against the builder's: a "
            "model favours its own output, so the signer is never the author."
        ),
    )
    summary: str = Field(
        ...,
        min_length=1,
        max_length=4000,
        description="Why, in the judge's own words. Required either way.",
    )
    removals: list[AttestedRemoval] = Field(
        default_factory=list,
        max_length=200,
        description=(
            "Each removal and what was found about it. Required when "
            "`attested` is true: attesting a set of removals without naming "
            "them is attesting nothing."
        ),
    )
    profile_id: uuid.UUID | None = None
    conversation_id: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def _attesting_means_naming(self) -> "AttestCycleRequest":
        if self.attested and not self.removals:
            raise ValueError(
                "attested=true needs at least one removal: signing off a set "
                "of removals without naming them is signing off nothing"
            )
        return self


class ResolveCycleRequest(BaseModel):
    """A person's answer to what a parked cycle is waiting for."""

    model_config = ConfigDict(extra="forbid")

    route: str = Field(
        ...,
        description=(
            "Where the work goes now: 'approve' to let it publish, 'cycle' to "
            "send it back for another pass, 'return' for more evidence, or "
            "'archive' to stop deliberately. 'escalate' is not available here "
            "-- this *is* the escalation."
        ),
    )
    resolution: str = Field(
        ...,
        min_length=1,
        max_length=4000,
        description=(
            "Why. Required, and carried into the resuming cycle's context: an "
            "answer with no reasoning behind it has to be guessed at again the "
            "next time the same question comes up."
        ),
    )

    @field_validator("route")
    @classmethod
    def _a_real_route_that_is_not_another_escalation(cls, value: str) -> str:
        normalized = normalize_route(value)
        if normalized is None:
            raise ValueError(
                f"{value!r} is not a route; expected one of {', '.join(ROUTES)}"
            )
        if normalized == "escalate":
            raise ValueError(
                "'escalate' is not an answer here: this endpoint is where an "
                "escalation is answered, so escalating again would park the "
                "cycle on the person who just unparked it"
            )
        return normalized


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
    #: What this cycle left for the next one when its runtime ran out, and the
    #: chain in both directions. Absent from the response until now, which
    #: made the whole handoff invisible: it was written, stored and resumed
    #: from, and no client could see any of that had happened.
    handoff: dict[str, Any] | None = None
    resumed_from_cycle_id: uuid.UUID | None = None
    resumed_by_cycle_id: uuid.UUID | None = None
    gate_decisions: list[dict[str, Any]] | None = None
    injected_lesson_ids: list[uuid.UUID] | None = None
    judge_attestation: dict[str, Any] | None = None
    parked_phase: str | None = None
    parked_question: str | None = None
    parked_plan: dict[str, Any] | None = None
    parked_at: UtcDatetime | None = None
    resolution: str | None = None
    resolved_at: UtcDatetime | None = None
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
    merged_at: UtcDatetime | None
    merge_commit_sha: str | None
    identity_cost: dict[str, Any] | None = None
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
            handoff=cycle.handoff,
            resumed_from_cycle_id=cycle.resumed_from_cycle_id,
            resumed_by_cycle_id=cycle.resumed_by_cycle_id,
            gate_decisions=cycle.gate_decisions,
            injected_lesson_ids=cycle.injected_lesson_ids,
            judge_attestation=cycle.judge_attestation,
            parked_phase=cycle.parked_phase,
            parked_question=cycle.parked_question,
            parked_plan=cycle.parked_plan,
            parked_at=cycle.parked_at,
            resolution=cycle.resolution,
            resolved_at=cycle.resolved_at,
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
            merged_at=cycle.merged_at,
            merge_commit_sha=cycle.merge_commit_sha,
            identity_cost=cycle.identity_cost,
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
    #: Why the previous cycle stopped. Two answers so far, and they call for
    #: different things from whoever continues: a runtime out of quota left
    #: off wherever it happened to be, while one out of time was attempting
    #: more than fits in a cycle and the next attempt should be smaller.
    reason: str | None = None
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
    #: Whether the program's packaged runner is the code this service would
    #: package now. A program's runner is packaged once, at creation, so a
    #: preset fix reaches only programs created afterwards -- and nothing said
    #: so, which made "this feature does not work for me" and "my program
    #: predates the feature" the same observation. False for a program
    #: packaged before the digest was recorded at all, which is strictly older.
    preset_code_is_current: bool = True
    #: What this program was packaged from, when that was recorded.
    preset_code_digest: str | None = None
    #: What the service would package now, so a client can show the pair
    #: rather than only an assertion.
    service_preset_code_digest: str | None = None


class CycleListResponse(BaseModel):
    """A page of cycle history, newest first."""

    cycles: list[CycleResponse]
    total: int
