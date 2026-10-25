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
#: Each title names the document's **full** path, not its filename.
#:
#: The user's memory and the repository's are both called MEMORY.md, and the
#: builder is told to append a rejected approach to the repository's one while
#: holding the user's in its prompt. A heading that said only "MEMORY.md"
#: left the two indistinguishable by name, which is the one confusion whose
#: cost is the user's personal continuity log committed into a repository.
IDENTITY_DOCUMENT_TITLES = [
    ("soul", "Who the user is (~/.openhands/soul/SOUL.md)"),
    ("style", "How the user writes and works (~/.openhands/soul/STYLE.md)"),
    ("memory", "What has already been learned (~/.openhands/soul/MEMORY.md)"),
    (
        "good-outputs",
        "Work that sounds like them (~/.openhands/soul/examples/good-outputs.md)",
    ),
    (
        "bad-outputs",
        "Work that does not (~/.openhands/soul/examples/bad-outputs.md)",
    ),
    (
        "influences",
        "What shaped their thinking (~/.openhands/soul/data/influences.md)",
    ),
]

#: What the repository's whole identity set may take of a cycle's prompt.
#:
#: Below the user's 24000 deliberately: the repository's identity must never
#: take more of the prompt than the person it is being worked for. The four
#: documents' own budgets sum to 22000, so without an aggregate the project
#: half could nearly match the user half -- and unlike the user half it was
#: neither capped in total nor measured, which is the same blind spot
#: `identity_cost` was written to close on the other side.
PROJECT_IDENTITY_BUDGET_CHARS = 16000

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


def _cost_of(documents: dict, order: list, budget: int, tail: str) -> dict:
    """What one identity set took, counted the way its sections are built.

    `order` is the load order, `tail` the document kept from the end rather
    than the start -- a log wants its most recent entries.
    """
    spent = 0
    per_document: dict[str, int] = {}
    trimmed: list[str] = []
    omitted: list[str] = []
    for name in order:
        text = documents.get(name)
        if not text:
            continue
        room = budget - spent
        if room <= 0:
            omitted.append(name)
            continue
        kept = trim_to_budget(text, room, keep_end=name == tail)
        if len(kept) < len(text):
            trimmed.append(name)
        per_document[name] = len(kept)
        spent += len(kept)
    return {
        "budget": budget,
        "used": spent,
        # Rounded to the nearest point: the question is "is identity taking
        # most of the prompt", and four decimal places answer a question
        # nobody asked.
        "share": round(spent / budget, 3) if budget else None,
        "documents": per_document,
        #: Documents that did not fit whole. This is the one a person acts on.
        "trimmed": trimmed,
        #: Documents the budget never reached at all.
        "omitted": omitted,
    }


def identity_cost(
    documents: dict,
    budget: int = IDENTITY_BUDGET_CHARS,
    project: dict | None = None,
) -> dict:
    """What identity actually took of the cycle's prompt.

    The budget was enforced from the start and never measured, so the one
    question the milestone asks — does identity crowd out the repository the
    cycle is supposed to improve — could not be answered, and the failure that
    matters was invisible: a SOUL.md that has grown past its slot is trimmed
    silently, and the user who wrote the part that got cut has no way to know.

    That was half the answer. The *repository's* identity set is read and
    injected on every cycle too, and it was neither capped in total nor
    recorded anywhere but the run log — up to 22000 characters of a cycle's
    prompt that no record could account for, so "a cycle on a repository with
    a full identity set demonstrably uses it" could not be demonstrated and
    "identity never exceeds its share" was measured against one half of
    identity. Both sets are counted here now, and `total` is the number the
    metric actually asks for.
    """
    user = _cost_of(
        documents,
        [name for name, _title in IDENTITY_DOCUMENT_TITLES],
        budget,
        "memory",
    )
    if project is None:
        return user
    repository = _cost_of(
        project,
        [relative for relative, _title, _budget in PROJECT_IDENTITY_DOCUMENTS],
        PROJECT_IDENTITY_BUDGET_CHARS,
        ".openhands/identity/MEMORY.md",
    )
    both = budget + PROJECT_IDENTITY_BUDGET_CHARS
    spent = user["used"] + repository["used"]
    return {
        **user,
        #: The repository's own identity, counted the same way.
        "project": repository,
        #: The two together, which is what "identity's share of the prompt"
        #: means. Reading either half alone understates it by the other.
        "total": {
            "budget": both,
            "used": spent,
            "share": round(spent / both, 3) if both else None,
        },
    }


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
    """The repository's identity, or an instruction to draft the missing one.

    Inside an aggregate budget, like the user's and for the same reason: four
    documents each inside its own slot can still add up to most of a prompt,
    and this one said so in the run log and nowhere a reader would find it.
    """
    blocks: list[str] = []
    spent = 0
    for relative, title, _budget in PROJECT_IDENTITY_DOCUMENTS:
        text = documents.get(relative)
        if not text:
            continue
        room = PROJECT_IDENTITY_BUDGET_CHARS - spent
        if room <= 0:
            blocks.append(
                f"## {title}\n\n[omitted: this project's identity budget was "
                "already spent on the documents above]\n"
            )
            continue
        kept = trim_to_budget(text, room, keep_end=relative.endswith("MEMORY.md"))
        spent += len(kept)
        blocks.append(f"## {title}\n\n{kept}\n")
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


def save_work_in_progress(
    repo_path: str, branch: str, *, base_ref: str, reason: str
) -> dict:
    """Put whatever the builder had onto the remote, before describing it.

    The handoff's order of priority is the code first and the description
    second, and until this existed the order was the other way round: the
    builder prompt commits and pushes in one step at the very end, so a
    builder that ran out mid-change had typically committed nothing. The
    handoff then recorded ``branch_pushed: false`` and told the next cycle to
    read a diff that lived only in a sandbox about to be deleted. Four live
    cycles were handed that.

    Costs no tokens -- every step is git -- and runs while the sandbox is
    still up, which is the only moment it can.

    Returns what it actually managed. A push that fails is reported, not
    passed over: the next cycle has to be told what is on the remote rather
    than what was intended.
    """

    def git(*args: str, timeout: int = 180) -> subprocess.CompletedProcess | None:
        try:
            return subprocess.run(
                ["git", "-C", repo_path, *args],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"  git {args[0]} failed: {exc}")
            return None

    saved: dict = {"branch": branch, "committed": False, "pushed": False}

    head = git("rev-parse", "--abbrev-ref", "HEAD")
    on_branch = (
        head is not None and head.returncode == 0 and head.stdout.strip() == branch
    )
    if not on_branch:
        # Only create it. Moving an existing branch to this HEAD would throw
        # away commits somebody already put on it, which is the one outcome
        # worse than losing an uncommitted tree.
        exists = git("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
        if exists is not None and exists.returncode == 0:
            saved["note"] = (
                f"{branch} exists but the builder left HEAD elsewhere; "
                "its work was not moved onto it"
            )
            print(f"  {saved['note']}")
            return saved
        made = git("checkout", "-b", branch)
        if made is None or made.returncode != 0:
            detail = made.stderr.strip()[:200] if made else "git could not run"
            saved["note"] = f"could not put the work on {branch}: {detail}"
            print(f"  {saved['note']}")
            return saved

    dirty = git("status", "--porcelain")
    if dirty is not None and dirty.returncode == 0 and dirty.stdout.strip():
        staged = git("add", "--all")
        message = (
            f"wip: {reason}\n\n"
            "Saved by the improvement cycle so the next one can continue it."
        )
        # `--no-verify`: a repository's own pre-commit hook rejecting a
        # half-finished tree would lose the whole change, and this commit
        # exists to be continued rather than to be reviewed.
        made = git("commit", "--no-verify", "-m", message) if staged else None
        if made is not None and made.returncode == 0:
            saved["committed"] = True
        else:
            detail = made.stderr.strip()[:200] if made else "git could not run"
            saved["note"] = f"could not commit the working tree: {detail}"
            print(f"  {saved['note']}")

    # Nothing on the branch means nothing to push, and a push that would
    # create an empty branch is noise rather than a saved change. Counted
    # against the cycle's own base rather than the remote's default branch:
    # a program targeting any other ref would otherwise count the difference
    # between the two as work.
    ahead = git("rev-list", "--count", f"{base_ref}..{branch}")
    if ahead is not None and ahead.returncode == 0 and ahead.stdout.strip() == "0":
        saved["note"] = saved.get("note") or "the builder produced no commits to save"
        print(f"  {saved['note']}")
        return saved

    pushed = git("push", "--set-upstream", "origin", branch, timeout=300)
    if pushed is not None and pushed.returncode == 0:
        saved["pushed"] = True
        print(f"  saved the interrupted work on {branch}")
    else:
        detail = pushed.stderr.strip()[:200] if pushed else "git could not run"
        saved["note"] = f"could not push {branch}: {detail}"
        print(f"  {saved['note']}")
    return saved


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


def phase_failure_notice(detail: str) -> str:
    """What to print the first time a progress phase does not reach the service.

    The phase is the only thing a person watching a run sees change, so a run
    whose phase updates are failing looks stalled while it is working. It must
    stay non-fatal — a cosmetic update cannot be allowed to end a cycle — but
    it must not be silent, and the consequence is the part worth saying: the
    dashboard is now behind, and will stay behind.
    """
    return (
        f"  phase not recorded (non-fatal): {detail}\n"
        "  the dashboard will show an earlier phase than this run is at"
    )


def lost_phase_summary(lost: list[str]) -> str | None:
    """What to print at the end when phases went missing, or None if none did.

    Once, at the end, where a reader comparing the dashboard against this log
    will be looking. Only the last attempted phase: the whole list is every
    phase of the run, and the question being answered is "how far did it
    actually get".
    """
    if not lost:
        return None
    return (
        f"{len(lost)} phase update(s) never reached the service, so the "
        "dashboard stopped short of where this run got to. The last one it "
        f"tried to report: {lost[-1]}"
    )


def ran_out_of_time(error: BaseException | None) -> bool:
    """Whether this failure is a run that exceeded its time, not one that broke.

    Typed first. The SDK wraps the real exception as `original_exception`, so
    a timeout is a `TimeoutError` somewhere in the chain rather than a phrase
    to match — unlike a usage limit, which arrives only as a runtime's own
    prose. The wording is a fallback for a server that did not carry the
    chain, and the cost of being wrong either way is a handoff that was or was
    not written, never a wrong verdict.
    """
    seen: set[int] = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, TimeoutError):
            return True
        if "timed out after" in str(error).casefold():
            return True
        error = (
            getattr(error, "original_exception", None)
            or error.__cause__
            or error.__context__
        )
    return False


def resume_section(resume: dict | None) -> str:
    """What a cycle continuing someone else's work is told.

    The rejected approaches go first and are stated as instructions rather
    than as history, because they are the part that actually saves the budget:
    a cycle that re-derives the last one's dead end has spent its runtime to
    learn something already written down.
    """
    if not isinstance(resume, dict) or not resume:
        return ""
    # Why it stopped is an instruction, not trivia. A runtime out of quota
    # left off wherever it happened to be and this cycle simply carries on; a
    # cycle out of *time* was attempting more than fits, and continuing at the
    # same size is how the next one runs out too.
    why = (
        "ran out of time before it could publish. Continue it rather than "
        "starting again — and take a smaller piece than it did, because the "
        "size of the attempt is what ran it out"
        if resume.get("reason") == "timed_out"
        else "ran out of runtime before it could publish. Continue it rather "
        "than starting again"
    )
    lines = [
        "## You are continuing unfinished work",
        "",
        f"A previous cycle on this repository {why}.",
        "",
    ]
    if resume.get("goal"):
        lines += [f"**What it was doing:** {resume['goal']}", ""]
    if resume.get("branch"):
        pushed = bool(resume.get("branch_pushed"))
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
