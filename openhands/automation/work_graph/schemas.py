"""The shape of the work graph on the wire.

A *projection*, not a store. Everything here is derived on each request from
rows this service already keeps, because the alternative — a second copy of
the same facts, updated alongside them — is a second copy that can disagree
with the first.

Keys are backend-relative (``cycle:<uuid>``). The client prefixes them with
its own name for this deployment, which keeps this service from having to know
how a browser refers to it.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from openhands.automation.utils.time import UtcDatetime


SCHEMA_VERSION = 1


class WorkNodeKind(StrEnum):
    """What a node *is*.

    ``RUN_TOMBSTONE`` exists because runs are pruned while the cycles that
    claimed them survive: without it, the chain from a program to its older
    work breaks, and the graph becomes less able to explain itself the further
    back you look — the opposite of an audit trail.
    """

    PROGRAM = "program"
    RUN = "run"
    RUN_TOMBSTONE = "run-tombstone"
    CYCLE = "cycle"
    AGENT = "agent"
    REPOSITORY = "repository"
    ARTIFACT = "artifact"
    SOURCE = "source"
    SUGGESTION = "suggestion"


class WorkEdgeKind(StrEnum):
    """What one node has to do with another.

    ``REVIEWS`` points a reviewer at the *artifact*, not at the builder: a
    reviewer judges the change, and drawing it at the builder would misstate
    what happened. ``DISPUTES`` and ``VERIFIES`` are the two halves of what a
    reviewer did with the evidence, and an account with only the first is a
    lopsided one.
    """

    SCHEDULES = "schedules"
    CLAIMS = "claims"
    TARGETS = "targets"
    BUILDS = "builds"
    PRODUCES = "produces"
    REVIEWS = "reviews"
    CITES = "cites"
    DISPUTES = "disputes"
    VERIFIES = "verifies"
    PROPOSES = "proposes"


class WorkNodeStatus(StrEnum):
    """One closed vocabulary across every kind, so a reader learns it once."""

    PENDING = "pending"
    IN_FLIGHT = "in_flight"
    DONE = "done"
    FAILED = "failed"
    #: Waiting for a person. Distinct from IN_FLIGHT because nothing is
    #: working on it, and distinct from FAILED because nothing went wrong:
    #: the client polls on the first and colours the second red.
    WAITING = "waiting"
    #: Closed by the janitor. An absence of evidence, not a verdict.
    ABANDONED = "abandoned"
    #: The row is gone; this node stands in its place.
    PRUNED = "pruned"


class OmissionKind(StrEnum):
    """Why something a reader might expect is not here."""

    RUN_PRUNED = "run_pruned"
    CYCLE_REAPED = "cycle_reaped"
    WINDOW_TRUNCATED = "window_truncated"


class WorkNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    kind: WorkNodeKind
    label: str
    status: WorkNodeStatus
    started_at: UtcDatetime | None = None
    ended_at: UtcDatetime | None = None
    updated_at: UtcDatetime
    attrs: dict[str, Any] = Field(default_factory=dict)


class WorkEdge(BaseModel):
    # `from` is a keyword in Python and the obvious name on the wire, so the
    # field is aliased rather than renamed into something nobody would guess.
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    kind: WorkEdgeKind
    from_key: str = Field(serialization_alias="from", validation_alias="from")
    to_key: str = Field(serialization_alias="to", validation_alias="to")


class Omission(BaseModel):
    """Something the graph knows it cannot show.

    Mandatory rather than optional: a view of autonomous work that cannot say
    "I do not know" is worse than no view, because a reader cannot tell the
    difference between nothing having happened and nothing having been kept.
    """

    model_config = ConfigDict(extra="forbid")

    kind: OmissionKind
    key: str | None = None
    detail: str


class WorkGraphResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = SCHEMA_VERSION
    service_id: str
    org_id: str | None
    generated_at: UtcDatetime
    cursor: str | None
    has_more: bool
    #: Nodes currently working. The client polls while this is non-zero and
    #: stops entirely when it is zero, which is why it is a count and not a
    #: boolean: a reader of the response can see how much is going on.
    in_flight: int
    nodes: list[WorkNode]
    edges: list[WorkEdge]
    removed: list[str] = Field(default_factory=list)
    omissions: list[Omission] = Field(default_factory=list)
