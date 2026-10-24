"""The work graph: what the agents are doing, and what they did.

A projection of rows this service already keeps. What is worth protecting is
that it stays honest about what it cannot show — a pruned run, a reaped cycle,
a truncated window — because a view of autonomous work that cannot say "I do
not know" is worse than no view at all.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from openhands.automation.config import clear_config_cache
from openhands.automation.models import (
    Automation,
    AutomationImproveCycle,
    AutomationRun,
    AutomationRunStatus,
    ImproveCycleStatus,
    ImproveVerdict,
)
from openhands.automation.watchdog import reap_abandoned_cycles


ORG_ID = uuid.UUID("87654321-4321-8765-4321-876543218765")
USER_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")
GRAPH = "/api/automation/v1/work-graph"


@pytest.fixture(autouse=True)
def local_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOMATION_AGENT_SERVER_URL", "http://localhost:3000")
    monkeypatch.setenv("AUTOMATION_WORKSPACE_BASE", str(tmp_path))
    clear_config_cache()
    yield
    clear_config_cache()


async def _program(async_session, name: str = "Nightly improvement") -> Automation:
    automation = Automation(
        id=uuid.uuid4(),
        org_id=ORG_ID,
        user_id=USER_ID,
        name=name,
        trigger={"type": "cron", "schedule": "0 3 * * *", "timezone": "UTC"},
        enabled=True,
        tarball_path="uploads/test.tar.gz",
        entrypoint="python main.py",
    )
    async_session.add(automation)
    await async_session.flush()
    return automation


async def _run(async_session, automation, status=AutomationRunStatus.COMPLETED):
    run = AutomationRun(
        id=uuid.uuid4(),
        automation_id=automation.id,
        status=status,
        trigger_source="cron",
    )
    async_session.add(run)
    await async_session.flush()
    return run


async def _cycle(async_session, automation, run, **overrides):
    cycle = AutomationImproveCycle(
        id=uuid.uuid4(),
        automation_id=automation.id,
        org_id=ORG_ID,
        run_id=run.id if run is not None else None,
        repo_index=0,
        repo_label="hazard",
        repo_url="https://github.com/zeroday-ninja/HAZARD",
        repo_ref="main",
        status=ImproveCycleStatus.COMPLETED,
        verdict=ImproveVerdict.ACCEPTED,
        builder_runtime="claude-code",
        builder_conversation_id="conv-builder",
        summary="Replaced the polling loop.",
        branch="improve/20261024",
        head_sha="a1b2c3d",
        sources=[{"url": "https://docs.python.org/3/", "title": "docs"}],
        reviews=[
            {
                "runtime": "codex",
                "conversation_id": "conv-review",
                "verdict": "ACCEPTED",
                "verified_sources": ["https://docs.python.org/3/"],
                "stale_sources": [],
                "failed": False,
            }
        ],
    )
    for key, value in overrides.items():
        setattr(cycle, key, value)
    async_session.add(cycle)
    await async_session.flush()
    return cycle


@pytest.mark.asyncio
async def test_a_cycle_becomes_a_chain_from_program_to_evidence(
    async_client, async_session
):
    # Arrange
    automation = await _program(async_session)
    run = await _run(async_session, automation)
    cycle = await _cycle(async_session, automation, run)
    await async_session.commit()

    # Act
    response = await async_client.get(GRAPH)

    # Assert: the whole chain, and the reviewer pointed at the change rather
    # than at the builder — a reviewer judges the work, not the worker.
    assert response.status_code == 200, response.text
    body = response.json()
    kinds = {node["key"]: node["kind"] for node in body["nodes"]}
    assert kinds[f"automation:{automation.id}"] == "program"
    assert kinds[f"run:{run.id}"] == "run"
    assert kinds[f"cycle:{cycle.id}"] == "cycle"
    assert kinds["agent:conv-builder"] == "agent"
    assert kinds["agent:conv-review"] == "agent"
    edges = {(edge["kind"], edge["from"], edge["to"]) for edge in body["edges"]}
    assert ("claims", f"run:{run.id}", f"cycle:{cycle.id}") in edges
    assert ("builds", f"cycle:{cycle.id}", "agent:conv-builder") in edges
    assert (
        "reviews",
        "agent:conv-review",
        "branch:https://github.com/zeroday-ninja/HAZARD#improve/20261024",
    ) in edges
    assert (
        "verifies",
        "agent:conv-review",
        "source:https://docs.python.org/3/",
    ) in edges


@pytest.mark.asyncio
async def test_a_pruned_run_leaves_a_tombstone_rather_than_a_broken_chain(
    async_client, async_session
):
    # Arrange: the run was pruned, which nulls the cycle's link to it.
    automation = await _program(async_session)
    cycle = await _cycle(async_session, automation, None)
    await async_session.commit()

    # Act
    body = (await async_client.get(GRAPH)).json()

    # Assert: the program still reaches its own older work, and the graph
    # says why the middle of the chain is a placeholder.
    kinds = {node["key"]: node["kind"] for node in body["nodes"]}
    tomb = f"run:pruned-{cycle.id}"
    assert kinds[tomb] == "run-tombstone"
    edges = {(edge["kind"], edge["from"], edge["to"]) for edge in body["edges"]}
    assert ("schedules", f"automation:{automation.id}", tomb) in edges
    assert ("claims", tomb, f"cycle:{cycle.id}") in edges
    assert any(omission["kind"] == "run_pruned" for omission in body["omissions"])


@pytest.mark.asyncio
async def test_two_cycles_on_one_repository_meet_at_a_single_node(
    async_client, async_session
):
    # Arrange: the same repository, improved twice.
    automation = await _program(async_session)
    first = await _cycle(
        async_session, automation, await _run(async_session, automation)
    )
    second = await _cycle(
        async_session, automation, await _run(async_session, automation)
    )
    await async_session.commit()

    # Act
    body = (await async_client.get(GRAPH)).json()

    # Assert: repositories are content-addressed, which is what lets two
    # deployments working on one project meet in a single picture.
    repos = [node for node in body["nodes"] if node["kind"] == "repository"]
    assert len(repos) == 1
    targets = {edge["from"] for edge in body["edges"] if edge["kind"] == "targets"}
    assert targets == {f"cycle:{first.id}", f"cycle:{second.id}"}


@pytest.mark.asyncio
async def test_work_in_flight_is_counted_so_a_client_can_stop_polling(
    async_client, async_session
):
    # Arrange: one cycle still building.
    automation = await _program(async_session)
    run = await _run(async_session, automation, AutomationRunStatus.RUNNING)
    await _cycle(
        async_session,
        automation,
        run,
        status=ImproveCycleStatus.BUILDING,
        verdict=None,
    )
    await async_session.commit()

    # Act
    working = (await async_client.get(GRAPH)).json()

    # Assert
    assert working["in_flight"] >= 1


@pytest.mark.asyncio
async def test_a_reaped_cycle_is_an_absence_of_evidence_not_a_verdict(
    async_client, async_session, async_session_factory
):
    # Arrange: a cycle whose run ended without anything ever reporting back.
    automation = await _program(async_session)
    run = await _run(async_session, automation, AutomationRunStatus.FAILED)
    cycle = await _cycle(
        async_session,
        automation,
        run,
        status=ImproveCycleStatus.BUILDING,
        verdict=None,
    )
    await async_session.commit()

    # Act
    reaped = await reap_abandoned_cycles(async_session_factory)

    # Assert: closed, marked, and explained — and never coloured like a
    # failure somebody actually reported.
    assert reaped == 1
    await async_session.refresh(cycle)
    assert cycle.reaped is True
    assert cycle.verdict is None
    assert "janitor" in (cycle.error or "")

    body = (await async_client.get(GRAPH)).json()
    node = next(n for n in body["nodes"] if n["key"] == f"cycle:{cycle.id}")
    assert node["status"] == "abandoned"
    assert any(o["kind"] == "cycle_reaped" for o in body["omissions"])


@pytest.mark.asyncio
async def test_a_cycle_whose_run_is_still_going_is_left_alone(
    async_session, async_session_factory
):
    # Arrange
    automation = await _program(async_session)
    run = await _run(async_session, automation, AutomationRunStatus.RUNNING)
    cycle = await _cycle(
        async_session, automation, run, status=ImproveCycleStatus.BUILDING
    )
    await async_session.commit()

    # Act
    reaped = await reap_abandoned_cycles(async_session_factory)

    # Assert: a long cycle is not a dead one.
    assert reaped == 0
    await async_session.refresh(cycle)
    assert cycle.reaped is False


@pytest.mark.asyncio
async def test_a_cycle_whose_run_was_pruned_is_reaped_only_once_it_is_old(
    async_session, async_session_factory
):
    # Arrange: no run to condemn it, because pruning removed the evidence.
    automation = await _program(async_session)
    recent = await _cycle(
        async_session, automation, None, status=ImproveCycleStatus.BUILDING
    )
    old = await _cycle(
        async_session, automation, None, status=ImproveCycleStatus.BUILDING
    )
    old.updated_at = datetime.now(UTC) - timedelta(days=30)
    await async_session.commit()

    # Act
    reaped = await reap_abandoned_cycles(async_session_factory)

    # Assert: pruning a run must not resurrect a phantom, and must not kill a
    # cycle that is still within the time it was given.
    assert reaped == 1
    await async_session.refresh(recent)
    await async_session.refresh(old)
    assert recent.reaped is False
    assert old.reaped is True


@pytest.mark.asyncio
async def test_a_builder_that_switched_runtime_says_so_on_its_own_node(
    async_client, async_session
):
    # Arrange: the cycle began on claude-code, which ran out, and codex
    # finished the change.
    automation = await _program(async_session)
    run = await _run(async_session, automation)
    cycle = await _cycle(
        async_session,
        automation,
        run,
        builder_runtime="codex",
        runtime_switches=[
            {
                "from": "claude-code",
                "to": "codex",
                "reason": "usage_limit",
                "at": "2026-10-24T08:00:00+00:00",
            }
        ],
    )
    await async_session.commit()

    # Act
    response = await async_client.get(GRAPH)

    # Assert: one agent node, because one conversation served both runtimes,
    # and it carries the route rather than hiding the hop.
    assert response.status_code == 200, response.text
    nodes = {node["key"]: node for node in response.json()["nodes"]}
    builder = nodes["agent:conv-builder"]
    assert builder["attrs"]["runtime"] == "codex"
    assert [
        (hop["from"], hop["to"], hop["reason"])
        for hop in builder["attrs"]["runtime_switches"]
    ] == [("claude-code", "codex", "usage_limit")]
    assert nodes[f"cycle:{cycle.id}"]["attrs"]["runtime_switches"]
