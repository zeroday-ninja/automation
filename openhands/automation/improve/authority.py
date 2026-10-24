"""Who may publish a change, and whether its evidence is there yet.

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


def resolve_authority(
    *,
    change_class: str | None,
    head_sha: str | None,
    changed_files: list | None,
    checks: list | None,
    summary: str | None,
    builder_runtime: str | None,
    judge_attestation: dict | None,
) -> Authority:
    """Whether this change may publish now.

    Order matters. The breaking case is answered before readiness, because
    perfect evidence does not make it an agent's decision and telling somebody
    to go and collect it first would be a lie about what the answer will be.
    """
    if change_class == "breaking":
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

    failures = _failed_checks(checks)
    if failures:
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
    if judge_runtime and builder_runtime and judge_runtime == builder_runtime:
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
            f"Every removal was attested by {judge_runtime or 'the judge'}, "
            "which did not build this change."
        ),
    )
