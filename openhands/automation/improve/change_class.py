"""What class of change a diff actually is.

Three classes decide who may publish a change: an *additive* one needs nobody's
confirmation, a *deleting* one needs a judge's attestation, and a *breaking* one
needs a human's. So the class cannot be the builder's opinion of its own work.
Everything here is computed from the diff, and the builder's claim is compared
against it rather than trusted.

Each rule that fires records its evidence -- the file and the line it matched --
because a class with no evidence behind it is a lead, not a finding, and because
the next reader has to be able to disagree with a specific line rather than with
a verdict.

Deliberately dependency-free (stdlib only, no pydantic): this module is packaged
into the improvement program's tarball and imported by ``main.py`` inside a
sandbox that has no ``openhands.automation`` installed. It must keep working
when imported with nothing but the standard library on ``sys.path``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


#: Ordered least to most restrictive. A class is only ever raised, never
#: lowered, so the order is what makes "the worst rule that fired" meaningful.
CHANGE_CLASSES: tuple[str, ...] = ("additive", "deleting", "breaking")

#: Deletions in one modified file above this are a deletion rather than an
#: edit. A whole deleted file counts regardless.
DEFAULT_DELETION_THRESHOLD = 10

#: Most reasons kept. Past this the account is no longer readable and the
#: class is already decided; the count is reported separately.
MAX_REASONS = 40


@dataclass(frozen=True, slots=True)
class ChangeReason:
    """One rule that fired, with the line it fired on."""

    rule: str
    path: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"rule": self.rule, "path": self.path, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class ChangeAssessment:
    """The class a diff is, and the evidence for it."""

    change_class: str
    reasons: tuple[ChangeReason, ...] = ()
    removed_exports: tuple[str, ...] = ()
    deleted_files: tuple[str, ...] = ()
    changed_files: tuple[str, ...] = ()
    insertions: int = 0
    deletions: int = 0
    #: Reasons found beyond MAX_REASONS, so a truncated account says so.
    reasons_omitted: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "change_class": self.change_class,
            "reasons": [reason.as_dict() for reason in self.reasons],
            "removed_exports": list(self.removed_exports),
            "deleted_files": list(self.deleted_files),
            "insertions": self.insertions,
            "deletions": self.deletions,
            "reasons_omitted": self.reasons_omitted,
        }


# A public definition disappearing is the one signal that means "something that
# used to be callable no longer is". Leading underscore excluded: that is the
# convention for "not part of the surface" in both languages here.
_REMOVED_PYTHON_EXPORT = re.compile(
    r"^-(?:async\s+)?(?:def|class)\s+(?P<name>[A-Za-z][\w]*)"
)
_REMOVED_PYTHON_CONSTANT = re.compile(r"^-(?P<name>[A-Z][A-Z0-9_]*)\s*(?::[^=]+)?=")
_REMOVED_TS_EXPORT = re.compile(
    r"^-export\s+(?:default\s+)?(?:async\s+)?"
    r"(?:const|let|var|function|class|type|interface|enum)\s+"
    r"(?P<name>[A-Za-z_$][\w$]*)"
)
_REMOVED_TS_REEXPORT = re.compile(r"^-export\s*\{(?P<names>[^}]*)\}")

#: Paths whose content *is* a contract, so a removed line there is a removed
#: promise rather than an implementation detail.
_CONTRACT_PATH = re.compile(
    r"(schemas?\.py|_schema\.ts|\.proto|openapi[^/]*\.json|"
    r"contracts?/|/schema/|\.graphql)$|openapi",
    re.IGNORECASE,
)

_ROUTE_DECORATOR = re.compile(
    r"^-\s*@\w+\.(?:get|post|put|patch|delete)\(|^-\s*(?:router|app)\.\w+\("
)

_MIGRATION_PATH = re.compile(r"(^|/)(migrations|alembic)/")
_DATA_DROPPING = re.compile(
    r"\b(drop_column|drop_table|drop_constraint|DROP\s+TABLE|DROP\s+COLUMN)\b",
    re.IGNORECASE,
)

_MANIFEST_PATH = re.compile(
    r"(^|/)(package\.json|pyproject\.toml|requirements[^/]*\.txt|Gemfile|go\.mod)$"
)
_VERSION_IN_LINE = re.compile(r"(?P<major>\d+)\.(?P<minor>\d+)(?:\.(?P<patch>\d+))?")
_DEP_LINE = re.compile(
    r"^(?P<sign>[-+])\s*[\"']?(?P<name>[A-Za-z0-9_@./-]+)[\"']?\s*[:=]"
)

_DIFF_HEADER = re.compile(r"^diff --git a/(?P<a>.+?) b/(?P<b>.+)$")


@dataclass
class _FileState:
    path: str
    deleted: bool = False
    added: bool = False
    renamed_from: str | None = None
    insertions: int = 0
    deletions: int = 0
    removed_deps: dict[str, str] = field(default_factory=dict)
    added_deps: dict[str, str] = field(default_factory=dict)


def _ts_reexport_names(block: str) -> list[str]:
    names = []
    for part in block.split(","):
        name = part.strip().split(" as ")[-1].strip()
        if name and not name.startswith("_") and name != "type":
            names.append(name)
    return names


def assess_diff(
    diff_text: str | None,
    *,
    deletion_threshold: int = DEFAULT_DELETION_THRESHOLD,
) -> ChangeAssessment:
    """Classify a unified diff.

    An empty or unreadable diff is ``additive`` with no reasons: there is
    nothing in it to be restrictive about, and a caller that has no diff at all
    should be saying so itself rather than reading a class out of this.
    """
    if not diff_text or not diff_text.strip():
        return ChangeAssessment(change_class="additive")

    files: dict[str, _FileState] = {}
    current: _FileState | None = None
    reasons: list[ChangeReason] = []
    removed_exports: list[str] = []

    def note(rule: str, path: str, detail: str) -> None:
        reasons.append(ChangeReason(rule=rule, path=path, detail=detail[:200].strip()))

    for line in diff_text.splitlines():
        header = _DIFF_HEADER.match(line)
        if header is not None:
            path = header.group("b")
            current = files.setdefault(path, _FileState(path=path))
            if header.group("a") != path:
                current.renamed_from = header.group("a")
            continue
        if current is None:
            continue
        if line.startswith("deleted file mode"):
            current.deleted = True
            continue
        if line.startswith("new file mode"):
            current.added = True
            continue
        if line.startswith(("+++", "---", "@@", "index ", "similarity ")):
            continue

        if line.startswith("+") and not line.startswith("+++"):
            current.insertions += 1
        elif line.startswith("-") and not line.startswith("---"):
            current.deletions += 1
        else:
            continue

        path = current.path
        if line.startswith("-"):
            for pattern in (
                _REMOVED_PYTHON_EXPORT,
                _REMOVED_PYTHON_CONSTANT,
                _REMOVED_TS_EXPORT,
            ):
                match = pattern.match(line)
                if match is not None:
                    removed_exports.append(match.group("name"))
                    note("removed_export", path, line)
                    break
            else:
                reexport = _REMOVED_TS_REEXPORT.match(line)
                if reexport is not None:
                    for name in _ts_reexport_names(reexport.group("names")):
                        removed_exports.append(name)
                        note("removed_export", path, line)
            if _CONTRACT_PATH.search(path) and ":" in line:
                note("contract_line_removed", path, line)
            if _ROUTE_DECORATOR.match(line):
                note("route_removed", path, line)

        if _MIGRATION_PATH.search(path) and _DATA_DROPPING.search(line):
            # Only an *added* drop: a migration being edited to remove its own
            # drop is the opposite of a data loss.
            if line.startswith("+"):
                note("migration_drops_data", path, line)

        if _MANIFEST_PATH.search(path):
            dep = _DEP_LINE.match(line)
            version = _VERSION_IN_LINE.search(line)
            if dep is not None and version is not None:
                removed = dep.group("sign") == "-"
                target = current.removed_deps if removed else current.added_deps
                target[dep.group("name")] = version.group("major")

    for state in files.values():
        for name, old_major in state.removed_deps.items():
            new_major = state.added_deps.get(name)
            if new_major is not None and int(new_major) > int(old_major):
                note(
                    "dependency_major_bump",
                    state.path,
                    f"{name} {old_major}.x -> {new_major}.x",
                )

    deleted_files = tuple(
        sorted(state.path for state in files.values() if state.deleted)
    )
    for path in deleted_files:
        note("file_deleted", path, "the whole file was removed")
    for state in files.values():
        if not state.deleted and state.deletions > deletion_threshold:
            note(
                "deletions_above_threshold",
                state.path,
                f"{state.deletions} lines removed (threshold {deletion_threshold})",
            )
        if state.renamed_from:
            note("file_renamed", state.path, f"was {state.renamed_from}")

    breaking_rules = {
        "removed_export",
        "contract_line_removed",
        "route_removed",
        "migration_drops_data",
    }
    # A dependency major bump is judged, not escalated. It used to be
    # breaking, and every one of them parked and waited for the person -- who
    # answered three of them in one evening with the same words: "I don't
    # want to deal with dependency bumps, you can troubleshoot and fix this
    # kind of maintenance." A captured correction that can be mechanised is
    # mechanised, so it is a deleting-class change now: a second runtime has
    # to attest it, with the checks and the review in force, and nobody is
    # asked. What stays breaking is what the plan names -- a removed public
    # export, a changed wire contract, a route gone, a migration that drops
    # data -- because those break somebody else's code rather than the
    # build's own dependencies.
    deleting_rules = {
        "file_deleted",
        "deletions_above_threshold",
        "file_renamed",
        "dependency_major_bump",
    }
    fired = {reason.rule for reason in reasons}
    if fired & breaking_rules:
        change_class = "breaking"
    elif fired & deleting_rules:
        change_class = "deleting"
    else:
        change_class = "additive"

    kept = reasons[:MAX_REASONS]
    return ChangeAssessment(
        change_class=change_class,
        reasons=tuple(kept),
        removed_exports=tuple(sorted(set(removed_exports))),
        deleted_files=deleted_files,
        changed_files=tuple(sorted(files)),
        insertions=sum(state.insertions for state in files.values()),
        deletions=sum(state.deletions for state in files.values()),
        reasons_omitted=max(0, len(reasons) - len(kept)),
    )


def normalize_change_class(value: object) -> str | None:
    """A claimed class, or ``None`` when it is not one of the three."""
    if not isinstance(value, str):
        return None
    folded = value.strip().casefold()
    return folded if folded in CHANGE_CLASSES else None


def claim_mismatch(claimed: object, assessed: ChangeAssessment) -> str | None:
    """How the builder's claim differs from the diff, or ``None``.

    A claim that is *more* restrictive than the diff is not a mismatch worth
    reporting: somebody being careful about their own change is the behaviour
    this is trying to encourage. A claim that is less restrictive is a finding,
    because that is the direction that publishes something unreviewed.
    """
    normalized = normalize_change_class(claimed)
    if normalized is None:
        if claimed in (None, ""):
            return None
        return f"the builder claimed an unknown change class {str(claimed)[:40]!r}"
    if CHANGE_CLASSES.index(normalized) >= CHANGE_CLASSES.index(assessed.change_class):
        return None
    evidence = "; ".join(
        f"{reason.rule} in {reason.path}" for reason in assessed.reasons[:3]
    )
    return (
        f"the builder called this {normalized}, but the diff is "
        f"{assessed.change_class}: {evidence}"
    )
