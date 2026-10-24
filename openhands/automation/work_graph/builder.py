"""Rows to graph.

The spine of all autonomous work is already in this service's tables: a
program schedules runs, a run claims a cycle, a cycle targets a repository and
is built and reviewed by agents, and produces a branch and a pull request from
sources it cited. This turns those rows into nodes and edges without storing a
second copy of any of it.

Three rules shape everything here:

- **Say what is missing.** A run that was pruned still gets a node, marked as
  such, so the chain from a program to its older work stays connected. An
  audit view that cannot say "I do not know" is worse than none.
- **A review is an agent, not a third kind.** A reviewer is an agent doing
  work, and modelling it twice would make "click an agent, read its
  transcript" ambiguous.
- **Nothing is invented.** A cycle that reported no sources contributes no
  source nodes; it does not contribute an empty one.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from openhands.automation.models import (
    Automation,
    AutomationImproveCycle,
    AutomationRun,
    AutomationRunStatus,
    ImproveCycleStatus,
)
from openhands.automation.work_graph.schemas import (
    Omission,
    OmissionKind,
    WorkEdge,
    WorkEdgeKind,
    WorkNode,
    WorkNodeKind,
    WorkNodeStatus,
)


_RUN_STATUS: dict[AutomationRunStatus, WorkNodeStatus] = {
    AutomationRunStatus.PENDING: WorkNodeStatus.PENDING,
    AutomationRunStatus.RUNNING: WorkNodeStatus.IN_FLIGHT,
    AutomationRunStatus.COMPLETED: WorkNodeStatus.DONE,
    AutomationRunStatus.FAILED: WorkNodeStatus.FAILED,
}

_CYCLE_STATUS: dict[ImproveCycleStatus, WorkNodeStatus] = {
    ImproveCycleStatus.CLAIMED: WorkNodeStatus.PENDING,
    ImproveCycleStatus.BUILDING: WorkNodeStatus.IN_FLIGHT,
    ImproveCycleStatus.REVIEWING: WorkNodeStatus.IN_FLIGHT,
    ImproveCycleStatus.COMPLETED: WorkNodeStatus.DONE,
    ImproveCycleStatus.FAILED: WorkNodeStatus.FAILED,
}


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def repository_key(url: str, ref: str | None) -> str:
    """Content-addressed, so two deployments improving one repository meet.

    The only nodes that are the same thing across backends are the ones not
    owned by any of them: a repository, a cited source, a pull request. That
    is what lets a reader see "both of my servers are working on this".
    """
    return f"repo:{url}@{ref or 'HEAD'}"


def _source_key(url: str) -> str:
    return f"source:{url}"


def _suggestion_key(text: str) -> str:
    digest = hashlib.sha256(text.strip().lower().encode("utf-8")).hexdigest()
    return f"suggestion:{digest[:16]}"


class GraphAccumulator:
    """Collects nodes and edges, keeping one of each."""

    def __init__(self) -> None:
        self.nodes: dict[str, WorkNode] = {}
        self.edges: dict[tuple[str, str, str], WorkEdge] = {}
        self.omissions: list[Omission] = []

    def node(self, node: WorkNode) -> None:
        existing = self.nodes.get(node.key)
        if existing is None:
            self.nodes[node.key] = node
            return
        # Same thing seen twice — a repository two cycles both targeted. Keep
        # the earliest start and the latest activity, so the node describes
        # the whole of what is known rather than whichever row came last.
        self.nodes[node.key] = existing.model_copy(
            update={
                "started_at": min(
                    filter(None, (existing.started_at, node.started_at)),
                    default=None,
                ),
                "updated_at": max(existing.updated_at, node.updated_at),
                "status": (
                    WorkNodeStatus.IN_FLIGHT
                    if WorkNodeStatus.IN_FLIGHT in (existing.status, node.status)
                    else existing.status
                ),
            }
        )

    def edge(self, kind: WorkEdgeKind, from_key: str, to_key: str) -> None:
        self.edges[(kind.value, from_key, to_key)] = WorkEdge(
            kind=kind, **{"from": from_key, "to": to_key}
        )

    def omit(self, kind: OmissionKind, detail: str, key: str | None = None) -> None:
        self.omissions.append(Omission(kind=kind, key=key, detail=detail))


def _agent_node(
    conversation_id: str,
    label: str,
    status: WorkNodeStatus,
    updated_at: datetime,
    attrs: dict[str, Any],
) -> WorkNode:
    return WorkNode(
        key=f"agent:{conversation_id}",
        kind=WorkNodeKind.AGENT,
        label=label,
        status=status,
        updated_at=updated_at,
        attrs=attrs,
    )


def build_graph(
    automations: Sequence[Automation],
    runs: Sequence[AutomationRun],
    cycles: Sequence[AutomationImproveCycle],
    *,
    truncated: bool,
) -> GraphAccumulator:
    """Project rows into nodes and edges."""
    graph = GraphAccumulator()
    run_ids = {run.id for run in runs}

    for automation in automations:
        graph.node(
            WorkNode(
                key=f"automation:{automation.id}",
                kind=WorkNodeKind.PROGRAM,
                label=automation.name,
                status=(
                    WorkNodeStatus.PENDING
                    if automation.enabled
                    else WorkNodeStatus.DONE
                ),
                updated_at=_utc(automation.updated_at) or datetime.now(UTC),
                attrs={"enabled": automation.enabled},
            )
        )

    for run in runs:
        key = f"run:{run.id}"
        graph.node(
            WorkNode(
                key=key,
                kind=WorkNodeKind.RUN,
                label=run.status.value,
                status=_RUN_STATUS.get(run.status, WorkNodeStatus.DONE),
                started_at=_utc(run.started_at),
                ended_at=_utc(run.completed_at),
                updated_at=_utc(run.created_at) or datetime.now(UTC),
                attrs={"trigger": run.trigger_source, "phase": run.current_phase},
            )
        )
        graph.edge(WorkEdgeKind.SCHEDULES, f"automation:{run.automation_id}", key)

    for cycle in cycles:
        cycle_key = f"cycle:{cycle.id}"
        status = _CYCLE_STATUS.get(cycle.status, WorkNodeStatus.DONE)
        if cycle.reaped:
            status = WorkNodeStatus.ABANDONED
            graph.omit(
                OmissionKind.CYCLE_REAPED,
                "Closed by the janitor, so nothing was reported about it.",
                cycle_key,
            )
        updated = _utc(cycle.updated_at) or datetime.now(UTC)
        graph.node(
            WorkNode(
                key=cycle_key,
                kind=WorkNodeKind.CYCLE,
                label=cycle.repo_label,
                status=status,
                started_at=_utc(cycle.created_at),
                ended_at=_utc(cycle.completed_at),
                updated_at=updated,
                attrs={
                    "verdict": cycle.verdict.value if cycle.verdict else None,
                    "summary": cycle.summary,
                    "builder_runtime": cycle.builder_runtime,
                    "head_sha": cycle.head_sha,
                    "base_sha": cycle.base_sha,
                    "changed_files": cycle.changed_files or [],
                    "checks": cycle.checks or [],
                    "risks": cycle.risks or [],
                    "followups": cycle.followups or [],
                    "prompt_digest": cycle.prompt_digest,
                    "verdict_rule_version": cycle.verdict_rule_version,
                    "runtime_switches": cycle.runtime_switches or [],
                    # Who was allowed to publish this, and on what evidence.
                    "change_class": cycle.change_class,
                    "claimed_change_class": cycle.claimed_change_class,
                    "change_evidence": cycle.change_evidence or {},
                    "reaped": cycle.reaped,
                    "error": cycle.error,
                },
            )
        )

        if cycle.run_id is not None and cycle.run_id in run_ids:
            graph.edge(WorkEdgeKind.CLAIMS, f"run:{cycle.run_id}", cycle_key)
        else:
            # The run is gone — pruned, with the link nulled. A tombstone
            # keeps the chain to the program intact; dropping the edge would
            # make older work progressively less explicable.
            tomb = f"run:pruned-{cycle.id}"
            graph.node(
                WorkNode(
                    key=tomb,
                    kind=WorkNodeKind.RUN_TOMBSTONE,
                    label="pruned",
                    status=WorkNodeStatus.PRUNED,
                    updated_at=updated,
                    attrs={},
                )
            )
            graph.edge(
                WorkEdgeKind.SCHEDULES, f"automation:{cycle.automation_id}", tomb
            )
            graph.edge(WorkEdgeKind.CLAIMS, tomb, cycle_key)
            graph.omit(
                OmissionKind.RUN_PRUNED,
                "The run that claimed this cycle has been pruned.",
                cycle_key,
            )

        repo = repository_key(cycle.repo_url, cycle.repo_ref)
        graph.node(
            WorkNode(
                key=repo,
                kind=WorkNodeKind.REPOSITORY,
                label=cycle.repo_label,
                status=status,
                updated_at=updated,
                attrs={"url": cycle.repo_url, "ref": cycle.repo_ref},
            )
        )
        graph.edge(WorkEdgeKind.TARGETS, cycle_key, repo)

        artifact_keys: list[str] = []
        if cycle.branch:
            branch_key = f"branch:{cycle.repo_url}#{cycle.branch}"
            graph.node(
                WorkNode(
                    key=branch_key,
                    kind=WorkNodeKind.ARTIFACT,
                    label=cycle.branch,
                    status=status,
                    updated_at=updated,
                    attrs={"kind": "branch", "head_sha": cycle.head_sha},
                )
            )
            artifact_keys.append(branch_key)
        if cycle.pr_url:
            pr_key = f"pr:{cycle.pr_url}"
            graph.node(
                WorkNode(
                    key=pr_key,
                    kind=WorkNodeKind.ARTIFACT,
                    label=cycle.pr_url,
                    status=status,
                    updated_at=updated,
                    attrs={"kind": "pull-request", "url": cycle.pr_url},
                )
            )
            artifact_keys.append(pr_key)

        builder_key: str | None = None
        if cycle.builder_conversation_id:
            builder_key = f"agent:{cycle.builder_conversation_id}"
            graph.node(
                _agent_node(
                    cycle.builder_conversation_id,
                    cycle.builder_runtime or "builder",
                    status,
                    updated,
                    {
                        "role": "builder",
                        "runtime": cycle.builder_runtime,
                        # The runtimes this builder moved through, oldest
                        # first, with the reason for each hop. Carried on the
                        # node rather than drawn as an edge because the node
                        # key is the conversation id and every hop shared one
                        # conversation: a second node would either collide
                        # with this one or claim a transcript that cannot be
                        # read apart from it.
                        "runtime_switches": cycle.runtime_switches or [],
                        "conversation_id": cycle.builder_conversation_id,
                    },
                )
            )
            graph.edge(WorkEdgeKind.BUILDS, cycle_key, builder_key)
            for artifact in artifact_keys:
                graph.edge(WorkEdgeKind.PRODUCES, builder_key, artifact)

        for source in cycle.sources or []:
            url = source.get("url") if isinstance(source, dict) else None
            if not isinstance(url, str) or not url:
                continue
            key = _source_key(url)
            graph.node(
                WorkNode(
                    key=key,
                    kind=WorkNodeKind.SOURCE,
                    label=source.get("title") or url,
                    status=WorkNodeStatus.DONE,
                    updated_at=updated,
                    attrs={"url": url, "note": source.get("note")},
                )
            )
            if builder_key is not None:
                graph.edge(WorkEdgeKind.CITES, builder_key, key)

        for index, review in enumerate(cycle.reviews or []):
            if not isinstance(review, dict):
                continue
            conversation = review.get("conversation_id")
            review_key = (
                f"agent:{conversation}"
                if isinstance(conversation, str) and conversation
                else f"agent:{cycle.id}-review-{index}"
            )
            failed = bool(review.get("failed"))
            graph.node(
                _agent_node(
                    review_key.removeprefix("agent:"),
                    str(review.get("runtime") or "reviewer"),
                    WorkNodeStatus.FAILED if failed else WorkNodeStatus.DONE,
                    updated,
                    {
                        "role": "reviewer",
                        "runtime": review.get("runtime"),
                        "verdict": review.get("verdict"),
                        "concerns": review.get("concerns") or [],
                        "updates_checked": review.get("updates_checked"),
                        "checks": review.get("checks") or [],
                        "failed": failed,
                        "conversation_id": conversation,
                    },
                )
            )
            # A reviewer judges the change, not the builder.
            for artifact in artifact_keys:
                graph.edge(WorkEdgeKind.REVIEWS, review_key, artifact)
            if not artifact_keys:
                graph.edge(WorkEdgeKind.REVIEWS, review_key, cycle_key)
            for stale in review.get("stale_sources") or []:
                if isinstance(stale, str) and stale.startswith("http"):
                    graph.edge(WorkEdgeKind.DISPUTES, review_key, _source_key(stale))
            for verified in review.get("verified_sources") or []:
                if isinstance(verified, str) and verified.startswith("http"):
                    graph.edge(WorkEdgeKind.VERIFIES, review_key, _source_key(verified))

        for suggestion in cycle.soul_suggestions or []:
            text = suggestion.get("text") if isinstance(suggestion, dict) else None
            if not isinstance(text, str) or not text.strip():
                continue
            key = _suggestion_key(text)
            graph.node(
                WorkNode(
                    key=key,
                    kind=WorkNodeKind.SUGGESTION,
                    label=text.strip()[:120],
                    # Outstanding human work: nothing changes until the user
                    # accepts it, so it is never "done".
                    status=WorkNodeStatus.PENDING,
                    updated_at=updated,
                    attrs={"text": text, "why": suggestion.get("why")},
                )
            )
            if builder_key is not None:
                graph.edge(WorkEdgeKind.PROPOSES, builder_key, key)

    if truncated:
        graph.omit(
            OmissionKind.WINDOW_TRUNCATED,
            "Older work is not shown: the window reached its limit.",
        )
    return graph


def count_in_flight(graph: GraphAccumulator) -> int:
    """Nodes currently working, which is what the client polls on."""
    return sum(
        1 for node in graph.nodes.values() if node.status is WorkNodeStatus.IN_FLIGHT
    )
