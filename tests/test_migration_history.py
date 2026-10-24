"""New migrations must coexist with migration IDs already published on main.

The fixture snapshot makes the integration regression reproducible offline,
including before a stale feature branch has been rebased onto that history.
"""

import ast
import shutil
import warnings
from pathlib import Path

import pytest
from alembic.script import ScriptDirectory


MIGRATIONS = sorted(
    (Path(__file__).resolve().parents[1] / "migrations" / "versions").glob("*.py")
)


def test_migrations_do_not_reuse_published_revision_ids(tmp_path: Path):
    repository = Path(__file__).resolve().parents[1]
    combined = tmp_path / "migrations"
    shutil.copytree(repository / "migrations", combined)
    published = Path(__file__).parent / "fixtures" / "published_migrations"
    for snapshot in published.glob("*.py.txt"):
        destination = combined / "versions" / snapshot.name.removesuffix(".txt")
        # After a rebase these migrations are already present in the branch.
        if not destination.exists():
            shutil.copyfile(snapshot, destination)

    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        heads = ScriptDirectory(str(combined)).get_heads()

    duplicate_revisions = [
        str(warning.message)
        for warning in captured
        if "is present more than once" in str(warning.message)
    ]
    assert not duplicate_revisions, (
        "New migrations collide with published main history: "
        + "; ".join(duplicate_revisions)
        + ". Assign unused revisions and update the down_revision chain."
    )
    assert len(heads) == 1, f"Expected one upgrade head after integration, got {heads}"


def _is_boolean_type(node: ast.AST) -> bool:
    """`sa.Boolean` or `sa.Boolean()`, however it was spelled."""
    if isinstance(node, ast.Call):
        node = node.func
    return isinstance(node, ast.Attribute) and node.attr == "Boolean"


def _integer_literal(node: ast.AST) -> str | None:
    """The literal if this default is an integer in disguise, else None."""
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "text"
        and len(node.args) == 1
    ):
        node = node.args[0]
    if isinstance(node, ast.Constant) and str(node.value) in {"0", "1"}:
        return repr(node.value)
    return None


@pytest.mark.parametrize("migration", MIGRATIONS, ids=lambda path: path.stem)
def test_boolean_columns_do_not_default_to_an_integer(migration: Path):
    """A boolean defaulting to 0 applies on SQLite and breaks on PostgreSQL.

    This deployment runs SQLite, so a migration can be written, applied and
    merged without anyone discovering that PostgreSQL refuses an integer
    default on a boolean column — and then it fails on the engine the service
    is written for, at startup, with the database half migrated. `sa.false()`
    renders correctly on both.
    """
    tree = ast.parse(migration.read_text(), filename=str(migration))

    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr in {"Column", "column"}):
            continue
        if not any(_is_boolean_type(arg) for arg in node.args):
            continue
        for keyword in node.keywords:
            if keyword.arg not in {"server_default", "default"}:
                continue
            literal = _integer_literal(keyword.value)
            if literal is not None:
                offenders.append(f"line {node.lineno}: {keyword.arg}={literal}")

    assert not offenders, (
        f"{migration.name} defaults a boolean column to an integer "
        f"({'; '.join(offenders)}); use sa.false() or sa.true()"
    )
