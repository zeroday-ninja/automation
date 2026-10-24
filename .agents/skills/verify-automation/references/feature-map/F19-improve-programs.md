# Improvement programs

An improvement program is a cron automation that works through a list of
repositories one at a time. Each run claims the next repository in the
program's *rotation*, has the configured **builder** agent profile research and
implement one improvement there with cited sources, and then has every other
available agent runtime review that change adversarially. Nothing is merged: a
cycle ends in a branch, optionally a pull request, a verdict, and whatever the
two stages propose for the user's `SOUL.md`. The rotation is the organization's
Git Sync remotes by default (ordered by remote creation, not default first) or
an explicit list, and it has no stored cursor: the next repository is the one
after the newest `automation_improve_cycle` row's repository, matched by label
and otherwise taken from that row's recorded position.
`POST /v1/preset/improve` creates the program and packages the runner;
`/v1/improve/...` is called by a run from inside its sandbox (claim, then
report) and by the dashboard (rotation, history, and
`PUT .../config` to change the program). The program's configuration lives on
the automation (`preset_metadata.improve`) and is read on every claim, so an
edit takes effect on the next cycle without touching the tarball and leaves a
cycle already running alone. A credential embedded in a repository URL is
dropped wherever the configuration is written, so nothing stores it.

Source: `openhands/automation/improve/`, `openhands/automation/presets/improve/`, `openhands/automation/preset_router.py`, `openhands/automation/models.py`, `migrations/versions/035_improve_cycles.py`

Needs: `git`

Routes: `POST /api/automation/v1/preset/improve`, `POST /api/automation/v1/improve/{automation_id}/claim`,
`PATCH /api/automation/v1/improve/cycles/{cycle_id}`,
`PUT /api/automation/v1/improve/{automation_id}/config`,
`GET /api/automation/v1/improve/{automation_id}/rotation`,
`GET /api/automation/v1/improve/{automation_id}/cycles`,
`POST /api/automation/v1/improve/{automation_id}/refresh-code`,
`GET /api/automation/v1/improve/runtime-usage`,
`GET /api/automation/v1/improve/builder-share`,
`GET /api/automation/v1/improve/parked`,
`POST /api/automation/v1/improve/cycles/{cycle_id}/resolve`,
`POST /api/automation/v1/improve/cycles/{cycle_id}/attest`,
`GET /api/automation/v1/improve/cycles/{cycle_id}/authority`

## Sub-features

- `F19.create`: a minimal improvement program answers 201 with `preset_metadata {preset_type: improve, improve: {...}}` carrying the configuration defaults (`repository_source: git_sync`, `branch_prefix: improve/`, `max_changed_files: 20`, `open_pr: true`, `reviewer_profile_ids: []`, `focus: null`), `agent_profile_id` set to the builder profile, `model: null`, `setup_script_path: setup.sh`, `entrypoint: .venv/bin/python main.py`, `timeout: 600`, an `oh-internal://uploads/<id>` tarball path, and a `COMPLETED` upload named `improve-program-<name>`; a username or password embedded in an explicit repository URL is dropped before the configuration is stored rather than refused.
- `F19.tarball`: the generated tarball holds exactly `builder_prompt.md`, `main.py`, `report.py`, `reviewer_prompt.md` and `setup.sh`, `setup.sh` is mode 0755, `main.py` calls the claim and cycle routes, and no member names the program's profiles or repositories — a run reads them from its claim.
- `F19.create-validation`: a missing `improve`, an unknown field, `state: DRAFT`, an over-long timeout, a builder that is also a reviewer, duplicate or more than four reviewers, `repository_source: explicit` without `repositories`, `repositories` or `repository_labels` combined with the wrong source, a repository URL git cannot clone, a `branch_prefix` git forbids and `max_changed_files` out of range each answer 422 with their own message and create nothing.
- `F19.config-replace`: `PUT .../config` replaces the whole configuration, answers the new rotation with the repository it puts up next, stores it on the automation, moves `agent_profile_id` to the new builder, marks the automation dirty for git sync, leaves the tarball alone, and is what the next claim runs on.
- `F19.config-validation`: the config route refuses every configuration a create would refuse and changes nothing; a non-program answers 409, an unknown automation 404, an id that is not a UUID 422.
- `F19.rotation-explicit`: `GET .../rotation` on an explicit program lists the configured repositories in order with their index, `next_index: 0`, `last_cycle: null` and the stored configuration, whose URLs are the rotation's: no copy of the configuration holds a credential.
- `F19.claim`: `POST .../claim` answers 201 with a `CLAIMED` cycle for index 0, the program's current configuration, and the whole rotation; the row is in `automation_improve_cycle` with the builder profile and the org, and the history repeats the claim's timestamps with their UTC offset.
- `F19.claim-rotation`: each further claim takes the next repository and wraps at the end; `next_index` and `last_cycle` in the rotation view follow the newest cycle.
- `F19.claim-run-id`: a `run_id` of this automation's run is recorded on the cycle; a run id belonging to another automation answers 400 and a non-UUID 422, and neither creates a cycle.
- `F19.cycle-build`: `PATCH /v1/improve/cycles/{id}` records the builder stage (`status: BUILDING`, runtime, conversation, branch, summary, cited sources) and a second read returns it.
- `F19.cycle-validation`: a `pr_url` that is not http(s), an unknown `status` or `verdict`, an unknown field, more than 40 sources and more than 10 soul suggestions answer 422; an explicit `null` leaves a stored value alone instead of clearing it.
- `F19.cycle-soul`: a cycle records `soul_suggestions` as `{text, why}` proposals for the user's `SOUL.md` (empty on a fresh cycle, an empty `text` refused), and no route lets a run edit that document.
- `F19.cycle-review`: a report of the reviews and the verdict completes the cycle, stamps `completed_at`, and keeps each review's runtime, concerns and stale sources as well as the builder's sources and soul suggestions.
- `F19.cycle-terminal`: a `COMPLETED` or `FAILED` cycle is immutable (409 naming its status); an unknown cycle id answers 404.
- `F19.cycles-page`: `GET .../cycles` lists the program's cycles newest first with the total, pages with `limit` and `offset`, clamps `limit` into 1..100 and a negative `offset` to 0, and answers 422 for a non-integer `limit`.
- `F19.rotation-git-sync`: a `git_sync` program rotates over every Git Sync remote that has a repository URL, ordered by `(created_at, id)`; promoting a remote to default does not reshuffle the rotation, a disabled remote stays in it, and a remote whose URL was cleared drops out.
- `F19.rotation-labels`: `repository_labels` keeps only the listed remotes in the rotation, ignores a label no remote has, and is what the claim rotates over.
- `F19.rotation-empty`: a program whose rotation resolves to nothing answers `repositories: []` with `next_index: null`, an empty history, and 409 on a claim.
- `F19.claim-index-fallback`: when the repository the newest cycle took is gone from the rotation, the next claim takes whatever now sits at that cycle's recorded position instead of starting over.
- `F19.claim-label-match`: when the rotation shrinks behind it, the next claim continues after the *label* the newest cycle took, not from its recorded index.
- `F19.not-a-program`: all three automation-scoped routes answer 409 for an automation that is not an improvement program (a raw automation and a prompt preset report different reasons), 404 for an unknown automation and 422 for an id that is not a UUID.
- `F19.auth`: every route, including the config route that needs `manage_automations`, answers 401 without a key and with a wrong key, and creates no program and no cycle.
- `F19.cycle-run`: a dispatched run claims a cycle, builds on a branch and has a second runtime review it (blocked here: needs two agent runtimes and their keys).
- `F19.refresh-code`: `POST .../refresh-code` repackages the runner from this build without touching the program's configuration, so a service upgrade reaches an existing program; it needs `manage_automations`.
- `F19.usage-ledger`: `GET /v1/improve/runtime-usage` answers which runtimes have usage left, with the evidence behind each reading — what was last seen, when it last refused, how many times in a row, and whether the reset time came from the runtime or from a default cooldown. Recovery is applied at read time, so an exhaustion whose reset has passed reads as available while the exhaustion stays on the record.
- `F19.usage-unknown`: a runtime nobody has observed is absent rather than reported healthy, and `unknown` stays distinct from `available` — nothing having gone wrong is not the same as something having gone right.
- `F19.builder-share`: `GET /v1/improve/builder-share` answers how evenly the building was divided between runtimes over a window of `days`, across every program in the org: each runtime's cycles and its share of them, and `evenness_gap`, the widest share's distance from an even split in percentage points, against the `target_gap: 20.0` the plan asks for, carried with the answer so a reader does not have to know it. Counted from each cycle's builder of record, which is the runtime that actually ran rather than the one the profile named, and `null` rather than a gap of zero when nothing was built.
- `F19.builder-share-not-yet`: `days_covered` travels with that answer and `within_target` is null, not false, until it reaches the window: a week asked of a day of history still reports the gap and withholds the verdict, because an unmet measure and an unmeasurable one are different answers.
- `F19.builder-share-scoped`: `automation_id` scopes that answer to one program, echoes the id back and reports `configured_builders` beside it, and changes what "even" measures against: the denominator is the larger of the runtimes seen and the runtimes the program is configured to rotate, so a program running one of its three is a 66.7-point gap where the org-wide reading calls the same cycles a perfect split. Both fields are null unscoped, because there is then no single configuration for "even" to mean anything against, and an `automation_id` that is not a program's is an error rather than quietly the whole org.
- `F19.parked-list`: `GET /v1/improve/parked` answers everything waiting for a person across every program in the org, because the question is "what is waiting on me" and an answer that needs the reader to know which program to look in is not an answer.
- `F19.resolve`: `POST .../resolve` records a person's answer with a required reason and returns the cycle to the phase it stopped at; `archive` ends it as COMPLETED with a REJECTED verdict, because a decision is not a malfunction. It needs `manage_automations` — the point of escalating is that the thing being gated is not the thing that answers.
- `F19.resolve-not-escalate`: `escalate` is refused as an answer: that endpoint *is* the escalation, so escalating again would park the cycle on the person who just unparked it.
- `F19.resolve-not-parked`: resolving a cycle that is not PARKED answers 409 rather than silently doing nothing.
- `F19.attest`: `POST .../attest` records the judge's verdict on a deletion, with each removal accounted for as dead, replaced or covered and a `where` that can be checked; attesting with no removals named is refused, because signing off a set of removals without naming them is signing off nothing.
- `F19.attest-not-the-author`: an attestation from the runtime that built the change is refused 409. The service checks what actually signed rather than trusting the configuration, because a rotation moves the builder between cycles and a model favours its own output.
- `F19.authority`: `GET .../authority` answers whether a change may publish, and what is standing in the way. Additive publishes on its own account; a deletion needs the judge's attestation; a breaking change is never authorised from here for anybody, and says so with `human_only` rather than as "not yet". A change whose review has not been recorded yet is returned with `missing: [verdict]` ahead of all of that, because authority is not a thing to collect before the review that decides it.
- `F19.authority-evidence`: a change that cannot show a check that actually *passed* is returned as a proposal with the gaps named, and a check that *failed* is answered separately — it is evidence, not missing evidence.
- `F19.gate-log`: every routing decision is appended with its route, trigger and required rationale; a decision the service cannot read is recorded as unreadable with the problem rather than dropped, because a client with a bug must not look like one that never decided anything.
- `F19.change-class`: the change class is stored with the evidence it was computed from, and a report whose class contradicts its own evidence is stored at the restrictive reading — the thing being gated does not lower its own gate.

## How to get to it (agent POV)

- REST: `POST /v1/preset/improve` (`CreateImproveAutomationRequest`, with the
  program in `improve`: `ImproveProgramConfig`) answers the F05
  `AutomationResponse`; reading, disabling, deleting and downloading the
  program go through the F05 routes. `POST /v1/improve/{id}/claim`
  (`ClaimCycleRequest` → `ClaimCycleResponse`),
  `PATCH /v1/improve/cycles/{id}` (`UpdateCycleRequest` → `CycleResponse`),
  `PUT /v1/improve/{id}/config` (a whole `ImproveProgramConfig` as the body →
  `RotationResponse`), `GET /v1/improve/{id}/rotation` (`RotationResponse`)
  and `GET /v1/improve/{id}/cycles` (`CycleListResponse`, `limit`/`offset`).
  All request models are `extra="forbid"`.
- Auth: every `/v1/improve` route needs ownership of the parent automation,
  exactly like the run callbacks (`complete_run`, `report_run_phase`):
  someone else's automation is a 404, not a 403, so the route never confirms
  it exists. The read and report routes need `view_automations`; only
  `PUT .../config` needs `manage_automations`, because it changes what the
  program will do. The local key carries both and there is one user and one
  org, so neither the 403 nor the cross-owner 404 is reachable from here.
- Editing: `PUT .../config` is the only way to change a program.
  `PATCH /v1/{id}` has no `improve` field, so the F05 route cannot reach the
  configuration (it can still rename, retime, disable or delete the
  automation). The config route is a whole replacement, not a patch: the
  fields constrain each other, so a partial update would have to re-validate
  the merged result anyway.
- The run is the main consumer: `presets/improve/sdk_main.py` (packaged as
  `main.py`, with `report.py` for reading and normalizing what the agents
  wrote) claims once before any agent work (`AUTOMATION_API_URL` +
  `AUTOMATION_CALLBACK_API_KEY`, the same credential the completion callback
  uses), then PATCHes the cycle at each stage. A 409 on the claim is its clean
  no-op: it prints "nothing to do" and the run still completes.
  `builder_prompt.md` and `reviewer_prompt.md` are the prompts it renders, and
  `AUTOMATION_AGENT_PROFILE_ID` is ignored because the program names its own
  profiles.
- Agent Server: the run creates one conversation per profile
  (`RemoteConversation` with `agent_profile_id`), reads the available profiles
  from `/api/agent-profiles` and picks every runtime other than the builder's
  when `reviewer_profile_ids` is empty; it also reads the user's `SOUL.md`
  from `GET /api/soul` to give both stages the user's standing preferences
  (F16 covers the run's contract). It never writes that document: what the
  stages learn arrives here as `soul_suggestions` for the user to accept.
- Git Sync (F18) owns the default rotation: a program with
  `repository_source: git_sync` rotates over `GET /v1/git-sync/remotes`,
  filtered by `repository_labels`, so adding a repository there adds it to
  every such program.
- Capabilities (F03): `presetImprove` in `GET /v1/capabilities` `features` is
  how a client knows this deployment can create programs.
- No SDK or TypeScript client method in this repository reaches these routes.
- Config: `AUTOMATION_DEFAULT_RUN_DURATION` / `AUTOMATION_MAX_RUN_DURATION`
  bound the program's `timeout`; `FILE_STORE` holds the generated runner.
  Nothing else configures a program — the rest is `preset_metadata.improve`,
  which the config route writes and `mark_git_sync_dirty` then queues for
  export to the automation's git sync remote (F18).

## Driving it with control-automation

Preconditions:

- A fresh run (`map run --file F19 --fresh`) and a clean `doctor`. No LLM.
  Every program is created with a yearly cron (`0 0 1 1 *`) so the scheduler
  never fires one mid-replay and claims a cycle the recipes did not expect;
  `F19.claim-run-id` dispatches one program by hand and keeps its history to
  itself.
- `git` on `PATH` for the Git Sync rotation bullets, which use local bare
  repositories (`fixture git-remote`) as the org's repositories.
- Profile ids are literals: in local mode the Agent Server resolves them at
  run time, so the create route accepts any UUID (see Gotchas).
  ```sh
  BUILDER=11111111-1111-4111-8111-111111111111
  REVIEWER=22222222-2222-4222-8222-222222222222
  ```

- **Create an improvement program (`F19.create`).** The automation, its configuration and its generated runner.
  ```sh
  AID=$(control-automation api POST /v1/preset/improve --json "{\"name\":\"qa improve explicit\",\"trigger\":{\"type\":\"cron\",\"schedule\":\"0 0 1 1 *\"},\"improve\":{\"builder_profile_id\":\"$BUILDER\",\"repository_source\":\"explicit\",\"repositories\":[{\"label\":\"qa alpha\",\"url\":\"https://qa-bot:qa-token@qa.invalid/alpha.git\",\"ref\":\"main\"},{\"label\":\"qa beta\",\"url\":\"https://qa.invalid/beta.git\"}]}}" --expect 201 --check preset_metadata.preset_type eq improve --check preset_metadata.improve.builder_profile_id eq "$BUILDER" --check preset_metadata.improve.reviewer_profile_ids len-eq 0 --check preset_metadata.improve.repository_source eq explicit --check preset_metadata.improve.repositories.0.label eq 'qa alpha' --check preset_metadata.improve.repositories.0.url eq https://qa.invalid/alpha.git --check preset_metadata.improve.repositories.1.ref eq null --check preset_metadata.improve.repository_labels len-eq 0 --check preset_metadata.improve.branch_prefix eq improve/ --check preset_metadata.improve.max_changed_files eq 20 --check preset_metadata.improve.open_pr eq true --check preset_metadata.improve.focus eq null --check agent_profile_id eq "$BUILDER" --check model eq null --check setup_script_path eq setup.sh --check entrypoint eq '.venv/bin/python main.py' --check timeout eq 600 --check state eq ACTIVE --check enabled eq true --check tarball_path matches '^oh-internal://uploads/[0-9a-f-]{36}$' --save F19.create/create --field id)
  ITP=$(control-automation api GET "/v1/$AID" --expect 200 --check preset_metadata.preset_type eq improve --check preset_metadata.improve.repositories len-eq 2 --field tarball_path)
  control-automation api GET "/v1/uploads/${ITP#oh-internal://uploads/}" --expect 200 --check name eq 'improve-program-qa improve explicit' --check status eq COMPLETED --check description eq 'Auto-generated improvement program runner' --save F19.create/upload
  ```
  The builder profile is stored twice on purpose: `agent_profile_id` so the
  F05 views and validation treat it like any profile-backed automation, and
  `preset_metadata.improve.builder_profile_id` as the program's own record,
  which is the one a run reads. The `qa-bot:qa-token@` in the first
  repository's URL is accepted and dropped: refusing it would echo the secret
  back in the 422 body, and a cycle clones with a credential the Agent Server
  holds, so the stored URL never needs one.
- **Generated runner tarball (`F19.tarball`).** The runner, the prompts, the
  rule modules the service shares with it, an executable setup script, and no
  configuration inside.
  Requires: `F19.create`
  ```sh
  control-automation api GET "/v1/$AID/tarball" --expect 200 --quiet --check-header content-type eq application/x-tar --raw-out "$AUTOMATION_VERIFY_RUN/qa-improve.tar" --save F19.tarball/download
  test "$(tar tzf "$AUTOMATION_VERIFY_RUN/qa-improve.tar" | sort | paste -sd, -)" = builder_prompt.md,change_class.py,context.py,handoff_prompt.md,judge_prompt.md,lessons.py,main.py,report.py,reviewer_prompt.md,routing.py,setup.sh
  tar tvzf "$AUTOMATION_VERIFY_RUN/qa-improve.tar" setup.sh | grep -q '^-rwxr-xr-x'
  tar xzOf "$AUTOMATION_VERIFY_RUN/qa-improve.tar" setup.sh | grep -q 'sdk-version'
  tar xzOf "$AUTOMATION_VERIFY_RUN/qa-improve.tar" main.py > "$AUTOMATION_VERIFY_RUN/qa-improve-main.py"
  grep -q '/v1/improve/{automation_id}/claim' "$AUTOMATION_VERIFY_RUN/qa-improve-main.py"
  grep -q '/v1/improve/cycles/' "$AUTOMATION_VERIFY_RUN/qa-improve-main.py"
  if grep -q "$BUILDER" "$AUTOMATION_VERIFY_RUN/qa-improve-main.py"; then false; fi
  if tar xzOf "$AUTOMATION_VERIFY_RUN/qa-improve.tar" builder_prompt.md | grep -q 'qa.invalid'; then false; fi
  ```
  `tar tzf` (gzip) is required although the response says
  `application/x-tar`. `main.py` is extracted to a file before it is grepped
  because it is now larger than a pipe buffer: `grep -q` stops at the first
  match, `tar` then dies writing the rest of the file, and `pipefail` reports
  that SIGPIPE as a failed line even though the string was found. The two
  negative greps are the packaging contract:
  profiles and repositories reach the run through its claim, so editing a
  program never has to regenerate this tarball.
- **Program validation (`F19.create-validation`).** Each refused body creates nothing.
  ```sh
  A0=$(control-automation state sql "SELECT count(*) AS n FROM automations" --field rows.0.n)
  control-automation api POST /v1/preset/improve --json '{"name":"qa iv","trigger":{"type":"cron","schedule":"0 0 1 1 *"}}' --expect 422 --check detail.0.type eq missing --check detail.0.loc.1 eq improve --save F19.create-validation/no-program
  control-automation api POST /v1/preset/improve --json "{\"name\":\"qa iv\",\"trigger\":{\"type\":\"cron\",\"schedule\":\"0 0 1 1 *\"},\"prompt\":\"hi\",\"improve\":{\"builder_profile_id\":\"$BUILDER\"}}" --expect 422 --check detail.0.type eq extra_forbidden --check detail.0.loc.1 eq prompt
  control-automation api POST /v1/preset/improve --json "{\"name\":\"qa iv\",\"trigger\":{\"type\":\"cron\",\"schedule\":\"0 0 1 1 *\"},\"state\":\"DRAFT\",\"improve\":{\"builder_profile_id\":\"$BUILDER\"}}" --expect 422 --check detail.0.msg contains 'state=DRAFT is reserved'
  control-automation api POST /v1/preset/improve --json "{\"name\":\"qa iv\",\"trigger\":{\"type\":\"cron\",\"schedule\":\"0 0 1 1 *\"},\"timeout\":1801,\"improve\":{\"builder_profile_id\":\"$BUILDER\"}}" --expect 422 --check detail.0.msg contains 'must not exceed 1800 seconds'
  control-automation api POST /v1/preset/improve --json "{\"name\":\"qa iv\",\"trigger\":{\"type\":\"cron\",\"schedule\":\"0 0 1 1 *\"},\"improve\":{\"builder_profile_id\":\"$BUILDER\",\"reviewer_profile_ids\":[\"$BUILDER\"]}}" --expect 422 --check detail.0.msg contains 'the builder profile cannot also be a reviewer' --save F19.create-validation/builder-reviews-itself
  control-automation api POST /v1/preset/improve --json "{\"name\":\"qa iv\",\"trigger\":{\"type\":\"cron\",\"schedule\":\"0 0 1 1 *\"},\"improve\":{\"builder_profile_id\":\"$BUILDER\",\"reviewer_profile_ids\":[\"$REVIEWER\",\"$REVIEWER\"]}}" --expect 422 --check detail.0.msg contains 'reviewer_profile_ids contains a duplicate'
  control-automation api POST /v1/preset/improve --json "{\"name\":\"qa iv\",\"trigger\":{\"type\":\"cron\",\"schedule\":\"0 0 1 1 *\"},\"improve\":{\"builder_profile_id\":\"$BUILDER\",\"reviewer_profile_ids\":[\"$REVIEWER\",\"33333333-3333-4333-8333-333333333333\",\"44444444-4444-4444-8444-444444444444\",\"55555555-5555-4555-8555-555555555555\",\"66666666-6666-4666-8666-666666666666\"]}}" --expect 422 --check detail.0.msg contains 'at most 4 items'
  control-automation api POST /v1/preset/improve --json "{\"name\":\"qa iv\",\"trigger\":{\"type\":\"cron\",\"schedule\":\"0 0 1 1 *\"},\"improve\":{\"builder_profile_id\":\"$BUILDER\",\"repository_source\":\"explicit\"}}" --expect 422 --check detail.0.msg contains "repositories must list at least one repository when repository_source is 'explicit'" --save F19.create-validation/explicit-empty
  control-automation api POST /v1/preset/improve --json "{\"name\":\"qa iv\",\"trigger\":{\"type\":\"cron\",\"schedule\":\"0 0 1 1 *\"},\"improve\":{\"builder_profile_id\":\"$BUILDER\",\"repository_source\":\"explicit\",\"repositories\":[{\"label\":\"a\",\"url\":\"https://qa.invalid/a.git\"}],\"repository_labels\":[\"a\"]}}" --expect 422 --check detail.0.msg contains 'repository_labels filters Git Sync remotes and cannot be combined'
  control-automation api POST /v1/preset/improve --json "{\"name\":\"qa iv\",\"trigger\":{\"type\":\"cron\",\"schedule\":\"0 0 1 1 *\"},\"improve\":{\"builder_profile_id\":\"$BUILDER\",\"repositories\":[{\"label\":\"a\",\"url\":\"https://qa.invalid/a.git\"}]}}" --expect 422 --check detail.0.msg contains "repositories is only used when repository_source is 'explicit'"
  control-automation api POST /v1/preset/improve --json "{\"name\":\"qa iv\",\"trigger\":{\"type\":\"cron\",\"schedule\":\"0 0 1 1 *\"},\"improve\":{\"builder_profile_id\":\"$BUILDER\",\"repository_source\":\"explicit\",\"repositories\":[{\"label\":\"a\",\"url\":\"ftp://qa.invalid/a.git\"}]}}" --expect 422 --check detail.0.msg contains 'url must start with one of: https://, git@, file://'
  control-automation api POST /v1/preset/improve --json "{\"name\":\"qa iv\",\"trigger\":{\"type\":\"cron\",\"schedule\":\"0 0 1 1 *\"},\"improve\":{\"builder_profile_id\":\"$BUILDER\",\"branch_prefix\":\"/qa/\"}}" --expect 422 --check detail.0.msg contains 'branch_prefix must be a valid git ref prefix'
  control-automation api POST /v1/preset/improve --json "{\"name\":\"qa iv\",\"trigger\":{\"type\":\"cron\",\"schedule\":\"0 0 1 1 *\"},\"improve\":{\"builder_profile_id\":\"$BUILDER\",\"branch_prefix\":\"qa improve/\"}}" --expect 422 --check detail.0.msg contains 'branch_prefix contains characters git forbids in a ref'
  control-automation api POST /v1/preset/improve --json "{\"name\":\"qa iv\",\"trigger\":{\"type\":\"cron\",\"schedule\":\"0 0 1 1 *\"},\"improve\":{\"builder_profile_id\":\"$BUILDER\",\"max_changed_files\":501}}" --expect 422 --check detail.0.msg contains 'less than or equal to 500'
  control-automation api POST /v1/preset/improve --json "{\"name\":\"qa iv\",\"trigger\":{\"type\":\"cron\",\"schedule\":\"0 0 1 1 *\"},\"improve\":{\"builder_profile_id\":\"$BUILDER\",\"nope\":1}}" --expect 422 --check detail.0.type eq extra_forbidden --check detail.0.loc.2 eq nope
  control-automation state sql "SELECT count(*) AS n FROM automations" --check rows.0.n eq "$A0"
  ```
  `repository_source` and its list are validated together: each source needs
  its own field and refuses the other's, so a program cannot say `git_sync`
  and carry an explicit list that silently never runs. The cloneable prefixes
  are `https://`, `git@` and `file://` and the refusal names them: `http://`
  is refused rather than silently upgraded, and `ssh://` is not something a
  cycle can clone, so neither is accepted at creation only to fail at 3am
  inside a run.
- **Change what a program does (`F19.config-replace`).** A whole new configuration, the rotation it produces, and the next cycle running on it.
  ```sh
  CFG=$(control-automation api POST /v1/preset/improve --json "{\"name\":\"qa improve editable\",\"trigger\":{\"type\":\"cron\",\"schedule\":\"0 0 1 1 *\"},\"improve\":{\"builder_profile_id\":\"$BUILDER\",\"repository_source\":\"explicit\",\"repositories\":[{\"label\":\"qa one\",\"url\":\"https://qa.invalid/one.git\"},{\"label\":\"qa two\",\"url\":\"https://qa.invalid/two.git\"}]}}" --expect 201 --field id)
  CFGTP=$(control-automation api GET "/v1/$CFG" --expect 200 --field tarball_path)
  control-automation api POST "/v1/improve/$CFG/claim" --json '{}' --expect 201 --check cycle.repository.label eq 'qa one' --check cycle.repository.index eq 0 --check cycle.builder_profile_id eq "$BUILDER"
  CFGU=$(control-automation fixture git-remote --name config-remote --field url)
  CFGR=$(control-automation api POST /v1/git-sync/remotes --json "{\"repo_url\":\"$CFGU\",\"label\":\"qa config\"}" --expect 201 --field id)
  control-automation api POST "/v1/git-sync/remotes/$CFGR/sync" --expect 202
  control-automation api GET "/v1/git-sync/remotes/$CFGR" --until-ok 60 --expect 200 --check sync_in_progress eq false --check dirty_count eq 0
  control-automation api PUT "/v1/improve/$CFG/config" --json "{\"builder_profile_id\":\"$REVIEWER\",\"reviewer_profile_ids\":[\"$BUILDER\"],\"repository_source\":\"explicit\",\"repositories\":[{\"label\":\"qa three\",\"url\":\"https://qa-bot:qa-token@qa.invalid/three.git\"},{\"label\":\"qa one\",\"url\":\"https://qa.invalid/one.git\"},{\"label\":\"qa two\",\"url\":\"https://qa.invalid/two.git\"}],\"focus\":\"Prefer a test for every fix\",\"open_pr\":false,\"max_changed_files\":5,\"branch_prefix\":\"qa-improve/\"}" --expect 200 --check repositories len-eq 3 --check repositories.0.label eq 'qa three' --check repositories.0.url eq https://qa.invalid/three.git --check repositories.2.index eq 2 --check next_index eq 2 --check last_cycle.repository.label eq 'qa one' --check config.builder_profile_id eq "$REVIEWER" --check config.reviewer_profile_ids.0 eq "$BUILDER" --check config.focus eq 'Prefer a test for every fix' --save F19.config-replace/put
  control-automation api GET "/v1/$CFG" --expect 200 --check preset_metadata.preset_type eq improve --check preset_metadata.improve.builder_profile_id eq "$REVIEWER" --check preset_metadata.improve.reviewer_profile_ids.0 eq "$BUILDER" --check preset_metadata.improve.repositories len-eq 3 --check preset_metadata.improve.repositories.0.url eq https://qa.invalid/three.git --check preset_metadata.improve.branch_prefix eq qa-improve/ --check preset_metadata.improve.max_changed_files eq 5 --check preset_metadata.improve.open_pr eq false --check agent_profile_id eq "$REVIEWER" --check tarball_path eq "$CFGTP" --save F19.config-replace/stored
  control-automation api GET "/v1/git-sync/remotes/$CFGR" --expect 200 --check dirty_count eq 1 --save F19.config-replace/dirty
  control-automation api DELETE "/v1/git-sync/remotes/$CFGR" --expect 204
  control-automation api POST "/v1/improve/$CFG/claim" --json '{}' --expect 201 --check cycle.repository.label eq 'qa two' --check cycle.repository.index eq 2 --check cycle.builder_profile_id eq "$REVIEWER" --check config.focus eq 'Prefer a test for every fix' --save F19.config-replace/next-cycle
  control-automation api GET "/v1/improve/$CFG/cycles" --expect 200 --check total eq 2 --check cycles.1.repository.label eq 'qa one' --check cycles.1.builder_profile_id eq "$BUILDER"
  ```
  The edit does not restart the rotation: the newest cycle took `qa one`,
  which the new list keeps at index 1, so `next_index` is 2 and the next
  claim takes `qa two` under the new builder. The program's own cycle that
  was already recorded keeps the builder it ran with. `tarball_path` is
  unchanged because a run reads its configuration from the claim, and the
  remote's `dirty_count` going from 0 back to 1 is the git sync export being
  queued; the remote is deleted again so later bullets start without one.
- **Config validation (`F19.config-validation`).** The route refuses what a create refuses, and changes nothing.
  Requires: `F19.config-replace`
  ```sh
  control-automation api PUT "/v1/improve/$CFG/config" --json '{}' --expect 422 --check detail.0.type eq missing --check detail.0.loc.1 eq builder_profile_id --save F19.config-validation/empty
  control-automation api PUT "/v1/improve/$CFG/config" --json "{\"builder_profile_id\":\"$BUILDER\",\"reviewer_profile_ids\":[\"$BUILDER\"]}" --expect 422 --check detail.0.msg contains 'the builder profile cannot also be a reviewer' --save F19.config-validation/builder-reviews-itself
  control-automation api PUT "/v1/improve/$CFG/config" --json "{\"builder_profile_id\":\"$BUILDER\",\"repository_source\":\"explicit\"}" --expect 422 --check detail.0.msg contains 'repositories must list at least one repository'
  control-automation api PUT "/v1/improve/$CFG/config" --json "{\"builder_profile_id\":\"$BUILDER\",\"branch_prefix\":\"qa improve/\"}" --expect 422 --check detail.0.msg contains 'branch_prefix contains characters git forbids in a ref'
  control-automation api PUT "/v1/improve/$CFG/config" --json "{\"builder_profile_id\":\"$BUILDER\",\"nope\":1}" --expect 422 --check detail.0.type eq extra_forbidden
  control-automation api GET "/v1/$CFG" --expect 200 --check preset_metadata.improve.builder_profile_id eq "$REVIEWER" --check preset_metadata.improve.repositories len-eq 3 --check agent_profile_id eq "$REVIEWER"
  PLAINCFG=$(control-automation fixture automation --kind echo --name 'qa plain config job' --field id)
  control-automation api PUT "/v1/improve/$PLAINCFG/config" --json "{\"builder_profile_id\":\"$BUILDER\"}" --expect 409 --check detail eq 'Not an improvement program: automation has no preset metadata' --save F19.config-validation/not-a-program
  control-automation api PUT /v1/improve/00000000-0000-0000-0000-000000000f19/config --json "{\"builder_profile_id\":\"$BUILDER\"}" --expect 404 --check detail eq 'Automation not found'
  control-automation api PUT /v1/improve/qa-not-a-uuid/config --json "{\"builder_profile_id\":\"$BUILDER\"}" --expect 422 --check detail.0.type eq uuid_parsing
  control-automation api GET "/v1/improve/$CFG/cycles" --expect 200 --check total eq 2
  ```
  The body is the configuration itself, so a refusal names its field under
  `body` and the program keeps the configuration it had: a half-applied edit
  would otherwise schedule a cast nobody asked for.
- **The rotation a dashboard reads (`F19.rotation-explicit`).** The configured repositories, in order, with nothing taken yet.
  Requires: `F19.create`
  ```sh
  control-automation api GET "/v1/improve/$AID/rotation" --expect 200 --check repositories len-eq 2 --check repositories.0.label eq 'qa alpha' --check repositories.0.url eq https://qa.invalid/alpha.git --check repositories.0.ref eq main --check repositories.0.index eq 0 --check repositories.0.remote_id eq null --check repositories.1.label eq 'qa beta' --check repositories.1.ref eq null --check repositories.1.index eq 1 --check next_index eq 0 --check last_cycle eq null --check config.builder_profile_id eq "$BUILDER" --check config.repository_source eq explicit --save F19.rotation-explicit/rotation
  control-automation api GET "/v1/improve/$AID/rotation" --check config.repositories.0.url eq https://qa.invalid/alpha.git --check config.repositories.0.url eq "$(control-automation api GET "/v1/improve/$AID/rotation" --field repositories.0.url)"
  ```
  The configuration and the rotation report the same URL, with any
  `user:password` gone from both: the credential is dropped as the
  configuration is validated, so neither the automation's metadata, the git
  sync export of it, the cycle history nor the dashboard ever holds one.
- **Claim the next repository (`F19.claim`).** One cycle row, the current configuration, the whole rotation.
  Requires: `F19.rotation-explicit`
  ```sh
  CCREATED=$(control-automation api POST "/v1/improve/$AID/claim" --json '{}' --expect 201 --check cycle.status eq CLAIMED --check cycle.verdict eq null --check cycle.run_id eq null --check cycle.repository.label eq 'qa alpha' --check cycle.repository.url eq https://qa.invalid/alpha.git --check cycle.repository.index eq 0 --check cycle.builder_profile_id eq "$BUILDER" --check cycle.branch eq null --check cycle.sources len-eq 0 --check cycle.reviews len-eq 0 --check cycle.soul_suggestions len-eq 0 --check cycle.completed_at eq null --check cycle.created_at matches 'Z$' --check config.max_changed_files eq 20 --check rotation len-eq 2 --check rotation.1.label eq 'qa beta' --save F19.claim/claim --field cycle.created_at)
  CYC=$(control-automation api GET "/v1/improve/$AID/cycles" --expect 200 --check total eq 1 --check cycles.0.created_at eq "$CCREATED" --check cycles.0.updated_at matches 'Z$' --check cycles.0.repository.label eq 'qa alpha' --save F19.claim/history --field cycles.0.id)
  ORG=$(control-automation api GET "/v1/$AID" --field org_id)
  control-automation state sql "SELECT status, repo_label, repo_index, org_id, builder_profile_id AS builder FROM automation_improve_cycle WHERE id = '$(echo "$CYC" | tr -d -)'" --check count eq 1 --check rows.0.status eq CLAIMED --check rows.0.repo_label eq 'qa alpha' --check rows.0.repo_index eq 0 --check rows.0.org_id eq "$(echo "$ORG" | tr -d -)" --check rows.0.builder eq "$(echo "$BUILDER" | tr -d -)"
  ```
  The cycle is written before any agent starts, so an interrupted run still
  leaves the repository it took in the history. The history repeats the
  claim's own `created_at` byte for byte, offset included: the run's database
  is SQLite, which drops the timezone, and a timestamp serialized without one
  is read as *local* time by the dashboard.
- **The rotation advances and wraps (`F19.claim-rotation`).** Two more claims take beta, then alpha again.
  Requires: `F19.claim`
  ```sh
  control-automation api POST "/v1/improve/$AID/claim" --json '{}' --expect 201 --check cycle.repository.label eq 'qa beta' --check cycle.repository.index eq 1 --save F19.claim-rotation/second
  control-automation api GET "/v1/improve/$AID/rotation" --check next_index eq 0 --check last_cycle.repository.label eq 'qa beta' --check last_cycle.status eq CLAIMED
  control-automation api POST "/v1/improve/$AID/claim" --json '{}' --expect 201 --check cycle.repository.label eq 'qa alpha' --check cycle.repository.index eq 0 --save F19.claim-rotation/wrap
  control-automation api GET "/v1/improve/$AID/rotation" --check next_index eq 1 --check last_cycle.repository.label eq 'qa alpha' --save F19.claim-rotation/rotation
  control-automation api GET "/v1/improve/$AID/cycles" --check total eq 3
  ```
  `next_index` is computed by the function the claim uses, so what the
  dashboard promises is what the next run takes. A cycle that is still
  `CLAIMED` already counts: the rotation moves on whatever became of it.
- **The run that owns a cycle (`F19.claim-run-id`).** A dispatched run claims; another automation's run cannot.
  Requires: `F19.claim-rotation`
  ```sh
  RUNP=$(control-automation api POST /v1/preset/improve --json "{\"name\":\"qa improve dispatched\",\"trigger\":{\"type\":\"cron\",\"schedule\":\"0 0 1 1 *\"},\"improve\":{\"builder_profile_id\":\"$BUILDER\",\"repository_source\":\"explicit\",\"repositories\":[{\"label\":\"qa dispatched\",\"url\":\"https://qa.invalid/dispatched.git\"}]}}" --expect 201 --field id)
  RID=$(control-automation api POST "/v1/$RUNP/dispatch" --expect 201 --check automation_id eq "$RUNP" --field id)
  control-automation api POST "/v1/runs/$RID/cancel" --expect 200,409
  control-automation api POST "/v1/improve/$RUNP/claim" --json "{\"run_id\":\"$RID\"}" --expect 201 --check cycle.run_id eq "$RID" --check cycle.automation_id eq "$RUNP" --check cycle.repository.label eq 'qa dispatched' --save F19.claim-run-id/claim
  OTHER=$(control-automation fixture automation --kind echo --name 'qa other job' --field id)
  ORID=$(control-automation api POST "/v1/$OTHER/dispatch" --expect 201 --field id)
  control-automation api POST "/v1/improve/$AID/claim" --json "{\"run_id\":\"$ORID\"}" --expect 400 --check detail eq 'run_id does not belong to this automation' --save F19.claim-run-id/foreign-run
  control-automation api POST "/v1/improve/$AID/claim" --json '{"run_id":"qa-not-a-uuid"}' --expect 422 --check detail.0.type eq uuid_parsing
  control-automation api GET "/v1/improve/$AID/cycles" --expect 200 --check total eq 3 --check cycles.0.run_id eq null --save F19.claim-run-id/untouched
  ```
  The claim only needs the run row, which `dispatch` creates; the cancel is
  an attempt to stop the sandbox before it starts and answers 409 when the
  run already finished, so it is not what the assertion rests on. The
  dispatch gets its own program because the run, if it does start, claims a
  cycle of its own — on that program, not on the one whose history the rest
  of the family counts. The two refused claims created nothing: the explicit
  program still has its three cycles and the newest still has no run.
- **Record the builder stage (`F19.cycle-build`).** Runtime, branch, summary and cited sources.
  Requires: `F19.claim-run-id`
  ```sh
  BCYC=$(control-automation api GET "/v1/improve/$AID/cycles" --query limit=1 --expect 200 --field cycles.0.id)
  control-automation api PATCH "/v1/improve/cycles/$BCYC" --json '{"status":"BUILDING","builder_runtime":"qa-runtime-one","builder_conversation_id":"qa-conv-1","branch":"improve/20260101-000000","summary":"Added the missing index the docs require","sources":[{"url":"https://qa.invalid/docs/indexes","title":"Index guide","note":"states the column needs an index"}]}' --expect 200 --check status eq BUILDING --check branch eq improve/20260101-000000 --check sources len-eq 1 --check sources.0.note eq 'states the column needs an index' --check completed_at eq null --save F19.cycle-build/patch
  control-automation api GET "/v1/improve/$AID/cycles" --query limit=1 --check cycles.0.status eq BUILDING --check cycles.0.builder_runtime eq qa-runtime-one --check cycles.0.builder_conversation_id eq qa-conv-1 --check cycles.0.summary eq 'Added the missing index the docs require' --check cycles.0.sources.0.url eq https://qa.invalid/docs/indexes --save F19.cycle-build/history
  control-automation api GET "/v1/improve/$AID/rotation" --check last_cycle.status eq BUILDING --check last_cycle.branch eq improve/20260101-000000
  ```
  Every field is optional, so a run reports the builder stage and the review
  stage separately and the dashboard sees the cycle move.
- **Cycle report validation (`F19.cycle-validation`).** Refused reports change nothing, and a `null` clears nothing.
  Requires: `F19.cycle-build`
  ```sh
  control-automation api PATCH "/v1/improve/cycles/$BCYC" --json '{"pr_url":"ftp://qa.invalid/pull/1"}' --expect 422 --check detail.0.msg contains 'pr_url must be an http(s) URL' --save F19.cycle-validation/pr-url
  control-automation api PATCH "/v1/improve/cycles/$BCYC" --json '{"status":"DONE"}' --expect 422 --check detail.0.type eq enum
  control-automation api PATCH "/v1/improve/cycles/$BCYC" --json '{"verdict":"LOOKS_FINE"}' --expect 422 --check detail.0.type eq enum
  control-automation api PATCH "/v1/improve/cycles/$BCYC" --json '{"nope":1}' --expect 422 --check detail.0.type eq extra_forbidden
  SRCS=$(printf '{"url":"https://qa.invalid/s/%s"},' $(seq 1 41))
  control-automation api PATCH "/v1/improve/cycles/$BCYC" --json "{\"sources\":[${SRCS%,}]}" --expect 422 --check detail.0.msg contains 'at most 40 items'
  SOULS=$(printf '{"text":"qa preference %s"},' $(seq 1 11))
  control-automation api PATCH "/v1/improve/cycles/$BCYC" --json "{\"soul_suggestions\":[${SOULS%,}]}" --expect 422 --check detail.0.msg contains 'at most 10 items'
  control-automation api PATCH "/v1/improve/cycles/$BCYC" --json '{"branch":null,"summary":null}' --expect 200 --check branch eq improve/20260101-000000 --check summary eq 'Added the missing index the docs require' --save F19.cycle-validation/null-keeps
  control-automation api GET "/v1/improve/$AID/cycles" --query limit=1 --check cycles.0.status eq BUILDING --check cycles.0.branch eq improve/20260101-000000
  ```
  The handler drops unset *and* null fields, so there is no way to unset a
  branch, a summary or an error through this route; a run that must correct
  one sends the new value.
- **Proposals for the user's SOUL.md (`F19.cycle-soul`).** What the cycle learned about the user, as proposals only.
  Requires: `F19.cycle-build`
  ```sh
  control-automation api GET "/v1/improve/$AID/cycles" --query limit=1 --check cycles.0.soul_suggestions len-eq 0
  control-automation api PATCH "/v1/improve/cycles/$BCYC" --json '{"soul_suggestions":[{"text":"Prefers a migration and its rollback in the same change","why":"asked for the down path in review"},{"text":"Wants the source cited for a performance claim"}]}' --expect 200 --check soul_suggestions len-eq 2 --check soul_suggestions.0.text eq 'Prefers a migration and its rollback in the same change' --check soul_suggestions.0.why eq 'asked for the down path in review' --check soul_suggestions.1.why eq null --save F19.cycle-soul/patch
  control-automation api GET "/v1/improve/$AID/cycles" --query limit=1 --check cycles.0.soul_suggestions.1.text eq 'Wants the source cited for a performance claim' --save F19.cycle-soul/history
  control-automation api PATCH "/v1/improve/cycles/$BCYC" --json '{"soul_suggestions":[{"text":""}]}' --expect 422 --check detail.0.type eq string_too_short
  control-automation api PATCH "/v1/improve/cycles/$BCYC" --json '{"soul_suggestions":[{"text":"qa","note":"x"}]}' --expect 422 --check detail.0.type eq extra_forbidden
  control-automation api GET "/v1/improve/$AID/cycles" --query limit=1 --check cycles.0.soul_suggestions len-eq 2
  ```
  Both stages may add to this list and the run deduplicates across them, so a
  preference two runtimes noticed is one decision for the user. The service
  stores proposals and nothing more: there is no route through which a run
  edits `SOUL.md`, which is why the whole list arrives on the cycle.
- **Record the review and finish (`F19.cycle-review`).** The reviews, the verdict and `completed_at`.
  Requires: `F19.cycle-soul`
  ```sh
  control-automation api PATCH "/v1/improve/cycles/$BCYC" --json '{"status":"REVIEWING"}' --expect 200 --check status eq REVIEWING
  control-automation api PATCH "/v1/improve/cycles/$BCYC" --json '{"status":"COMPLETED","verdict":"CHANGES_REQUESTED","pr_url":"https://qa.invalid/alpha/pull/7","reviews":[{"runtime":"qa-runtime-two","profile_name":"qa reviewer","verdict":"CHANGES_REQUESTED","summary":"The cited guide was superseded","concerns":["no regression test"],"stale_sources":["https://qa.invalid/docs/indexes"]}]}' --expect 200 --check status eq COMPLETED --check verdict eq CHANGES_REQUESTED --check pr_url eq https://qa.invalid/alpha/pull/7 --check reviews len-eq 1 --check reviews.0.runtime eq qa-runtime-two --check reviews.0.verdict eq CHANGES_REQUESTED --check reviews.0.concerns.0 eq 'no regression test' --check reviews.0.stale_sources.0 eq https://qa.invalid/docs/indexes --check completed_at exists --save F19.cycle-review/patch
  control-automation api GET "/v1/improve/$AID/cycles" --query limit=1 --check cycles.0.status eq COMPLETED --check cycles.0.verdict eq CHANGES_REQUESTED --check cycles.0.reviews.0.profile_name eq 'qa reviewer' --check cycles.0.completed_at exists --check cycles.0.sources len-eq 1 --check cycles.0.soul_suggestions len-eq 2 --save F19.cycle-review/history
  control-automation state sql "SELECT status, verdict FROM automation_improve_cycle WHERE id = '$(echo "$BCYC" | tr -d -)'" --check count eq 1 --check rows.0.status eq COMPLETED --check rows.0.verdict eq CHANGES_REQUESTED
  ```
  A reviewer's `stale_sources` is the check that keeps an existing
  implementation from drifting: it records which of the builder's sources the
  reviewer found superseded. The builder's own fields survive the review
  report, which is why they are separate PATCHes.
- **A finished cycle is history (`F19.cycle-terminal`).** `COMPLETED` and `FAILED` are immutable.
  Requires: `F19.cycle-review`
  ```sh
  control-automation api PATCH "/v1/improve/cycles/$BCYC" --json '{"summary":"a late callback"}' --expect 409 --check detail eq 'Cycle already completed' --save F19.cycle-terminal/completed
  control-automation api GET "/v1/improve/$AID/cycles" --query limit=1 --check cycles.0.summary eq 'Added the missing index the docs require'
  FCYC=$(control-automation api GET "/v1/improve/$AID/cycles" --query limit=1 --query offset=1 --expect 200 --field cycles.0.id)
  control-automation api PATCH "/v1/improve/cycles/$FCYC" --json '{"status":"FAILED","error":"clone failed: qa fixture"}' --expect 200 --check status eq FAILED --check error eq 'clone failed: qa fixture' --check completed_at exists
  control-automation api PATCH "/v1/improve/cycles/$FCYC" --json '{"status":"CLAIMED"}' --expect 409 --check detail eq 'Cycle already failed' --save F19.cycle-terminal/failed
  control-automation api PATCH /v1/improve/cycles/00000000-0000-0000-0000-000000000f19 --json '{"status":"FAILED"}' --expect 404 --check detail eq 'Cycle not found'
  control-automation api PATCH /v1/improve/cycles/qa-not-a-uuid --json '{}' --expect 422 --check detail.0.type eq uuid_parsing
  ```
  A retried or late callback cannot overwrite a recorded result, and a failed
  cycle keeps its repository in the history so the rotation still moves past
  it.
- **History, newest first (`F19.cycles-page`).** Paging, clamping and the total.
  Requires: `F19.cycle-review`
  ```sh
  control-automation api GET "/v1/improve/$AID/cycles" --expect 200 --check total eq 3 --check cycles len-eq 3 --check cycles.0.repository.label eq 'qa alpha' --check cycles.1.repository.label eq 'qa beta' --check cycles.2.repository.label eq 'qa alpha' --save F19.cycles-page/all
  control-automation api GET "/v1/improve/$AID/cycles" --query limit=2 --check total eq 3 --check cycles len-eq 2 --check cycles.0.id eq "$BCYC" --save F19.cycles-page/first-page
  control-automation api GET "/v1/improve/$AID/cycles" --query limit=2 --query offset=2 --check total eq 3 --check cycles len-eq 1 --check cycles.0.repository.label eq 'qa alpha' --save F19.cycles-page/second-page
  control-automation api GET "/v1/improve/$AID/cycles" --query limit=0 --check cycles len-eq 1 --check cycles.0.id eq "$BCYC"
  control-automation api GET "/v1/improve/$AID/cycles" --query limit=1000 --check cycles len-eq 3
  control-automation api GET "/v1/improve/$AID/cycles" --query offset=-5 --check cycles len-eq 3 --check cycles.0.id eq "$BCYC"
  control-automation api GET "/v1/improve/$AID/cycles" --query offset=99 --check total eq 3 --check cycles len-eq 0
  control-automation api GET "/v1/improve/$AID/cycles" --query limit=abc --expect 422 --check detail.0.type eq int_parsing --save F19.cycles-page/bad-limit
  ```
  `limit` is clamped into 1..100 and a negative `offset` to 0 rather than
  refused, so a paging bug in a client cannot make the history unreadable;
  only a value that is not an integer is a 422. `total` counts every cycle,
  not the page.
- **The Git Sync rotation (`F19.rotation-git-sync`).** Every remote with a repository URL, ordered by creation.
  ```sh
  U1=$(control-automation fixture git-remote --name improve-one --field url)
  U2=$(control-automation fixture git-remote --name improve-two --field url)
  U3=$(control-automation fixture git-remote --name improve-three --field url)
  U4=$(control-automation fixture git-remote --name improve-four --field url)
  control-automation api POST /v1/git-sync/remotes --json "{\"repo_url\":\"$U1\",\"label\":\"qa r1\"}" --expect 201 --check is_default eq true
  control-automation api POST /v1/git-sync/remotes --json "{\"repo_url\":\"$U2\",\"label\":\"qa r2\",\"branch\":\"trunk\"}" --expect 201 --check is_default eq false
  control-automation api POST /v1/git-sync/remotes --json "{\"repo_url\":\"$U3\",\"label\":\"qa r3\"}" --expect 201
  control-automation api POST /v1/git-sync/remotes --json "{\"repo_url\":\"$U4\",\"label\":\"qa r4\"}" --expect 201
  GID=$(control-automation api POST /v1/preset/improve --json "{\"name\":\"qa improve git sync\",\"trigger\":{\"type\":\"cron\",\"schedule\":\"0 0 1 1 *\"},\"improve\":{\"builder_profile_id\":\"$BUILDER\",\"reviewer_profile_ids\":[\"$REVIEWER\"],\"open_pr\":false,\"max_changed_files\":3,\"branch_prefix\":\"qa-improve/\",\"focus\":\"Prefer documentation fixes\"}}" --expect 201 --check preset_metadata.improve.repository_source eq git_sync --check preset_metadata.improve.open_pr eq false --check preset_metadata.improve.focus eq 'Prefer documentation fixes' --save F19.rotation-git-sync/program --field id)
  G0=$(control-automation api GET "/v1/improve/$GID/rotation" --expect 200 --check repositories len-eq 4 --check next_index eq 0 --save F19.rotation-git-sync/rotation --field repositories.0.label)
  G1=$(control-automation api GET "/v1/improve/$GID/rotation" --field repositories.1.label)
  G2=$(control-automation api GET "/v1/improve/$GID/rotation" --field repositories.2.label)
  G3=$(control-automation api GET "/v1/improve/$GID/rotation" --field repositories.3.label)
  GR0=$(control-automation api GET "/v1/improve/$GID/rotation" --field repositories.0.remote_id)
  GR1=$(control-automation api GET "/v1/improve/$GID/rotation" --field repositories.1.remote_id)
  GR2=$(control-automation api GET "/v1/improve/$GID/rotation" --field repositories.2.remote_id)
  GR3=$(control-automation api GET "/v1/improve/$GID/rotation" --field repositories.3.remote_id)
  for i in 0 1 2 3; do control-automation state sql "SELECT label FROM automation_git_sync_remote ORDER BY created_at ASC, id ASC LIMIT 1 OFFSET $i" --field rows.0.label; done > "$AUTOMATION_VERIFY_RUN/qa-remote-order.txt"
  test "$(cat "$AUTOMATION_VERIFY_RUN/qa-remote-order.txt" | paste -sd, -)" = "$G0,$G1,$G2,$G3"
  control-automation api GET "/v1/improve/$GID/rotation" --check repositories.1.url eq "$(control-automation api GET "/v1/git-sync/remotes/$GR1" --field repo_url)" --check repositories.1.ref eq "$(control-automation api GET "/v1/git-sync/remotes/$GR1" --field branch)"
  control-automation api PUT "/v1/git-sync/remotes/$GR3" --json '{"is_default":true}' --expect 200 --check is_default eq true
  control-automation api GET /v1/git-sync/remotes --expect 200 --check remotes.0.id eq "$GR3" --save F19.rotation-git-sync/default-first
  control-automation api GET "/v1/improve/$GID/rotation" --check repositories.0.label eq "$G0" --check repositories.3.label eq "$G3" --save F19.rotation-git-sync/default-unmoved
  control-automation api PUT "/v1/git-sync/remotes/$GR2" --json '{"enabled":false}' --expect 200 --check enabled eq false
  control-automation api GET "/v1/improve/$GID/rotation" --check repositories len-eq 4 --check repositories.2.label eq "$G2"
  control-automation api PUT "/v1/git-sync/remotes/$GR2" --json '{"enabled":true}' --expect 200 --check enabled eq true
  URL2=$(control-automation api GET "/v1/git-sync/remotes/$GR2" --field repo_url)
  control-automation api PUT "/v1/git-sync/remotes/$GR2" --json '{"repo_url":""}' --expect 200 --check repo_url eq ''
  control-automation api GET "/v1/improve/$GID/rotation" --check repositories len-eq 3 --check repositories.2.label eq "$G3" --save F19.rotation-git-sync/url-cleared
  control-automation api PUT "/v1/git-sync/remotes/$GR2" --json "{\"repo_url\":\"$URL2\",\"enabled\":true}" --expect 200 --check repo_url eq "$URL2"
  control-automation api GET "/v1/improve/$GID/rotation" --check repositories len-eq 4 --check repositories.2.label eq "$G2"
  ```
  The order is the remotes' own `(created_at, id)`, *not* the default-first
  order the Git Sync page shows: which repository is the default changes as
  the user works and must not reshuffle a program underneath it. Pausing a
  remote (`enabled: false`) does not take it out of the rotation — only a
  remote with no repository URL drops out, which is how a half-configured row
  is skipped instead of failing the claim.
- **Only the labelled repositories (`F19.rotation-labels`).** A filtered rotation, and the claim follows it.
  Requires: `F19.rotation-git-sync`
  ```sh
  LID=$(control-automation api POST /v1/preset/improve --json "{\"name\":\"qa improve labels\",\"trigger\":{\"type\":\"cron\",\"schedule\":\"0 0 1 1 *\"},\"improve\":{\"builder_profile_id\":\"$BUILDER\",\"repository_labels\":[\"$G1\",\"qa label nothing has\"]}}" --expect 201 --check preset_metadata.improve.repository_labels len-eq 2 --field id)
  control-automation api GET "/v1/improve/$LID/rotation" --expect 200 --check repositories len-eq 1 --check repositories.0.label eq "$G1" --check repositories.0.index eq 0 --check next_index eq 0 --save F19.rotation-labels/rotation
  control-automation api POST "/v1/improve/$LID/claim" --json '{}' --expect 201 --check cycle.repository.label eq "$G1" --check rotation len-eq 1 --save F19.rotation-labels/claim
  control-automation api POST "/v1/improve/$LID/claim" --json '{}' --expect 201 --check cycle.repository.label eq "$G1" --check cycle.repository.index eq 0
  control-automation api GET "/v1/improve/$LID/cycles" --check total eq 2
  ```
  A label no remote carries is ignored rather than refused, so a renamed
  repository narrows a program instead of breaking it; with one entry left
  the rotation keeps returning it.
- **Nothing to rotate over (`F19.rotation-empty`).** A configured program with an empty rotation is a clean no-op.
  ```sh
  EID=$(control-automation api POST /v1/preset/improve --json "{\"name\":\"qa improve empty\",\"trigger\":{\"type\":\"cron\",\"schedule\":\"0 0 1 1 *\"},\"improve\":{\"builder_profile_id\":\"$BUILDER\",\"repository_labels\":[\"qa no such repository\"]}}" --expect 201 --field id)
  control-automation api GET "/v1/improve/$EID/rotation" --expect 200 --check repositories len-eq 0 --check next_index eq null --check last_cycle eq null --check config.repository_labels.0 eq 'qa no such repository' --save F19.rotation-empty/rotation
  control-automation api POST "/v1/improve/$EID/claim" --json '{}' --expect 409 --check detail.code eq no_repositories --check detail.message eq 'the program has no repositories to rotate over' --save F19.rotation-empty/claim
  control-automation api GET "/v1/improve/$EID/cycles" --expect 200 --check total eq 0 --check cycles len-eq 0
  ```
  The run reads this 409 as "nothing to do" and still completes, so a program
  created before its repositories exist does not fail every night. The detail
  is a typed object rather than a sentence because `no_repositories` is only
  one of the clean no-ops a claim answers with: a run that could read prose
  alone could not tell "add a repository" from "this one is done for today".
- **Continuing from the recorded position (`F19.claim-index-fallback`).** The repository the last cycle took is gone.
  Requires: `F19.rotation-git-sync`
  ```sh
  control-automation api POST "/v1/improve/$GID/claim" --json '{}' --expect 201 --check cycle.repository.label eq "$G0" --check cycle.repository.index eq 0
  control-automation api POST "/v1/improve/$GID/claim" --json '{}' --expect 201 --check cycle.repository.label eq "$G1" --check cycle.repository.index eq 1 --save F19.claim-index-fallback/before
  control-automation api DELETE "/v1/git-sync/remotes/$GR1" --expect 204
  control-automation api GET "/v1/improve/$GID/rotation" --check repositories len-eq 3 --check repositories.0.label eq "$G0" --check repositories.1.label eq "$G2" --check repositories.2.label eq "$G3" --check next_index eq 1 --check last_cycle.repository.label eq "$G1" --check last_cycle.repository.remote_id eq null --save F19.claim-index-fallback/rotation
  control-automation api POST "/v1/improve/$GID/claim" --json '{}' --expect 201 --check cycle.repository.label eq "$G2" --check cycle.repository.index eq 1 --save F19.claim-index-fallback/claim
  ```
  The deleted repository sat at index 1 and its successor shifted into that
  slot, so the next claim takes index 1 again — the position, with no `+1`,
  because the removal already did the advancing. Losing a repository neither
  sends the rotation back to the top nor skips the entry that replaced it. The
  cycle keeps the repository's label and URL after its remote is gone; only
  `remote_id` is cleared with it.
- **Continuing after the same label (`F19.claim-label-match`).** The rotation shrinks behind the last cycle.
  Requires: `F19.claim-index-fallback`
  ```sh
  control-automation api DELETE "/v1/git-sync/remotes/$GR3" --expect 204
  control-automation api GET "/v1/improve/$GID/rotation" --check repositories len-eq 2 --check repositories.0.label eq "$G0" --check repositories.1.label eq "$G2" --check next_index eq 0 --check last_cycle.repository.label eq "$G2" --check last_cycle.repository.index eq 1 --save F19.claim-label-match/rotation
  control-automation api POST "/v1/improve/$GID/claim" --json '{}' --expect 201 --check cycle.repository.label eq "$G0" --check cycle.repository.index eq 0 --save F19.claim-label-match/claim
  control-automation api GET "/v1/improve/$GID/cycles" --check total eq 4 --check cycles.0.repository.label eq "$G0" --check cycles.1.repository.label eq "$G2"
  ```
  The last cycle's repository is still there and still at index 1, but the
  rotation now ends with it, so the claim after it wraps to index 0. Falling
  back to the recorded position instead of matching the label would have
  handed it the same repository twice in a row.
- **Not an improvement program (`F19.not-a-program`).** Every automation-scoped route refuses, with the reason.
  ```sh
  PLAIN=$(control-automation fixture automation --kind echo --name 'qa plain job' --field id)
  control-automation api POST "/v1/improve/$PLAIN/claim" --json '{}' --expect 409 --check detail eq 'Not an improvement program: automation has no preset metadata' --save F19.not-a-program/claim
  control-automation api GET "/v1/improve/$PLAIN/rotation" --expect 409 --check detail eq 'Not an improvement program: automation has no preset metadata'
  control-automation api GET "/v1/improve/$PLAIN/cycles" --expect 409 --check detail eq 'Not an improvement program: automation has no preset metadata'
  PROMPT=$(control-automation api POST /v1/preset/prompt --json '{"name":"qa prompt not improve","prompt":"hi","trigger":{"type":"cron","schedule":"0 0 1 1 *"}}' --expect 201 --field id)
  control-automation api GET "/v1/improve/$PROMPT/rotation" --expect 409 --check detail eq 'Not an improvement program: automation is not an improvement program' --save F19.not-a-program/prompt-preset
  control-automation api GET /v1/improve/00000000-0000-0000-0000-000000000f19/rotation --expect 404 --check detail eq 'Automation not found'
  control-automation api POST /v1/improve/00000000-0000-0000-0000-000000000f19/claim --json '{}' --expect 404 --check detail eq 'Automation not found'
  control-automation api GET /v1/improve/qa-not-a-uuid/cycles --expect 422 --check detail.0.type eq uuid_parsing --check detail.0.loc.1 eq automation_id
  control-automation state sql "SELECT count(*) AS n FROM automation_improve_cycle WHERE automation_id = '$(echo "$PLAIN" | tr -d -)'" --check rows.0.n eq 0
  ```
  409 rather than 404 is the useful answer: the automation exists and the
  caller may read it, it simply is not a program, which is what a run needs
  to tell apart from a rotation that is merely empty.
- **Auth (`F19.auth`).** No key, no program and no cycle.
  Requires: `F19.create`
  ```sh
  N0=$(control-automation state sql "SELECT count(*) AS n FROM automation_improve_cycle" --field rows.0.n)
  A1=$(control-automation state sql "SELECT count(*) AS n FROM automations" --field rows.0.n)
  control-automation api POST /v1/preset/improve --auth none --json "{\"name\":\"qa improve unauth\",\"trigger\":{\"type\":\"cron\",\"schedule\":\"0 0 1 1 *\"},\"improve\":{\"builder_profile_id\":\"$BUILDER\"}}" --expect 401 --save F19.auth/preset
  control-automation api POST "/v1/improve/$AID/claim" --auth none --json '{}' --expect 401 --save F19.auth/claim
  control-automation api PATCH /v1/improve/cycles/00000000-0000-0000-0000-000000000f19 --auth none --json '{"status":"FAILED"}' --expect 401
  control-automation api PUT "/v1/improve/$AID/config" --auth none --json "{\"builder_profile_id\":\"$BUILDER\"}" --expect 401 --save F19.auth/config
  control-automation api GET "/v1/improve/$AID/rotation" --auth none --expect 401
  control-automation api GET "/v1/improve/$AID/cycles" --auth none --expect 401
  control-automation api GET "/v1/improve/$AID/rotation" --auth bad --expect 401 --check detail eq 'Invalid API key'
  control-automation api POST "/v1/improve/$AID/claim" --auth bad --json '{}' --expect 401 --check detail eq 'Invalid API key'
  control-automation api PUT "/v1/improve/$AID/config" --auth bad --json "{\"builder_profile_id\":\"$BUILDER\"}" --expect 401 --check detail eq 'Invalid API key'
  control-automation api GET "/v1/$AID" --expect 200 --check preset_metadata.improve.builder_profile_id eq "$BUILDER" --check preset_metadata.improve.repositories len-eq 2
  control-automation state sql "SELECT count(*) AS n FROM automation_improve_cycle" --check rows.0.n eq "$N0"
  control-automation state sql "SELECT count(*) AS n FROM automations" --check rows.0.n eq "$A1"
  control-automation api GET "/v1/improve/$AID/rotation" --expect 200 --check repositories len-eq 2
  ```
  The same key the run callbacks use is the one these routes take, in header
  or bearer form (F02); the last two calls are the positive control that the
  key itself still works and that the refused config writes left the
  program's own configuration untouched.
- **A full cycle run (`F19.cycle-run`), blocked: needs two agent runtimes.** A dispatched program clones the claimed repository, has the builder profile implement one improvement on a `branch_prefix` branch with cited sources, and then has a profile on a *different* runtime review it, ending `COMPLETED` with a verdict (`SINGLE_RUNTIME` when no second runtime exists, `NO_CHANGE` when the builder decided against a change). It needs two agent profiles on different runtimes with their provider keys on the Agent Server, plus `pypi` for `setup.sh`; `llm preset deepseek` configures one model, not two runtimes. Drive it with `api POST /v1/$GID/dispatch` and `run wait` once such a pair exists, and read the result from `GET /v1/improve/$GID/cycles`.

- **Repackage a program's runner (`F19.refresh-code`).** A service upgrade
  reaches an existing program without an edit to its configuration.
  Requires: `F19.create`
  ```sh
  control-automation api POST "/v1/improve/$AID/refresh-code" --expect 200 --check tarball_path matches '^oh-internal://uploads/[0-9a-f-]{36}$'
  ```

- **Read the usage ledger (`F19.usage-ledger`, `F19.usage-unknown`).** Nothing
  has been observed yet, which is its own answer rather than an error.
  ```sh
  control-automation api GET /v1/improve/runtime-usage --expect 200 --check runtimes len-eq 0 --check earliest_reset eq null
  ```

- **How evenly the building was divided (`F19.builder-share`, `F19.builder-share-not-yet`).** One
  runtime holding everything, then a second runtime taking most of it, and the
  verdict withheld while the window is longer than the history.
  Requires: `F19.cycle-build`
  ```sh
  control-automation api GET /v1/improve/builder-share --query days=7 --expect 200 --check window_days eq 7 --check days_covered eq 1 --check cycles eq 1 --check runtimes len-eq 1 --check runtimes.0.runtime eq qa-runtime-one --check runtimes.0.cycles eq 1 --check runtimes.0.share eq 1.0 --check evenness_gap eq 0.0 --check target_gap eq 20.0 --check within_target eq null --save F19.builder-share-not-yet/week
  BS1=$(control-automation api POST "/v1/improve/$AID/claim" --json '{}' --expect 201 --field cycle.id)
  control-automation api PATCH "/v1/improve/cycles/$BS1" --json '{"status":"BUILDING","builder_runtime":"qa-runtime-two"}' --expect 200 --check builder_runtime eq qa-runtime-two
  control-automation api GET /v1/improve/builder-share --query days=1 --expect 200 --check window_days eq 1 --check days_covered eq 1 --check cycles eq 2 --check runtimes len-eq 2 --check runtimes.0.share eq 0.5 --check runtimes.1.share eq 0.5 --check evenness_gap eq 0.0 --check within_target eq true --save F19.builder-share/even
  BS2=$(control-automation api POST "/v1/improve/$AID/claim" --json '{}' --expect 201 --field cycle.id)
  control-automation api PATCH "/v1/improve/cycles/$BS2" --json '{"status":"BUILDING","builder_runtime":"qa-runtime-two"}' --expect 200
  BS3=$(control-automation api POST "/v1/improve/$AID/claim" --json '{}' --expect 201 --field cycle.id)
  control-automation api PATCH "/v1/improve/cycles/$BS3" --json '{"status":"BUILDING","builder_runtime":"qa-runtime-two"}' --expect 200
  control-automation api GET /v1/improve/builder-share --query days=1 --expect 200 --check cycles eq 4 --check runtimes len-eq 2 --check runtimes.0.runtime eq qa-runtime-two --check runtimes.0.cycles eq 3 --check runtimes.0.share eq 0.75 --check runtimes.1.runtime eq qa-runtime-one --check runtimes.1.share eq 0.25 --check evenness_gap eq 25.0 --check within_target eq false --save F19.builder-share/lopsided
  control-automation api GET /v1/improve/builder-share --query days=7 --expect 200 --check days_covered eq 1 --check cycles eq 4 --check evenness_gap eq 25.0 --check within_target eq null --save F19.builder-share-not-yet/uncovered
  control-automation api GET /v1/improve/builder-share --query days=0 --expect 200 --check window_days eq 1 --check evenness_gap eq 25.0 --check within_target eq false
  control-automation api GET /v1/improve/builder-share --query days=abc --expect 422 --check detail.0.type eq int_parsing
  ```
  The share is read off the cycles rather than the ledger, whose counters are
  per day and roll over: the runtime of record on a cycle is the one that
  actually ran, so a cycle that failed over mid-change counts for whichever
  runtime finished it. Three cycles against one is 75/25 where an even split
  is 50, so the gap is 25 points and the answer is `within_target: false` —
  the verdict, not a bare number for the reader to judge against a threshold
  they would have to know. The same gap asked of a week comes back with
  `within_target: null` and `days_covered: 1`, because every cycle a replay
  creates was created today; only the verdict is withheld, the measurement is
  still reported. `days` is clamped rather than refused, so `days=0` asks
  about today, and only a value that is not an integer is a 422.

- **One program's rotation, not the whole machine's (`F19.builder-share-scoped`).**
  The same cycles, asked about the org and about the program that ran them.
  Requires: `F19.builder-share`
  ```sh
  SID=$(control-automation api POST /v1/preset/improve --json "{\"name\":\"qa improve rotating builder\",\"trigger\":{\"type\":\"cron\",\"schedule\":\"0 0 1 1 *\"},\"improve\":{\"builder_profile_id\":\"$BUILDER\",\"rotate_builder\":[\"$REVIEWER\",\"33333333-3333-4333-8333-333333333333\"],\"repository_source\":\"explicit\",\"repositories\":[{\"label\":\"qa gamma\",\"url\":\"https://qa.invalid/gamma.git\"},{\"label\":\"qa delta\",\"url\":\"https://qa.invalid/delta.git\"}]}}" --expect 201 --check preset_metadata.improve.rotate_builder len-eq 2 --field id)
  SC1=$(control-automation api POST "/v1/improve/$SID/claim" --json '{}' --expect 201 --field cycle.id)
  control-automation api PATCH "/v1/improve/cycles/$SC1" --json '{"status":"BUILDING","builder_runtime":"qa-runtime-one"}' --expect 200
  SC2=$(control-automation api POST "/v1/improve/$SID/claim" --json '{}' --expect 201 --field cycle.id)
  control-automation api PATCH "/v1/improve/cycles/$SC2" --json '{"status":"BUILDING","builder_runtime":"qa-runtime-one"}' --expect 200
  control-automation api GET /v1/improve/builder-share --query days=1 --query "automation_id=$SID" --expect 200 --check automation_id eq "$SID" --check configured_builders eq 3 --check cycles eq 2 --check runtimes len-eq 1 --check runtimes.0.runtime eq qa-runtime-one --check runtimes.0.share eq 1.0 --check evenness_gap eq 66.7 --check within_target eq false --save F19.builder-share-scoped/one-of-three
  control-automation api GET /v1/improve/builder-share --query days=1 --expect 200 --check automation_id eq null --check configured_builders eq null --check cycles eq 6 --check runtimes len-eq 2 --check evenness_gap eq 0.0 --check within_target eq true --save F19.builder-share-scoped/org-wide
  control-automation api PUT "/v1/improve/$SID/config" --json "{\"builder_profile_id\":\"$BUILDER\",\"repository_source\":\"explicit\",\"repositories\":[{\"label\":\"qa gamma\",\"url\":\"https://qa.invalid/gamma.git\"},{\"label\":\"qa delta\",\"url\":\"https://qa.invalid/delta.git\"}]}" --expect 200 --check config.rotate_builder len-eq 0
  control-automation api GET /v1/improve/builder-share --query days=1 --query "automation_id=$SID" --expect 200 --check configured_builders eq 1 --check cycles eq 2 --check evenness_gap eq 0.0 --check within_target eq true --save F19.builder-share-scoped/one-builder
  control-automation api GET /v1/improve/builder-share --query days=1 --query automation_id=00000000-0000-0000-0000-000000000f19 --expect 404 --check detail eq 'Automation not found'
  control-automation api GET /v1/improve/builder-share --query days=1 --query automation_id=qa-not-a-uuid --expect 422 --check detail.0.type eq uuid_parsing
  ```
  Two cycles, both built by `qa-runtime-one`, on a program configured to
  rotate three. Scoped that is a 66.7-point gap and `within_target: false`,
  because the denominator is the larger of the runtimes seen and
  `configured_builders`: a configured runtime that never builds widens the gap
  by its own absence, which is the collapse the rotation exists to prevent.
  The *same* cycles read unscoped land on the other side of the verdict —
  three each for two runtimes across both programs, `evenness_gap: 0.0`, the
  machine reading as perfectly even while one of its programs runs on a third
  of its rotation. That is what the parameter is for, and why both
  `automation_id` and `configured_builders` come back null when nobody asked
  about a program: there is then no configuration for "even" to mean anything
  against. Replacing the configuration without `rotate_builder` leaves those
  two cycles at `evenness_gap: 0.0` and `configured_builders: 1`, so the
  denominator is the configuration rather than the history and a program that
  only ever had one builder is not failing a rotation it never asked for.
  Scoping asks about a program, so an id that is not one is answered like the
  program routes (404, and 422 for an id that is not a UUID) instead of
  quietly widening back to the org.

- **Park a change and answer it (`F19.parked-list`, `F19.resolve`, `F19.resolve-not-escalate`, `F19.resolve-not-parked`, `F19.gate-log`).**
  Requires: `F19.create`
  ```sh
  PCID=$(control-automation api POST "/v1/improve/$AID/claim" --json '{}' --expect 201 --field cycle.id)
  control-automation api PATCH "/v1/improve/cycles/$PCID" --json '{"status":"PARKED","change_class":"breaking","branch":"improve/envelope","parked_phase":"publish","parked_question":"Publish the rename now, or wait?","parked_plan":{"breaks":["the 1.55 client"],"rollback":"revert the branch"},"gate_decisions":[{"phase":"publish","route":"escalate","trigger":"the diff is a breaking change","rationale":"a wire contract changed"},{"phase":"publish","route":"merge-it","rationale":"nonsense"}]}' --expect 200 --check gate_decisions len-eq 2 --check gate_decisions.1.unreadable eq true
  control-automation api GET /v1/improve/parked --expect 200 --check total eq 1 --check cycles.0.parked_plan.rollback matches 'revert the branch'
  control-automation api POST "/v1/improve/cycles/$PCID/resolve" --json '{"route":"escalate","resolution":"ask someone else"}' --expect 422
  control-automation api POST "/v1/improve/cycles/$PCID/resolve" --json '{"route":"approve","resolution":"The client release is out; publish it."}' --expect 200 --check status eq REVIEWING --check resolution matches 'client release'
  control-automation api POST "/v1/improve/cycles/$PCID/resolve" --json '{"route":"approve","resolution":"again"}' --expect 409
  ```

- **A deletion needs a signature that is not the author's (`F19.attest`, `F19.attest-not-the-author`, `F19.authority`, `F19.change-class`).**
  Requires: `F19.create`
  ```sh
  DCID=$(control-automation api POST "/v1/improve/$AID/claim" --json '{}' --expect 201 --field cycle.id)
  control-automation api PATCH "/v1/improve/cycles/$DCID" --json '{"status":"REVIEWING","builder_runtime":"claude-code","change_class":"additive","change_evidence":{"reasons":[{"rule":"removed_export","path":"a.py","detail":"-def gone():"}]},"head_sha":"a1b2c3d","changed_files":["a.py"],"checks":[{"command":"pytest","passed":true}],"summary":"removed the unused resolver"}' --expect 200 --check change_class eq breaking --check claimed_change_class eq null
  control-automation api GET "/v1/improve/cycles/$DCID/authority" --expect 200 --check authorised eq false --check human_only eq true --check route eq escalate
  control-automation api PATCH "/v1/improve/cycles/$DCID" --json '{"change_class":"deleting","change_evidence":{"reasons":[{"rule":"file_deleted","path":"a.py","detail":"the whole file was removed"}]}}' --expect 200 --check change_class eq deleting
  control-automation api GET "/v1/improve/cycles/$DCID/authority" --expect 200 --check authorised eq false --check missing.0 eq verdict --check reason matches 'No review has been recorded'
  control-automation api PATCH "/v1/improve/cycles/$DCID" --json '{"verdict":"ACCEPTED"}' --expect 200 --check verdict eq ACCEPTED --check completed_at eq null
  control-automation api GET "/v1/improve/cycles/$DCID/authority" --expect 200 --check authorised eq false --check missing.0 eq judge_attestation
  control-automation api POST "/v1/improve/cycles/$DCID/attest" --json '{"attested":true,"runtime":"claude-code","summary":"looks dead","removals":[{"path":"a.py","finding":"dead","where":"grep returns only the definition"}]}' --expect 409
  control-automation api POST "/v1/improve/cycles/$DCID/attest" --json '{"attested":true,"runtime":"codex","summary":"all fine"}' --expect 422
  control-automation api POST "/v1/improve/cycles/$DCID/attest" --json '{"attested":true,"runtime":"codex","summary":"nothing imports it","removals":[{"path":"a.py","finding":"dead","where":"grep returns only the definition"}]}' --expect 200 --check judge_attestation.runtime eq codex
  control-automation api GET "/v1/improve/cycles/$DCID/authority" --expect 200 --check authorised eq true --check reason matches codex
  ```
  The gates answer in their own order, and the recipe walks it: a breaking
  change is refused before anything is asked of the change, and once the class
  is a deletion the next thing standing in the way is the review rather than
  the judge. So the cycle records `ACCEPTED` first — a verdict alone leaves
  `completed_at` null, because only a terminal `status` finishes a cycle —
  and the attestation is then the only thing left.

- **Evidence that was never produced is a proposal, not a merge (`F19.authority-evidence`).**
  Requires: `F19.create`
  ```sh
  ECID=$(control-automation api POST "/v1/improve/$AID/claim" --json '{}' --expect 201 --field cycle.id)
  control-automation api PATCH "/v1/improve/cycles/$ECID" --json '{"status":"REVIEWING","builder_runtime":"codex","change_class":"additive","verdict":"ACCEPTED","head_sha":"b2c3d4e","changed_files":["b.py"],"summary":"added a thing"}' --expect 200
  control-automation api GET "/v1/improve/cycles/$ECID/authority" --expect 200 --check authorised eq false --check missing.0 eq checks --check reason matches proposal
  control-automation api PATCH "/v1/improve/cycles/$ECID" --json '{"checks":[{"command":"npm run lint","passed":false}]}' --expect 200
  control-automation api GET "/v1/improve/cycles/$ECID/authority" --expect 200 --check route eq cycle --check reason matches 'did not pass'
  ```

## Gotchas

- `PUT .../config` is a replacement, not a patch: a field left out of the
  body goes back to its default (no reviewers, `improve/`, 20 changed files,
  `open_pr: true`, no focus), so send the whole configuration, which is what
  `GET .../rotation` hands back in `config`.
- `PATCH /v1/{id}` with `agent_profile_id` changes the automation's profile
  but not `preset_metadata.improve.builder_profile_id`, and a run builds with
  the one from its claim: after such a PATCH the dashboard's profile is not
  the one that builds. Only `PUT .../config` keeps the two in step. Likewise
  `PATCH /v1/{id}` with `prompt` answers 200 and stores a `prompt` nothing in
  this preset reads, and regenerates nothing.
- In local mode the create route accepts any profile UUID: profiles are
  resolved by the Agent Server at run time, so `ensure_agent_profile_exists`
  returns immediately (in cloud mode an unknown id is a 422). A program can
  therefore be created against profiles that do not exist, and the failure
  only shows up in the run.
- The rotation has no cursor row. Deleting cycle history rewinds the
  rotation, and two overlapping runs of the same program are serialized by a
  row lock on the automation — which SQLite ignores, so this run can never
  show the lock working.
- A claim consumes a rotation slot even when the run that made it dies
  immediately, and a `CLAIMED` row is enough to advance the next claim. Do not
  re-run a claim bullet to "retry": re-read `GET .../cycles` instead.
- A soft-deleted program keeps answering: `DELETE /v1/{id}` only marks the
  automation, and these routes look it up without that filter, so once
  `GET /v1/{id}` is a 404 the rotation and the history are still readable and
  a claim still succeeds. Nothing dispatches the program any more, so only a
  caller holding its id can reach that state.
- `GET .../rotation` on a `git_sync` program reads the Git Sync remotes on
  every call, so a program's rotation changes under it whenever someone adds,
  relabels or clears a repository on the Git Sync page (F18). Capture the
  labels, as these recipes do, instead of assuming the order the remotes were
  created in: SQLite stores `created_at` to the second, so remotes created in
  the same second are ordered by their random id.
- `GET /v1/improve/builder-share` asked without `automation_id` divides by
  the runtimes that *built* something in the window, not by the runtimes a
  program is configured with, so one runtime holding every cycle reads as
  `evenness_gap: 0.0` and, once the window is covered, `within_target: true` —
  the first reading in the builder-share bullet is exactly that shape. Read
  `runtimes` beside the gap, as that bullet does: a rotation that has
  collapsed to a single runtime is the thing the program exists to fix, and
  the gap alone calls it even. Ask with `?automation_id=` for the reading that
  divides by `configured_builders` when that is the larger number, which is
  the only one that can see that collapse — and the only one that leaves out
  the org's other programs, its disabled ones and this harness's own cycles.
- `days_covered` counts from the oldest cycle inside the window, so every
  cycle a replay creates is "today" and a seven-day verdict is unreachable in
  one run; ask for `days=1` to see `within_target` resolve. `days` is clamped
  into 1..365 rather than refused, so `days=0` answers about today and
  `days=400` about a year.
