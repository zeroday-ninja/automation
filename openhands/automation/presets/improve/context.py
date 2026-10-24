"""The context a cycle reasons from, and the budget it has to fit in.

A separate module for the same reason ``report.py`` is one: these are pure
functions over text, and a test should be able to state what they do without
standing up a sandbox or satisfying the runner's environment.

Identity is *context*, not the task. A set that has grown over months must not
be able to crowd out the repository the cycle is supposed to improve, which is
why everything here is bounded and why the bounds are stated rather than
discovered when a prompt is silently truncated somewhere downstream.
"""

from __future__ import annotations

import os


#: Largest a single document may be before it is cut, in characters.
MAX_DOC_CHARS = 20000


def read_doc(path: str) -> str | None:
    """A document from disk, or ``None`` when there is nothing to read.

    Bounded on read rather than after: a file someone committed by accident
    should not be pulled into memory in full just to be thrown away.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            text = handle.read(MAX_DOC_CHARS + 1)
    except OSError:
        return None
    text = text.strip()
    if not text:
        return None
    if len(text) > MAX_DOC_CHARS:
        text = text[:MAX_DOC_CHARS] + "\n\n[truncated]"
    return text


#: How much of a cycle's prompt identity may occupy, in characters.
#:
#: Identity is context, not the task. A set that has grown over months must not
#: be able to crowd out the repository the cycle is supposed to improve, so the
#: budget is fixed here and spent in load order: who the user is, then how they
#: write, then what has already been learned, then calibration.
IDENTITY_BUDGET_CHARS = 24000

#: What each identity document may take of that budget, by its id.
#:
#: Proportional to how much it changes what the work looks like. Voice and
#: identity earn the most; examples are calibration and a few are enough;
#: memory is bounded here and trimmed to its most recent entries, because the
#: oldest thing someone learned is rarely the most relevant.
IDENTITY_DOCUMENT_BUDGET = {
    "soul": 8000,
    "style": 6000,
    "memory": 6000,
    "good-outputs": 2000,
    "bad-outputs": 2000,
    "influences": 2000,
}

#: Human titles for the identity documents, in load order.
IDENTITY_DOCUMENT_TITLES = [
    ("soul", "Who the user is (SOUL.md)"),
    ("style", "How the user writes and works (STYLE.md)"),
    ("memory", "What has already been learned (MEMORY.md)"),
    ("good-outputs", "Work that sounds like them (examples/good-outputs.md)"),
    ("bad-outputs", "Work that does not (examples/bad-outputs.md)"),
    ("influences", "What shaped their thinking (data/influences.md)"),
]

#: The project's own identity set, read from the repository being improved.
PROJECT_IDENTITY_DOCUMENTS = [
    ("PROJECT_IDENTITY.md", "What this project is for (PROJECT_IDENTITY.md)", 8000),
    (
        ".openhands/identity/STYLE.md",
        "How code and prose are written here (.openhands/identity/STYLE.md)",
        6000,
    ),
    (
        ".openhands/identity/MEMORY.md",
        "Decisions made, and approaches already tried and rejected "
        "(.openhands/identity/MEMORY.md)",
        6000,
    ),
    (
        ".openhands/identity/examples/bad-changes.md",
        "Changes this project did not want (.openhands/identity/examples/"
        "bad-changes.md)",
        2000,
    ),
]


def trim_to_budget(text: str, budget: int, *, keep_end: bool = False) -> str:
    """``text`` cut to ``budget`` characters, saying so where it was cut.

    ``keep_end`` keeps the tail instead of the head, which is what a log wants:
    the most recent thing learned is the one most likely to matter.
    """
    if len(text) <= budget:
        return text
    if keep_end:
        return "[earlier entries omitted]\n\n" + text[-budget:]
    return text[:budget] + "\n\n[truncated]"


def read_project_identity(repo_path: str) -> dict:
    """The repository's own identity documents, keyed by relative path."""
    found: dict = {}
    for relative, _title, budget in PROJECT_IDENTITY_DOCUMENTS:
        text = read_doc(os.path.join(repo_path, relative))
        if text:
            found[relative] = trim_to_budget(
                text, budget, keep_end=relative.endswith("MEMORY.md")
            )
    return found


def identity_sections(documents: dict, note: str | None) -> str:
    """The user's identity, in load order, inside the character budget.

    Identity first, then voice, then memory, then calibration: a model that
    reads the examples before it knows whose voice it is in has read them for
    nothing.
    """
    blocks: list[str] = []
    spent = 0
    for name, title in IDENTITY_DOCUMENT_TITLES:
        text = documents.get(name)
        if not text:
            continue
        room = IDENTITY_BUDGET_CHARS - spent
        if room <= 0:
            blocks.append(
                f"## {title}\n\n[omitted: the identity budget was already "
                "spent on the documents above]\n"
            )
            continue
        trimmed = trim_to_budget(text, room, keep_end=name == "memory")
        spent += len(trimmed)
        blocks.append(f"## {title}\n\n{trimmed}\n")
    if note:
        # Stated rather than swallowed: a cycle working without the user's
        # preferences must know that is what it is doing.
        blocks.insert(0, f"## About this context\n\nIncomplete — {note}\n")
    if not blocks:
        return (
            "## Who the user is\n\nThe user has not written any identity "
            "documents yet. Work to the repository's own conventions and do "
            "not invent preferences for them.\n"
        )
    return "\n".join(blocks)


def project_identity_sections(documents: dict, repo_label: str) -> str:
    """The repository's identity, or an instruction to draft the missing one."""
    blocks: list[str] = []
    for relative, title, _budget in PROJECT_IDENTITY_DOCUMENTS:
        text = documents.get(relative)
        if text:
            blocks.append(f"## {title}\n\n{text}\n")
    if blocks:
        return "\n".join(blocks)
    return (
        f"## What {repo_label} is for\n\n{repo_label} has no "
        "PROJECT_IDENTITY.md. Infer its purpose and current goals from its "
        "README, its AGENTS.md and its recent commits, and say in your report "
        "that the document is missing — writing a first draft of it, with a "
        "`.openhands/identity/` set beside it, is itself a good cycle.\n"
    )


def fetch_identity_set(workspace) -> tuple[dict, str | None]:
    """The user's identity documents, and why any are missing.

    Returns the documents the user has actually written, keyed by id, and a
    note when the set could not be read at all. The note is not cosmetic: a
    cycle that reasons without the user's standing preferences and does not
    know it is missing them will state its conclusions with the same
    confidence as one that had them.

    Falls back to the single ``/api/soul`` resource, so an older agent server
    still contributes the one document it has.
    """
    try:
        response = workspace.client.get("/api/soul/documents")
        if response.status_code == 404:
            soul = fetch_soul(workspace)
            if soul is None:
                return {}, None
            return {"soul": soul}, None
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        note = f"the identity set could not be read from the agent server: {exc}"
        print(f"  identity fetch failed: {exc}")
        return {}, note

    documents: dict = {}
    if not isinstance(payload, dict):
        return {}, (
            "the agent server returned an identity set in a shape this "
            "preset does not understand"
        )
    for entry in payload.get("documents") or []:
        if not isinstance(entry, dict) or not entry.get("exists"):
            continue
        name = entry.get("name")
        content = entry.get("content")
        if not isinstance(name, str) or not isinstance(content, str):
            continue
        text = content.strip()
        if not text:
            continue
        budget = IDENTITY_DOCUMENT_BUDGET.get(name, 2000)
        documents[name] = trim_to_budget(text, budget, keep_end=name == "memory")
    return documents, None


def fetch_soul(workspace) -> str | None:
    """The user's SOUL.md from the agent server, if that server serves it.

    Older agent servers have no such route; a program should still run, just
    without the user's standing preferences, so a 404 is not an error here.
    """
    try:
        response = workspace.client.get("/api/soul")
        if response.status_code == 404:
            print("  agent server has no /api/soul route (older version)")
            return None
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        print(f"  SOUL.md fetch failed (non-fatal): {exc}")
        return None
    if not isinstance(payload, dict):
        return None
    # Only a soul the user actually wrote. The server returns its built-in
    # default otherwise, which says nothing about this user's preferences.
    if not payload.get("exists"):
        print("  no user SOUL.md on the agent server")
        return None
    content = payload.get("content")
    if not isinstance(content, str) or not content.strip():
        return None
    text = content.strip()
    if len(text) > MAX_DOC_CHARS:
        text = text[:MAX_DOC_CHARS] + "\n\n[truncated]"
    return text
