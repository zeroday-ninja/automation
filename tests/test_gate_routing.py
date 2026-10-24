"""Where a gate sends work, and a cycle that waits for a person.

A verdict says whether a change is good; it never said what happened next,
which is why a changes-requested cycle used to simply stop. What is protected
here is that every gate *sends* work somewhere, that a breaking change can
only ever go to a person, and that waiting for that person is not a failure.
"""

import pytest

from openhands.automation.improve.routing import (
    ROUTES,
    GateDecision,
    normalize_route,
    route_for,
)


@pytest.mark.parametrize(
    ("verdict", "expected"),
    [
        ("ACCEPTED", "approve"),
        # Not a quality judgement: it records that no second runtime was
        # available. Refusing to publish on that basis would make a
        # one-runtime deployment unable to do anything at all.
        ("SINGLE_RUNTIME", "approve"),
        ("CHANGES_REQUESTED", "cycle"),
        ("REJECTED", "archive"),
        # Nothing is wrong with the change that anybody has named, so
        # rebuilding it would be rework on no evidence. What it lacks is a
        # judgement.
        ("INCONCLUSIVE", "return"),
    ],
)
def test_an_additive_change_goes_where_its_verdict_sends_it(
    verdict: str, expected: str
) -> None:
    route, trigger = route_for(verdict, "additive")

    assert route == expected
    assert verdict in trigger


def test_a_breaking_change_escalates_however_well_it_reviewed() -> None:
    # The question a person is being asked is not "is this good" but "do you
    # want this now", so a clean review cannot answer it.
    route, trigger = route_for("ACCEPTED", "breaking")

    assert route == "escalate"
    assert "never an agent's decision" in trigger


def test_a_deletion_waits_for_the_judge_and_then_follows_its_verdict() -> None:
    assert route_for("ACCEPTED", "deleting")[0] == "return"
    assert route_for("ACCEPTED", "deleting", judge_attested=True)[0] == "approve"
    # The judge refusing to attest is not "no verdict": it is a reason to go
    # back round, with something specific to fix.
    assert route_for("ACCEPTED", "deleting", judge_attested=False)[0] == "cycle"


def test_an_unknown_class_returns_rather_than_publishing() -> None:
    # Publishing on an unreadable diff would treat absence of evidence as
    # evidence of safety.
    route, trigger = route_for("ACCEPTED", None)

    assert route == "return"
    assert "could not be computed" in trigger


@pytest.mark.parametrize("verdict", ["REJECTED", "CHANGES_REQUESTED"])
def test_the_class_cannot_make_not_publishing_worse(verdict: str) -> None:
    # Escalating something nobody wants published asks a person a question
    # with no useful answer.
    assert route_for(verdict, "breaking")[0] == route_for(verdict, "additive")[0]


def test_a_missing_verdict_returns_instead_of_guessing() -> None:
    assert route_for(None, "additive")[0] == "return"


def test_a_route_with_no_stated_reason_is_refused() -> None:
    # An unreviewable decision is how an invented stop criterion survives:
    # the point of writing it down is that somebody can disagree.
    with pytest.raises(ValueError, match="rationale"):
        GateDecision(phase="review", route="approve", trigger="t", rationale="  ")


def test_a_decision_outside_the_closed_set_is_refused() -> None:
    with pytest.raises(ValueError, match="not a route"):
        GateDecision(phase="review", route="merge-it", trigger="t", rationale="because")
    with pytest.raises(ValueError, match="not a gate phase"):
        GateDecision(
            phase="whenever", route="approve", trigger="t", rationale="because"
        )
    assert normalize_route("Approve") == "approve"
    assert normalize_route("merge-it") is None
    assert set(ROUTES) == {"approve", "cycle", "return", "archive", "escalate"}
