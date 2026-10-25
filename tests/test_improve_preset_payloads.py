"""Every payload the improvement preset sends must be one the service accepts.

The cycle schemas forbid extra fields, and the runner that builds those
payloads lives in `presets/improve/sdk_main.py` — a script that ships inside
the run tarball and executes a whole cycle on import, so no test can import it
and call it. That gap let a real defect through: a field was added to the
review record and not to `CycleReview`, so the final report of *every cycle
with a reviewer* was rejected with 422, the cycle was left in REVIEWING, and
the janitor closed it as an abandonment. The work was built, reviewed and
merged, and the record said it failed.

So read the runner as source rather than running it: parse it, collect the
literal keys of the payload dictionaries, and check each key against the
schema that will receive it. This is the contract between the two files, and
it is the only place either side is checked against the other.
"""

import ast
import re
from pathlib import Path

import pytest

from openhands.automation.improve.schemas import CycleReview, UpdateCycleRequest


RUNNER_PATH = (
    Path(__file__).resolve().parents[1]
    / "openhands"
    / "automation"
    / "presets"
    / "improve"
    / "sdk_main.py"
)


def _literal_keys(node: ast.AST) -> set[str] | None:
    """The string keys of a dict literal, or None if it is not one.

    A payload assembled conditionally (`payload[key] = ...`, `**rest`) cannot
    be read statically; those keys are reported by the caller as unreadable
    rather than silently passing.
    """
    if not isinstance(node, ast.Dict):
        return None
    keys = set()
    for key in node.keys:
        if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
            return None
        keys.add(key.value)
    return keys


@pytest.fixture(scope="module")
def runner() -> ast.Module:
    return ast.parse(RUNNER_PATH.read_text(), filename=str(RUNNER_PATH))


def _payloads(tree: ast.Module, *, call: str, attr: str | None = None) -> list[set]:
    """Dict literals passed as the only argument of `call`."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        if attr is None:
            if not (isinstance(func, ast.Name) and func.id == call):
                continue
        elif not (
            isinstance(func, ast.Attribute)
            and func.attr == attr
            and isinstance(func.value, ast.Name)
            and func.value.id == call
        ):
            continue
        keys = _literal_keys(node.args[0])
        if keys is not None:
            found.append((node.lineno, keys))
    return found


class TestTheRunnerSendsOnlyFieldsTheServiceAccepts:
    def test_review_records_match_the_review_schema(self, runner):
        # Arrange
        accepted = set(CycleReview.model_fields)

        # Act
        records = _payloads(runner, call="reviews", attr="append")

        # Assert
        assert records, "no review record found in the runner; move it or fix the test"
        for lineno, keys in records:
            assert keys <= accepted, (
                f"{RUNNER_PATH.name}:{lineno} sends review fields the service "
                f"rejects: {sorted(keys - accepted)}"
            )

    def test_cycle_updates_match_the_update_schema(self, runner):
        accepted = set(UpdateCycleRequest.model_fields)

        # Both forms the runner uses: a literal passed straight to the
        # reporter, and one built as a named `*_update` first.
        payloads = _payloads(runner, call="report_cycle")
        for node in ast.walk(runner):
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            if not (isinstance(target, ast.Name) and target.id.endswith("_update")):
                continue
            keys = _literal_keys(node.value)
            if keys is not None:
                payloads.append((node.lineno, keys))

        assert len(payloads) >= 2, "expected the runner's progress and final reports"
        for lineno, keys in payloads:
            assert keys <= accepted, (
                f"{RUNNER_PATH.name}:{lineno} sends cycle fields the service "
                f"rejects: {sorted(keys - accepted)}"
            )


def test_what_a_cycle_was_told_is_recorded_before_it_can_be_lost(runner) -> None:
    """A cycle that ran out of usage used to record no lessons at all.

    `injected_lesson_ids` was sent only with a finished build, so a cycle
    handed a correction and then refused recorded nothing — and that field's
    whole job is to tell "ignored the correction" from "never saw it" when the
    same one arrives again. Every failed cycle lost exactly the evidence the
    repeat diagnosis needs.

    Asserted against the payload it travels in rather than against a run,
    because the runner executes a whole cycle on import and cannot be called.
    """
    # Act: every payload that carries the field, with its own `status`.
    carrying = [
        _constants(node)
        for node in ast.walk(runner)
        if isinstance(node, ast.Dict)
        and "injected_lesson_ids" in (_literal_keys(node) or set())
    ]

    # Assert: reported once, with the status that means the builder is about
    # to start — the moment the fact becomes true, and the last one before a
    # refusal can take it away.
    assert len(carrying) == 1, (
        "what a cycle was told should be reported exactly once, as soon as it "
        f"is known; found {len(carrying)} payloads carrying it"
    )
    assert carrying[0].get("status") == "BUILDING"


def _constants(node: ast.Dict) -> dict:
    """The dict literal's string-keyed constant values."""
    found = {}
    for key, value in zip(node.keys, node.values, strict=False):
        if isinstance(key, ast.Constant) and isinstance(value, ast.Constant):
            found[key.value] = value.value
    return found


PROMPT_PATH = RUNNER_PATH.parent / "builder_prompt.md"

REPORT_PATH = RUNNER_PATH.parent / "report.py"


def _prompt_report_fields() -> set[str]:
    """The report keys the builder prompt actually asks for.

    Read out of the fenced JSON block the prompt tells the builder to write
    verbatim — "Nothing else goes in that file" — so a field the service reads
    and the prompt never names is visible as the empty set difference it is.
    """
    text = PROMPT_PATH.read_text()
    start = text.index("Write exactly this JSON")
    block = text[start : text.index("Rules for the report")]
    return set(re.findall(r'^\s+"([a-z_]+)":', block, re.MULTILINE))


def _fields_read_from_the_report(function: str) -> set[str]:
    """Every `report.get("x")` inside one function of `report.py`."""
    tree = ast.parse(REPORT_PATH.read_text())
    target = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == function
    )
    found = set()
    for node in ast.walk(target):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "report"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            found.add(node.args[0].value)
    return found


def test_the_changeover_plan_asks_for_what_it_reads() -> None:
    """A plan assembled from fields nobody is asked for is three fallbacks.

    `changeover_plan` reads `affects`, `changeover` and `rollback`, and the
    prompt's report contract named none of them — so every breaking change
    that parked for a person carried "The builder did not say who depends on
    this", "The builder proposed no changeover sequence" and "Not stated by
    the builder", always. Three live cycles were parked that way.

    Milestone 5 asks a parked breaking change to carry what breaks, who
    consumes it, the sequence that keeps both sides working, and the rollback.
    Only the first is read from the diff; the other three have to be asked.
    """
    # Act
    read = _fields_read_from_the_report("changeover_plan")
    asked = _prompt_report_fields()

    # Assert
    assert read, "changeover_plan reads nothing from the report at all"
    assert read <= asked, (
        "the changeover plan reads report fields the builder prompt never "
        f"asks for: {sorted(read - asked)}"
    )
