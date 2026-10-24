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

import os
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


def report_phase(message: str) -> None:
    """POST a short progress phase to the automation service. Never raises."""
    if not AUTOMATION_PHASE_URL or not _CYCLE_TOKEN or not message:
        return
    try:
        import httpx

        httpx.post(
            AUTOMATION_PHASE_URL,
            json={"phase": message[:200]},
            headers={"Authorization": f"Bearer {_CYCLE_TOKEN}"},
            timeout=5.0,
        )
    except Exception:
        pass  # phases are cosmetic; the run must never fail because of them


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
from context import (  # noqa: E402
    fetch_identity_set,
    git_commit_range,
    identity_sections,
    normalize_checks,
    project_identity_sections,
    prompt_digest,
    read_project_identity,
)
from report import (  # noqa: E402
    VERDICT_RULE_VERSION,
    MAX_SUMMARY_CHARS,
    parse_json_report,
    acp_server_from_detail,
    cycle_verdict,
    choose_reviewers,
    clamp,
    normalize_sources,
    normalize_soul_suggestions,
    normalize_verdict,
    read_json_report,
    string_list,
    worst_verdict,
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
            # Nothing to rotate over, or this automation is not a program.
            # Either way the run has nothing to do and saying so plainly beats
            # failing with a stack trace.
            print(f"  nothing to do: {exc.response.text}")
            report_phase("No repositories configured")
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

    def report_cycle(payload: dict) -> None:
        """Record cycle progress. Never fatal: the work matters more."""
        try:
            cycle_request("PATCH", f"/v1/improve/cycles/{cycle_id}", payload)
        except Exception as exc:
            print(f"  cycle update failed (non-fatal): {exc}")

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
        base_ref = f"origin/{repo_ref}" if repo_ref else "origin/HEAD"

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
        builder_conversation_id = run_conversation(
            workspace,
            builder_id,
            builder_prompt,
            f"Improve {repo_label}",
        )

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
        }
        shas = git_commit_range(repo_path, base_ref, reported_branch)
        builder_update.update(shas)
        if soul_suggestions:
            builder_update["soul_suggestions"] = soul_suggestions
        if pr_url:
            builder_update["pr_url"] = pr_url
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
        if not reviewers:
            report_cycle({"status": "COMPLETED", "verdict": "SINGLE_RUNTIME"})
            report_phase(f"{repo_label}: built, no second runtime to review")
            print("\n  only one runtime available; review skipped")
            return

        report_cycle({"status": "REVIEWING"})
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

        reviews = []
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
                    "runtime": reviewer["runtime"],
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

        final_update = {
            "status": "COMPLETED",
            "verdict": cycle_verdict(reviews),
            "reviews": reviews,
        }
        if soul_suggestions:
            # Deduplicated across stages: both runtimes noticing the same
            # thing is one proposal for the user, not two.
            final_update["soul_suggestions"] = normalize_soul_suggestions(
                soul_suggestions
            )
        report_cycle(final_update)
        report_phase(f"{repo_label}: cycle complete")
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        report_cycle({"status": "FAILED", "error": detail[:MAX_SUMMARY_CHARS]})
        raise


with workspace_ctx as workspace:
    execute_cycle(workspace)

print("\n=== RESULT ===")
print("ALL_OK")
