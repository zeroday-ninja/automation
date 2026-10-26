"""Tests for the improvement preset's report parsing and reviewer selection.

This is the logic that turns an agent's free-form JSON file into what the
dashboard shows about a cycle, so the cases that matter are the ones where an
agent formats something slightly wrong. A formatting slip must not lose the
cycle's work; a *missing* judgement must never be read as approval.

The module is loaded from its path because `presets/` ships inside the run
tarball and is deliberately not an importable package of the service.
"""

import importlib.util
from pathlib import Path

import pytest


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "openhands"
    / "automation"
    / "presets"
    / "improve"
    / "report.py"
)


@pytest.fixture(scope="module")
def report():
    spec = importlib.util.spec_from_file_location("improve_report", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestReadJsonReport:
    def test_reads_the_report_an_agent_was_asked_to_write(self, report, tmp_path):
        path = tmp_path / "report.json"
        path.write_text('{"status": "changed", "summary": "did a thing"}')

        assert report.read_json_report(str(path)) == {
            "status": "changed",
            "summary": "did a thing",
        }

    def test_unwraps_a_stray_code_fence(self, report, tmp_path):
        # A fence is a formatting slip, not a failed cycle.
        path = tmp_path / "report.json"
        path.write_text('```json\n{"status": "changed"}\n```\n')

        assert report.read_json_report(str(path)) == {"status": "changed"}

    def test_ignores_prose_around_the_object(self, report, tmp_path):
        path = tmp_path / "report.json"
        path.write_text('Here you go:\n{"status": "no_change"}\nHope that helps.')

        assert report.read_json_report(str(path)) == {"status": "no_change"}

    def test_a_missing_file_is_not_an_error(self, report, tmp_path):
        assert report.read_json_report(str(tmp_path / "absent.json")) is None

    def test_unparseable_content_is_not_an_error(self, report, tmp_path):
        path = tmp_path / "report.json"
        path.write_text("{this is not json")

        assert report.read_json_report(str(path)) is None


class TestParseJsonReport:
    """Pulling a report out of whatever the agent actually said."""

    def test_reads_a_bare_object(self, report):
        assert report.parse_json_report('{"verdict": "accepted"}') == {
            "verdict": "accepted"
        }

    def test_unwraps_a_fence_and_ignores_surrounding_prose(self, report):
        text = (
            "Here is my review:\n"
            '```json\n{"verdict": "rejected"}\n```\n'
            "Hope that helps."
        )

        assert report.parse_json_report(text) == {"verdict": "rejected"}

    def test_returns_nothing_for_text_with_no_object(self, report):
        # A model that answered in prose has said nothing a cycle can record,
        # and inventing a verdict from it would be worse than recording none.
        assert report.parse_json_report("Looks fine to me.") is None
        assert report.parse_json_report("") is None


class TestNormalizeSources:
    def test_keeps_a_full_citation(self, report):
        sources = report.normalize_sources(
            [{"url": "https://example.com/a", "title": "A", "note": "supports X"}]
        )

        assert sources == [
            {"url": "https://example.com/a", "title": "A", "note": "supports X"}
        ]

    def test_accepts_a_bare_url_string(self, report):
        assert report.normalize_sources(["https://example.com/a"]) == [
            {"url": "https://example.com/a"}
        ]

    def test_drops_entries_that_are_not_links(self, report):
        # "the docs" is not a source anyone can check, so it is not a source.
        sources = report.normalize_sources(
            [{"note": "the docs"}, {"url": "ftp://example.com"}, "the changelog"]
        )

        assert sources == []


class TestSoulSuggestions:
    def test_deduplicates_what_both_runtimes_noticed(self, report):
        # Both runtimes spotting the same preference is one decision for the
        # user, not two.
        suggestions = report.normalize_soul_suggestions(
            [
                {"text": "Run lint before pushing.", "why": "the repo enforces it"},
                {"text": "run lint before pushing."},
            ]
        )

        assert len(suggestions) == 1
        assert suggestions[0]["why"] == "the repo enforces it"

    def test_accepts_a_bare_string(self, report):
        assert report.normalize_soul_suggestions(["Prefer small diffs."]) == [
            {"text": "Prefer small diffs."}
        ]

    def test_caps_the_number_a_cycle_may_propose(self, report):
        # Each entry costs a human a decision.
        many = [{"text": f"preference {index}"} for index in range(50)]

        assert len(report.normalize_soul_suggestions(many)) == 10


class TestVerdicts:
    def test_a_missing_verdict_is_inconclusive_not_approval(self, report):
        assert report.normalize_verdict(None) == "INCONCLUSIVE"
        assert report.normalize_verdict("looks good to me") == "INCONCLUSIVE"

    def test_reads_the_verdicts_reviewers_are_asked_for(self, report):
        assert report.normalize_verdict("accepted") == "ACCEPTED"
        assert report.normalize_verdict("Changes_Requested") == "CHANGES_REQUESTED"

    def test_the_worst_verdict_wins_when_runtimes_disagree(self, report):
        # One runtime finding the change wrong is the finding.
        assert report.worst_verdict(["ACCEPTED", "REJECTED"]) == "REJECTED"
        assert (
            report.worst_verdict(["ACCEPTED", "CHANGES_REQUESTED"])
            == "CHANGES_REQUESTED"
        )

    def test_no_reviews_is_inconclusive(self, report):
        assert report.worst_verdict([]) == "INCONCLUSIVE"


BUILDER = "b-1"
# Two codex profiles on purpose: the second runtime should be asked once, not
# once per profile that happens to use it.
PROFILES = {
    BUILDER: {"name": "claude-code", "kind": "acp", "runtime": "claude-code"},
    "c-1": {"name": "codex", "kind": "acp", "runtime": "codex"},
    "c-2": {"name": "codex-fast", "kind": "acp", "runtime": "codex"},
    "o-1": {"name": "local", "kind": "openhands", "runtime": "lm-studio"},
}


class TestAcpServerFromDetail:
    """Resolving which runtime a profile actually is."""

    def test_reads_the_nested_profile_the_agent_server_returns(self, report):
        payload = {"name": "claude-code", "profile": {"acp_server": "claude-code"}}

        assert report.acp_server_from_detail(payload) == "claude-code"

    def test_reads_a_top_level_field_too(self, report):
        assert report.acp_server_from_detail({"acp_server": "codex"}) == "codex"

    def test_returns_none_when_the_shape_says_nothing(self, report):
        # The caller falls back to a generic label and warns; it must not
        # invent a runtime, because "which runtime reviewed this" is the one
        # claim the program makes.
        assert report.acp_server_from_detail({"name": "x", "profile": {}}) is None
        assert report.acp_server_from_detail({"acp_server": "  "}) is None
        assert report.acp_server_from_detail(None) is None


class TestCycleVerdict:
    """A reviewer that never judged must not outrank one that did."""

    def test_a_failed_reviewer_does_not_veto_an_acceptance(self, report):
        # The live case: one runtime accepted after verifying, the other's
        # server was down. Counting the dead one as INCONCLUSIVE let a broken
        # runtime hold a permanent veto over every cycle.
        reviews = [
            {"verdict": "ACCEPTED", "failed": False},
            {"verdict": "INCONCLUSIVE", "failed": True},
        ]

        assert report.cycle_verdict(reviews) == "ACCEPTED"

    def test_a_reviewer_that_judged_and_was_unsure_still_counts(self, report):
        reviews = [
            {"verdict": "ACCEPTED", "failed": False},
            {"verdict": "INCONCLUSIVE", "failed": False},
        ]

        assert report.cycle_verdict(reviews) == "INCONCLUSIVE"

    def test_every_reviewer_failing_means_nothing_reviewed_it(self, report):
        reviews = [{"verdict": "INCONCLUSIVE", "failed": True}]

        assert report.cycle_verdict(reviews) == "SINGLE_RUNTIME"

    def test_a_real_rejection_still_wins(self, report):
        reviews = [
            {"verdict": "ACCEPTED", "failed": False},
            {"verdict": "REJECTED", "failed": False},
        ]

        assert report.cycle_verdict(reviews) == "REJECTED"


class TestChooseReviewers:
    def test_picks_one_profile_per_other_runtime_when_none_configured(self, report):
        # Adding a second runtime to the server is enough to turn review on.
        chosen = report.choose_reviewers({}, PROFILES, BUILDER)

        runtimes = sorted(reviewer["runtime"] for reviewer in chosen)
        assert runtimes == ["codex", "lm-studio"]

    def test_never_picks_the_builders_own_runtime(self, report):
        # A runtime reviewing its own work is not independent review.
        chosen = report.choose_reviewers({}, PROFILES, BUILDER)

        assert all(reviewer["runtime"] != "claude-code" for reviewer in chosen)

    def test_a_single_runtime_server_yields_no_reviewers(self, report):
        only_builder = {BUILDER: PROFILES[BUILDER]}

        assert report.choose_reviewers({}, only_builder, BUILDER) == []

    def test_never_returns_the_builder_even_if_configured(self, report):
        # The builder moves between cycles when the program rotates it, so a
        # list that was valid when it was written can name this turn's
        # builder. A runtime reviewing its own work is the one outcome this
        # design exists to prevent.
        chosen = report.choose_reviewers(
            {"reviewer_profile_ids": [BUILDER, "c-1"]}, PROFILES, BUILDER
        )

        assert [reviewer["id"] for reviewer in chosen] == ["c-1"]

    def test_configured_reviewers_win_over_auto_selection(self, report):
        chosen = report.choose_reviewers(
            {"reviewer_profile_ids": ["c-2"]}, PROFILES, BUILDER
        )

        assert [reviewer["id"] for reviewer in chosen] == ["c-2"]

    def test_keeps_a_configured_reviewer_the_server_did_not_list(self, report):
        # The id may still be valid; the conversation will fail loudly if not,
        # which beats silently dropping the review the user asked for.
        chosen = report.choose_reviewers(
            {"reviewer_profile_ids": ["unknown"]}, PROFILES, BUILDER
        )

        assert [reviewer["id"] for reviewer in chosen] == ["unknown"]
        assert chosen[0]["runtime"] is None


class TestNormalizeRemovals:
    """The judge's removals, before they reach the service."""

    def test_an_assertion_without_a_checkable_where_is_dropped(self, report):
        removals = report.normalize_removals(
            [
                {"path": "a.py", "finding": "dead", "where": "grep found nothing"},
                {"path": "b.py", "finding": "dead", "where": "  "},
                {"path": "c.py", "finding": "dead"},
            ]
        )

        # "It looks unused" is an assertion, and an attestation built out of
        # assertions is worse than none because it reads as evidence.
        assert [entry["path"] for entry in removals] == ["a.py"]

    def test_a_finding_outside_the_three_is_dropped(self, report):
        removals = report.normalize_removals(
            [
                {"path": "a.py", "finding": "probably fine", "where": "I looked"},
                {"path": "b.py", "finding": "COVERED", "where": "tests/test_b.py"},
            ]
        )

        assert [entry["finding"] for entry in removals] == ["covered"]

    def test_anything_that_is_not_a_list_of_records_is_nothing(self, report):
        for raw in (None, "dead", 7, [None, 3, "x"], [{"finding": "dead"}]):
            assert report.normalize_removals(raw) == []


class TestChangeoverPlan:
    """What a person needs in order to answer a breaking change."""

    def test_what_the_builder_did_not_supply_is_recorded_as_absent(self, report):
        plan = report.changeover_plan({}, [], "improve/20261024")

        # A plan with a silently empty rollback reads as a plan with no
        # rollback needed, which is the opposite of what an empty field means.
        assert "did not say who depends" in plan["consumers"][0]
        assert "no changeover sequence" in plan["sequence"][0]
        assert "Not stated by the builder" in plan["rollback"]
        assert plan["branch"] == "improve/20261024"

    def test_what_breaks_comes_from_the_diff_not_from_the_builder(self, report):
        plan = report.changeover_plan(
            {"rollback": "revert the server commit"},
            [
                {
                    "rule": "removed_export",
                    "path": "openhands/schemas.py",
                    "detail": "-class EventEnvelope:",
                }
            ],
            "improve/20261024",
        )

        assert plan["breaks"] == [
            "An export was removed from openhands/schemas.py: -class EventEnvelope:"
        ]
        assert plan["rollback"] == "revert the server commit"

    def test_what_breaks_is_said_in_words_and_an_unknown_rule_keeps_its_name(
        self, report
    ):
        plan = report.changeover_plan(
            {},
            [
                {
                    "rule": "deletions_above_threshold",
                    "path": "server/characterize.js",
                    "detail": "19 lines removed (threshold 10)",
                },
                {"rule": "file_deleted", "path": "server/old.js", "detail": "gone"},
                {"rule": "mystery_rule", "path": "a.py", "detail": "something"},
            ],
            "improve/20261024",
        )

        assert plan["breaks"] == [
            "More was deleted from server/characterize.js than the threshold "
            "allows: 19 lines removed (threshold 10)",
            "server/old.js was deleted",
            "mystery_rule in a.py: something",
        ]

    def test_an_unreadable_diff_says_so_rather_than_claiming_nothing_breaks(
        self, report
    ):
        plan = report.changeover_plan({}, None, "improve/20261024")

        assert plan["breaks"] == ["the diff could not be read"]


class TestClaimRefusalPhase:
    """Why the claim refused, in the phase a person reads.

    The service answers a refused claim with a typed code so this does not
    have to guess — and until now the run reported "No repositories
    configured" whatever the reason, which sends somebody to add a repository
    when the truth is "wait until midnight".
    """

    def test_each_typed_reason_says_what_it_is(self, report):
        cases = {
            "no_repositories": "No repositories configured",
            "all_repositories_capped": ("Every repository has reached its daily cap"),
            "daily_ceiling_reached": ("The service's daily cycle ceiling is reached"),
        }
        for code, expected in cases.items():
            assert report.claim_refusal_phase({"code": code}) == expected

    def test_a_cap_carries_when_it_lifts(self, report):
        phrase = report.claim_refusal_phase(
            {
                "code": "all_repositories_capped",
                "resumes_at": "2026-10-25T00:00:00+00:00",
            }
        )

        # "Wait" is only actionable with an "until when".
        assert "resumes 2026-10-25T00:00:00+00:00" in phrase

    def test_a_reason_this_build_has_not_seen_is_reported_as_itself(self, report):
        # Flattening it would be the exact defect this function exists to fix.
        assert "something_new" in report.claim_refusal_phase({"code": "something_new"})

    def test_an_older_service_answering_prose_is_carried_through(self, report):
        phrase = report.claim_refusal_phase(None, "Not an improvement program")

        assert "Not an improvement program" in phrase

    def test_a_refusal_with_nothing_in_it_still_says_something(self, report):
        assert report.claim_refusal_phase(None, "") == "Nothing to claim"
