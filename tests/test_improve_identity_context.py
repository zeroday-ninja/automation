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


def _repo_with_a_remote(tmp_path):
    """A clone with somewhere to push to, which is what `pushed` means."""
    import subprocess

    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(remote), str(work)], check=True)
    git = ["git", "-C", str(work)]
    subprocess.run([*git, "config", "user.email", "cycle@example.test"], check=True)
    subprocess.run([*git, "config", "user.name", "Cycle"], check=True)
    (work / "README.md").write_text("start\n")
    subprocess.run([*git, "add", "README.md"], check=True)
    subprocess.run([*git, "commit", "-qm", "start"], check=True)
    subprocess.run([*git, "push", "-q", "origin", "HEAD:main"], check=True)
    subprocess.run([*git, "remote", "set-head", "origin", "main"], check=True)
    return work


def test_an_interrupted_builders_work_reaches_the_remote(preset, tmp_path) -> None:
    """The code before the description, which is the order the plan asks for.

    The builder prompt commits and pushes in one step at the very end, so a
    builder interrupted mid-change has a dirty tree and no commits — in a
    sandbox that is about to be deleted. The handoff used to *observe* that
    and tell the next cycle to read a diff nobody could reach.
    """
    # Arrange: work in progress, nothing committed, no branch yet.
    work = _repo_with_a_remote(tmp_path)
    (work / "feature.py").write_text("def added():\n    return 1\n")
    (work / "README.md").write_text("start\nchanged\n")

    # Act
    saved = preset.save_work_in_progress(
        str(work),
        "improve/20261025-0700",
        base_ref="origin/main",
        reason="ran out of time",
    )

    # Assert: on the remote, where the next cycle's fresh sandbox can see it.
    assert saved["committed"] is True
    assert saved["pushed"] is True
    assert preset.branch_is_pushed(str(work), "improve/20261025-0700") is True


def test_it_will_not_move_a_branch_that_already_has_commits(preset, tmp_path) -> None:
    """Losing an uncommitted tree is bad; discarding commits is worse.

    Pointing an existing branch at this HEAD to carry the tree across would
    throw away whatever was already on it, so the work is left where it is
    and the record says why.
    """
    # Arrange: a branch with a commit on it, and HEAD somewhere else.
    import subprocess

    work = _repo_with_a_remote(tmp_path)
    git = ["git", "-C", str(work)]
    subprocess.run([*git, "checkout", "-q", "-b", "improve/taken"], check=True)
    (work / "earlier.py").write_text("earlier\n")
    subprocess.run([*git, "add", "earlier.py"], check=True)
    subprocess.run([*git, "commit", "-qm", "earlier work"], check=True)
    kept = subprocess.run(
        [*git, "rev-parse", "improve/taken"], capture_output=True, text=True, check=True
    ).stdout.strip()
    subprocess.run([*git, "checkout", "-q", "main"], check=True)
    (work / "later.py").write_text("later\n")

    # Act
    saved = preset.save_work_in_progress(
        str(work), "improve/taken", base_ref="origin/main", reason="ran out of time"
    )

    # Assert: refused, said so, and the earlier commit is still the tip.
    assert saved["pushed"] is False
    assert "exists" in saved["note"]
    still = subprocess.run(
        [*git, "rev-parse", "improve/taken"], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert still == kept


def test_a_clean_tree_is_reported_rather_than_pushed_empty(preset, tmp_path) -> None:
    # Arrange: the builder changed nothing before it ran out.
    work = _repo_with_a_remote(tmp_path)

    # Act
    saved = preset.save_work_in_progress(
        str(work),
        "improve/20261025-0800",
        base_ref="origin/main",
        reason="ran out of time",
    )

    # Assert: an empty branch on the remote is noise, and saying "no commits
    # to save" is the difference between nothing happened and something was
    # lost.
    assert saved["committed"] is False
    assert saved["pushed"] is False
    assert "no commits" in saved["note"]


def test_a_handoff_that_does_not_say_is_treated_as_unpushed(preset) -> None:
    """An unknown goes the safe way round.

    This defaulted a missing `branch_pushed` to True, and the service never
    sent the field at all — `HandoffResume` did not declare it and forbids
    extras — so every resume was presented as a branch to carry on from. Four
    live cycles continued a branch whose commits existed only in a sandbox
    that had already been deleted.

    Read as pushed, an unknown loses the work in silence. Read as unpushed, it
    costs one fresh branch.
    """
    # Act: no `branch_pushed` key at all, which is what an older service sends.
    section = preset.resume_section(
        {
            "reason": "timed_out",
            "branch": "improve/20261025-0100",
            "next_steps": ["Finish the migration."],
        }
    )

    # Assert
    assert "commits are gone" in section


def test_the_projects_own_identity_is_counted_too(preset) -> None:
    """Half of identity was injected on every cycle and measured on none.

    `identity_cost` answered for the user's six documents against their 24000
    budget. The *repository's* four were read, trimmed to their own slots, and
    put in the same prompt — up to 22000 characters that no record accounted
    for. So "a cycle on a repository with a full identity set demonstrably
    uses it" had nothing to demonstrate it with, and "identity never exceeds
    its share of the cycle's context budget" was measured against one half of
    identity.
    """
    # Arrange: a user document and a full project set.
    documents = {"soul": "a" * 1000}
    project = {
        relative: "p" * 400
        for relative, _title, _budget in preset.PROJECT_IDENTITY_DOCUMENTS
    }

    # Act
    cost = preset.identity_cost(documents, project=project)

    # Assert: which of the repository's documents the cycle read, and what the
    # two halves cost together — the number the metric actually asks for.
    assert set(cost["project"]["documents"]) == set(project)
    assert cost["project"]["used"] == 400 * len(project)
    assert cost["total"]["used"] == cost["used"] + cost["project"]["used"]
    assert cost["total"]["budget"] == (
        preset.IDENTITY_BUDGET_CHARS + preset.PROJECT_IDENTITY_BUDGET_CHARS
    )
    # The user's own figures stay exactly where a reader already looks for
    # them, because the record is read by things that predate this.
    assert cost["documents"] == {"soul": 1000}


def test_a_projects_identity_is_capped_in_total_not_only_per_document(
    preset,
) -> None:
    """Four documents inside their own slots still add up.

    Each was trimmed to its own budget and nothing capped the set, so a
    repository with four long identity documents could take most of a cycle's
    prompt away from the repository it was improving.
    """
    # Arrange: four documents of 8000 — each inside the largest per-document
    # slot, 32000 together. A fixed size rather than one derived from the
    # budget: a test that scales its own input with the constant it is
    # checking cannot tell the cap from the absence of one.
    project = {
        relative: "p" * 8000
        for relative, _title, _budget in preset.PROJECT_IDENTITY_DOCUMENTS
    }

    # Act
    section = preset.project_identity_sections(project, "acme/alpha")
    cost = preset.identity_cost({}, project=project)

    # Assert: the first document fills the budget, the rest are named as
    # omitted rather than silently absent — in the prompt as well as in the
    # record, because the cycle has to know what it is not being told.
    assert cost["project"]["used"] <= 16000 + len("\n\n[truncated]")
    assert cost["project"]["omitted"]
    assert "identity budget was already spent" in section


def test_a_repository_with_no_identity_is_asked_for_a_first_draft(preset) -> None:
    # The other half of the milestone's exit: a missing set is an instruction,
    # not an empty section. Kept beside the budget tests because the budget is
    # what could have turned a present set into an absent-looking one.
    section = preset.project_identity_sections({}, "acme/alpha")

    flat = " ".join(section.split())
    assert "has no PROJECT_IDENTITY.md" in flat
    assert "first draft" in flat


def test_the_users_memory_is_named_apart_from_the_projects(preset) -> None:
    """Two files called MEMORY.md, one of which must never be committed.

    The builder is told to append a rejected approach to the repository's
    `.openhands/identity/MEMORY.md` while the user's own MEMORY.md is quoted
    in the same prompt. The user's heading said only "MEMORY.md", so the two
    were indistinguishable by name — and the cost of that confusion is the
    user's personal continuity log, which syncs across their machines, landing
    in a repository. The plan's own decision table says session memory is "not
    mirrored to a repository".
    """
    # Act
    titles = dict(preset.IDENTITY_DOCUMENT_TITLES)
    project_paths = {
        relative for relative, _title, _budget in preset.PROJECT_IDENTITY_DOCUMENTS
    }

    # Assert: the user's memory names its own path, and no project document
    # shares a *title* with it.
    assert "~/.openhands/soul/MEMORY.md" in titles["memory"]
    assert ".openhands/identity/MEMORY.md" in project_paths
    for _name, title in preset.IDENTITY_DOCUMENT_TITLES:
        assert ".openhands/identity/" not in title


def test_the_builder_is_told_not_to_commit_the_users_memory(preset) -> None:
    """Said in the prompt, because that is where the instruction that creates
    the risk lives: "add a dated entry … to `.openhands/identity/MEMORY.md`".

    A rule in a design document the model never reads is not a rule. This one
    is checkable only as prose — the file is the agent's own to write, so
    nothing downstream can tell a copied entry from an authored one.
    """
    import pathlib

    prompt = (pathlib.Path(preset.__file__).parent / "builder_prompt.md").read_text()

    assert "never goes into a repository" in prompt
    assert "~/.openhands/soul/MEMORY.md" in prompt


def test_the_handoff_prose_lands_on_the_branch(preset, tmp_path) -> None:
    """Step 2 of the four a dying runtime is asked for, which had nowhere to go.

    It wrote `.improve-cycle/handoff.md` — the cycle's scratch directory, a
    sibling of the clone — and nothing read it. The directory went with the
    sandbox, so the last tokens of an exhausted runtime bought 500 words
    nobody could ever see.

    On the branch as a commit message: it belongs to the branch rather than
    the project, `git log` is where somebody picking one up looks, and a file
    would have to be remembered and deleted by whoever finishes the work.
    """
    # Arrange
    import subprocess

    work = _repo_with_a_remote(tmp_path)
    git = ["git", "-C", str(work)]
    subprocess.run([*git, "checkout", "-q", "-b", "improve/handing-over"], check=True)

    # Act
    carried = preset.commit_handoff_prose(
        str(work),
        "improve/handing-over",
        "Tried the polling loop; it deadlocks under two writers.",
    )

    # Assert: on the branch, and on the remote where the next sandbox can
    # read it.
    assert carried is True
    log = subprocess.run(
        [*git, "log", "-1", "--pretty=%B", "origin/improve/handing-over"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "deadlocks under two writers" in log


def test_nothing_is_committed_when_the_runtime_wrote_no_prose(preset, tmp_path) -> None:
    # An exhausted runtime often has nothing left to say, and an empty commit
    # saying nothing is worse than no commit: it reads as a handoff that was
    # written and said nothing, rather than one that was never written.
    work = _repo_with_a_remote(tmp_path)

    assert preset.commit_handoff_prose(str(work), "improve/x", "   \n") is False


def test_the_prose_lands_on_the_named_branch_whatever_head_says(
    preset, tmp_path
) -> None:
    """The branch it says it wrote to is the branch it wrote to.

    `git commit` writes to HEAD; the push that follows names the branch. When
    those two disagree the prose was committed to whichever branch the builder
    happened to leave checked out, the push of the named branch reported
    success because that branch was already up to date, and the cycle printed
    "the handoff's own words are on <branch>" — so the last tokens of an
    exhausted runtime bought prose on a ref nothing ever fetches, and said
    they had not.

    `save_work_in_progress` reaches this on its own: when the builder leaves
    HEAD elsewhere and the branch already exists it deliberately does not move
    HEAD, and the next thing the handoff does is commit.

    Observed on a live QA cycle on 2026-10-25: the prose commit landed on the
    builder's own branch while the cycle recorded it on `improve/qa-handoff…`.
    """
    # Arrange: the branch carries the work, and HEAD is somewhere else.
    import subprocess

    work = _repo_with_a_remote(tmp_path)
    git = ["git", "-C", str(work)]
    subprocess.run([*git, "checkout", "-q", "-b", "improve/20261025-1706"], check=True)
    (work / "half-done.py").write_text("def started():\n    return 1\n")
    subprocess.run([*git, "add", "half-done.py"], check=True)
    subprocess.run([*git, "commit", "-qm", "wip: the actual work"], check=True)
    subprocess.run([*git, "push", "-q", "origin", "improve/20261025-1706"], check=True)
    subprocess.run([*git, "checkout", "-q", "-b", "builders-own-branch"], check=True)

    # Act
    carried = preset.commit_handoff_prose(
        str(work),
        "improve/20261025-1706",
        "Tried the single-writer lock; it deadlocks under two writers.",
    )

    # Assert: on the named branch, on the remote, and not on HEAD's.
    assert carried is True
    remote_log = subprocess.run(
        [*git, "log", "-1", "--pretty=%B", "origin/improve/20261025-1706"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "deadlocks under two writers" in remote_log
    elsewhere = subprocess.run(
        [*git, "log", "-1", "--pretty=%B", "builders-own-branch"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "deadlocks" not in elsewhere
    # And the builder's work is still the parent of it, not replaced by it.
    assert (
        "the actual work"
        in subprocess.run(
            [*git, "log", "-2", "--pretty=%B", "origin/improve/20261025-1706"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
