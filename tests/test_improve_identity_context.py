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
