# verify-automation: F19 handoff lifecycle and rotation faults

PR-only artifact (removed on approval). Verification-skill maintenance only —
no product source, tests or CI are touched.

## What was driven

Three gaps in `F19-improve-programs.md`, each checked against the source
before it was written up:

1. **The five fault counts on `GET /v1/improve/builder-share` were named
   nowhere in the map.** Confirmed: `grep -n 'unattributed\|misattributed\|
   work_redone\|handoffs_waiting\|handoffs_unsaved'` over the file returned
   nothing but the schema's own route line. The response model
   (`improve/schemas.py:BuilderShareResponse`) and `improve/usage.py:
   rotation_faults` carry all five. Now driven by `F19.handoff-faults` and
   `F19.attribution-faults`, each counter observed at 1 on a fresh run.
2. **The handoff lifecycle had no sub-feature.** Confirmed: `handoff`
   appeared once in the whole map, as the tarball member `handoff_prompt.md`.
   Now `F19.handoff-write`, `F19.handoff-resume` and `F19.handoff-unpushed`.
3. **`F19.tarball`'s prose contradicted its own recipe.** Confirmed: prose
   said five members, the recipe asserted eleven, and
   `preset_router.py:_generate_improve_tarball` writes eleven. A live
   download lists exactly:
   `builder_prompt.md,change_class.py,context.py,handoff_prompt.md,judge_prompt.md,lessons.py,main.py,report.py,reviewer_prompt.md,routing.py,setup.sh`.
   The prose is fixed. The same stale sentence in the repository's `AGENTS.md`
   ("The tarball carries only `main.py`, `report.py`, `setup.sh` and the two
   prompt templates") is fixed with it.

## Full fresh family replay

With the Automation Service in local mode (SQLite) plus an Agent Server at the
release `uv.lock` pins. No model: F19 needs none. `map run` reads the map from
the working tree, and it read the tree that became this branch's commit — the
launcher records the parent sha (`6d036da`) because the map edits were not
committed yet. Everything written after the replay is outside the executed
part of the file: the two `## Gotchas` numbers, the index counts and the
`AGENTS.md` sentence.

```sh
control-automation map run --file F19 --fresh --record
```

**Result: ok. 36 pass, 0 fail, 0 xfail, 0 xpass, 1 blocked, 0 skipped.**
Run directory `20261025-100323-1056-map-f19`. The one blocked bullet is the
pre-existing `F19.cycle-run`, which needs two agent runtimes and their keys.

The selection closure was replayed on its own fresh run as well, to show the
new bullets do not depend on untracked state from the rest of the family:

```sh
control-automation map run --file F19 --only F19.handoff-faults,F19.attribution-faults --fresh
```

**Result: ok. 5 pass (Preconditions plus the four new bullets), 0 fail.**

An earlier full replay (`20261025-095921-3f66-map-f19`) failed exactly one
assertion — `cycles eq 5` on the first fault reading, where the real answer is
4, because the program's fourth cycle was claimed and never reported and so
names no builder. The map now asserts 4 and says why; that difference between
what the share counts and what the fault counts cover is the point of the
bullet.

## The readings behind the new sub-features

Every exchange is in `F19-handoff-and-faults-evidence.json`. The load-bearing
ones, abbreviated:

`F19.handoff-write` — the agent's record plus what only the service adds:

```json
{"schema_version": 1, "status": "incomplete", "reason": "runtime_exhausted",
 "branch": "improve/qa-handoff", "branch_pushed": true,
 "head_sha": "c0ffee1...", "next_steps": ["Write the migration"],
 "do_not": ["do not add a second index on the same column"],
 "long_form_path": ".improve-cycle/handoff.md", "written_by": "qa-runtime-one",
 "written_at": "2026-10-25T10:05:02.135630+00:00"}
```

A record with no `next_steps` is stored as `null`, not half-trusted.

`F19.handoff-resume` — the next claim on that repository is handed it, and the
chain is readable in both directions (`cycles.0.resumed_from_cycle_id` on the
new cycle, `cycles.2.resumed_by_cycle_id` on the old one).

`F19.handoff-unpushed` — stored re-aimed rather than dropped:

```json
{"branch": null, "branch_pushed": false, "head_sha": null,
 "next_steps": ["Redo the work from the plan below: the previous attempt was never pushed, so its commits are gone.",
                "Run the migration against SQLite"]}
```

and the claim that picks it up is handed the same thing — no branch, and the
redo line first. It is handed the *third* cycle's handoff, not the first's,
which is the exactly-once property: taking a handoff marks it.

`F19.handoff-faults` — two readings, one program each:

```json
{"cycles": 4, "runtimes": [{"runtime": "qa-runtime-one", "cycles": 2, "share": 0.5},
                           {"runtime": "qa-runtime-two", "cycles": 2, "share": 0.5}],
 "work_redone": 0, "handoffs_waiting": 1, "handoffs_unsaved": 1}
{"cycles": 1, "work_redone": 1, "handoffs_waiting": 0, "handoffs_unsaved": 0}
```

`handoffs_unsaved` still reads 1 after that handoff was resumed exactly as
intended — the code was gone either way, which is why it is not folded into
`work_redone`. `work_redone` is driven by relabelling the repository through
`PUT .../config`: the claim matches a handoff by label (or `remote_id`), the
counter groups by URL, so the next cycle on the same URL is handed nothing and
starts over. `resume: null` on that claim is the read-back.

`F19.attribution-faults` — two cycles, one share:

```json
{"cycles": 1, "runtimes": [{"runtime": "qa-runtime-one", "cycles": 1, "share": 1.0}],
 "unattributed_cycles": 1, "misattributed_cycles": 1}
```

The `custom` cycle is counted beside the share instead of inside it, and the
cycle whose `builder_runtime` disagrees with the last leg of its own
`runtime_switches` trail is counted as misattributed.

## Checks

```
control_automation.py map check     -> ok, 21 files, 341 ids, no problems
control_automation.py map coverage  -> ok, 94 routes, 91 owned, 91 driven
```

`341` is `336 + 5`; the index's F19 row moves from 41 to 46.

## Noted, not changed

- The claim's two brakes — 10 cycles per repository per day
  (`AUTOMATION_IMPROVE_DAILY_CYCLES_PER_REPOSITORY`) and 40 per organization
  (`AUTOMATION_IMPROVE_GLOBAL_DAILY_CEILING`) — have no sub-feature in F19.
  A full replay now takes 36 of those 40, with `qa alpha` at 8 of its 10, so
  the family is close to both; that is recorded as a gotcha with the measured
  numbers. Verified in passing that the env var is live: restarting a run with
  `AUTOMATION_IMPROVE_GLOBAL_DAILY_CEILING=1` makes the next claim answer
  `409 {"detail": {"code": "daily_ceiling_reached", ...}}`. Mapping the brakes
  themselves belongs in a separate pass.
- `AGENTS.md`'s `#### Files` list for the improve preset is also out of date
  (it names four preset files and four `improve/` modules; there are eight and
  fourteen). Left alone: it is not the claim this PR is about.
