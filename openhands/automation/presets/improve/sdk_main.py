"""Improvement-program run script — executes inside an OpenHands sandbox.

One run is one *cycle*: it claims the next repository in the program's
rotation, has the builder profile research and implement a single improvement
there, and then has every other available runtime review that work
adversarially. Nothing is merged; a cycle produces a branch, a pull request
and a verdict for the user to act on.

Why a run creates several conversations instead of the service chaining
several runs: a conversation is created from an ``agent_profile_id``, so one
script can run the builder under Claude Code and the review under Codex and
compare them. Chaining runs would need the rotation state to survive between
them for no gain.

Mode detection, env contract and phase reporting match
``presets/prompt/sdk_main.py``; see that file for the full env var list. The
pieces specific to this preset are:

  AUTOMATION_API_URL            - automation service base (claim/report cycle)
  AUTOMATION_CALLBACK_API_KEY   - credential for those calls (local mode)
  OPENHANDS_API_KEY             - the same, in cloud mode
  AUTOMATION_ID                 - which program is running
  AUTOMATION_RUN_ID             - the run that owns this cycle
  AUTOMATION_AGENT_PROFILE_ID   - ignored: the program names its own profiles
"""

import json
import os
import subprocess
from datetime import datetime, UTC
import sys
import time
import uuid

# Detect execution mode based on AGENT_SERVER_URL presence
agent_server_url = os.environ.get("AGENT_SERVER_URL", "").rstrip("/")
IS_LOCAL_MODE = bool(agent_server_url)

api_key = os.environ.get("OPENHANDS_API_KEY", "")
api_url = os.environ.get("OPENHANDS_CLOUD_API_URL", "").rstrip("/")
sandbox_id = os.environ.get("SANDBOX_ID", "")
session_key = os.environ.get("OH_SESSION_API_KEYS_0") or os.environ.get(
    "SESSION_API_KEY", ""
)
automation_user_id = os.environ.get("AUTOMATION_USER_ID") or None
automation_id = os.environ.get("AUTOMATION_ID", "")
automation_run_id = os.environ.get("AUTOMATION_RUN_ID") or None
automation_api_url = os.environ.get("AUTOMATION_API_URL", "").rstrip("/")

print("=== EXECUTION MODE ===")
print(f"  mode: {'LOCAL' if IS_LOCAL_MODE else 'CLOUD'}")

print("\n=== ENV VARS ===")
if IS_LOCAL_MODE:
    print(f"  AGENT_SERVER_URL: {'OK' if agent_server_url else 'MISSING'}")
    print(f"  OH_SESSION_API_KEYS_0: {'OK' if session_key else 'NONE (may fail auth)'}")
    if not agent_server_url:
        print("FAIL: AGENT_SERVER_URL not set for local mode", file=sys.stderr)
        sys.exit(1)
else:
    for name, val in [
        ("OPENHANDS_API_KEY", api_key),
        ("OPENHANDS_CLOUD_API_URL", api_url),
        ("SANDBOX_ID", sandbox_id),
        ("OH_SESSION_API_KEYS_0", session_key),
    ]:
        print(f"  {name}: {'OK' if val else 'MISSING'}")
        if not val:
            print(f"FAIL: {name} not set", file=sys.stderr)
            sys.exit(1)

# The cycle API is what makes this preset a *program* rather than one prompt:
# without it there is no rotation and no history, so this is fatal.
_CYCLE_TOKEN = (
    os.environ.get("AUTOMATION_CALLBACK_API_KEY")
    or os.environ.get("OPENHANDS_API_KEY")
    or ""
)
print(f"  AUTOMATION_API_URL: {automation_api_url or 'MISSING'}")
print(f"  AUTOMATION_ID: {automation_id or 'MISSING'}")
print(f"  cycle credential: {'OK' if _CYCLE_TOKEN else 'MISSING'}")
for label, value in (
    ("AUTOMATION_API_URL", automation_api_url),
    ("AUTOMATION_ID", automation_id),
    ("cycle credential", _CYCLE_TOKEN),
):
    if not value:
        print(f"FAIL: {label} is required by the improve preset", file=sys.stderr)
        sys.exit(1)

# --- Live phase reporting (best-effort, never fatal) -------------------------
# Keep this block in sync with presets/prompt/sdk_main.py.
AUTOMATION_PHASE_URL = os.environ.get("AUTOMATION_PHASE_URL", "")


#: Phases that never reached the service, counted so a lost one is visible.
#:
#: The phase is the only thing a person watching sees change, so a run whose
#: phase POSTs are failing looks stalled while it is working — and before
#: this, nothing anywhere said otherwise. It stays non-fatal: a cosmetic
#: update must not end a cycle. It stops being silent.
_LOST_PHASES: list[str] = []


def report_phase(message: str) -> None:
    """POST a short progress phase to the automation service. Never raises."""
    if not AUTOMATION_PHASE_URL or not _CYCLE_TOKEN or not message:
        return
    try:
        import httpx

        response = httpx.post(
            AUTOMATION_PHASE_URL,
            json={"phase": message[:200]},
            headers={"Authorization": f"Bearer {_CYCLE_TOKEN}"},
            timeout=5.0,
        )
        response.raise_for_status()
    except Exception as exc:
        # The first one says what went wrong; the rest are counted, because a
        # service that is down fails every phase and a hundred identical
        # lines would bury the work they are interleaved with.
        if not _LOST_PHASES:
            print(phase_failure_notice(f"{type(exc).__name__}: {exc}"))
        _LOST_PHASES.append(message[:200])


# SDK imports (before the workspace context so import errors are caught)
import httpx  # noqa: E402

from openhands.sdk import RemoteConversation  # noqa: E402
from openhands.sdk.automation import automation_conversation_kwargs  # noqa: E402
from openhands.sdk.conversation.request import StartConversationRequest  # noqa: E402
from openhands.sdk.workspace.local import LocalWorkspace  # noqa: E402
from openhands.sdk.workspace.remote.base import RemoteWorkspace  # noqa: E402
from openhands.workspace import OpenHandsCloudWorkspace  # noqa: E402

# Packaged alongside main.py in the tarball; see its docstring for why the
# report parsing lives in a module that can be imported without side effects.
from change_class import assess_diff, claim_mismatch  # noqa: E402
from lessons import lessons_in_scope, lessons_section  # noqa: E402
from routing import GateDecision, route_for  # noqa: E402
from context import (  # noqa: E402
    fetch_identity_set,
    identity_cost,
    looks_exhausted,
    lost_phase_summary,
    phase_failure_notice,
    ran_out_of_time,
    resume_section,
    git_commit_range,
    identity_sections,
    branch_is_pushed,
    normalize_checks,
    project_identity_sections,
    prompt_digest,
    read_project_identity,
    save_work_in_progress,
    trace_runtime,
)
from report import (  # noqa: E402
    VERDICT_RULE_VERSION,
    MAX_SUMMARY_CHARS,
    parse_json_report,
    acp_server_from_detail,
    cycle_verdict,
    choose_reviewers,
    clamp,
    changeover_plan,
    claim_refusal_phase,
    normalize_removals,
    normalize_sources,
    normalize_soul_suggestions,
    normalize_verdict,
    read_json_report,
    string_list,
)


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CYCLE_DIR_NAME = ".improve-cycle"
MAX_DOC_CHARS = 20000

# How long to wait for the cycle API. Short: these calls sit between stages of
# a run that may legitimately take hours, and a hung report should not consume
# the budget meant for the work.
CYCLE_HTTP_TIMEOUT = 30.0


def cycle_request(method: str, path: str, payload: dict | None = None) -> dict:
    """Call the automation service's improvement-cycle API."""
    response = httpx.request(
        method,
        f"{automation_api_url}{path}",
        json=payload,
        headers={"Authorization": f"Bearer {_CYCLE_TOKEN}"},
        timeout=CYCLE_HTTP_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def report_from_final_response(workspace, conversation_id: str | None) -> dict | None:
    """The agent's last message, parsed as its report.

    The contract asks for a file. A weaker runtime often does the work and
    then *answers* instead of writing it — the cycle is done, and throwing it
    away over where the JSON landed wastes the whole run. This is the fallback
    for that, not a replacement: the file is still what is asked for and still
    read first.
    """
    if not conversation_id:
        return None
    try:
        response = workspace.client.get(
            f"/api/conversations/{conversation_id}/agent_final_response"
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        print(f"  final-response fallback failed (non-fatal): {exc}")
        return None
    text = payload.get("response") if isinstance(payload, dict) else None
    parsed = parse_json_report(text or "")
    if parsed is not None:
        print("  recovered the report from the agent's final answer")
    return parsed


#: Most observations one report may carry, matching the service's own cap.
#: Over it, the newest are kept: the oldest were already sent by an earlier
#: stage of the same cycle.
MAX_OBSERVATIONS = 20


def _now_iso() -> str:
    """This moment, as the service's schemas expect it."""
    return datetime.now(UTC).isoformat()


def conversation_events(workspace, conversation_id: str | None) -> list[dict] | None:
    """A conversation's events, oldest first, or ``None`` when unreadable.

    Read for one reason: the typed record of which runtime actually served the
    conversation. ``None`` rather than ``[]`` on failure, because an empty
    list means "nothing switched" and that is a different claim from "nobody
    could tell"; the caller reports the second as a degraded trace.

    One page: a switch happens at most a handful of times in a conversation,
    and the newest events are the ones that carry the route, so a truncated
    read loses the beginning -- which the profile's own runtime already gives
    us -- rather than the end.
    """
    if not conversation_id:
        return None
    try:
        response = workspace.client.get(
            f"/api/conversations/{conversation_id}/events/search",
            params={"limit": 100, "sort_order": "TIMESTAMP_DESC"},
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        print(f"  WARNING: could not read the conversation's events: {exc}")
        return None
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        print("  WARNING: the events response carried no items")
        return None
    return [event for event in reversed(items) if isinstance(event, dict)]


def runtime_trace(
    workspace,
    conversation_id: str | None,
    *,
    started_on: str | None,
    finished: bool,
) -> dict:
    """What actually ran this conversation, and what it tells the ledger.

    A switch mid-conversation is reported rather than left in the transcript:
    recording the work against a runtime that ran out would misstate both the
    audit trail and which subscription is being spent.
    """
    trace = trace_runtime(
        conversation_events(workspace, conversation_id),
        started_on=started_on,
        finished=finished,
        conversation_id=conversation_id,
        observed_at=_now_iso(),
    )
    if trace["degraded"]:
        print(f"  runtime trace degraded: {trace['degraded']}")
    for switch in trace["switches"]:
        print(
            f"  runtime switched {switch['from']} -> {switch['to']} "
            f"({switch['reason']})"
        )
    return trace


def classify_change(repo_path: str, base_ref: str, branch: str):
    """Which class of change this cycle made, read from its own diff.

    Not asked of the builder: the class decides who may publish, so a builder
    that called its own deletion additive would be deciding its own gate. The
    builder's claim is reported separately and compared.

    ``None`` when the diff cannot be read, which is a degraded cycle rather
    than an additive one: an unreadable diff is not evidence of a safe change.
    """
    try:
        result = subprocess.run(
            ["git", "diff", "--no-color", f"{base_ref}...{branch}"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except Exception as exc:
        print(f"  WARNING: could not read the diff to classify it: {exc}")
        return None
    if result.returncode != 0:
        print(f"  WARNING: git diff failed: {result.stderr.strip()[:200]}")
        return None
    assessment = assess_diff(result.stdout)
    print(
        f"  change class: {assessment.change_class} "
        f"(+{assessment.insertions}/-{assessment.deletions}, "
        f"{len(assessment.reasons)} reason(s))"
    )
    return assessment


#: Share of a prompt's budget reserved for corrections, ahead of everything
#: else. Identity explains who the work is for; a correction is a rule the work
#: has already been told once, so it goes first and it is the last thing cut.
LESSON_BUDGET_CHARS = 6000


class LessonsUnavailable(Exception):
    """The corrections in force could not be read.

    Fatal on purpose. A cycle that runs without the corrections it is under is
    how a correction comes back as something the user has to give again, and
    the whole point of this mechanism is that they should not have to. An
    *empty* set of corrections is fine; an unreadable one is not.
    """


def fetch_lessons(repo_label: str, remote_id: str | None, runtime: str | None) -> tuple:
    """The corrections this cycle works under, and their ids.

    The ids are reported back with the cycle, because they are the only thing
    that tells "the work ignored a correction" apart from "the work never saw
    it" when the same correction arrives again.
    """
    try:
        payload = cycle_request("GET", "/v1/lessons")
    except Exception as exc:
        raise LessonsUnavailable(str(exc)[:300]) from exc
    lessons = payload.get("lessons") if isinstance(payload, dict) else None
    if not isinstance(lessons, list):
        raise LessonsUnavailable("the lessons response carried no list")
    scoped = lessons_in_scope(
        lessons, repository=remote_id or repo_label, runtime=runtime
    )
    print(f"  corrections in force: {len(scoped)} of {len(lessons)}")
    repeated = [item for item in scoped if (item.get("repeats") or 0) > 0]
    if repeated:
        # Named in the log, because a correction that has already had to be
        # repeated is the one most likely to be repeated again.
        print(f"  {len(repeated)} of them have had to be given more than once")
    return scoped, [str(item.get("id")) for item in scoped if item.get("id")]


#: What each claim refusal means, in the words a reader can act on.
#:
#: Keyed by the service's own typed code rather than matched against its
#: prose, so rewording a message cannot silently turn a known reason back
#: into "something went wrong".
def refusal_of(response) -> tuple:
    """A refusal response as `(detail, text)`, which is all the phrasing needs.

    Split from the phrasing so the phrasing can be tested: this module runs on
    import and cannot be imported by a test, while `report.py` can.
    """
    try:
        detail = (response.json() or {}).get("detail")
    except Exception as exc:
        print(f"  could not read the refusal body: {exc}")
        detail = None
    return detail if isinstance(detail, dict) else None, response.text or ""


def gate(
    phase: str,
    route: str,
    *,
    trigger: str,
    rationale: str,
    verdict: str | None = None,
    change_class: str | None = None,
    recommends: str | None = None,
    decided_by: str | None = None,
) -> dict:
    """One routing decision, ready to report.

    Built through the shared type rather than as a dict literal, so a route
    outside the closed set or a missing rationale fails here -- in the cycle
    that made the decision -- rather than arriving at the service as an
    unreadable entry.
    """
    return GateDecision(
        phase=phase,
        route=route,
        trigger=trigger,
        rationale=rationale,
        verdict=verdict,
        change_class=change_class,
        recommends=recommends,
        decided_by=decided_by,
        at=_now_iso(),
    ).as_dict()


def removals_section(assessment) -> str:
    """The removals the judge has to account for, as the diff reported them."""
    if assessment is None:
        return "The diff could not be read, so no removal list was computed."
    lines = [
        f"- `{reason.path}`: {reason.detail}"
        for reason in assessment.reasons
        if reason.rule
        in ("removed_export", "file_deleted", "deletions_above_threshold")
    ]
    if assessment.removed_exports:
        lines.append(
            "\nNames removed: "
            + ", ".join(f"`{name}`" for name in assessment.removed_exports)
        )
    return "\n".join(lines) or "No removals were computed from the diff."


def cycle_authority(cycle_id: str) -> dict:
    """What the service says about publishing this change.

    Asked rather than decided here, because the one thing a cycle must not do
    is work out its own permission. A failure to reach the service is reported
    as an unauthorised answer: not knowing whether you may publish is not the
    same as being allowed to.
    """
    try:
        return cycle_request("GET", f"/v1/improve/cycles/{cycle_id}/authority")
    except Exception as exc:
        print(f"  WARNING: could not ask whether this may publish: {exc}")
        return {
            "authorised": False,
            "route": "return",
            "reason": f"the authority check could not be reached: {exc}",
            "missing": ["authority"],
            "human_only": False,
        }


def merge_outcome(repo_path: str, pr_url: str) -> dict:
    """Ask the forge what the merge produced.

    Not read out of `gh pr merge`'s output, which reports the local
    fast-forward of the checkout and not the commit the forge created. An
    answer it cannot give is left absent rather than guessed.
    """
    try:
        result = subprocess.run(
            ["gh", "pr", "view", pr_url, "--json", "state,mergedAt,mergeCommit"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            print(f"  merge outcome unreadable: {result.stderr.strip()[:200]}")
            return {}
        data = json.loads(result.stdout)
    except Exception as exc:
        print(f"  merge outcome unreadable: {type(exc).__name__}: {exc}")
        return {}
    if data.get("state") != "MERGED":
        return {}
    outcome = {}
    if data.get("mergedAt"):
        outcome["merged_at"] = data["mergedAt"]
    sha = (data.get("mergeCommit") or {}).get("oid")
    if sha:
        outcome["merge_commit_sha"] = sha[:64]
    return outcome


def merge_pull_request(repo_path: str, pr_url: str) -> dict:
    """Merge an authorised pull request, and say what happened either way."""
    try:
        result = subprocess.run(
            ["gh", "pr", "merge", pr_url, "--squash", "--delete-branch"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except Exception as exc:
        return {"merged": False, "detail": f"{type(exc).__name__}: {exc}"[:300]}
    if result.returncode != 0:
        return {"merged": False, "detail": result.stderr.strip()[:300]}
    merged = {"merged": True, "detail": result.stdout.strip()[:300]}
    merged.update(merge_outcome(repo_path, pr_url))
    return merged


def _no_updates_answer(error: str | None) -> str | None:
    """What to record when a reviewer skipped the supersession question.

    Not left empty: an empty field reads as "nothing to report", and the whole
    point of asking every time is that "we looked and it is current" and "we
    did not look" are different answers.
    """
    if error:
        return None
    return "This reviewer did not say whether anything newer supersedes this."


def _no_verdict_summary(error: str | None) -> str:
    """What to record when a reviewer left no verdict behind."""
    if error:
        return f"This reviewer did not finish: {error}"[:MAX_SUMMARY_CHARS]
    return (
        "This reviewer finished without writing a verdict, so nothing it "
        "found was recorded."
    )


def read_doc(path: str) -> str | None:
    """Read a context document, or None when it is absent or unreadable."""
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


def render(template: str, values: dict) -> str:
    """Substitute ``{{KEY}}`` placeholders.

    Deliberately not ``str.format``: these templates are Markdown full of
    JSON braces, and the agent must receive them exactly as written.
    """
    for key, value in values.items():
        template = template.replace("{{" + key + "}}", value)
    return template


def load_template(name: str) -> str:
    with open(os.path.join(SCRIPT_DIR, name), encoding="utf-8") as handle:
        return handle.read()


def quoted_section(title: str, body: str | None, absent_note: str) -> str:
    """A context document as a titled section, or a note that it is missing.

    Saying a document is absent is worth the tokens: it tells the agent the
    silence is a fact about the repository, not something it failed to find.
    """
    if not body:
        return f"## {title}\n\n{absent_note}\n"
    return f"## {title}\n\n{body}\n"


def list_agent_profiles(workspace) -> dict:
    """Map profile id -> {name, kind, runtime}.

    ``runtime`` is what the program compares to keep review independent: the
    ACP server for an ACP profile (``claude-code``, ``codex``, ...), or the
    LLM profile for an OpenHands one. The list endpoint does not carry the ACP
    server, so each profile is fetched by name to find it.
    """
    profiles: dict = {}
    try:
        response = workspace.client.get("/api/agent-profiles")
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        print(f"  listing agent profiles failed (non-fatal): {exc}")
        return profiles

    summaries = payload.get("profiles") if isinstance(payload, dict) else None
    if not isinstance(summaries, list):
        return profiles

    for summary in summaries:
        if not isinstance(summary, dict):
            continue
        profile_id = summary.get("id")
        name = summary.get("name")
        if not profile_id or not isinstance(name, str):
            continue
        kind = summary.get("agent_kind") or "openhands"
        runtime = summary.get("llm_profile_ref") or kind
        if kind == "acp":
            try:
                detail = workspace.client.get(f"/api/agent-profiles/{name}")
                detail.raise_for_status()
                runtime = acp_server_from_detail(detail.json()) or "acp"
            except Exception as exc:
                print(f"  profile {name!r} detail fetch failed (non-fatal): {exc}")
                runtime = "acp"
            if runtime == "acp":
                print(f"  WARNING: could not resolve {name!r}'s ACP server")
        profiles[str(profile_id)] = {
            "name": name,
            "kind": kind,
            "runtime": str(runtime),
        }
    return profiles


class ConversationFailed(Exception):
    """A conversation was created but did not finish.

    Carries the conversation id, because the most useful thing about a failed
    review is being able to open it and see what the runtime actually said.
    """

    def __init__(self, conversation_id: str | None, cause: Exception) -> None:
        super().__init__(str(cause))
        self.conversation_id = conversation_id
        self.cause = cause


def run_conversation(
    workspace,
    profile_id: str,
    prompt: str,
    title: str,
) -> str | None:
    """Run one agent-profile conversation to completion. Returns its id.

    A failure after the conversation exists raises ``ConversationFailed``
    carrying that id, so a caller can record where it went wrong rather than
    only that it did.
    """
    request_kwargs = {
        "workspace": LocalWorkspace(working_dir=workspace.working_dir),
        "agent_profile_id": uuid.UUID(profile_id),
        "delete_on_close": False,
        **automation_conversation_kwargs(),
    }
    if automation_user_id:
        request_kwargs["user_id"] = automation_user_id

    conversation = RemoteConversation.create(
        workspace, StartConversationRequest(**request_kwargs)
    )
    assert isinstance(conversation, RemoteConversation)
    print(f"  conversation: {conversation.id}")

    try:
        response = workspace.client.patch(
            f"/api/conversations/{conversation.id}",
            json={"title": title[:200]},
        )
        response.raise_for_status()
    except Exception as exc:
        print(f"  set title failed (non-fatal): {exc}")

    secrets = {}
    try:
        secrets = workspace.get_secrets(agent_profile_id=profile_id)
    except Exception as exc:
        print(f"  get_secrets() failed (ok if none configured): {exc}")
    if secrets:
        # The count only: a profile's secret names are its own business.
        conversation.update_secrets(secrets)
        print(f"  injected {len(secrets)} secret(s)")

    try:
        conversation.send_message(prompt)
        conversation.run()
    except Exception as exc:
        raise ConversationFailed(str(conversation.id), exc) from exc
    finally:
        try:
            conversation.close()
        except Exception as exc:
            print(f"  conversation.close() failed (non-fatal): {exc}")
    return str(conversation.id)


workspace_base = os.path.expanduser(os.environ.get("WORKSPACE_BASE", "/workspace"))
if not os.path.isabs(workspace_base):
    print(
        f"ERROR: WORKSPACE_BASE must be absolute path, got: {workspace_base}",
        file=sys.stderr,
    )
    sys.exit(1)
if IS_LOCAL_MODE and not os.path.isdir(workspace_base):
    print(
        f"ERROR: WORKSPACE_BASE directory does not exist: {workspace_base}",
        file=sys.stderr,
    )
    sys.exit(1)

print("\n=== SDK WORKSPACE ===")
if IS_LOCAL_MODE:
    print(f"  using RemoteWorkspace at {agent_server_url}")
    workspace_ctx = RemoteWorkspace(
        host=agent_server_url,
        api_key=session_key if session_key else None,
        working_dir=workspace_base,
    )
else:
    print(f"  using OpenHandsCloudWorkspace at {api_url}")
    workspace_ctx = OpenHandsCloudWorkspace(
        local_agent_server_mode=True,
        cloud_api_url=api_url,
        cloud_api_key=api_key,
        keep_alive=True,
    )


def write_handoff(
    workspace,
    repo_path: str,
    *,
    branch: str,
    base_ref: str,
    builder_id,
    cycle_dir: str,
    repo_label: str,
    reason: str = "runtime_exhausted",
    ask_runtime: bool = True,
) -> dict | None:
    """Spend what is left of a runtime recording where it got to.

    The runtime that wrote it is not passed in: the service stamps the handoff
    with the cycle's own `builder_runtime`, which the same report updates to
    whichever one actually ran, so a second source here could only disagree.

    Asks the runtime that just refused, because it is the only thing that
    knows what it was doing — and if it has nothing left to answer with, this
    script writes a degraded record from git on its own. A branch with work on
    it that nobody knows about is the outcome worth avoiding, and a record
    built from `git rev-parse` costs no tokens at all.

    `ask_runtime` is off for a builder that ran out of *time* rather than out
    of usage. That one is still working as far as the server is concerned, so
    asking it to describe itself waits out another whole timeout before
    answering nothing. Git alone is the better trade there.
    """
    # The code before the description, which is the order the plan asks for
    # and the order this did not follow: the builder prompt commits and
    # pushes in one step at the end, so a builder interrupted mid-change had
    # committed nothing, and this function used to *observe* that the branch
    # was unpushed rather than push it. The sandbox is still up here; it is
    # the last moment anything can be saved.
    saved = save_work_in_progress(repo_path, branch, base_ref=base_ref, reason=reason)
    if ask_runtime:
        try:
            prompt = render(load_template("handoff_prompt.md"), {"BRANCH": branch})
            run_conversation(workspace, builder_id, prompt, f"Hand off {repo_label}")
        except Exception as exc:  # noqa: BLE001 - nothing here may fail the cycle
            print(f"  the runtime could not write its own handoff: {exc}")

    written = read_json_report(os.path.join(cycle_dir, "handoff.json")) or {}
    shas = git_commit_range(repo_path, base_ref, branch)
    # Asked of the remote rather than taken from the push's exit code: a push
    # that reported success and left nothing there would be the same silent
    # loss in a new place.
    pushed = branch_is_pushed(repo_path, branch)
    if saved.get("note") and not pushed:
        print(f"  the work was not saved: {saved['note']}")
    if written.get("next_steps"):
        written.setdefault("branch", branch)
        written["branch_pushed"] = pushed
        written.setdefault("head_sha", shas.get("head_sha"))
        written.setdefault("status", "incomplete")
        written.setdefault("reason", reason)
        return written

    # Nothing readable. A record built from git alone still tells the next
    # cycle where to look, which is the whole point.
    return {
        "status": "incomplete",
        "reason": reason,
        "branch": branch,
        "branch_pushed": pushed,
        "head_sha": shas.get("head_sha"),
        "stage": "unknown",
        "goal": "",
        "done": [],
        "next_steps": [
            "Read the branch diff and continue from it. "
            + (
                "The previous runtime ran out of time before it could "
                "describe where it had got to, so the diff is the only "
                "account of it."
                if reason == "timed_out"
                else "The previous runtime ran out before it could describe "
                "where it had got to."
            )
        ],
        "do_not": [],
        "files_touched": [],
    }


def execute_cycle(workspace) -> None:
    """Run one cycle to completion.

    A function rather than inline code so that "nothing to do" can simply
    return: raising ``SystemExit`` inside the workspace context would reach
    the context manager's ``__exit__`` as an exception and report a healthy
    no-op run as a failure.
    """
    # --- Claim the next repository in the rotation --------------------------
    report_phase("Claiming the next repository")
    print("\n=== CLAIM CYCLE ===")
    try:
        claim = cycle_request(
            "POST",
            f"/v1/improve/{automation_id}/claim",
            {"run_id": automation_run_id} if automation_run_id else {},
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 409:
            # Nothing to claim. The run has nothing to do either way, but
            # *why* is the whole difference between "add a repository",
            # "wait until midnight" and "every runtime is spent" — and this
            # phase is the only place a reader sees it. The service answers
            # with a typed code precisely so this does not have to guess.
            print(f"  nothing to do: {exc.response.text}")
            report_phase(claim_refusal_phase(*refusal_of(exc.response)))
            return
        raise

    cycle = claim["cycle"]
    config = claim["config"]
    rotation = claim.get("rotation") or []
    cycle_id = cycle["id"]
    repository = cycle["repository"]
    repo_label = repository["label"]
    repo_url = repository["url"]
    repo_ref = repository.get("ref")
    print(
        f"  cycle {cycle_id}: {repo_label} "
        f"({repository['index'] + 1} of {len(rotation)})"
    )

    def report_cycle(payload: dict) -> str | None:
        """Record cycle progress. Never fatal: the work matters more.

        Returns the rejection, or None when the service took it, so a caller
        whose payload *is* the record can do something about being refused.
        """
        try:
            cycle_request("PATCH", f"/v1/improve/cycles/{cycle_id}", payload)
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            print(f"  cycle update failed (non-fatal): {detail}")
            return detail
        return None

    try:
        # --- Clone the repository ------------------------------------------
        report_phase(f"Cloning {repo_label}")
        print("\n=== CLONE ===")
        repo_spec = {"url": repo_url}
        if repo_ref:
            repo_spec["ref"] = repo_ref
        clone_result = workspace.clone_repos([repo_spec])
        if not clone_result.repo_mappings:
            raise RuntimeError(
                f"could not clone {repo_label} from {repo_url}: "
                f"{', '.join(clone_result.failed_repos) or 'no mapping returned'}"
            )
        mapping = next(iter(clone_result.repo_mappings.values()))
        repo_path = mapping.local_path
        # The directory name, not the path. The clone happens here, but the
        # agent runs somewhere else — a Docker sandbox mounts this workspace
        # at its own path, so naming the host path sends it looking for a
        # directory that does not exist and it has to guess. A name relative
        # to its own working directory is true in both places.
        repo_dir = os.path.basename(repo_path.rstrip("/")) or repo_path
        print(f"  cloned to {repo_path} (agents are told: ./{repo_dir})")

        # --- Assemble the context the cycle reasons from --------------------
        print("\n=== CONTEXT ===")
        identity_documents, identity_note = fetch_identity_set(workspace)
        project_documents = read_project_identity(repo_path)
        print(f"  user identity: {sorted(identity_documents) or 'none'}")
        print(f"  project identity: {sorted(project_documents) or 'none'}")
        if identity_note:
            print(f"  WARNING: {identity_note}")

        soul_section = identity_sections(identity_documents, identity_note)
        spent = identity_cost(identity_documents)
        if spent["trimmed"] or spent["omitted"]:
            # Said out loud, because the person whose document was cut is not
            # reading the prompt. The record carries it too; this is for
            # whoever is watching the run.
            print(
                f"  WARNING: the identity budget was not enough — trimmed "
                f"{spent['trimmed'] or 'nothing'}, omitted "
                f"{spent['omitted'] or 'nothing'}"
            )
        identity_section = project_identity_sections(project_documents, repo_label)
        focus = clamp(config.get("focus"), 4000)
        focus_section = (
            f"## Standing guidance for this program\n\n{focus}\n" if focus else ""
        )

        cycle_dir = os.path.join(workspace.working_dir, CYCLE_DIR_NAME)
        os.makedirs(cycle_dir, exist_ok=True)
        report_path = os.path.join(cycle_dir, "report.json")
        # Told to the agent relative to its working directory, for the same
        # reason the repository is: the absolute path is this process's, and
        # a sandboxed agent cannot see it. The script still reads the real one.
        report_path_told = f"{CYCLE_DIR_NAME}/report.json"

        branch_prefix = config.get("branch_prefix") or "improve/"
        branch = f"{branch_prefix}{time.strftime('%Y%m%d-%H%M%S')}"
        # Continuing someone else's work means continuing it on their branch.
        # A fresh branch would leave their commits behind and produce a second
        # change that does the same thing.
        resume = claim.get("resume") if isinstance(claim, dict) else None
        if isinstance(resume, dict) and resume.get("branch_pushed"):
            resumed_branch = resume.get("branch")
            if isinstance(resumed_branch, str) and resumed_branch:
                branch = resumed_branch
                print(f"  continuing {branch} from cycle {resume.get('from_cycle_id')}")
        base_ref = f"origin/{repo_ref}" if repo_ref else "origin/HEAD"

        # What this run saw about each runtime's remaining usage. The
        # runtimes expose no balance, so this list is the only way the
        # scheduler ever learns which subscriptions are spent.
        observations: list[dict] = []

        # --- Resolve the runtimes taking part ------------------------------
        print("\n=== RUNTIMES ===")
        profiles = list_agent_profiles(workspace)
        # The *claim* decides whose turn it is to build, because the rotation
        # lives in the history. Reading the configuration here instead meant
        # that with `rotate_builder` set, the cycle recorded one builder and
        # ran another — and the reviewers were then chosen to exclude the
        # wrong runtime, so a cycle reviewed its own work. The config value is
        # only the fallback for a server too old to send one.
        builder_id = str(
            cycle.get("builder_profile_id") or config["builder_profile_id"]
        )
        builder = profiles.get(builder_id) or {}
        builder_runtime = builder.get("runtime")

        # Read once the runtime is known, because a correction can be about
        # one runtime's quirks, and fatal if unreadable: a cycle that runs
        # without the corrections it is under is how a correction comes back
        # as something the user has to give again. An *empty* set is fine.
        #
        # Prepended to the identity block and stated as rules rather than as
        # history: a correction rendered as "the user once said" invites a
        # judgement about whether it still applies, which is exactly the
        # judgement that produces a repeat.
        lessons, lesson_ids = fetch_lessons(
            repo_label, cycle.get("remote_id"), builder_runtime
        )
        soul_section = lessons_section(lessons, LESSON_BUDGET_CHARS) + soul_section

        reviewers = choose_reviewers(config, profiles, builder_id)
        print(f"  builder: {builder.get('name') or builder_id} ({builder_runtime})")
        if reviewers:
            print(
                "  reviewers: "
                + ", ".join(
                    f"{reviewer['name'] or reviewer['id']} ({reviewer['runtime']})"
                    for reviewer in reviewers
                )
            )
        else:
            print("  reviewers: none — only one runtime is available")

        # --- Builder stage --------------------------------------------------
        report_cycle(
            {
                "status": "BUILDING",
                "branch": branch,
                "builder_runtime": builder_runtime,
            }
        )
        report_phase(f"Researching an improvement for {repo_label}")
        print("\n=== BUILD ===")
        builder_prompt = render(
            load_template("builder_prompt.md"),
            {
                "REPO_LABEL": repo_label,
                "REPO_PATH": repo_dir,
                "SOUL_SECTION": soul_section,
                "IDENTITY_SECTION": identity_section,
                "FOCUS_SECTION": focus_section,
                "RESUME_SECTION": resume_section(resume),
                "MAX_CHANGED_FILES": str(config.get("max_changed_files") or 20),
                "BRANCH": branch,
                "BRANCH_PREFIX": branch_prefix,
                "REPORT_PATH": report_path_told,
                "PR_INSTRUCTION": (
                    "- Open a pull request against the default branch. Its body "
                    "must list the sources you relied on and the checks you ran."
                    if config.get("open_pr", True)
                    else "- Do not open a pull request; report the pushed branch "
                    "instead."
                ),
            },
        )
        try:
            builder_conversation_id = run_conversation(
                workspace,
                builder_id,
                builder_prompt,
                f"Improve {repo_label}",
            )
        except ConversationFailed as failure:
            # Two failures worth handling differently, for the same reason: a
            # builder that stopped mid-change has done real work on a branch,
            # and throwing it away means the next cycle pays to rediscover it.
            # So what is left of this runtime — or, if nothing is, this script
            # on its own — writes down where it got to.
            exhausted = looks_exhausted(str(failure.cause))
            out_of_time = not exhausted and ran_out_of_time(failure.cause)
            if not exhausted and not out_of_time:
                raise
            if out_of_time:
                print("  the builder ran out of time; handing off from git")
            else:
                print("  the builder's runtime is out of usage; handing off")
            trace = runtime_trace(
                workspace,
                failure.conversation_id,
                started_on=builder_runtime,
                finished=False,
            )
            # The conversation's own events say which runtime refused; the
            # exception string is the fallback for a server too old to emit
            # the typed events, and for a failure that never reached one.
            observations.extend(trace["observations"])
            if not trace["observations"] and exhausted:
                # Only for a usage limit. A builder that ran out of time says
                # nothing about that runtime's remaining quota, and recording
                # it as spent would park a runtime that is perfectly healthy.
                observations.append(
                    {
                        "runtime": trace["ran"] or builder_runtime,
                        "outcome": "exhausted",
                        "observed_at": _now_iso(),
                        "evidence": str(failure.cause)[:500],
                        "conversation_id": failure.conversation_id,
                    }
                )
            # Whatever ran last is what ran out, and what the handoff is
            # written by. Recording the profile's runtime instead would park
            # a runtime that is still healthy.
            builder_runtime = trace["ran"] or builder_runtime
            handoff = write_handoff(
                workspace,
                repo_path,
                branch=branch,
                base_ref=base_ref,
                builder_id=builder_id,
                cycle_dir=cycle_dir,
                repo_label=repo_label,
                reason="timed_out" if out_of_time else "runtime_exhausted",
                # A builder that ran out of time is, as far as the server is
                # concerned, still working: asking it to describe itself
                # waits out another whole timeout before answering nothing.
                ask_runtime=not out_of_time,
            )
            report_cycle(
                {
                    "status": "FAILED",
                    "error": (
                        f"{builder_runtime} ran out of time mid-change. Its "
                        "work is handed to the next cycle."
                        if out_of_time
                        else f"{builder_runtime} ran out of usage mid-change. "
                        "Its work is handed to the next cycle."
                    ),
                    "handoff": handoff,
                    "builder_runtime": builder_runtime,
                    "runtime_switches": trace["switches"],
                    "runtime_observations": observations,
                }
            )
            report_phase(
                f"{repo_label}: builder out of time, work handed over"
                if out_of_time
                else f"{repo_label}: runtime out of usage, work handed over"
            )
            return

        # Read before the report, because a cycle that switched runtime
        # mid-change must not be filed under the one that ran out: the
        # reviewers are chosen to exclude the builder's runtime, so a stale
        # value is how a cycle ends up reviewing its own work.
        trace = runtime_trace(
            workspace,
            builder_conversation_id,
            started_on=builder_runtime,
            finished=True,
        )
        builder_runtime = trace["ran"] or builder_runtime
        observations.extend(trace["observations"])

        report = read_json_report(report_path) or {}
        if not report:
            report = (
                report_from_final_response(workspace, builder_conversation_id) or {}
            )
        if not report:
            print("  WARNING: the builder wrote no readable report")
        builder_status = str(report.get("status") or "").strip().lower()
        builder_summary = clamp(report.get("summary")) or (
            "The builder produced no readable report for this cycle."
        )
        sources = normalize_sources(report.get("sources"))
        risks = string_list(report.get("risks"))
        soul_suggestions = normalize_soul_suggestions(report.get("soul_suggestions"))
        pr_url = clamp(report.get("pr_url"), 2000)
        if pr_url and not pr_url.startswith(("http://", "https://")):
            pr_url = None
        reported_branch = clamp(report.get("branch"), 255) or branch

        # The evidence, not just the account of it. Every one of these is
        # asked for in the builder's prompt and was, until now, read by
        # nothing — so a merged change could not be traced back to the
        # commit, the checks, or the prompt that produced it.
        builder_update = {
            "builder_conversation_id": builder_conversation_id,
            "builder_runtime": builder_runtime,
            "summary": builder_summary,
            "branch": reported_branch,
            "sources": sources,
            "changed_files": string_list(report.get("changed_files"))[:200],
            "checks": normalize_checks(report.get("checks")),
            "followups": string_list(report.get("followups"))[:40],
            "risks": risks[:40],
            "prompt_digest": prompt_digest(builder_prompt),
            "verdict_rule_version": VERDICT_RULE_VERSION,
            "runtime_switches": trace["switches"],
            "runtime_observations": observations[-MAX_OBSERVATIONS:],
            # What this cycle actually had in front of it. Reported rather
            # than inferred: when the same correction arrives again, this is
            # the only thing that tells "ignored it" from "never saw it".
            "injected_lesson_ids": lesson_ids,
            # What the builder said it did, kept beside what the diff says it
            # did. The service raises the class to whatever the evidence
            # requires, so an understated claim cannot lower the gate.
            "claimed_change_class": clamp(report.get("change_class"), 32),
        }
        shas = git_commit_range(repo_path, base_ref, reported_branch)
        builder_update.update(shas)
        assessment = classify_change(repo_path, base_ref, reported_branch)
        if assessment is not None:
            builder_update["change_class"] = assessment.change_class
            builder_update["change_evidence"] = assessment.as_dict()
            mismatch = claim_mismatch(report.get("change_class"), assessment)
            if mismatch:
                # A claim that understates the diff is a finding in its own
                # right, recorded where the reviewers will read it.
                print(f"  WARNING: {mismatch}")
                risks = [mismatch, *risks]
                builder_update["risks"] = risks[:40]
        else:
            risks = [
                "The cycle's own diff could not be read, so its change class "
                "is unknown rather than additive.",
                *risks,
            ]
            builder_update["risks"] = risks[:40]
        if soul_suggestions:
            builder_update["soul_suggestions"] = soul_suggestions
        if pr_url:
            builder_update["pr_url"] = pr_url
        builder_update["identity_cost"] = spent
        report_cycle(builder_update)
        print(f"  status: {builder_status or 'unreported'}")
        print(f"  sources cited: {len(sources)}")

        # A blocked cycle is a failed one. It may have done real work — the
        # first live run implemented its change, tested it and then could not
        # push — but it produced nothing anyone can review, and recording that
        # as NO_CHANGE would read as "we looked and decided against it". The
        # whole point of these verdicts is to keep those two apart.
        if builder_status == "blocked":
            report_cycle({"status": "FAILED", "error": builder_summary})
            report_phase(f"{repo_label}: blocked")
            print("  blocked; recorded as a failed cycle")
            return

        # Nothing to review, and that is a real outcome: researching an idea
        # and deciding against it is a successful cycle.
        if builder_status == "no_change" or not reported_branch:
            report_cycle({"status": "COMPLETED", "verdict": "NO_CHANGE"})
            report_phase(f"{repo_label}: no change this cycle")
            return

        # --- Review stage ---------------------------------------------------
        # With no second runtime there is nothing to review, but the change
        # still has to go through the publish gate: the gate reads the change
        # class and the evidence, neither of which depends on a reviewer, and
        # a gate some paths walk around is a gate with a hole in it.
        report_cycle({"status": "REVIEWING"})
        reviews = []
        if not reviewers:
            print("\n  only one runtime available; review skipped")
        else:
            print("\n=== REVIEW ===")
        reviewer_template = load_template("reviewer_prompt.md")
        sources_rendered = (
            "\n".join(
                f"- {source['url']}"
                + (f" — {source['note']}" if source.get("note") else "")
                for source in sources
            )
            or "The builder cited no sources, which is itself a finding."
        )
        risks_rendered = (
            "\n".join(f"- {risk}" for risk in risks)
            or "The builder admitted no risks, which is itself a finding."
        )

        for position, reviewer in enumerate(reviewers, start=1):
            review_path = os.path.join(cycle_dir, f"review-{position}.json")
            review_path_told = f"{CYCLE_DIR_NAME}/review-{position}.json"
            label = reviewer["name"] or reviewer["id"]
            report_phase(f"{label} is reviewing the change to {repo_label}")
            print(f"\n  reviewer {position}: {label} ({reviewer['runtime']})")
            reviewer_prompt = render(
                reviewer_template,
                {
                    "REPO_LABEL": repo_label,
                    "REPO_PATH": repo_dir,
                    "BRANCH": reported_branch,
                    "BASE_REF": base_ref,
                    "SOUL_SECTION": soul_section,
                    "IDENTITY_SECTION": identity_section,
                    "BUILDER_SUMMARY": builder_summary,
                    "BUILDER_SOURCES": sources_rendered,
                    "BUILDER_RISKS": risks_rendered,
                    "REVIEW_PATH": review_path_told,
                },
            )
            review_conversation_id = None
            review_error = None
            reviewer_refused = False
            try:
                review_conversation_id = run_conversation(
                    workspace,
                    reviewer["id"],
                    reviewer_prompt,
                    f"Review {repo_label}",
                )
            except Exception as exc:
                # One reviewer failing must not discard the others or the
                # builder's work; it is recorded as an inconclusive review —
                # but with the reason, and with the conversation when there
                # was one. A silent "no readable verdict" sends the user
                # looking for a bad model when the real answer is that its
                # server was not running.
                review_conversation_id = getattr(exc, "conversation_id", None)
                review_error = f"{type(exc).__name__}: {exc}"
                print(f"  reviewer {label} failed (non-fatal): {review_error}")
                # A reviewer that ran out of usage is scheduling news, not
                # just a missing verdict: the ledger has to learn it or the
                # next cycle picks the same spent runtime.
                reviewer_refused = looks_exhausted(review_error)

            review_trace = runtime_trace(
                workspace,
                review_conversation_id,
                started_on=reviewer["runtime"],
                finished=review_error is None,
            )
            reviewer_ran = review_trace["ran"] or reviewer["runtime"]
            if review_trace["observations"]:
                observations.extend(review_trace["observations"])
            elif reviewer_refused:
                # A reviewer that ran out of usage is scheduling news, not
                # just a missing verdict: the ledger has to learn it, or the
                # next cycle picks the same spent runtime. The exception
                # string is the fallback for a server too old to emit the
                # typed events.
                observations.append(
                    {
                        "runtime": reviewer_ran,
                        "outcome": "exhausted",
                        "observed_at": _now_iso(),
                        "evidence": (review_error or "")[:500],
                        "conversation_id": review_conversation_id,
                    }
                )

            review_report = read_json_report(review_path) or {}
            if not review_report and review_error is None:
                review_report = (
                    report_from_final_response(workspace, review_conversation_id) or {}
                )
            verdict = normalize_verdict(review_report.get("verdict"))
            reviews.append(
                {
                    "profile_id": reviewer["id"],
                    "profile_name": reviewer["name"],
                    "runtime": reviewer_ran,
                    "runtime_switches": review_trace["switches"],
                    "conversation_id": review_conversation_id,
                    "verdict": verdict,
                    # Recorded so the aggregate can tell "judged it and was
                    # unsure" from "never got to look".
                    "failed": review_error is not None,
                    "summary": clamp(review_report.get("summary"))
                    or _no_verdict_summary(review_error),
                    "concerns": string_list(review_report.get("concerns")),
                    "stale_sources": string_list(review_report.get("stale_sources"))
                    + string_list(review_report.get("newer_available")),
                    # Evidence confirmed, not only evidence disputed. An
                    # account with one half is a lopsided account of a review.
                    "verified_sources": string_list(
                        review_report.get("verified_sources")
                    ),
                    "checks": normalize_checks(review_report.get("checks")),
                    "updates_checked": clamp(review_report.get("updates_checked"))
                    or _no_updates_answer(review_error),
                }
            )
            soul_suggestions.extend(
                normalize_soul_suggestions(review_report.get("soul_suggestions"))
            )
            print(f"  verdict: {verdict}")

        verdict = cycle_verdict(reviews)
        computed_class = assessment.change_class if assessment is not None else None
        decisions = [
            gate(
                "review",
                route_for(verdict, computed_class)[0],
                trigger=f"{len(reviews)} review(s) on another runtime",
                rationale=(
                    "; ".join(
                        clamp(review.get("summary"), 200) or "no summary"
                        for review in reviews[:3]
                    )
                    or "the reviewers recorded no summary"
                ),
                verdict=verdict,
                change_class=computed_class,
            )
        ]

        # --- Judge stage ----------------------------------------------------
        # Only for a deletion, and only with a judge configured. A removal
        # published on its author's word is the thing this whole stage exists
        # to prevent, so no judge means the change waits for a person rather
        # than going ahead.
        judge_id = config.get("judge_profile_id")
        if computed_class == "deleting" and judge_id:
            judge = profiles.get(str(judge_id)) or {}
            judge_runtime = judge.get("runtime")
            if judge_runtime and judge_runtime == builder_runtime:
                # The service refuses this too. Caught here so the cycle does
                # not pay for a conversation whose answer cannot be accepted.
                decisions.append(
                    gate(
                        "judge",
                        "return",
                        trigger="the configured judge runs the builder's runtime",
                        rationale=(
                            f"{judge_runtime} built this change, so it cannot "
                            "attest its own removals"
                        ),
                        change_class=computed_class,
                    )
                )
            else:
                report_phase(f"The judge is examining what {repo_label} removed")
                print(f"\n=== JUDGE === {judge.get('name') or judge_id}")
                judge_path = os.path.join(cycle_dir, "judge.json")
                judge_prompt = render(
                    load_template("judge_prompt.md"),
                    {
                        "REPO_LABEL": repo_label,
                        "REPO_PATH": repo_dir,
                        "BRANCH": reported_branch,
                        "BASE_REF": base_ref,
                        "SOUL_SECTION": soul_section,
                        "IDENTITY_SECTION": identity_section,
                        "BUILDER_SUMMARY": builder_summary,
                        "REMOVALS_SECTION": removals_section(assessment),
                        "REPORT_PATH": f"{CYCLE_DIR_NAME}/judge.json",
                    },
                )
                judge_conversation_id = None
                judge_error = None
                try:
                    judge_conversation_id = run_conversation(
                        workspace,
                        str(judge_id),
                        judge_prompt,
                        f"Judge removals in {repo_label}",
                    )
                except Exception as exc:
                    judge_conversation_id = getattr(exc, "conversation_id", None)
                    judge_error = f"{type(exc).__name__}: {exc}"
                    print(f"  the judge failed (non-fatal): {judge_error}")

                judge_trace = runtime_trace(
                    workspace,
                    judge_conversation_id,
                    started_on=judge_runtime,
                    finished=judge_error is None,
                )
                observations.extend(judge_trace["observations"])
                judge_ran = judge_trace["ran"] or judge_runtime

                verdict_report = read_json_report(judge_path) or {}
                if not verdict_report and judge_error is None:
                    verdict_report = (
                        report_from_final_response(workspace, judge_conversation_id)
                        or {}
                    )
                if not verdict_report:
                    # Silence is not an attestation. Recorded as a refusal
                    # with the reason, so the cycle reads as "nobody signed"
                    # rather than as "the judge said no".
                    decisions.append(
                        gate(
                            "judge",
                            "return",
                            trigger="the judge produced no readable answer",
                            rationale=(
                                judge_error
                                or "no report was written and the final answer "
                                "could not be parsed"
                            ),
                            change_class=computed_class,
                            decided_by=f"runtime:{judge_ran}",
                        )
                    )
                else:
                    attestation = {
                        "attested": bool(verdict_report.get("attested")),
                        "runtime": judge_ran or "unknown",
                        "summary": clamp(verdict_report.get("summary"))
                        or "The judge recorded no reasoning.",
                        "removals": normalize_removals(verdict_report.get("removals")),
                        "profile_id": str(judge_id),
                        "conversation_id": judge_conversation_id,
                    }
                    if attestation["attested"] and not attestation["removals"]:
                        # The service refuses this; downgrading here keeps the
                        # judge's own words instead of losing the whole answer
                        # to a 422.
                        attestation["attested"] = False
                        attestation["summary"] = (
                            "Attested without naming any removal, which is "
                            "attesting nothing. Original answer: "
                            + attestation["summary"]
                        )
                    try:
                        cycle_request(
                            "POST",
                            f"/v1/improve/cycles/{cycle_id}/attest",
                            attestation,
                        )
                        print(
                            "  judge: "
                            + ("attested" if attestation["attested"] else "refused")
                        )
                    except Exception as exc:
                        print(f"  WARNING: the attestation was refused: {exc}")
                        decisions.append(
                            gate(
                                "judge",
                                "return",
                                trigger="the service refused the attestation",
                                rationale=str(exc)[:500],
                                change_class=computed_class,
                                decided_by=f"runtime:{judge_ran}",
                            )
                        )
        elif computed_class == "deleting":
            decisions.append(
                gate(
                    "judge",
                    "return",
                    trigger="this change removes code and no judge is configured",
                    rationale=(
                        "A removal published on its author's word is not "
                        "attested. Configure a judge profile, or answer this "
                        "one yourself."
                    ),
                    change_class=computed_class,
                )
            )

        # --- Publish gate ---------------------------------------------------
        # The reviews go first. The service decides publishing authority from
        # them, and asking before reporting them means asking a service that
        # has not been told what the reviewers concluded -- which is how an
        # additive change with changes requested against it got merged.
        report_cycle({"verdict": verdict, "reviews": reviews})
        authority = cycle_authority(cycle_id)
        decisions.append(
            gate(
                "publish",
                authority.get("route") or "return",
                trigger="asked the service whether this may publish",
                rationale=clamp(authority.get("reason"), 1000)
                or "the service gave no reason",
                verdict=verdict,
                change_class=computed_class,
                recommends=("merge" if authority.get("authorised") else "wait"),
                decided_by="service",
            )
        )

        if authority.get("human_only"):
            # A breaking change stops here with its plan, and the work stays
            # on the branch. Parking rather than failing is what makes the
            # answer resumable instead of something to redo.
            plan = changeover_plan(
                report,
                assessment.as_dict()["reasons"] if assessment is not None else None,
                reported_branch,
            )
            report_cycle(
                {
                    "status": "PARKED",
                    "verdict": verdict,
                    "reviews": reviews,
                    "parked_phase": "publish",
                    "parked_question": clamp(
                        f"{builder_summary}\n\nThis is a breaking change. "
                        "Publish it now, or wait?",
                        4000,
                    ),
                    "parked_plan": plan,
                    "gate_decisions": decisions,
                    "runtime_observations": observations[-MAX_OBSERVATIONS:],
                }
            )
            report_phase(f"{repo_label}: waiting on you — a breaking change")
            print("\n  parked: a breaking change is never published from here")
            return

        merged = None
        if authority.get("authorised") and config.get("merge_when_authorised", True):
            if pr_url:
                report_phase(f"Merging the change to {repo_label}")
                merged = merge_pull_request(repo_path, pr_url)
                print(f"  merge: {'ok' if merged['merged'] else merged['detail']}")
                decisions.append(
                    gate(
                        "publish",
                        "approve" if merged["merged"] else "return",
                        trigger="merged the authorised pull request",
                        rationale=merged["detail"] or "no output",
                        change_class=computed_class,
                        decided_by="cycle",
                    )
                )
            else:
                decisions.append(
                    gate(
                        "publish",
                        "approve",
                        trigger="authorised, with no pull request to merge",
                        rationale=(
                            "The branch is pushed and authorised; this program "
                            "does not open pull requests."
                        ),
                        change_class=computed_class,
                        decided_by="cycle",
                    )
                )

        final_update = {
            "status": "COMPLETED",
            "verdict": verdict,
            "reviews": reviews,
            "gate_decisions": decisions,
            # Newest last and bounded, because the service caps the list: a
            # cycle with many reviewers must not lose the reviewer that just
            # ran out in order to re-send what the builder stage already
            # recorded.
            "runtime_observations": observations[-MAX_OBSERVATIONS:],
        }
        if soul_suggestions:
            # Deduplicated across stages: both runtimes noticing the same
            # thing is one proposal for the user, not two.
            final_update["soul_suggestions"] = normalize_soul_suggestions(
                soul_suggestions
            )
        if merged and merged.get("merged"):
            # The gate log says the publish gate approved, which is a
            # decision. These two say the change is on the default branch and
            # name the commit it is, so counting merged cycles or following
            # one to its commit does not mean reading git's output back out of
            # a rationale sentence.
            for field in ("merged_at", "merge_commit_sha"):
                if merged.get(field):
                    final_update[field] = merged[field]
        rejection = report_cycle(final_update)
        if rejection:
            # This report is the record, not progress. Left unrecorded the
            # cycle stays mid-flight until the janitor closes it as an
            # abandonment — which says the work failed when it was built,
            # reviewed and published. Land the terminal state with the
            # rejection named, carrying only the fields that cannot be its
            # cause.
            if report_cycle(
                {
                    "status": "COMPLETED",
                    "verdict": verdict,
                    "error": clamp(
                        "The cycle finished, but the service rejected its "
                        f"full report, so the detail is lost: {rejection}",
                        MAX_SUMMARY_CHARS,
                    ),
                }
            ):
                print(
                    "  WARNING: this cycle cannot be recorded at all; the "
                    "janitor will close it as abandoned"
                )
        report_phase(
            f"{repo_label}: cycle complete"
            + (" and merged" if merged and merged["merged"] else "")
        )
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        report_cycle({"status": "FAILED", "error": detail[:MAX_SUMMARY_CHARS]})
        raise


with workspace_ctx as workspace:
    execute_cycle(workspace)

print("\n=== RESULT ===")
_lost = lost_phase_summary(_LOST_PHASES)
if _lost:
    print(_lost)
print("ALL_OK")
