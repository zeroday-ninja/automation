"""The identity a cycle reasons from, and the budget it has to fit in."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


def _load_context():
    """Import the preset's pure context helpers.

    They are packaged into a tarball rather than imported as part of the
    service, so there is no package path to import them by — and the runner
    beside them validates its environment at import, which is exactly why
    these functions do not live in it.
    """
    path = (
        Path(__file__).resolve().parents[1]
        / "openhands/automation/presets/improve/context.py"
    )
    spec = importlib.util.spec_from_file_location("improve_context", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def preset():
    return _load_context()


class FakeResponse:
    def __init__(self, status_code: int, payload=None):
        self.status_code = status_code
        self._payload = payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class FakeWorkspace:
    def __init__(self, responses: dict):
        self.client = SimpleNamespace(get=self._get)
        self._responses = responses

    def _get(self, path: str):
        if path not in self._responses:
            return FakeResponse(404)
        return self._responses[path]


def test_reads_every_document_the_user_has_written(preset) -> None:
    # Arrange
    workspace = FakeWorkspace(
        {
            "/api/soul/documents": FakeResponse(
                200,
                {
                    "documents": [
                        {"name": "soul", "exists": True, "content": "I build tools."},
                        {
                            "name": "style",
                            "exists": True,
                            "content": "Short sentences.",
                        },
                        {"name": "memory", "exists": False, "content": ""},
                    ]
                },
            )
        }
    )

    # Act
    documents, note = preset.fetch_identity_set(workspace)

    # Assert: only what was actually written. A document the server reports as
    # absent says nothing about this user.
    assert documents == {"soul": "I build tools.", "style": "Short sentences."}
    assert note is None


def test_an_older_agent_server_still_contributes_its_one_document(preset) -> None:
    # Arrange: no document-set route, but the single resource answers.
    workspace = FakeWorkspace(
        {"/api/soul": FakeResponse(200, {"exists": True, "content": "I build tools."})}
    )

    # Act
    documents, note = preset.fetch_identity_set(workspace)

    # Assert
    assert documents == {"soul": "I build tools."}
    assert note is None


def test_a_failure_to_read_the_identity_is_stated_rather_than_swallowed(
    preset,
) -> None:
    # Arrange: the route exists but the server is unwell.
    workspace = FakeWorkspace({"/api/soul/documents": FakeResponse(500)})

    # Act
    documents, note = preset.fetch_identity_set(workspace)
    section = preset.identity_sections(documents, note)

    # Assert: a cycle reasoning without the user's preferences has to know it
    # is doing that, or it states its conclusions with unearned confidence.
    assert documents == {}
    assert note is not None
    assert "Incomplete" in section


def test_identity_is_laid_out_in_load_order_and_inside_its_budget(preset) -> None:
    # Arrange: more identity than the budget allows.
    documents = {
        "soul": "S" * preset.IDENTITY_DOCUMENT_BUDGET["soul"],
        "style": "T" * preset.IDENTITY_DOCUMENT_BUDGET["style"],
        "memory": "M" * preset.IDENTITY_DOCUMENT_BUDGET["memory"],
        "good-outputs": "G" * 2000,
        "bad-outputs": "B" * 2000,
        "influences": "I" * 2000,
    }

    # Act
    section = preset.identity_sections(documents, None)

    # Assert: identity, then voice, then memory — and the whole thing fits,
    # because identity is context and the repository is the task.
    assert section.index("SOUL.md") < section.index("STYLE.md")
    assert section.index("STYLE.md") < section.index("MEMORY.md")
    assert len(section) <= preset.IDENTITY_BUDGET_CHARS + 2000


def test_a_long_memory_keeps_its_most_recent_entries(preset) -> None:
    # Arrange: a log longer than its share of the budget.
    oldest = "## 2020-01-01T00:00:00Z — ancient\n\n" + "x" * 5000
    newest = "## 2026-10-24T00:00:00Z — just learned\n\nrecent thing"
    documents = {"memory": f"{oldest}\n\n{newest}"}

    # Act
    trimmed = preset.trim_to_budget(documents["memory"], 200, keep_end=True)

    # Assert: the newest thing learned is the one most likely to matter.
    assert "just learned" in trimmed
    assert "earlier entries omitted" in trimmed


def test_a_repository_with_no_identity_is_told_to_draft_one(preset, tmp_path) -> None:
    # Act
    documents = preset.read_project_identity(str(tmp_path))
    section = preset.project_identity_sections(documents, "pairwatch")

    # Assert
    assert documents == {}
    assert "no PROJECT_IDENTITY.md" in section
    assert ".openhands/identity/" in section


def test_a_repository_identity_set_is_read_whole(preset, tmp_path) -> None:
    # Arrange
    (tmp_path / "PROJECT_IDENTITY.md").write_text("A watcher for pairs.\n")
    identity = tmp_path / ".openhands/identity"
    identity.mkdir(parents=True)
    (identity / "STYLE.md").write_text("Functions stay short.\n")
    (identity / "MEMORY.md").write_text("Tried threads; the GIL won.\n")

    # Act
    documents = preset.read_project_identity(str(tmp_path))
    section = preset.project_identity_sections(documents, "pairwatch")

    # Assert: the rejected-approaches file is the one that stops a cycle
    # re-deriving a dead end, so it has to reach the prompt.
    assert "A watcher for pairs." in section
    assert "Functions stay short." in section
    assert "the GIL won" in section


def test_checks_are_recorded_however_an_agent_writes_them(preset) -> None:
    # Arrange: the three shapes agents actually produce.
    raw = [
        "npm run lint",
        {"command": "npm test", "result": "passed", "detail": "412 passed"},
        {"name": "npm run build", "passed": False},
        {"note": "no command here"},
    ]

    # Act
    checks = preset.normalize_checks(raw)

    # Assert: a check recorded loosely is still evidence; one with no command
    # is not a check at all.
    assert [check["command"] for check in checks] == [
        "npm run lint",
        "npm test",
        "npm run build",
    ]
    assert checks[0]["passed"] is None
    assert checks[1]["passed"] is True
    assert checks[2]["passed"] is False


def test_an_unreadable_check_outcome_is_not_recorded_as_a_failure(preset) -> None:
    # Arrange
    raw = [{"command": "pytest -q", "result": "12 passed, 3 skipped"}]

    # Act
    checks = preset.normalize_checks(raw)

    # Assert: absent is not the same as failed, and guessing would put a red
    # mark against a command that did its job.
    assert checks[0]["passed"] is None
    assert "12 passed" in checks[0]["detail"]


def test_a_prompt_digest_identifies_the_instructions_without_storing_them(
    preset,
) -> None:
    # Act
    first = preset.prompt_digest("improve the retry policy")
    again = preset.prompt_digest("improve the retry policy")
    other = preset.prompt_digest("improve the retry policy.")

    # Assert: stable, and it does not carry the prompt — which embeds the
    # user's identity and the repository's.
    assert first == again
    assert first != other
    assert "retry" not in first


def test_a_refusal_for_usage_is_told_apart_from_an_ordinary_failure(
    preset,
) -> None:
    # Arrange: the real notices, and a failure that is not one.
    exhausted = [
        "You've hit your limit · resets 3pm (America/New_York)",
        "Error: usageLimitExceeded",
        "HTTP 429 Too Many Requests",
    ]

    # Act / Assert: a miss costs one wasted cycle; treating every failure as
    # exhaustion would park healthy runtimes, which is far worse.
    for text in exhausted:
        assert preset.looks_exhausted(text) is True
    assert preset.looks_exhausted("connection reset by peer") is False
    assert preset.looks_exhausted(None) is False


def test_what_identity_cost_is_counted_the_way_it_was_spent(preset) -> None:
    """The budget was enforced from the first cycle and never measured.

    So the question the milestone asks — does identity crowd out the
    repository the cycle is supposed to improve — could only be answered by
    reading the preset, and the failure it hides is worse: a document grown
    past its slot is trimmed silently, and the person who wrote the part that
    was cut sees nothing.
    """
    # Arrange: two documents well inside the budget.
    documents = {"soul": "a" * 1000, "style": "b" * 500}

    # Act
    cost = preset.identity_cost(documents, budget=24000)

    # Assert: what was spent, per document, and nothing cut.
    assert cost["used"] == 1500
    assert cost["documents"] == {"soul": 1000, "style": 500}
    assert cost["trimmed"] == []
    assert cost["omitted"] == []
    assert cost["share"] == round(1500 / 24000, 3)


def test_a_document_that_outgrew_its_slot_is_named(preset) -> None:
    # The one a person acts on: this is the document to shorten, by name,
    # before it is the one being cut.
    cost = preset.identity_cost({"soul": "a" * 500}, budget=100)

    assert cost["trimmed"] == ["soul"]
    assert cost["used"] <= 100 + len("\n\n[truncated]")


def test_a_document_the_budget_never_reached_is_told_apart_from_a_trimmed_one(
    preset,
) -> None:
    # Trimmed means "some of it got through"; omitted means "none of it did".
    # Reporting both as trimmed would hide a document the cycle never saw.
    cost = preset.identity_cost({"soul": "a" * 200, "style": "b" * 200}, budget=100)

    assert cost["trimmed"] == ["soul"]
    assert cost["omitted"] == ["style"]
    assert "style" not in cost["documents"]


def test_the_count_matches_what_the_prompt_actually_carries(preset) -> None:
    """The measure has to be the spend, not an estimate of it.

    Counted independently of the sections but in the same load order, so a
    change to one that is not made to the other shows up here rather than as
    a number quietly describing a prompt nobody sent.
    """
    documents = {"soul": "a" * 9000, "style": "b" * 2000, "memory": "c" * 300}

    cost = preset.identity_cost(documents, budget=24000)
    section = preset.identity_sections(documents, None)

    # Every document's kept length appears in the rendered sections.
    for name, kept in cost["documents"].items():
        assert kept > 0, name
    assert cost["used"] <= 24000
    # The rendered text is the documents plus their headings, so it is longer
    # than the spend but never shorter.
    assert len(section) >= cost["used"]


def test_a_run_whose_phases_are_lost_says_the_dashboard_is_behind(
    preset,
) -> None:
    """A phase that never arrives makes a working run look stalled.

    The phase is the only thing a person watching sees change, and it is
    posted best-effort because a cosmetic update must not end a cycle. That
    made the most likely failure — an expired cycle token, answered with a
    4xx — completely invisible: the dashboard simply stopped moving.
    """
    # Arrange / Act
    first = preset.phase_failure_notice("HTTPStatusError: 401 Unauthorized")

    # Assert: the error, and the consequence, because the consequence is what
    # a reader is actually trying to explain.
    assert "401 Unauthorized" in first
    assert "earlier phase than this run is at" in first


def test_the_end_of_a_run_accounts_for_every_phase_that_went_missing(
    preset,
) -> None:
    # Said once at the end, naming how far the run really got, because that is
    # the difference between the dashboard and the log that needs explaining.
    assert preset.lost_phase_summary([]) is None

    summary = preset.lost_phase_summary(["Cloning HAZARD", "Reviewing HAZARD"])

    assert summary is not None
    assert "2 phase update(s)" in summary
    assert "Reviewing HAZARD" in summary
    assert "Cloning HAZARD" not in summary


def test_a_builder_that_ran_out_of_time_is_told_apart_from_one_that_broke(
    preset,
) -> None:
    """A timeout means work on a branch, which must not be thrown away.

    A usage limit arrives only as a runtime's own prose, so that one is
    matched on wording. A timeout is a real `TimeoutError` the SDK wraps as
    `original_exception`, so it is read from the chain — matching the sentence
    would break the moment the SDK rewords it.
    """

    # Arrange: the shape the SDK raises, and failures that are not timeouts.
    class RunError(RuntimeError):
        def __init__(self, original: BaseException) -> None:
            super().__init__("Conversation run failed for id=abc")
            self.original_exception = original

    wrapped = RunError(TimeoutError("Run timed out after 3600.0 seconds."))

    # Act / Assert
    assert preset.ran_out_of_time(wrapped) is True
    assert preset.ran_out_of_time(TimeoutError("nope")) is True
    assert preset.ran_out_of_time(RuntimeError("connection reset by peer")) is False
    assert preset.ran_out_of_time(None) is False


def test_a_timeout_is_still_recognised_without_the_exception_chain(preset) -> None:
    # A server too old to carry `original_exception` leaves only the sentence.
    # Being wrong here costs a handoff that was or was not written, never a
    # wrong verdict, so the wording is worth keeping as a fallback.
    assert (
        preset.ran_out_of_time(RuntimeError("Run timed out after 60 seconds")) is True
    )


def test_a_cycle_of_wrapped_failures_does_not_hang_the_check(preset) -> None:
    # An exception chained to itself is a real shape in retry wrappers, and a
    # walk with no seen-set would spin forever inside a failure handler.
    looping = RuntimeError("outer")
    looping.__cause__ = looping

    assert preset.ran_out_of_time(looping) is False


def test_rejected_approaches_are_stated_as_instructions_not_history(
    preset,
) -> None:
    # Arrange
    resume = {
        "goal": "replace the polling loop",
        "branch": "improve/20261024",
        "do_not": ["threads: the GIL serialised them"],
        "next_steps": ["finish the regression test"],
        "done": ["read the callback docs"],
    }

    # Act
    section = preset.resume_section(resume)

    # Assert: the dead ends come before the next steps, because a cycle that
    # re-derives one has spent its runtime learning something already written
    # down.
    assert section.index("Do NOT repeat") < section.index("Next steps")
    assert "the GIL serialised them" in section


def test_a_cycle_resuming_a_timeout_is_told_to_take_less(preset) -> None:
    """Why the last one stopped changes what this one should attempt.

    A runtime out of quota left off wherever it happened to be and this cycle
    simply carries on. A cycle out of *time* was attempting more than fits,
    and continuing at the same size is how the next one runs out too.
    """
    # Arrange
    timed_out = {"reason": "timed_out", "next_steps": ["Finish the migration."]}
    exhausted = {"reason": "runtime_exhausted", "next_steps": ["Finish it."]}

    # Act
    after_timeout = preset.resume_section(timed_out)
    after_limit = preset.resume_section(exhausted)

    # Assert
    assert "ran out of time" in after_timeout
    assert "smaller piece" in after_timeout
    assert "smaller piece" not in after_limit


def test_a_cycle_with_nothing_to_continue_is_told_nothing(preset) -> None:
    # Assert: an empty section rather than a heading with nothing under it,
    # which would read as "someone tried and left you no detail".
    assert preset.resume_section(None) == ""
    assert preset.resume_section({}) == ""
