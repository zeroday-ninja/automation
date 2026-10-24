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
