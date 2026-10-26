"""Who may publish a change, and whether its evidence is there yet.

A review that objected stops a change before any of that. The class decides
*who* may publish; the review decides whether there is anything fit to
publish, and an additive change is not exempt from being wrong. Only an
accepted change reaches the class rules below.

Authority resolves from the change's class, which is computed from its diff, so
nothing here is the builder's opinion of its own work:

- **additive** publishes on its own account;
- **deleting** needs a judge's attestation, from a runtime other than the one
  that built it, because a model favours its own output;
- **breaking** has no publishing path at all. Not "a stricter one" -- there is
  no caller, agent or human, that this module will authorise. A breaking change
  moves by a person answering the parked question, which is a different
  endpoint and a different act.

Before any of that, a readiness check. It asks for the same shape a disclosure
asks for: what changed, what it affects, the argument that it is safe, and
**evidence the verification actually ran** rather than a claim that it would
pass. A change that cannot produce its evidence is returned as a proposal, not
merged on an assertion.
"""

from __future__ import annotations

from dataclasses import dataclass


#: What a change must be able to show before authority is even considered.
#: Each is a question a reviewer would ask, and each has a field that answers
#: it -- so a missing one is a missing answer, not a formality.
READINESS_REQUIREMENTS: tuple[tuple[str, str], ...] = (
    ("head_sha", "which commit this is, so a merge can be traced back to it"),
    ("changed_files", "what it changed"),
    ("checks", "evidence the verification ran, not a claim that it would pass"),
    ("summary", "the argument that it is safe"),
)


@dataclass(frozen=True, slots=True)
class Authority:
    """Whether this change may publish, and what is standing in the way."""

    authorised: bool
    route: str
    reason: str
    #: Named, so the answer is actionable rather than only negative.
    missing: tuple[str, ...] = ()
    #: True when no caller can ever be authorised, however complete the
    #: evidence. Kept distinct from "not yet": one is a wait, the other is a
    #: different act by a different person.
    human_only: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "authorised": self.authorised,
            "route": self.route,
            "reason": self.reason,
            "missing": list(self.missing),
            "human_only": self.human_only,
        }


def readiness_gaps(
    *,
    head_sha: str | None,
    changed_files: list | None,
    checks: list | None,
    summary: str | None,
) -> tuple[str, ...]:
    """Which readiness requirements this change cannot answer.

    ``checks`` is the one that is about evidence rather than description: a
    list of commands with results. An empty list, or one where nothing ran, is
    a claim that verification would pass -- which is the thing being refused.
    """
    gaps: list[str] = []
    if not head_sha:
        gaps.append("head_sha")
    if not changed_files:
        gaps.append("changed_files")
    if not _verification_ran(checks):
        gaps.append("checks")
    if not (summary or "").strip():
        gaps.append("summary")
    return tuple(gaps)


def _verification_ran(checks: list | None) -> bool:
    """True when at least one recorded check passed.

    A check with ``passed`` unset ran but settled nothing -- the stored shape
    uses that for a command whose outcome is a count or a diff rather than a
    pass or a fail. It is evidence that *something* was done and not evidence
    that the change works, so it does not satisfy this on its own.

    A failing check is handled before this is reached: it is evidence, not
    missing evidence, and it deserves its own answer.
    """
    return any(
        isinstance(check, dict) and check.get("passed") is True
        for check in (checks or [])
    )


def _failed_checks(checks: list | None) -> list[str]:
    return [
        str(check.get("command") or "a check")[:120]
        for check in (checks or [])
        if isinstance(check, dict) and check.get("passed") is False
    ]


#: Why each verdict other than ACCEPTED is not something to merge.
#:
#: Phrased as the answer a person would want: what the review concluded, and
#: what to do about it. None of them is a failure of the cycle -- the work is
#: on its branch either way -- so none of them is routed as one.
UNMERGEABLE_VERDICTS: dict[str, tuple[str, str]] = {
    "CHANGES_REQUESTED": (
        "cycle",
        "A reviewer asked for changes. Its concerns are on the cycle; the "
        "change goes round again rather than landing over them.",
    ),
    "REJECTED": (
        "cycle",
        "A reviewer rejected this change. Publishing it would make the "
        "review a formality.",
    ),
    "INCONCLUSIVE": (
        "cycle",
        "No reviewer reached a verdict, so nothing judged this change. "
        "Publishing on that treats absence of evidence as evidence of "
        "safety.",
    ),
    "SINGLE_RUNTIME": (
        "return",
        "No second runtime was available to review this, so it carries only "
        "its builder's own word. It stands as a proposal; the next cycle "
        "with a reviewer free can judge it.",
    ),
    "NO_CHANGE": (
        "archive",
        "This cycle changed nothing, so there is nothing to publish.",
    ),
}


def resolve_authority(
    *,
    change_class: str | None,
    verdict: str | None = None,
    head_sha: str | None,
    changed_files: list | None,
    checks: list | None,
    summary: str | None,
    builder_runtime: str | None,
    judge_attestation: dict | None,
    resolved_route: str | None = None,
    person_insisted: bool = False,
) -> Authority:
    """Whether this change may publish now.

    Order matters. The breaking case is answered before readiness, because
    perfect evidence does not make it an agent's decision and telling somebody
    to go and collect it first would be a lie about what the answer will be.

    ``resolved_route`` is the route a *person* recorded against a parked
    cycle. "A breaking change is never published from here" is a rule about
    agents — the point of escalating is that the thing being gated is not the
    thing that answers — so once the person has answered `approve`, the class
    is no longer what decides. Without it, resolving a park could only ever
    produce the same refusal, and "raise, resolve, resume" had no resume.

    The evidence checks below still apply to a first approval. A person
    approved the change, not the absence of a passing test -- and on the first
    answer they had not been shown the test. ``person_insisted`` is a second
    `approve`, given after the run came back with the failing check named as
    the new question. That is the person's call to make: the two live cases
    that produced this were checks the builders had documented as failing
    identically on main, and refusing a second time was the machine
    overruling the human on a human-only decision. Missing evidence is still
    refused -- there is no "anyway" for a change with no commit to merge.
    """
    approved_by_a_person = resolved_route == "approve"

    if change_class == "breaking" and not approved_by_a_person:
        return Authority(
            authorised=False,
            route="escalate",
            reason=(
                "A breaking change is never published from here. Answer the "
                "parked question and it carries on from where it stopped."
            ),
            human_only=True,
        )

    if change_class is None:
        return Authority(
            authorised=False,
            route="return",
            reason=(
                "The change class could not be computed from the diff, and "
                "publishing on an unknown class would treat absence of "
                "evidence as evidence of safety."
            ),
            missing=("change_class",),
        )

    # Before the class rules, because the class decides who may publish and
    # this decides whether there is anything fit to publish. An additive
    # change publishes on its own account -- not over its reviewer's head.
    if verdict is None:
        return Authority(
            authorised=False,
            route="return",
            reason=(
                "No review has been recorded for this change yet. Authority "
                "is not a thing to collect before the review that decides it."
            ),
            missing=("verdict",),
        )
    if verdict in UNMERGEABLE_VERDICTS and not approved_by_a_person:
        # A reviewer's verdict is an opinion, and the one thing a person's
        # answer is allowed to outrank: the concerns are on the cycle, the
        # person read them when they were asked, and they said publish. Left
        # in force here, their approval was refused once a minute by a review
        # they had already overruled -- observed live. What a person's answer
        # does *not* outrank is below: a check that failed and evidence that is
        # missing are facts, not opinions.
        route, reason = UNMERGEABLE_VERDICTS[verdict]
        return Authority(authorised=False, route=route, reason=reason)

    failures = _failed_checks(checks)
    if failures and not (approved_by_a_person and person_insisted):
        return Authority(
            authorised=False,
            route="cycle",
            reason=(
                "The verification this change ran did not pass: "
                + ", ".join(failures[:3])
            ),
        )

    gaps = readiness_gaps(
        head_sha=head_sha,
        changed_files=changed_files,
        checks=checks,
        summary=summary,
    )
    if gaps:
        wanted = {name: why for name, why in READINESS_REQUIREMENTS}
        return Authority(
            authorised=False,
            route="return",
            reason=(
                "This change cannot show "
                + "; ".join(f"{name} ({wanted[name]})" for name in gaps)
                + ". It stands as a proposal rather than something to merge "
                "on an assertion."
            ),
            missing=gaps,
        )

    if approved_by_a_person:
        # The signature requirement exists because a model favours its own
        # output. A person's answer is not the author's, whatever the class,
        # so it satisfies what the judge was standing in for.
        return Authority(
            authorised=True,
            route="approve",
            reason=(
                "A person answered the question this was parked on and said "
                "to publish it."
            ),
        )

    if change_class == "deleting":
        return _deletion_authority(builder_runtime, judge_attestation)

    return Authority(
        authorised=True,
        route="approve",
        reason="An additive change publishes on its own account.",
    )


def _deletion_authority(
    builder_runtime: str | None, attestation: dict | None
) -> Authority:
    """A deletion needs a signature, and not from the author."""
    if not isinstance(attestation, dict) or not attestation:
        return Authority(
            authorised=False,
            route="return",
            reason="A deletion needs the judge to attest each removal first.",
            missing=("judge_attestation",),
        )
    if attestation.get("attested") is not True:
        detail = str(attestation.get("summary") or "no reason recorded")[:300]
        return Authority(
            authorised=False,
            route="cycle",
            reason=f"The judge would not attest a removal: {detail}",
        )
    judge_runtime = attestation.get("runtime")
    # Both sides have to be known for the comparison to mean anything, and
    # "cannot tell" is not "is not". Either being absent used to pass the
    # check, and the reason given then claimed the signer "did not build this
    # change" -- which the service had no way to know. A null builder runtime
    # needs nobody to misbehave: the progress report that records it is
    # deliberately non-fatal, so a dropped PATCH leaves it empty on a cycle
    # that ran fine.
    if not builder_runtime or not judge_runtime:
        unknown = "builder_runtime" if not builder_runtime else "judge_attestation"
        return Authority(
            authorised=False,
            route="return",
            reason=(
                "The signer is never the author, and this cannot be shown: "
                + (
                    "the runtime that built the change was never recorded."
                    if not builder_runtime
                    else "the attestation does not say which runtime judged it."
                )
            ),
            missing=(unknown,),
        )
    if judge_runtime == builder_runtime:
        # The service checks this rather than trusting the configuration,
        # because a rotation moves the builder between cycles and a model
        # favours its own output.
        return Authority(
            authorised=False,
            route="return",
            reason=(
                f"The attestation came from {judge_runtime}, which is the "
                "runtime that built the change. The signer is never the "
                "author."
            ),
            missing=("judge_attestation",),
        )
    return Authority(
        authorised=True,
        route="approve",
        reason=(
            f"Every removal was attested by {judge_runtime}, which did not "
            f"build this change ({builder_runtime} did)."
        ),
    )
