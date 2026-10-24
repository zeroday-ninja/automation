"""Which class of change a diff is.

The class decides who may publish: additive needs nobody, deleting needs a
judge, breaking needs a human. So the number that matters is at the bottom of
this file -- on a labelled set, nothing that removed a public export may read
as additive, because that is the failure that merges without asking.
"""

import pytest

from openhands.automation.improve.change_class import (
    assess_diff,
    claim_mismatch,
    normalize_change_class,
)


def _diff(*hunks: str) -> str:
    return "\n".join(hunks)


ADDED_FILE = """diff --git a/src/widget.ts b/src/widget.ts
new file mode 100644
--- /dev/null
+++ b/src/widget.ts
@@ -0,0 +1,3 @@
+export function widget() {
+  return 1;
+}
"""

ADDED_METHOD = """diff --git a/openhands/service.py b/openhands/service.py
index 1111111..2222222 100644
--- a/openhands/service.py
+++ b/openhands/service.py
@@ -10,3 +10,7 @@ class Service:
     def existing(self) -> int:
         return 1
+
+    def added(self) -> int:
+        return 2
"""

REMOVED_PY_FUNCTION = """diff --git a/openhands/service.py b/openhands/service.py
index 1111111..2222222 100644
--- a/openhands/service.py
+++ b/openhands/service.py
@@ -10,7 +10,3 @@
-def resolve_backend(name: str) -> str:
-    return name
-
 def other() -> None:
     pass
"""

REMOVED_TS_EXPORT = """diff --git a/src/api/client.ts b/src/api/client.ts
index 1111111..2222222 100644
--- a/src/api/client.ts
+++ b/src/api/client.ts
@@ -4,5 +4,3 @@
-export const legacyClient = makeClient();
 export const client = makeClient();
"""

REMOVED_PRIVATE_HELPER = """diff --git a/openhands/service.py b/openhands/service.py
index 1111111..2222222 100644
--- a/openhands/service.py
+++ b/openhands/service.py
@@ -10,5 +10,3 @@
-def _unused_helper() -> None:
-    pass
 def kept() -> None:
     pass
"""

DELETED_PRIVATE_FILE = """diff --git a/openhands/_scratch.py b/openhands/_scratch.py
deleted file mode 100644
index 1111111..0000000
--- a/openhands/_scratch.py
+++ /dev/null
@@ -1,2 +0,0 @@
-# a dead note to self
-x = 1
"""

DELETED_EXPORTING_FILE = """diff --git a/openhands/legacy.py b/openhands/legacy.py
deleted file mode 100644
index 1111111..0000000
--- a/openhands/legacy.py
+++ /dev/null
@@ -1,2 +0,0 @@
-DEFAULT_TIMEOUT = 30
"""

BIG_DELETION = "".join(
    [
        "diff --git a/docs/notes.md b/docs/notes.md\n",
        "index 1111111..2222222 100644\n",
        "--- a/docs/notes.md\n",
        "+++ b/docs/notes.md\n",
        "@@ -1,20 +1,2 @@\n",
        *[f"-stale line {n}\n" for n in range(18)],
        "+one line instead\n",
    ]
)

MIGRATION_DROPS = """\
diff --git a/migrations/versions/040.py b/migrations/versions/040.py
new file mode 100644
--- /dev/null
+++ b/migrations/versions/040.py
@@ -0,0 +1,3 @@
+def upgrade() -> None:
+    op.drop_column("automation_run", "legacy_phase")
"""

MAJOR_BUMP = """diff --git a/package.json b/package.json
index 1111111..2222222 100644
--- a/package.json
+++ b/package.json
@@ -5,7 +5,7 @@
-    "react-router": "7.9.2",
+    "react-router": "8.0.0",
     "react": "19.2.0"
"""

PATCH_BUMP = """diff --git a/package.json b/package.json
index 1111111..2222222 100644
--- a/package.json
+++ b/package.json
@@ -5,7 +5,7 @@
-    "react-router": "7.9.2",
+    "react-router": "7.9.3",
     "react": "19.2.0"
"""


@pytest.mark.parametrize(
    ("label", "diff"),
    [
        ("a new file", ADDED_FILE),
        ("a method added to a class", ADDED_METHOD),
        # A leading underscore is the convention for "not part of the surface"
        # in both languages here, so removing one is not a removed promise.
        ("a private helper removed", REMOVED_PRIVATE_HELPER),
        ("a patch-level dependency bump", PATCH_BUMP),
    ],
)
def test_changes_that_take_nothing_away_are_additive(label: str, diff: str) -> None:
    assert assess_diff(diff).change_class == "additive", label


@pytest.mark.parametrize(
    ("label", "diff", "rule"),
    [
        ("a file with no public surface", DELETED_PRIVATE_FILE, "file_deleted"),
        ("many lines cut from one file", BIG_DELETION, "deletions_above_threshold"),
    ],
)
def test_taking_a_lot_out_is_deleting(label: str, diff: str, rule: str) -> None:
    assessment = assess_diff(diff)

    assert assessment.change_class == "deleting", label
    assert rule in {reason.rule for reason in assessment.reasons}


@pytest.mark.parametrize(
    ("label", "diff", "rule"),
    [
        ("a public function removed", REMOVED_PY_FUNCTION, "removed_export"),
        ("an exported const removed", REMOVED_TS_EXPORT, "removed_export"),
        ("a migration that drops a column", MIGRATION_DROPS, "migration_drops_data"),
        ("a dependency major bump", MAJOR_BUMP, "dependency_major_bump"),
        # Deleting a file that exported something removes a public promise;
        # only a file with no surface is a mere deletion.
        ("a file that exported something", DELETED_EXPORTING_FILE, "removed_export"),
    ],
)
def test_breaking_somebody_elses_code_is_breaking(
    label: str, diff: str, rule: str
) -> None:
    assessment = assess_diff(diff)

    assert assessment.change_class == "breaking", label
    assert rule in {reason.rule for reason in assessment.reasons}


def test_the_class_comes_with_the_line_it_was_read_from() -> None:
    assessment = assess_diff(REMOVED_PY_FUNCTION)

    # A class with no evidence behind it is a lead, not a finding: the next
    # reader has to be able to disagree with a specific line.
    reason = next(r for r in assessment.reasons if r.rule == "removed_export")
    assert reason.path == "openhands/service.py"
    assert "resolve_backend" in reason.detail
    assert assessment.removed_exports == ("resolve_backend",)


def test_an_empty_diff_is_additive_with_nothing_claimed() -> None:
    for empty in (None, "", "   \n"):
        assessment = assess_diff(empty)
        assert assessment.change_class == "additive"
        assert assessment.reasons == ()


def test_a_claim_less_restrictive_than_the_diff_is_a_finding() -> None:
    assessment = assess_diff(REMOVED_PY_FUNCTION)

    mismatch = claim_mismatch("additive", assessment)

    assert mismatch is not None
    assert "additive" in mismatch and "breaking" in mismatch
    assert "removed_export" in mismatch


def test_a_claim_more_restrictive_than_the_diff_is_left_alone() -> None:
    # Being careful about your own change is the behaviour this encourages,
    # so it must not read as a defect.
    assert claim_mismatch("breaking", assess_diff(ADDED_FILE)) is None
    assert claim_mismatch(None, assess_diff(ADDED_FILE)) is None


def test_a_claim_that_is_not_a_class_is_reported_rather_than_ignored() -> None:
    assert claim_mismatch("probably fine", assess_diff(ADDED_FILE)) is not None
    assert normalize_change_class("probably fine") is None
    assert normalize_change_class(" Breaking ") == "breaking"


def test_nothing_that_removed_a_public_export_reads_as_additive() -> None:
    # The number the gate exists for: this is the misclassification that
    # merges somebody's broken build without asking a human.
    removals = [REMOVED_PY_FUNCTION, REMOVED_TS_EXPORT, DELETED_EXPORTING_FILE]

    classes = [assess_diff(diff).change_class for diff in removals]

    assert "additive" not in classes


def test_the_rules_ship_into_the_sandbox_that_applies_them() -> None:
    # The service re-reads a reported class against these same rules, so there
    # is exactly one copy of them and it has to reach the sandbox. A module
    # that stayed behind would leave the cycle unable to classify its own diff,
    # which fails as an ImportError at the end of a long run.
    import io
    import tarfile

    from openhands.automation.preset_router import _generate_improve_tarball

    with tarfile.open(fileobj=io.BytesIO(_generate_improve_tarball())) as tar:
        shipped = set(tar.getnames())

    assert {
        "change_class.py",
        "routing.py",
        "lessons.py",
        "judge_prompt.md",
    } <= shipped


@pytest.mark.parametrize("module", ["change_class.py", "routing.py", "lessons.py"])
def test_a_shipped_rule_module_needs_nothing_but_the_standard_library(
    module: str,
) -> None:
    # Imported inside a sandbox that has no `openhands.automation` installed,
    # so a convenience import added here is an ImportError there.
    import ast
    from pathlib import Path as _Path

    source = (
        _Path(__file__).resolve().parents[1]
        / "openhands"
        / "automation"
        / "improve"
        / module
    ).read_text()
    tree = ast.parse(source)
    imported = {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }

    assert imported <= {"re", "dataclasses", "__future__"}
