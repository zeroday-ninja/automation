"""Handing unfinished work to the next cycle.

A cycle whose runtime runs out mid-change has done real work and can publish
none of it. What it leaves behind is the difference between the next cycle
continuing and the next cycle paying to rediscover.
"""

import pytest

from openhands.automation.config import clear_config_cache
from openhands.automation.improve.handoff import (
    degraded_handoff,
    downgrade_unpushed,
    is_resumable,
    parse_handoff,
)


@pytest.fixture(autouse=True)
def local_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOMATION_AGENT_SERVER_URL", "http://localhost:3000")
    monkeypatch.setenv("AUTOMATION_WORKSPACE_BASE", str(tmp_path))
    clear_config_cache()
    yield
    clear_config_cache()


def test_a_handoff_with_no_next_step_is_not_a_handoff() -> None:
    # Arrange: everything but the one field that makes it actionable.
    raw = {
        "status": "incomplete",
        "branch": "improve/20261024",
        "goal": "replace the polling loop",
        "done": ["read the docs"],
        "next_steps": [],
    }

    # Act / Assert: indistinguishable from starting over, and a record the
    # next cycle half-trusts is worse than none, because it will act on it.
    assert parse_handoff(raw) is None


def test_what_was_tried_and_rejected_survives_the_parse() -> None:
    # Arrange
    raw = {
        "next_steps": ["finish the regression test"],
        "do_not": ["threads: the GIL serialised them"],
        "branch": "improve/20261024",
        "branch_pushed": True,
        "head_sha": "a1b2c3d",
    }

    # Act
    handoff = parse_handoff(raw, written_by="claude-code")

    # Assert: this is the field that saves the next runtime its budget.
    assert handoff is not None
    assert handoff.do_not == ["threads: the GIL serialised them"]
    assert handoff.written_by == "claude-code"


def test_a_record_can_be_written_with_no_runtime_left_to_ask() -> None:
    # Act: the degraded path, built from git alone.
    handoff = degraded_handoff(
        branch="improve/20261024",
        head_sha="a1b2c3d",
        branch_pushed=True,
        summary="replacing the polling loop",
        reason="crashed",
    )

    # Assert: always available, costs no tokens, and still points the next
    # cycle at the branch — which beats a branch nobody knows about.
    assert handoff.next_steps
    assert handoff.branch == "improve/20261024"
    assert parse_handoff(handoff.as_dict()) is not None


def test_work_that_was_never_pushed_becomes_advice_rather_than_a_lie() -> None:
    # Arrange: the file was written but the push failed.
    handoff = parse_handoff(
        {
            "next_steps": ["finish the regression test"],
            "do_not": ["threads: the GIL serialised them"],
            "branch": "improve/20261024",
            "branch_pushed": False,
        }
    )
    assert handoff is not None

    # Act
    downgraded = downgrade_unpushed(handoff)

    # Assert: a new sandbox cannot see an unpushed branch, so the commits are
    # gone — but the reasoning is most of the value and it stays.
    assert downgraded.branch is None
    assert "never pushed" in downgraded.next_steps[0]
    assert downgraded.do_not == handoff.do_not


def test_a_branch_that_moved_is_not_resumed_from() -> None:
    # Arrange
    stored = {
        "branch": "improve/20261024",
        "branch_pushed": True,
        "head_sha": "a1b2c3d",
    }

    # Act / Assert: somebody else has been there, so continuing as though the
    # commit were still the tip is how a cycle silently reverts their work.
    assert is_resumable(stored, "a1b2c3d") is True
    assert is_resumable(stored, "9999999") is False
    assert is_resumable({**stored, "branch_pushed": False}, "a1b2c3d") is False
