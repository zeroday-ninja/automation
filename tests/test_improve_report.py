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
