"""Who may publish a change, and whether its evidence is there.

Four properties matter here. A review that objected stops a change outright.
A breaking change has no publishing path at all, for anybody. A deletion needs
a signature that is not the author's. And a change that cannot show that its
verification *ran* stands as a proposal rather than being merged on an
assertion.
"""

import pytest

from openhands.automation.improve.authority import (
    readiness_gaps,
    resolve_authority,
)


READY = {
    "verdict": "ACCEPTED",
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


class TestTheReviewDecidesBeforeTheClassDoes:
    """An additive change publishes on its own account — not over a reviewer.

    This is the failure that made the rule necessary. A real cycle merged a
    change its reviewer had asked for changes on, with a specific and correct
    concern, because authority resolved from the diff's class and never looked
    at what the review concluded:

        review  -> cycle   | ...the new coverage guard gives false assurance...
        publish -> approve | An additive change publishes on its own account.
        publish -> approve | Updating 1e0ed2a..c67956b  Fast-forward

    "No confirmation" in the change-risk table waives the *user's*
    confirmation. The review is a different gate, and it was not being read.
    """

    @pytest.mark.parametrize(
        ("verdict", "route"),
        [
            ("CHANGES_REQUESTED", "cycle"),
            ("REJECTED", "cycle"),
            ("INCONCLUSIVE", "cycle"),
            ("SINGLE_RUNTIME", "return"),
            ("NO_CHANGE", "archive"),
        ],
    )
    def test_a_change_that_was_not_accepted_does_not_publish(
        self, verdict: str, route: str
    ) -> None:
        # Arrange: everything else is perfect — additive, complete evidence.
        ready = {**READY, "verdict": verdict}

        # Act
        authority = resolve_authority(
            change_class="additive", judge_attestation=None, **ready
        )

        # Assert: refused, and routed somewhere rather than simply stopped.
        assert authority.authorised is False
        assert authority.route == route
        assert authority.reason

    def test_asking_before_any_review_is_refused_rather_than_assumed(self) -> None:
        # The preset reports its reviews and *then* asks. A service asked
        # first has not been told what the reviewers concluded, and answering
        # "yes" there is how the original defect happened.
        ready = {**READY, "verdict": None}

        authority = resolve_authority(
            change_class="additive", judge_attestation=None, **ready
        )

        assert authority.authorised is False
        assert authority.missing == ("verdict",)

    def test_a_rejected_review_outranks_a_judge_attestation(self) -> None:
        # The signature answers "is this removal safe to make", not "is this
        # change worth making". A deletion a reviewer rejected is still
        # rejected.
        authority = resolve_authority(
            change_class="deleting",
            judge_attestation=ATTESTED,
            **{**READY, "verdict": "REJECTED"},
        )

        assert authority.authorised is False
        assert authority.route == "cycle"

    def test_a_breaking_change_is_still_answered_before_the_review(self) -> None:
        # Order matters the other way here: an accepted breaking change is
        # still never an agent's decision, and saying "your reviewer objected"
        # would be the wrong reason to refuse it.
        authority = resolve_authority(
            change_class="breaking",
            judge_attestation=ATTESTED,
            **{**READY, "verdict": "CHANGES_REQUESTED"},
        )

        assert authority.human_only is True
        assert authority.route == "escalate"
