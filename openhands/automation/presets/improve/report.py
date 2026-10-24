"""Reading the JSON reports agents write, and picking the reviewing runtimes.

Shipped inside the improvement-program tarball next to ``main.py`` and kept
separate from it for one reason: ``main.py`` runs on import, so nothing can
test it, and this is the logic most able to go quietly wrong. An agent writes
free-form JSON into a file, and everything the dashboard later shows about a
cycle comes from parsing it.

The guiding rule here is that a slip in an agent's formatting must not lose its
work: a stray code fence, a bare string where an object was asked for, a
reviewer answering ``newer_available`` with objects instead of strings. None of
those are failed cycles, so each is coerced rather than discarded. What is
*not* tolerated is inventing content: a missing verdict reads as inconclusive,
never as approval.
"""

import json

MAX_SUMMARY_CHARS = 20000
MAX_SOURCES = 40
MAX_SOUL_SUGGESTIONS = 10


def parse_json_report(text: str) -> dict | None:
    """Pull a JSON report out of free text.

    Shared by the file and the fallback below, because an agent that answers
    in chat instead of writing the file makes exactly the same formatting
    slips — a fence, a sentence before the object.
    """
    text = (text or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[: -len("```")]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def read_json_report(path: str) -> dict | None:
    """Read an agent-written JSON report, tolerating a fenced code block.

    Agents are told to write bare JSON and usually do, but a stray ```json
    fence is a formatting slip, not a failed cycle, so it is unwrapped rather
    than discarded.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            raw = handle.read()
    except OSError:
        return None
    parsed = parse_json_report(raw)
    if parsed is None and raw.strip():
        print(f"  report at {path} is not a readable JSON object")
    return parsed


def clamp(value, limit: int = MAX_SUMMARY_CHARS) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    return value[:limit]


def string_list(value, limit: int = 40, item_limit: int = 2000) -> list[str]:
    if not isinstance(value, list):
        return []
    out = []
    for item in value[:limit]:
        if isinstance(item, str) and item.strip():
            out.append(item.strip()[:item_limit])
        elif isinstance(item, dict):
            # Reviewers sometimes answer "newer_available" as objects; keep
            # them readable rather than dropping the finding.
            rendered = " ".join(
                str(item[key]) for key in ("url", "title", "note") if item.get(key)
            ).strip()
            if rendered:
                out.append(rendered[:item_limit])
    return out


def normalize_sources(value) -> list[dict]:
    """Coerce a builder's ``sources`` into the cycle API's shape."""
    if not isinstance(value, list):
        return []
    sources = []
    for item in value[:40]:
        if isinstance(item, str) and item.strip().startswith(("http://", "https://")):
            sources.append({"url": item.strip()[:2000]})
            continue
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if not isinstance(url, str) or not url.strip().startswith(
            ("http://", "https://")
        ):
            continue
        source = {"url": url.strip()[:2000]}
        for key, limit in (("title", 500), ("note", 2000)):
            text = clamp(item.get(key), limit)
            if text:
                source[key] = text
        sources.append(source)
    return sources


def normalize_soul_suggestions(value) -> list[dict]:
    """Coerce proposed SOUL.md additions into the cycle API's shape.

    Capped and deduplicated: every entry is something a human has to read and
    decide on, so a run that produces a wall of them has produced nothing.
    """
    if not isinstance(value, list):
        return []
    suggestions = []
    seen = set()
    for item in value[:10]:
        if isinstance(item, str):
            item = {"text": item}
        if not isinstance(item, dict):
            continue
        text = clamp(item.get("text"), 2000)
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        suggestion = {"text": text}
        why = clamp(item.get("why"), 2000)
        if why:
            suggestion["why"] = why
        suggestions.append(suggestion)
    return suggestions


_VERDICTS = {
    "accepted": "ACCEPTED",
    "changes_requested": "CHANGES_REQUESTED",
    "rejected": "REJECTED",
    "inconclusive": "INCONCLUSIVE",
}
# Worst wins when runtimes disagree: one runtime finding the change wrong is
# the finding, even if another was content with it.
#: Which rule turns a set of reviews into one verdict.
#:
#: Bumped whenever the severity table or the aggregation changes. Recorded on
#: every cycle, because this code ships into the sandbox: without it, editing
#: the table below would retroactively reinterpret every verdict ever stored.
VERDICT_RULE_VERSION = "worst-of-judged/1"

_VERDICT_SEVERITY = {
    "ACCEPTED": 0,
    "SINGLE_RUNTIME": 1,
    "INCONCLUSIVE": 2,
    "CHANGES_REQUESTED": 3,
    "REJECTED": 4,
}


def normalize_verdict(value) -> str:
    if isinstance(value, str):
        return _VERDICTS.get(value.strip().lower(), "INCONCLUSIVE")
    return "INCONCLUSIVE"


def worst_verdict(verdicts: list[str]) -> str:
    if not verdicts:
        return "INCONCLUSIVE"
    return max(verdicts, key=lambda verdict: _VERDICT_SEVERITY.get(verdict, 2))


def cycle_verdict(reviews: list[dict]) -> str:
    """The cycle's verdict, from the reviewers that actually judged it.

    A reviewer that *failed* — an unreachable server, a stuck conversation —
    did not judge anything, and must not outrank one that did. Counting it as
    INCONCLUSIVE made one broken runtime outrank every real verdict, so a
    change two working runtimes would have accepted could never read as
    accepted. That is not caution; it is a broken runtime holding a veto.

    A failed review is still recorded and still visible. It just does not vote.
    When *every* reviewer failed, nothing independent judged the change, and
    that is exactly what SINGLE_RUNTIME says.
    """
    judged = [
        review["verdict"]
        for review in reviews
        if review.get("verdict") and not review.get("failed")
    ]
    if not judged:
        return "SINGLE_RUNTIME"
    return worst_verdict(judged)


def acp_server_from_detail(payload) -> str | None:
    """The ACP server named by a ``GET /api/agent-profiles/{name}`` response.

    The agent server answers ``{"name": ..., "profile": {...}}``, but the
    field has lived at the top level in other shapes of this API. Reading
    both is the difference between knowing a review came from Codex and
    recording the useless string "acp" — which is what the program exists to
    tell apart.
    """
    if not isinstance(payload, dict):
        return None
    for candidate in (payload.get("profile"), payload):
        if isinstance(candidate, dict):
            server = candidate.get("acp_server")
            if isinstance(server, str) and server.strip():
                return server.strip()
    return None


def choose_reviewers(config: dict, profiles: dict, builder_id: str) -> list[dict]:
    """Which profiles review this cycle, and under which runtime.

    Configured reviewers win. With none configured the program picks one
    profile per *other* runtime available on the server, so adding a second
    runtime is all it takes to turn independent review on — which is the
    behavior the program is for.
    """
    builder = profiles.get(builder_id) or {}
    builder_runtime = builder.get("runtime")
    configured = [str(pid) for pid in config.get("reviewer_profile_ids") or []]

    if configured:
        chosen = []
        for profile_id in configured:
            if profile_id == builder_id:
                # Belt and braces. The config validator refuses this, but the
                # builder can also move between cycles, and a runtime
                # reviewing its own work is the one outcome this whole design
                # exists to prevent.
                continue
            profile = profiles.get(profile_id)
            if profile is None:
                # Keep it: the server may simply not have listed it. The
                # conversation will fail loudly if the id is really gone.
                chosen.append({"id": profile_id, "name": None, "runtime": None})
                continue
            chosen.append(
                {
                    "id": profile_id,
                    "name": profile.get("name"),
                    "runtime": profile.get("runtime"),
                }
            )
        return chosen

    chosen = []
    seen_runtimes = {builder_runtime} if builder_runtime else set()
    for profile_id, profile in sorted(
        profiles.items(), key=lambda item: item[1].get("name") or ""
    ):
        runtime = profile.get("runtime")
        if profile_id == builder_id or not runtime or runtime in seen_runtimes:
            continue
        seen_runtimes.add(runtime)
        chosen.append(
            {"id": profile_id, "name": profile.get("name"), "runtime": runtime}
        )
    return chosen
