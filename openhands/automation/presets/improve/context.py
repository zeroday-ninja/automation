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

import hashlib
import os
import subprocess


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


def branch_is_pushed(repo_path: str, branch: str) -> bool:
    """Whether the branch exists on the remote.

    It matters because an unpushed branch cannot be seen by the next cycle,
    which runs in a fresh sandbox: the reasoning in a handoff still helps, but
    the commits are gone and saying otherwise would send the next runtime
    looking for something that is not there.
    """
    try:
        result = subprocess.run(
            ["git", "-C", repo_path, "ls-remote", "--exit-code", "origin", branch],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"  could not check whether {branch} was pushed: {exc}")
        return False
    return result.returncode == 0


def prompt_digest(prompt: str) -> str:
    """A fingerprint of the rendered prompt.

    The digest rather than the text: the rendered prompt embeds the user's
    identity and the repository's, which is not something to store by default.
    It is still enough to answer "was this change made under the instructions
    I think it was".
    """
    return "sha256:" + hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def normalize_checks(raw: object) -> list:
    """Checks as typed records: a command, whether it passed, and detail.

    Accepts the shapes an agent actually produces — a list of strings, or of
    objects — because a check recorded loosely is still evidence, and refusing
    it would mean recording nothing.
    """
    if not isinstance(raw, list):
        return []
    checks: list = []
    for entry in raw[:40]:
        if isinstance(entry, str) and entry.strip():
            checks.append({"command": entry.strip()[:500], "passed": None})
            continue
        if not isinstance(entry, dict):
            continue
        command = entry.get("command") or entry.get("name") or entry.get("check")
        if not isinstance(command, str) or not command.strip():
            continue
        passed = entry.get("passed")
        if passed is None and isinstance(entry.get("result"), str):
            text = entry["result"].strip().lower()
            # "pass"/"fail" is what agents write; anything else stays None,
            # because an outcome nobody can read is not a failure.
            passed = (
                True
                if text.startswith("pass")
                else (False if text.startswith("fail") else None)
            )
        detail = entry.get("detail") or entry.get("result") or entry.get("output")
        checks.append(
            {
                "command": command.strip()[:500],
                "passed": passed if isinstance(passed, bool) else None,
                "detail": (
                    str(detail)[:2000] if isinstance(detail, (str, int)) else None
                ),
            }
        )
    return checks


def git_commit_range(repo_path: str, base_ref: str, branch: str) -> dict:
    """The commits this cycle started from and produced.

    Without a head SHA there is no path from a merged change back to the
    prompt and the sources behind it, which is the one question an audit trail
    exists to answer. Best-effort — a repository in an unexpected state should
    not fail a cycle that otherwise succeeded — but a failure is reported in
    the run log rather than passed over in silence.
    """
    shas: dict = {}
    for field, ref in (("base_sha", base_ref), ("head_sha", branch)):
        try:
            result = subprocess.run(
                ["git", "-C", repo_path, "rev-parse", ref],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"  could not read {ref}: {exc}")
            continue
        if result.returncode != 0:
            print(f"  could not read {ref}: {result.stderr.strip()[:200]}")
            continue
        shas[field] = result.stdout.strip()[:64]
    return shas


#: Substrings that mean a runtime refused for usage reasons.
#:
#: The protocol exposes no remaining balance, so exhaustion is only ever
#: visible as a refusal. Matching text is crude and it is what there is; a
#: miss costs one wasted cycle, and the alternative — treating every failure
#: as exhaustion — would park healthy runtimes.
USAGE_LIMIT_MARKERS = (
    "you've hit your",
    "usage limit",
    "usagelimitexceeded",
    "out of usage",
    "out of credits",
    "insufficient_quota",
    "quota exceeded",
    "rate limit",
    "rate_limit",
    "429",
)


def looks_exhausted(text: str | None) -> bool:
    """Whether this text is a runtime saying it has nothing left."""
    if not text:
        return False
    lowered = text.casefold()
    return any(marker in lowered for marker in USAGE_LIMIT_MARKERS)


def resume_section(resume: dict | None) -> str:
    """What a cycle continuing someone else's work is told.

    The rejected approaches go first and are stated as instructions rather
    than as history, because they are the part that actually saves the budget:
    a cycle that re-derives the last one's dead end has spent its runtime to
    learn something already written down.
    """
    if not isinstance(resume, dict) or not resume:
        return ""
    lines = [
        "## You are continuing unfinished work",
        "",
        "A previous cycle on this repository ran out of runtime before it "
        "could publish. Continue it rather than starting again.",
        "",
    ]
    if resume.get("goal"):
        lines += [f"**What it was doing:** {resume['goal']}", ""]
    if resume.get("branch"):
        pushed = resume.get("branch_pushed", True)
        lines += [
            f"**Its branch:** `{resume['branch']}`"
            + ("" if pushed else " — never pushed, so its commits are gone"),
            "",
        ]
    for title, key in (
        ("Already done", "done"),
        ("Do NOT repeat these — they were tried and rejected", "do_not"),
        ("Next steps it left", "next_steps"),
    ):
        items = resume.get(key) or []
        if items:
            lines += [f"**{title}:**", ""]
            lines += [f"- {item}" for item in items]
            lines += [""]
    return "\n".join(lines)


#: Error codes the SDK raises when an ACP runtime refuses for usage reasons.
#:
#: Typed, so this is the signal to prefer over ``USAGE_LIMIT_MARKERS``: the
#: markers read a sentence an agent wrote, these read what the runtime
#: actually reported.
USAGE_LIMIT_CODES = ("ACPUsageLimit", "ACPUsageLimitChainExhausted")

#: Longest a runtime's own words are kept on a switch or an observation.
MAX_EVIDENCE_CHARS = 500


def _evidence(value: object) -> str | None:
    """A runtime's own words, bounded, or ``None`` when it said nothing."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    return text[:MAX_EVIDENCE_CHARS]


def trace_runtime(
    events: list[dict] | None,
    *,
    started_on: str | None,
    finished: bool,
    conversation_id: str | None,
    observed_at: str,
) -> dict:
    """What actually ran, read from a conversation's own typed events.

    A conversation can change runtime underneath itself: the SDK fails over on
    a usage limit and keeps going, so the profile's runtime is what the cycle
    *began* on and not necessarily what produced the work. Until this was
    read, a cycle built by the fallback was still recorded against the runtime
    that ran out, which makes both the audit trail and any share-of-work
    measurement wrong in the same direction.

    ``events`` is the conversation's events, oldest first, as the agent server
    serialises them; ``None`` means they could not be read, which is reported
    as a degraded trace rather than as an absence of switches.

    Returns the route, the runtime that ended up running, the switches worth
    auditing, and the ledger observations they imply.
    """
    route: list[str] = [started_on] if started_on else []
    switches: list[dict] = []
    observations: list[dict] = []
    degraded: str | None = None

    if events is None:
        degraded = "the conversation's events could not be read"
        events = []

    exhausted: set[str] = set()
    for event in events:
        if not isinstance(event, dict):
            continue
        kind = event.get("kind")
        if kind == "ACPRuntimeSwitchEvent":
            came_from = str(event.get("from_server") or "") or (
                route[-1] if route else ""
            )
            went_to = str(event.get("to_server") or "")
            if not went_to:
                continue
            reason = str(event.get("reason") or "")
            raw_hint = event.get("reset_hint")
            hint = raw_hint if isinstance(raw_hint, dict) else {}
            switches.append(
                {
                    "from": came_from,
                    "to": went_to,
                    "model": event.get("to_model"),
                    "reason": reason or "unknown",
                    "at": event.get("timestamp") or observed_at,
                    "detail": _evidence(event.get("detail")),
                }
            )
            if not route or route[-1] != came_from:
                route.append(came_from)
            route.append(went_to)
            # A runtime abandoned mid-conversation is the honest meaning of
            # "nearly out": it refused, and only this hop proves it.
            if reason == "usage_limit" and came_from:
                exhausted.add(came_from)
                observations.append(
                    {
                        "runtime": came_from,
                        "outcome": "failed_over_from",
                        "observed_at": event.get("timestamp") or observed_at,
                        "reset_at": hint.get("at"),
                        "reset_source": hint.get("source"),
                        "evidence": _evidence(event.get("detail")),
                        "conversation_id": conversation_id,
                    }
                )
        elif kind == "ConversationErrorEvent" and event.get("code") in (
            USAGE_LIMIT_CODES
        ):
            # The runtime live when the chain gave up. Every earlier one
            # already has its own `failed_over_from`.
            refused = route[-1] if route else (started_on or "")
            if refused and refused not in exhausted:
                exhausted.add(refused)
                observations.append(
                    {
                        "runtime": refused,
                        "outcome": "exhausted",
                        "observed_at": event.get("timestamp") or observed_at,
                        "evidence": _evidence(event.get("detail")),
                        "conversation_id": conversation_id,
                    }
                )

    ran = route[-1] if route else started_on
    # Only the runtime that finished earns an `ok`: a success clears an
    # exhaustion, and clearing it for one that was abandoned would undo the
    # observation recorded two lines above.
    #
    # A degraded trace earns none at all. The conversation succeeded, but with
    # the events unreadable there is no telling whether it switched, so an
    # `ok` here could clear a real exhaustion on a runtime that in fact ran
    # out -- asserting the one thing that was not observed.
    if finished and ran and ran not in exhausted and degraded is None:
        observations.append(
            {
                "runtime": ran,
                "outcome": "ok",
                "observed_at": observed_at,
                "conversation_id": conversation_id,
            }
        )

    return {
        "route": route,
        "ran": ran,
        "switches": switches,
        "observations": observations,
        "degraded": degraded,
    }
