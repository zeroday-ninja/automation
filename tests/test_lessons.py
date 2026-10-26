"""Corrections, so the same one never has to be given twice.

A document a model forgot to read is indistinguishable from a document that
does not exist, so what is protected here is the pipeline rather than the
storage: a correction is captured from something that happened, scoped, put in
front of the work, and — when it comes back anyway — diagnosed against the
stage that let it through. That last property is the whole difference between
a memory feature and a learning one.
"""

import uuid

import pytest

from openhands.automation.config import clear_config_cache
from openhands.automation.improve.lessons import (
    LESSON_FAILURE_STAGES,
    REPEAT_SIMILARITY,
    diagnose_repeat,
    lessons_in_scope,
    lessons_section,
    normalize_trigger,
    resolution_is_a_correction,
    similarity,
)


ALPHA = "aaaaaaaa-0000-0000-0000-000000000001"
BETA = "aaaaaaaa-0000-0000-0000-000000000002"


@pytest.fixture(autouse=True)
def local_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOMATION_AGENT_SERVER_URL", "http://localhost:3000")
    monkeypatch.setenv("AUTOMATION_WORKSPACE_BASE", str(tmp_path))
    clear_config_cache()
    yield
    clear_config_cache()


def _lesson(**overrides) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "scope": "project",
        "subject": ALPHA,
        "text": "Never force-push to a shared branch without telling me first.",
        "mechanised": False,
        "status": "active",
        **overrides,
    }


class TestRecognisingTheSameCorrectionTwice:
    @pytest.mark.parametrize(
        ("first", "again"),
        [
            # The same correction is rarely phrased the same way, so word
            # order and exact wording cannot be what decides this.
            (
                "Never force-push to a shared branch without telling me.",
                "Do not force-push a shared branch unless you tell me first.",
            ),
            # "renaming" and "rename" are the same word; a stemmer that
            # leaves them apart reads one correction as two.
            (
                "Wait for the client release before renaming a wire contract.",
                "Do not rename a wire contract until the client release is out.",
            ),
        ],
    )
    def test_the_same_point_in_different_words_is_the_same_correction(
        self, first: str, again: str
    ):
        assert similarity(first, again) >= REPEAT_SIMILARITY

    @pytest.mark.parametrize(
        ("one", "other"),
        [
            (
                "Never force-push to a shared branch.",
                "Always run the linter before you open a pull request.",
            ),
            # Same shape, different rule. A match here would hide the second
            # correction behind the first.
            (
                "Run the linter before you open a pull request.",
                "Run the tests before you merge anything.",
            ),
        ],
    )
    def test_two_different_corrections_are_not_confused(self, one: str, other: str):
        assert similarity(one, other) < REPEAT_SIMILARITY

    def test_nothing_matches_an_empty_correction(self):
        assert similarity("", "anything at all") == 0.0


class TestDiagnosingWhichStageFailed:
    """A repeat with no diagnosis is a complaint, not a defect report."""

    def test_a_correction_never_given_before_is_not_a_repeat(self):
        assert (
            diagnose_repeat(
                "Always run the linter before opening a pull request.",
                scope="project",
                subject=ALPHA,
                existing=[_lesson()],
                injected_lesson_ids=[],
            )
            is None
        )

    def test_loaded_and_gone_against_is_ignored(self):
        lesson = _lesson()

        match = diagnose_repeat(
            "Do not force-push a shared branch unless you tell me first.",
            scope="project",
            subject=ALPHA,
            existing=[lesson],
            injected_lesson_ids=[lesson["id"]],
        )

        assert match is not None
        assert match.stage == "ignored"
        # The fix this points at: nothing checks it, so nothing could stop it.
        assert "Nothing checks it" in match.detail

    def test_in_scope_but_not_loaded_is_an_injection_failure(self):
        lesson = _lesson()

        match = diagnose_repeat(
            "Do not force-push a shared branch unless you tell me first.",
            scope="project",
            subject=ALPHA,
            existing=[lesson],
            injected_lesson_ids=[],
        )

        assert match is not None
        assert match.stage == "not_injected"

    def test_work_that_never_said_what_it_loaded_cannot_be_blamed_for_ignoring(
        self,
    ):
        lesson = _lesson()

        match = diagnose_repeat(
            "Do not force-push a shared branch unless you tell me first.",
            scope="project",
            subject=ALPHA,
            existing=[lesson],
            injected_lesson_ids=None,
        )

        # "It had no corrections loaded" and "it never said" are different
        # facts, and only the first is grounds for blaming injection on the
        # work itself.
        assert match is not None
        assert match.stage == "not_injected"
        assert "did not report" in match.detail

    def test_filed_against_another_subject_is_a_scoping_failure(self):
        lesson = _lesson(subject=BETA)

        match = diagnose_repeat(
            "Do not force-push a shared branch unless you tell me first.",
            scope="project",
            subject=ALPHA,
            existing=[lesson],
            injected_lesson_ids=[],
        )

        assert match is not None
        assert match.stage == "out_of_scope"

    def test_a_mechanised_lesson_that_let_it_through_blames_the_check(self):
        lesson = _lesson(mechanised=True)

        match = diagnose_repeat(
            "Do not force-push a shared branch unless you tell me first.",
            scope="project",
            subject=ALPHA,
            existing=[lesson],
            injected_lesson_ids=[lesson["id"]],
        )

        assert match is not None
        assert match.stage == "not_mechanisable"

    def test_every_stage_a_diagnosis_can_name_is_a_known_stage(self):
        # A stage nobody enumerated is a stage no dashboard can count.
        lesson = _lesson()
        cases = [
            (lesson, [lesson["id"]]),
            (lesson, []),
            (lesson, None),
            (_lesson(subject=BETA), []),
            (_lesson(mechanised=True, id=lesson["id"]), [lesson["id"]]),
        ]
        for existing, injected in cases:
            match = diagnose_repeat(
                "Do not force-push a shared branch unless you tell me first.",
                scope="project",
                subject=ALPHA,
                existing=[existing],
                injected_lesson_ids=injected,
            )
            assert match is not None
            assert match.stage in LESSON_FAILURE_STAGES


class TestScoping:
    def test_a_user_level_correction_applies_everywhere(self):
        chosen = lessons_in_scope(
            [_lesson(scope="user", subject=None)],
            repository=BETA,
            runtime="codex",
        )

        assert len(chosen) == 1

    def test_a_correction_that_named_no_subject_applies_to_all_of_them(self):
        # Which is what "I never said which" means.
        chosen = lessons_in_scope(
            [_lesson(subject=None)], repository=BETA, runtime=None
        )

        assert len(chosen) == 1

    def test_another_projects_correction_stays_there(self):
        chosen = lessons_in_scope(
            [_lesson(subject=BETA)], repository=ALPHA, runtime=None
        )

        assert chosen == []

    def test_a_retired_correction_is_not_applied(self):
        chosen = lessons_in_scope(
            [_lesson(status="retired")], repository=ALPHA, runtime=None
        )

        assert chosen == []

    def test_a_surface_correction_is_not_a_builders_business(self):
        chosen = lessons_in_scope(
            [_lesson(scope="surface", subject="map")],
            repository=ALPHA,
            runtime=None,
        )

        assert chosen == []


class TestRendering:
    def test_corrections_are_stated_as_rules_not_as_history(self):
        rendered = lessons_section([_lesson(why="you said so in #81")], 4000)

        # "The user once said" invites a judgement about whether it still
        # applies, which is the judgement that produces a repeat.
        assert "Giving them again is a defect" in rendered
        assert "force-push" in rendered
        assert "you said so in #81" in rendered

    def test_a_truncated_list_says_what_did_not_fit(self):
        many = [
            _lesson(text=f"Correction number {n} about something") for n in range(40)
        ]

        rendered = lessons_section(many, 400)

        # A silently cut list is a list whose last entries were never
        # injected: an injection failure disguised as a complete context.
        assert "did not fit in the context budget" in rendered

    def test_no_corrections_renders_nothing(self):
        assert lessons_section([], 4000) == ""


class TestWhatProducesACorrection:
    def test_an_adapters_incident_is_a_trigger_the_system_raises_itself(self):
        # Every other trigger is something a person did. This one is the
        # runtime's own report, and it is a trigger so it files the same way.
        assert normalize_trigger("adapter_incident") == "adapter_incident"
        assert normalize_trigger(" Adapter_Incident ") == "adapter_incident"
        assert normalize_trigger("adapter_feeling") is None


class TestWhatCountsAsACorrection:
    @pytest.mark.parametrize(
        "answer", ["Yes", "go ahead", "not now", "ok do it", "   "]
    )
    def test_an_acknowledgement_is_not_a_correction(self, answer: str):
        # Storing these would bury the real corrections in a list a person
        # has to read.
        assert resolution_is_a_correction(answer) is False

    def test_an_answer_with_reasoning_is_a_correction(self):
        assert (
            resolution_is_a_correction(
                "Wait for the client release before renaming a wire contract."
            )
            is True
        )
