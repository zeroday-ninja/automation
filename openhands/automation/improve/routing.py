"""Where a gate sends work, as opposed to what it thought of it.

A verdict says whether a change is good. It does not say what happens next, and
conflating the two is why a `CHANGES_REQUESTED` cycle used to simply stop: the
judgement was recorded and the work went nowhere. Every gate here records one
route from a closed set, so a failed gate *sends* work somewhere.

``escalate`` exists because an agent must not assume it is the final authority.
It is the only route that hands the decision to a person, and it is the route a
breaking change always takes.

Deliberately dependency-free (stdlib only): packaged into the improvement
program's tarball and imported by ``main.py`` inside a sandbox that has no
``openhands.automation`` installed.
"""

from __future__ import annotations

from dataclasses import dataclass


#: The closed set. Nothing else is a route.
#:
#: ``approve``  — publish it.
#: ``cycle``    — send it back round for another pass; the work survives.
#: ``return``   — hand it back for more evidence before it can be judged.
#: ``archive``  — stop, deliberately, and say so. Not a failure to record.
#: ``escalate`` — a person decides. The agent is not the authority here.
ROUTES: tuple[str, ...] = ("approve", "cycle", "return", "archive", "escalate")

#: Phases a gate decision can be recorded at.
GATE_PHASES: tuple[str, ...] = ("build", "classify", "review", "judge", "publish")


@dataclass(frozen=True, slots=True)
class GateDecision:
    """One routing decision, as data rather than as a sentence.

    ``rationale`` is required. A route with no stated reason cannot be
    reviewed, and an unreviewable decision is how an invented stop criterion
    survives: the point of writing it down is that somebody can disagree.
    """

    phase: str
    route: str
    trigger: str
    rationale: str
    verdict: str | None = None
    change_class: str | None = None
    recommends: str | None = None
    decided_by: str | None = None
    at: str | None = None

    def __post_init__(self) -> None:
        if self.route not in ROUTES:
            raise ValueError(
                f"{self.route!r} is not a route; expected one of {', '.join(ROUTES)}"
            )
        if self.phase not in GATE_PHASES:
            raise ValueError(
                f"{self.phase!r} is not a gate phase; expected one of "
                f"{', '.join(GATE_PHASES)}"
            )
        if not self.rationale.strip():
            raise ValueError(
                "a gate decision needs a rationale: a route with no "
                "stated reason cannot be reviewed"
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "phase": self.phase,
            "route": self.route,
            "trigger": self.trigger,
            "rationale": self.rationale,
            "verdict": self.verdict,
            "change_class": self.change_class,
            "recommends": self.recommends,
            "decided_by": self.decided_by,
            "at": self.at,
        }


#: What each verdict means for where the work goes, with nothing else known.
#:
#: ``INCONCLUSIVE`` returns rather than cycling: nothing is wrong with the
#: change that anybody has named, so sending it back to be rebuilt would be
#: rework on no evidence. What it lacks is a judgement.
#:
#: ``SINGLE_RUNTIME`` approves. It is not a quality judgement -- it records
#: that no second runtime was available -- and refusing to publish on that
#: basis would make a one-runtime deployment unable to do anything at all.
_VERDICT_ROUTE: dict[str, str] = {
    "ACCEPTED": "approve",
    "SINGLE_RUNTIME": "approve",
    "INCONCLUSIVE": "return",
    "CHANGES_REQUESTED": "cycle",
    "REJECTED": "archive",
}


def route_for(
    verdict: str | None,
    change_class: str | None,
    *,
    judge_attested: bool | None = None,
) -> tuple[str, str]:
    """Where this cycle's work goes, and what sent it there.

    The class can only ever make the route *more* restrictive, never less: a
    breaking change escalates however well it reviewed, because the question a
    human is being asked is not "is this good" but "do you want this now".
    """
    base = _VERDICT_ROUTE.get(verdict or "", "return")
    trigger = f"verdict {verdict or 'missing'}"

    if base in ("archive", "cycle"):
        # Already not publishing. The class cannot make "send it back" worse,
        # and escalating something nobody wants published would ask the user a
        # question with no useful answer.
        return base, trigger

    if change_class == "breaking":
        return "escalate", "a breaking change is never an agent's decision"
    if change_class == "deleting":
        if judge_attested is True:
            return base, f"{trigger}; the judge attested each removal"
        if judge_attested is False:
            return "cycle", "the judge would not attest a removal"
        return "return", "a deletion needs a judge's attestation first"
    if change_class is None:
        # The diff could not be read. Publishing on an unknown class would be
        # treating absence of evidence as evidence of safety.
        return "return", "the change class could not be computed"
    return base, trigger


def normalize_route(value: object) -> str | None:
    """A route name, or ``None`` when it is not one of the five."""
    if not isinstance(value, str):
        return None
    folded = value.strip().casefold()
    return folded if folded in ROUTES else None
