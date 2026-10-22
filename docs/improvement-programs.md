# Improvement programs

An improvement program is a cron automation that works through several
repositories in rotation. One run is one **cycle**: it takes the next
repository, researches and implements a single improvement there, and then has
a *different* agent runtime review that work adversarially.

Nothing merges itself. A cycle ends in a branch, optionally a pull request, and
a verdict for a human to act on.

## Why it is shaped this way

**One run, several conversations.** A conversation is created from an
`agent_profile_id`, so one script can run the builder under Claude Code and the
review under Codex. The alternative — a run per stage, chained — would need the
rotation state to survive between runs, and the service has no chaining
concept. Nothing is gained by splitting it.

**Review must come from another runtime.** A model reviewing its own work
shares its blind spots: the same missing context, the same misread
documentation, the same confident wrong citation. The program's premise is that
two different runtimes fail differently, so the second one is worth asking. The
verdict `SINGLE_RUNTIME` exists to record when that did not happen, and it is
deliberately not a pass.

**Cited sources are the unit of evidence.** The builder must record the URL
behind every claim its change rests on, and the reviewer's *first* job is to
open each one and check it says what the change says it says. A citation that
turns out not to support its claim is the most serious finding available,
because every other judgement in the cycle rested on it. The reviewer also
checks whether something newer has superseded the approach, which is how an
implementation that has quietly fallen behind gets caught.

**The rotation has no stored cursor.** The next repository is the one after the
newest cycle's repository. There is one fact, not a counter plus a history that
can disagree with it, and `GET .../rotation` computes "up next" with the same
function the claim uses, so the dashboard cannot promise one repository and the
run take another.

## A cycle, step by step

1. **Claim.** `POST /v1/improve/{automation_id}/claim`. The automation row is
   locked, the rotation resolved, and a cycle row written *before* any agent
   starts — so an interrupted run is still visible and still advances the
   rotation. The response carries the program's current configuration, read
   from the automation rather than the run's packaged code.
2. **Clone** the chosen repository into the run's workspace.
3. **Gather context**: the user's `SOUL.md` from the agent server
   (`GET /api/soul`, skipped on an older server), the repository's own
   `PROJECT_IDENTITY.md`, and the program's standing `focus` text. The
   repository's `AGENTS.md` is read by the agent itself.
4. **Build.** A conversation under the builder profile, given
   `builder_prompt.md`. It must choose one improvement, research it from
   primary sources, implement it on a branch, run the project's own checks,
   push, open a pull request, and write a JSON report.
5. **Review.** One conversation per reviewing runtime, given
   `reviewer_prompt.md` plus the builder's summary, its cited sources and the
   risks it admitted. Each writes its own JSON verdict.
6. **Record.** `PATCH /v1/improve/cycles/{cycle_id}` after each stage. The
   overall verdict is the worst of the reviewers': one runtime finding the
   change wrong is the finding, even if another was content.

## Configuration

`ImproveProgramConfig`, stored as `preset_metadata["improve"]`:

| Field | Meaning |
|---|---|
| `builder_profile_id` | The profile that researches and implements. Its runtime is the one reviewers must differ from. |
| `reviewer_profile_ids` | Explicit reviewers. **Empty means auto**: each run picks one profile per runtime other than the builder's, so adding a second runtime to the server is enough to turn review on. |
| `repository_source` | `git_sync` (the org's synced repositories) or `explicit`. |
| `repositories` | The rotation when the source is `explicit`. |
| `repository_labels` | Optional filter over Git Sync remotes by label. |
| `branch_prefix` | Branches the program may create and force-push. Validated as a git ref prefix. |
| `max_changed_files` | Upper bound on one cycle's diff. A cycle that would exceed it must narrow its scope. |
| `open_pr` | Open a pull request, or just push the branch. |
| `focus` | Standing guidance, added to the repository's identity rather than replacing it. |

The builder may not also be a reviewer: the request is rejected, because a
runtime reviewing its own work is not independent review.

`PUT /v1/improve/{automation_id}/config` replaces the whole configuration. It
is a replacement rather than a patch because the fields constrain each other —
a repository source and its list, a builder and its reviewers — so a partial
update would have to re-validate the merged result anyway. It applies from the
next cycle; one already running keeps the configuration it claimed with.

An explicit repository URL has any embedded username or password dropped. It
would otherwise be stored in the automation's metadata, exported to the Git
Sync repository with it, and shown in the dashboard, and a cycle clones with a
credential the agent server holds, so the URL never needs one.

## Verdicts

| Verdict | Meaning |
|---|---|
| `ACCEPTED` | A second runtime looked for problems and did not find one worth acting on. |
| `CHANGES_REQUESTED` | Worth keeping once the concerns are addressed. |
| `REJECTED` | Should not land: wrong, off mission, or resting on a citation that does not hold. |
| `INCONCLUSIVE` | A reviewer could not get far enough to judge. |
| `SINGLE_RUNTIME` | Built, but no second runtime was available to review it. Not a pass. |
| `NO_CHANGE` | The cycle researched and deliberately changed nothing. A valid outcome. |

A cycle whose builder reports itself **blocked** is recorded as a *failed*
cycle with the reason, not as `NO_CHANGE`. It may have done real work and still
produced nothing reviewable, and the difference between "we decided against it"
and "we could not finish" is exactly what these verdicts exist to keep apart.

`NO_CHANGE` matters: "we looked into it and it is a bad idea, here are the
sources" is a successful cycle, and keeping it distinct from a silent failure is
how you tell a program that is working from one that is idling.

## Learned preferences

A cycle may report `soul_suggestions`: things it learned about how the *user*
wants to be worked for, as `{text, why}` proposals for their `SOUL.md`.

Proposals only. A run may notice a preference but may not rewrite the document
that tells every future run what the user wants, so there is no API path for a
run to edit `SOUL.md`. The user accepts or discards each entry. Both stages may
propose and the entries are deduplicated across them, because both runtimes
noticing the same thing is one decision for the user, not two.

## Documents a program reads

| Document | Lives | Written by | Says |
|---|---|---|---|
| `SOUL.md` | the user's agent-server state (`~/.openhands/SOUL.md`) | the user, with proposals from cycles | how this user wants to be worked for |
| `PROJECT_IDENTITY.md` | the repository root | the user, or a cycle drafting a first version | what this project is for and where it is going |
| `AGENTS.md` | the repository root | the project | how to work in this codebase |

`AGENTS.md` says *how*; `PROJECT_IDENTITY.md` says *why* and *what next*, which
is what lets a cycle judge whether a change is on mission rather than merely
correct. When it is absent the builder is told to infer the project's purpose
and to say the document is missing — drafting a first version of it is itself a
good cycle.

## Operating notes

- A cycle does real research and real implementation, so give a program a
  generous `timeout`. The service caps user timeouts at
  `AUTOMATION_MAX_RUN_DURATION`.
- The rotation skips a Git Sync remote with no repository URL rather than
  failing: local mode creates such a row on its first tick.
- Deleting cycle history legitimately rewinds the rotation, since the history
  *is* the cursor.
