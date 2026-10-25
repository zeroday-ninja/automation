"""What a cycle leaves behind when its runtime runs out mid-change.

A cycle whose runtime is exhausted halfway through has done real work and can
publish none of it. Restarting from nothing is the expensive answer; the cheap
one is to spend what remains of that runtime writing down where it got to, so
the next one continues rather than rediscovering.

Two things make the difference between a handoff and a note:

- **``next_steps`` must not be empty.** A handoff with no next step is
  indistinguishable from a restart, so one without is rejected and treated as
  absent rather than half-trusted.
- **``do_not`` is what actually saves the credits.** It records the approaches
  already tried and rejected, which is the one thing that stops the next
  runtime spending its whole budget rediscovering the same dead end.

The branch is secured before any of this is written: pushed code is worth more
than a description of code, and a handoff describing work nobody can fetch is
advice, not a continuation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


#: Current shape of the structured record.
HANDOFF_SCHEMA_VERSION = 1

#: Where the prose version lives on the branch.
HANDOFF_PROSE_PATH = ".improve-cycle/handoff.md"


@dataclass(frozen=True, slots=True)
class Handoff:
    """A cycle's unfinished work, in a form the next one can pick up."""

    status: str
    reason: str
    branch: str | None
    branch_pushed: bool
    head_sha: str | None
    stage: str
    goal: str
    done: list[str]
    next_steps: list[str]
    do_not: list[str]
    files_touched: list[str]
    #: Why this cycle's work did not reach the branch, when it did not.
    #:
    #: `branch_pushed: false` says the code is gone; this says what stopped
    #: it. Without it the only account was a line in the run's log, and run
    #: logs are pruned -- fifteen unsaved handoffs on this machine and not one
    #: of them could still be asked why. A degraded path has to write its
    #: reason onto the record, not print it.
    save_note: str | None
    written_by: str | None
    written_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": HANDOFF_SCHEMA_VERSION,
            "status": self.status,
            "reason": self.reason,
            "branch": self.branch,
            "branch_pushed": self.branch_pushed,
            "head_sha": self.head_sha,
            "stage": self.stage,
            "goal": self.goal,
            "done": self.done,
            "next_steps": self.next_steps,
            "do_not": self.do_not,
            "files_touched": self.files_touched,
            "save_note": self.save_note,
            "long_form_path": HANDOFF_PROSE_PATH,
            "written_by": self.written_by,
            "written_at": self.written_at,
        }


def _strings(value: Any, limit: int, length: int) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value[:limit]:
        if isinstance(item, str) and item.strip():
            out.append(item.strip()[:length])
    return out


def parse_handoff(raw: Any, *, written_by: str | None = None) -> Handoff | None:
    """A handoff from whatever an agent wrote, or ``None`` if it is not one.

    Returns ``None`` rather than a partial record when there are no next
    steps: the next cycle has to be able to tell "here is where I got to" from
    "something was written here", and a record it half-trusts is worse than no
    record, because it will act on it.
    """
    if not isinstance(raw, dict):
        return None
    next_steps = _strings(raw.get("next_steps"), 10, 300)
    if not next_steps:
        return None
    branch = raw.get("branch")
    return Handoff(
        status=str(raw.get("status") or "incomplete")[:32],
        reason=str(raw.get("reason") or "runtime_exhausted")[:64],
        branch=branch.strip()[:255] if isinstance(branch, str) and branch else None,
        branch_pushed=bool(raw.get("branch_pushed")),
        head_sha=(
            str(raw["head_sha"])[:64] if isinstance(raw.get("head_sha"), str) else None
        ),
        stage=str(raw.get("stage") or "implementing")[:32],
        goal=str(raw.get("goal") or "")[:600],
        done=_strings(raw.get("done"), 20, 200),
        next_steps=next_steps,
        do_not=_strings(raw.get("do_not"), 20, 300),
        files_touched=_strings(raw.get("files_touched"), 50, 300),
        save_note=(
            str(raw["save_note"])[:500]
            if isinstance(raw.get("save_note"), str) and raw["save_note"].strip()
            else None
        ),
        written_by=written_by,
        written_at=datetime.now(UTC).isoformat(),
    )


def degraded_handoff(
    *,
    branch: str | None,
    head_sha: str | None,
    branch_pushed: bool,
    summary: str | None,
    reason: str,
    save_note: str | None = None,
    written_by: str | None = None,
) -> Handoff:
    """A handoff written by this script, with no model involved.

    For when the runtime died before it could describe its own state — or
    there was no runtime left to ask. It costs nothing and it is always
    available, which is the point: the alternative is a branch with work on it
    that nobody knows about.
    """
    return Handoff(
        status="incomplete",
        reason=reason,
        branch=branch,
        branch_pushed=branch_pushed,
        head_sha=head_sha,
        stage="unknown",
        goal=(summary or "")[:600],
        done=[],
        next_steps=[
            "Read the branch diff and continue from it. The previous runtime "
            "ran out before it could describe where it had got to."
        ],
        do_not=[],
        files_touched=[],
        save_note=save_note,
        written_by=written_by,
        written_at=datetime.now(UTC).isoformat(),
    )


def downgrade_unpushed(handoff: Handoff) -> Handoff:
    """A handoff whose branch never reached the remote.

    The diff is gone — a new sandbox cannot see an unpushed branch — but the
    reasoning is not, and the reasoning is most of the value. So the record
    stays, re-aimed at redoing the work from the plan rather than continuing
    from a commit that does not exist anywhere the next cycle can reach.
    """
    if handoff.branch_pushed:
        return handoff
    return Handoff(
        status=handoff.status,
        reason=handoff.reason,
        branch=None,
        branch_pushed=False,
        head_sha=None,
        stage=handoff.stage,
        goal=handoff.goal,
        done=handoff.done,
        next_steps=[
            "Redo the work from the plan below: the previous attempt was "
            "never pushed, so its commits are gone.",
            *handoff.next_steps,
        ],
        do_not=handoff.do_not,
        files_touched=handoff.files_touched,
        save_note=handoff.save_note,
        written_by=handoff.written_by,
        written_at=handoff.written_at,
    )


def is_resumable(stored: Any, head_sha: str | None) -> bool:
    """Whether a stored handoff can still be continued from.

    A branch that moved under the handoff means somebody else has been there,
    so the record becomes advice rather than a continuation — acting on it as
    though the commit were still the tip is how a cycle silently reverts
    somebody's work.
    """
    if not isinstance(stored, dict):
        return False
    if not stored.get("branch_pushed"):
        return False
    recorded = stored.get("head_sha")
    if not isinstance(recorded, str) or not recorded:
        return False
    return head_sha is None or recorded == head_sha
