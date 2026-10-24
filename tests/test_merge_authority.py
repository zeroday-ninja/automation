"""Who may publish a change, and whether its evidence is there.

Three properties matter here. A breaking change has no publishing path at all,
for anybody. A deletion needs a signature that is not the author's. And a
change that cannot show that its verification *ran* stands as a proposal rather
than being merged on an assertion.
"""

import pytest

from openhands.automation.improve.authority import (
    readiness_gaps,
    resolve_authority,
)


READY = {
    "head_sha": "a1b2c3d",
    "changed_files": ["openhands/service.py"],
    "checks": [{"command": "pytest tests/", "passed": True}],
    "summary": "Pinned the parser to the patched release.",
    "builder_runtime": "claude-code",
}

ATTESTED = {
    "attested": True,
    "runtime": "codex",
    "summary": "Both removals are dead; nothing imports them.",
    "removals": [
        {
            "path": "openhands/legacy.py",
            "symbol": "resolve_backend",
            "finding": "dead",
            "where": "grep -rn resolve_backend returns only its definition",
        }
    ],
}


def test_an_additive_change_publishes_on_its_own_account() -> None:
    authority = resolve_authority(
        change_class="additive", judge_attestation=None, **READY
    )

    assert authority.authorised is True
    assert authority.route == "approve"


def test_a_breaking_change_has_no_publishing_path_however_good_its_evidence() -> None:
    # Answered before readiness on purpose: perfect evidence does not make it
    # an agent's decision, and asking for more first would be a lie about what
    # the answer will be.
    authority = resolve_authority(
        change_class="breaking", judge_attestation=ATTESTED, **READY
    )

    assert authority.authorised is False
    assert authority.route == "escalate"
    # Distinct from "not yet": one is a wait, the other is a different act.
    assert authority.human_only is True
    assert authority.missing == ()


def test_a_deletion_needs_a_signature_and_not_the_authors() -> None:
    # Arrange: the judge ran on the runtime that built the change.
    self_signed = {**ATTESTED, "runtime": "claude-code"}

    authority = resolve_authority(
        change_class="deleting", judge_attestation=self_signed, **READY
    )

    assert authority.authorised is False
    assert "never the author" in authority.reason


def test_an_attested_deletion_from_another_runtime_publishes() -> None:
    authority = resolve_authority(
        change_class="deleting", judge_attestation=ATTESTED, **READY
    )

    assert authority.authorised is True
    assert "codex" in authority.reason


def test_a_deletion_with_no_attestation_waits_rather_than_cycling() -> None:
    authority = resolve_authority(
        change_class="deleting", judge_attestation=None, **READY
    )

    assert authority.route == "return"
    assert "judge_attestation" in authority.missing


def test_a_judge_that_refused_sends_the_work_back_round() -> None:
    refused = {**ATTESTED, "attested": False, "summary": "legacy.py is still imported"}

    authority = resolve_authority(
        change_class="deleting", judge_attestation=refused, **READY
    )

    # Not "return": there is something specific to fix.
    assert authority.route == "cycle"
    assert "still imported" in authority.reason


def test_a_claim_that_verification_would_pass_is_not_evidence_that_it_ran() -> None:
    for checks in ([], None, [{"command": "wc -l", "passed": None}]):
        authority = resolve_authority(
            change_class="additive",
            judge_attestation=None,
            **{**READY, "checks": checks},
        )

        assert authority.authorised is False
        assert authority.route == "return"
        assert "checks" in authority.missing
        assert "proposal" in authority.reason


def test_a_failing_check_is_evidence_and_says_so() -> None:
    authority = resolve_authority(
        change_class="additive",
        judge_attestation=None,
        **{**READY, "checks": [{"command": "npm run lint", "passed": False}]},
    )

    # A failing check is not missing paperwork, and the answer must not read
    # as though it were.
    assert authority.route == "cycle"
    assert "did not pass" in authority.reason
    assert "npm run lint" in authority.reason
    assert authority.missing == ()


def test_an_unknown_class_returns_rather_than_publishing() -> None:
    authority = resolve_authority(change_class=None, judge_attestation=None, **READY)

    assert authority.authorised is False
    assert "change_class" in authority.missing


@pytest.mark.parametrize(
    ("field", "value"),
    [("head_sha", None), ("changed_files", []), ("summary", "  ")],
)
def test_each_readiness_requirement_is_named_when_it_is_missing(
    field: str, value: object
) -> None:
    complete = {
        "head_sha": READY["head_sha"],
        "changed_files": READY["changed_files"],
        "checks": READY["checks"],
        "summary": READY["summary"],
    }

    gaps = readiness_gaps(**{**complete, field: value})

    # Named so the answer is actionable rather than only negative.
    assert gaps == (field,)
