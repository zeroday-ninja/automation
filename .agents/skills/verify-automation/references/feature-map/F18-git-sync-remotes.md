# Git sync remotes and assignments

An org syncs its automations to any number of git repositories, each a
*remote* (`automation_git_sync_remote`) with its own repository URL, branch,
path, credentials, interval, status and manual trigger. Exactly one remote
per org is the *default*: the org's first remote, or any created or updated
with `is_default: true`. Automations that no remote owns (created before the
org had a default, or left behind by a deleted remote) are adopted by the
default on its next cycle; every other remote holds only the automations
imported from its repository or moved to it through the assignments API.
`PUT /assignments/{automation_id}` moves one automation: the old remote's
next cycle deletes `<path>/<slug>/` from its repository and the new one's
exports it. The single-repo routes of F13 (`/status`, `/config`, `/check`,
`/sync`) act on the default remote, and `PUT /config` creates it on first
save, so a client that predates remotes keeps working. Recipes use local
bare repositories (`fixture git-remote`), which drive the same `git` CLI
code path as hosted ones, translated from "GitHub" to "a git remote".

Source: `openhands/automation/git_sync/`, `openhands/automation/models.py`, `migrations/versions/034_git_sync_remotes.py`

Needs: `git`

Routes: `GET /api/automation/v1/git-sync/remotes`, `POST /api/automation/v1/git-sync/remotes`,
`POST /api/automation/v1/git-sync/remotes/check`, `GET /api/automation/v1/git-sync/remotes/{remote_id}`,
`PUT /api/automation/v1/git-sync/remotes/{remote_id}`, `DELETE /api/automation/v1/git-sync/remotes/{remote_id}`,
`POST /api/automation/v1/git-sync/remotes/{remote_id}/check`, `POST /api/automation/v1/git-sync/remotes/{remote_id}/sync`,
`GET /api/automation/v1/git-sync/assignments`, `PUT /api/automation/v1/git-sync/assignments/{automation_id}`

## Sub-features

- `F18.list-empty`: with no remote, `GET /remotes` answers `{remotes:[], unassigned_count:N}` counting the org's live automations, and `GET /assignments` lists each of them with `remote_id`, `effective_remote_id` and (without a state row) `slug` null and `dirty:true`; every route here answers 401 without a key.
- `F18.check-new`: `POST /remotes/check` tests a repository before it is added: `{}` answers 200 `{ok:false, detail:"No repository URL is configured."}`, an empty bare repo `{ok:true, branch_exists:false}`, a missing path `ok:false` with git's failure; nothing is saved.
- `F18.create-default`: `POST /remotes` without `repo_url` answers 422; the org's first remote answers 201 with `is_default:true`, `enabled:true` and an `automation_count` that already includes the unassigned automations it will adopt; `GET /remotes/{id}`, `GET /remotes` and the legacy `GET /status` all read it back, and the caller is stored as its configuring user.
- `F18.create-secondary`: a second `POST /remotes` is not the default (`is_default:false`, `automation_count:0`), lists after it, and leaves the legacy status and the unassigned automations' `effective_remote_id` on the default.
- `F18.repo-conflict`: `POST /remotes`, `PUT /remotes/{id}` and `PUT /config` answer 409 `Another repository configuration already syncs this repository, branch and path...` when any other remote already syncs that repository, branch and path (URL compared case-insensitively, without trailing `/` or `.git`), and change nothing.
- `F18.create-as-default`: `POST /remotes` with `is_default:true` makes the new remote the default (a different `path` on an already-synced repository does not collide); deleting the default leaves the org with none, so `GET /status` reads unconfigured and `effective_remote_id` is null until another remote is promoted.
- `F18.default-promote`: `PUT /remotes/{id}` with `is_default:false` on the default answers 422; `is_default:true` on another remote promotes it, the legacy status follows, an automation created meanwhile is assigned to the new default, and promoting the old one back moves nothing.
- `F18.adopt-unassigned`: `POST /remotes/{id}/sync` on the default answers 202 `{triggered:true}`; the cycle adopts the unassigned automations (`remote_id` set, `dirty:false`, `unassigned_count:0`), exports them to that repository only, and clones into the run's `workspaces/git-sync/<remote id>`.
- `F18.update-remote`: `PUT /remotes/{id}` renames, re-branches, re-paths and sets the interval of one remote without touching the others; `null` reverts a field, a traversing `path` answers 422, and a per-remote `token` is stored encrypted and never returned.
- `F18.pause-and-check`: `enabled:false` pauses one remote (`POST /remotes/{id}/sync` 503 `Git sync is not enabled`, the default still syncs); `POST /remotes/{id}/check` tests what a `PUT` would leave in place, even while paused, and `{"repo_url":null}` answers `ok:false` `No repository URL is configured.`; `enabled:true` resumes.
- `F18.sync-remote`: `POST /remotes/{id}/sync` on a secondary remote exports only its own automations to its own repository and checkout, leaves the default's `last_synced_at` untouched, and `/remotes/{id}/check` then reports `branch_exists:true`.
- `F18.assign-move`: `PUT /assignments/{automation_id}` with `{remote_id}` moves an automation (`remote_id`, `effective_remote_id`, `dirty:true`), leaves an `automation_git_sync_removal` row for the old remote, whose next cycle deletes `<path>/<slug>/` from its repository, while the new remote's next cycle exports it; moving to the current remote is a no-op.
- `F18.assign-slug-collision`: moving an automation to a remote that already uses its slug renames the slug to `<slug>-<first 8 hex of its id>`.
- `F18.assign-404`: `PUT /assignments/{automation_id}` answers 404 `Git sync remote not found` for an unknown remote and 404 `Automation not found` for an unknown automation, and 422 for a body without a valid `remote_id`; nothing moves.
- `F18.delete-remote`: `DELETE /remotes/{id}` answers 204; the remote is gone (404), its automations survive unassigned and dirty (`remote_id` null, `effective_remote_id` the default), its repository is untouched, and the default's next cycle adopts and exports them.
- `F18.legacy-default`: with no remote, the legacy routes read unconfigured (`POST /sync` 503, `POST /check` no URL); `PUT /config` creates the default remote on first save (visible in `GET /remotes`), `POST /sync` runs its cycle and adopts the unassigned automations, and `PUT /config` and `PUT /remotes/{id}` edit the same row.

## How to get to it (agent POV)

- REST: the ten routes above, under the service base path
  (`/api/automation/v1/git-sync/...`). `GET /remotes`, `GET /remotes/{id}`
  and `GET /assignments` need `view_automations`; every other route needs
  `manage_automations`. The local key carries both, so the 403 path is not
  reachable in local mode. Every route acts on the caller's org
  (`user.org_id`); a remote id from another org is a 404.
- The legacy single-repo routes (F13) are the second entry point to the
  default remote: `GET /status` reports it, `PUT /config` creates or edits
  it, `POST /check` tests it and `POST /sync` triggers it. `F18.legacy-default`
  drives that mapping; F13 drives the single-repo behavior in depth.
- Automation CRUD (F05): every create assigns the automation to the org's
  current default remote (or leaves it unassigned when there is none) and
  marks it dirty; that is how `F18.default-promote` gets an automation onto
  the promoted remote.
- Agent Canvas: the Git Sync repositories page (in `OpenHands/OpenHands`)
  lists `GET /remotes`, adds with `POST /remotes/check` then `POST /remotes`,
  edits with `PUT /remotes/{id}`, triggers `POST /remotes/{id}/sync`, and
  moves automations with `GET`/`PUT /assignments`. No SDK or TypeScript
  client method in this repository reaches these routes.
- Git: anything pushed to a remote's branch under `{path}/{slug}/` is
  imported by that remote's next cycle as an automation assigned to it
  (F13 covers the import rules).
- Config: the same `AUTOMATION_GIT_SYNC_*` env defaults as F13. In local
  mode they are the default remote's base configuration; every other remote
  starts from the neutral defaults (branch `main`, path `automations`, no
  repo, token or key). Each remote's checkout is
  `$AUTOMATION_WORKSPACE_BASE/git-sync/<remote id>` (the run's
  `workspaces/git-sync/<remote id>`).
- Recipes drive everything through REST and read results from the bare
  repositories (`git -C "$B_A" ls-tree/cat-file/rev-parse`), the remote,
  state and removal tables (`state sql`) and `GET /v1/{id}`.

## Driving it with control-automation

Preconditions:

- A fresh run (`map run --file F18 --fresh`). No LLM. `git` on `PATH`.
- Three empty bare remotes, and one automation created before the org has
  any remote, so the adoption path is real.
  ```sh
  URL_A=$(control-automation fixture git-remote --name remote-a --field url)
  B_A=$(control-automation fixture git-remote --name remote-a --field bare)
  URL_B=$(control-automation fixture git-remote --name remote-b --field url)
  B_B=$(control-automation fixture git-remote --name remote-b --field bare)
  URL_C=$(control-automation fixture git-remote --name remote-c --field url)
  B_C=$(control-automation fixture git-remote --name remote-c --field bare)
  test -d "$B_A" && test -d "$B_B" && test -d "$B_C"
  AM0=$(control-automation fixture automation --kind echo --name 'qa alpha job' --field id)
  ```

- **No remotes yet, and auth (`F18.list-empty`).** One live automation, nowhere to sync it.
  ```sh
  control-automation api GET /v1/git-sync/remotes --expect 200 --check remotes len-eq 0 --check unassigned_count eq 1 --save F18.list-empty/remotes
  control-automation api GET /v1/git-sync/assignments --expect 200 --check automations len-eq 1 --check automations.0.automation_id eq "$AM0" --check automations.0.name eq 'qa alpha job' --check automations.0.remote_id eq null --check automations.0.effective_remote_id eq null --check automations.0.slug eq qa-alpha-job --check automations.0.dirty eq true --save F18.list-empty/assignments
  control-automation api GET /v1/git-sync/remotes --auth none --expect 401
  control-automation api POST /v1/git-sync/remotes --auth none --json "{\"repo_url\":\"$URL_A\"}" --expect 401
  control-automation api POST /v1/git-sync/remotes/check --auth none --json '{}' --expect 401
  control-automation api GET /v1/git-sync/remotes/00000000-0000-0000-0000-000000000f18 --auth none --expect 401
  control-automation api PUT /v1/git-sync/remotes/00000000-0000-0000-0000-000000000f18 --auth none --json '{}' --expect 401
  control-automation api DELETE /v1/git-sync/remotes/00000000-0000-0000-0000-000000000f18 --auth none --expect 401
  control-automation api POST /v1/git-sync/remotes/00000000-0000-0000-0000-000000000f18/check --auth none --json '{}' --expect 401
  control-automation api POST /v1/git-sync/remotes/00000000-0000-0000-0000-000000000f18/sync --auth none --expect 401
  control-automation api GET /v1/git-sync/assignments --auth none --expect 401
  control-automation api PUT "/v1/git-sync/assignments/$AM0" --auth none --json '{"remote_id":"00000000-0000-0000-0000-000000000f18"}' --expect 401
  control-automation api GET /v1/git-sync/remotes --auth bad --expect 401 --check detail eq 'Invalid API key'
  control-automation api GET /v1/git-sync/remotes --check remotes len-eq 0 --check unassigned_count eq 1
  ```
  The automation already has a state row (created with it, F13) with its
  slug, but no remote: `effective_remote_id` is null because there is no
  default to fall back to. The refused calls left nothing behind.
- **Check a repository before adding it (`F18.check-new`).**
  ```sh
  control-automation api POST /v1/git-sync/remotes/check --json '{}' --expect 200 --check ok eq false --check branch_exists eq false --check detail eq 'No repository URL is configured.' --save F18.check-new/no-url
  control-automation api POST /v1/git-sync/remotes/check --json "{\"repo_url\":\"$URL_A\"}" --expect 200 --check ok eq true --check branch_exists eq false --check detail eq null --save F18.check-new/empty-repo
  control-automation api POST /v1/git-sync/remotes/check --json '{"repo_url":"/qa-nonexistent/repo.git"}' --expect 200 --check ok eq false --check branch_exists eq false --check detail matches '^git command failed \(128\): git ls-remote --heads -- /qa-nonexistent/repo.git main'
  control-automation api GET /v1/git-sync/remotes --check remotes len-eq 0
  control-automation api GET /v1/git-sync/status --check enabled eq false --check repo_url eq ''
  ```
- **The first remote is the default (`F18.create-default`).**
  ```sh
  control-automation api POST /v1/git-sync/remotes --json '{"label":"qa no repo"}' --expect 422 --check detail.0.loc.1 eq repo_url --save F18.create-default/no-url
  RA=$(control-automation api POST /v1/git-sync/remotes --json "{\"repo_url\":\"$URL_A\",\"label\":\"qa alpha\"}" --expect 201 --check is_default eq true --check enabled eq true --check repo_url eq "$URL_A" --check branch eq main --check path eq automations --check label eq 'qa alpha' --check automation_count eq 1 --check dirty_count eq 1 --check last_synced_at eq null --check sync_in_progress eq false --field id)
  control-automation api GET "/v1/git-sync/remotes/$RA" --expect 200 --check id eq "$RA" --check is_default eq true --check label eq 'qa alpha' --check repo_url eq "$URL_A" --check automation_count eq 1 --save F18.create-default/get
  control-automation api GET /v1/git-sync/remotes --expect 200 --check remotes len-eq 1 --check remotes.0.id eq "$RA" --check unassigned_count eq 1 --save F18.create-default/list
  control-automation api GET /v1/git-sync/assignments --check automations.0.remote_id eq null --check automations.0.effective_remote_id eq "$RA"
  control-automation api GET /v1/git-sync/status --expect 200 --check enabled eq true --check repo_url eq "$URL_A" --check dirty_count eq 1
  ME=$(control-automation api GET "/v1/$AM0" --field user_id)
  control-automation state sql "SELECT is_default, label, configured_by_user_id AS who FROM automation_git_sync_remote" --check count eq 1 --check rows.0.is_default eq 1 --check rows.0.label eq 'qa alpha' --check rows.0.who eq "$(echo "$ME" | tr -d -)"
  ```
  The unassigned automation is counted as the default's
  (`automation_count:1`) and is its `effective_remote_id`, but it stays
  `remote_id:null` until a cycle adopts it. No cycle runs on create.
- **A second repository (`F18.create-secondary`).**
  Requires: `F18.create-default`
  ```sh
  RB=$(control-automation api POST /v1/git-sync/remotes --json "{\"repo_url\":\"$URL_B\",\"label\":\"qa beta\"}" --expect 201 --check is_default eq false --check enabled eq true --field id)
  control-automation api GET "/v1/git-sync/remotes/$RB" --expect 200 --check is_default eq false --check repo_url eq "$URL_B" --check branch eq main --check automation_count eq 0 --check dirty_count eq 0 --save F18.create-secondary/get
  control-automation api GET /v1/git-sync/remotes --expect 200 --check remotes len-eq 2 --check remotes.0.id eq "$RA" --check remotes.1.id eq "$RB" --check unassigned_count eq 1
  control-automation api GET /v1/git-sync/assignments --check automations.0.effective_remote_id eq "$RA"
  control-automation api GET /v1/git-sync/status --check repo_url eq "$URL_A"
  ```
  The list is default first, then oldest first.
- **One repository, branch and path per remote (`F18.repo-conflict`).**
  Requires: `F18.create-secondary`
  ```sh
  control-automation api POST /v1/git-sync/remotes --json "{\"repo_url\":\"${URL_A%.git}/\"}" --expect 409 --check detail contains 'Another repository configuration already syncs this repository, branch and path' --save F18.repo-conflict/create
  control-automation api PUT "/v1/git-sync/remotes/$RB" --json "{\"repo_url\":\"$URL_A\"}" --expect 409 --check detail contains 'Another repository configuration already syncs this repository, branch and path' --save F18.repo-conflict/update
  control-automation api PUT /v1/git-sync/config --json "{\"repo_url\":\"$URL_B\"}" --expect 409 --check detail contains 'Another repository configuration already syncs this repository, branch and path'
  control-automation api GET "/v1/git-sync/remotes/$RB" --check repo_url eq "$URL_B"
  control-automation api GET /v1/git-sync/status --check repo_url eq "$URL_A"
  control-automation api GET /v1/git-sync/remotes --check remotes len-eq 2
  ```
  The first body spells the URL without `.git` and with a trailing slash;
  both normalize to the same identity. A remote's own repository is excluded
  from its own check, so saving a remote without changing its repository is
  not a conflict (`F18.update-remote`).
- **Created as the default (`F18.create-as-default`).** A third remote on repository A under another path, created as the default, then deleted.
  Requires: `F18.create-secondary`
  ```sh
  RC=$(control-automation api POST /v1/git-sync/remotes --json "{\"repo_url\":\"$URL_A\",\"path\":\"qa-gamma\",\"label\":\"qa gamma\",\"is_default\":true}" --expect 201 --check is_default eq true --check path eq qa-gamma --check automation_count eq 1 --field id)
  control-automation api GET "/v1/git-sync/remotes/$RA" --check is_default eq false --check automation_count eq 0 --save F18.create-as-default/demoted
  control-automation api GET /v1/git-sync/remotes --check remotes len-eq 3 --check remotes.0.id eq "$RC"
  control-automation api GET /v1/git-sync/status --check repo_url eq "$URL_A" --check path eq qa-gamma
  control-automation api GET /v1/git-sync/assignments --check automations.0.effective_remote_id eq "$RC"
  control-automation api DELETE "/v1/git-sync/remotes/$RC" --expect 204
  control-automation api GET "/v1/git-sync/remotes/$RC" --expect 404 --check detail eq 'Git sync remote not found'
  control-automation api GET /v1/git-sync/remotes --expect 200 --check remotes len-eq 2 --check remotes.0.is_default eq false --check remotes.1.is_default eq false --check unassigned_count eq 1 --save F18.create-as-default/no-default
  control-automation api GET /v1/git-sync/status --check enabled eq false --check repo_url eq '' --check dirty_count eq 1
  control-automation api GET /v1/git-sync/assignments --check automations.0.effective_remote_id eq null
  control-automation api PUT "/v1/git-sync/remotes/$RA" --json '{"is_default":true}' --expect 200 --check is_default eq true
  control-automation api GET /v1/git-sync/status --check enabled eq true --check repo_url eq "$URL_A" --check path eq automations
  ```
  Deleting the default does not hand the role to another remote: the org
  has no default until one is promoted, the legacy status reads as
  unconfigured meanwhile, and the unassigned automation waits.
- **Promote another remote (`F18.default-promote`).**
  Requires: `F18.create-secondary`
  ```sh
  control-automation api PUT "/v1/git-sync/remotes/$RA" --json '{"is_default":false}' --expect 422 --check detail contains 'gives up its default remote by making another one the default' --save F18.default-promote/unset-refused
  control-automation api GET "/v1/git-sync/remotes/$RA" --check is_default eq true
  control-automation api PUT "/v1/git-sync/remotes/$RB" --json '{"is_default":true}' --expect 200 --check id eq "$RB" --check is_default eq true --save F18.default-promote/promote
  control-automation api GET "/v1/git-sync/remotes/$RA" --check is_default eq false
  control-automation api GET /v1/git-sync/remotes --check remotes.0.id eq "$RB" --check remotes.1.id eq "$RA"
  control-automation api GET /v1/git-sync/status --check enabled eq true --check repo_url eq "$URL_B"
  AM1=$(control-automation fixture automation --kind echo --name 'qa beta job' --field id)
  control-automation api GET /v1/git-sync/assignments --expect 200 --check automations len-eq 2 --check automations.1.automation_id eq "$AM1" --check automations.1.remote_id eq "$RB" --check automations.1.effective_remote_id eq "$RB" --check automations.1.slug eq qa-beta-job --check automations.1.dirty eq true --check automations.0.remote_id eq null --check automations.0.effective_remote_id eq "$RB" --save F18.default-promote/assignments
  control-automation api PUT "/v1/git-sync/remotes/$RA" --json '{"is_default":true}' --expect 200 --check is_default eq true
  control-automation api GET "/v1/git-sync/remotes/$RB" --check is_default eq false --check automation_count eq 1
  control-automation api GET /v1/git-sync/assignments --check automations.1.remote_id eq "$RB" --check automations.1.effective_remote_id eq "$RB" --check automations.0.remote_id eq null --check automations.0.effective_remote_id eq "$RA"
  control-automation api GET /v1/git-sync/status --check repo_url eq "$URL_A"
  ```
  `qa beta job`, created while B was the default, was assigned to B at
  creation and stays there when A is promoted back; only the unassigned
  automation follows the default. Assignments are listed by name, so the
  new automation is second.
- **The default adopts the unassigned (`F18.adopt-unassigned`).**
  Requires: `F18.default-promote`
  ```sh
  control-automation api POST "/v1/git-sync/remotes/$RA/sync" --expect 202 --check triggered eq true --save F18.adopt-unassigned/trigger
  control-automation api GET "/v1/git-sync/remotes/$RA" --until-ok 60 --check last_synced_at exists --check sync_in_progress eq false --check last_error eq null --check dirty_count eq 0 --check automation_count eq 1 --save F18.adopt-unassigned/remote
  control-automation api GET /v1/git-sync/remotes --check unassigned_count eq 0 --check remotes.0.automation_count eq 1 --check remotes.1.automation_count eq 1
  control-automation api GET /v1/git-sync/assignments --check automations.0.automation_id eq "$AM0" --check automations.0.remote_id eq "$RA" --check automations.0.effective_remote_id eq "$RA" --check automations.0.dirty eq false --save F18.adopt-unassigned/assignments
  control-automation state sql "SELECT remote_id FROM automation_git_sync_state WHERE automation_id = '$(echo "$AM0" | tr -d -)'" --check rows.0.remote_id eq "$(echo "$RA" | tr -d -)"
  control-automation api GET "/v1/git-sync/remotes/$RA" --check last_synced_commit eq "$(git -C "$B_A" rev-parse main)"
  control-automation exec --save F18.adopt-unassigned/tree-a --expect-output automations/qa-alpha-job/automation.yaml --expect-output automations/qa-alpha-job/tarball/run.sh --reject-output qa-beta-job -- git -C "$B_A" ls-tree -r --name-only main
  test "$(git -C "$AUTOMATION_VERIFY_RUN/workspaces/git-sync/$RA" remote get-url origin)" = "$URL_A"
  control-automation api GET "/v1/git-sync/remotes/$RB" --check last_synced_at eq null --check dirty_count eq 1
  control-automation api GET /v1/git-sync/status --check last_synced_commit eq "$(git -C "$B_A" rev-parse main)" --check dirty_count eq 0
  ```
  Only A ran: B's automation is still dirty and B has never synced. The
  legacy status is A's status.
- **Reconfigure one remote (`F18.update-remote`).**
  Requires: `F18.create-secondary`
  ```sh
  control-automation api PUT "/v1/git-sync/remotes/$RB" --json '{"label":"qa beta renamed","branch":"qa-dev","path":"/qa-beta/autos/","interval_seconds":0}' --expect 200 --check label eq 'qa beta renamed' --check branch eq qa-dev --check path eq qa-beta/autos --check interval_seconds eq 0 --check repo_url eq "$URL_B" --check is_default eq false --save F18.update-remote/put
  control-automation api GET "/v1/git-sync/remotes/$RB" --check label eq 'qa beta renamed' --check branch eq qa-dev --check path eq qa-beta/autos
  control-automation api PUT "/v1/git-sync/remotes/$RB" --json '{"path":"../qa-evil"}' --expect 422 --check detail.0.loc.1 eq path --save F18.update-remote/traversal
  control-automation api PUT "/v1/git-sync/remotes/$RB" --json '{"interval_seconds":-1}' --expect 422 --check detail.0.loc.1 eq interval_seconds
  control-automation api GET "/v1/git-sync/remotes/$RB" --check path eq qa-beta/autos --check interval_seconds eq 0
  control-automation api GET "/v1/git-sync/remotes/$RA" --check label eq 'qa alpha' --check branch eq main --check path eq automations
  control-automation api GET /v1/git-sync/status --check branch eq main --check path eq automations --check repo_url eq "$URL_A"
  control-automation api PUT "/v1/git-sync/remotes/$RB" --json '{"token":"qa-remote-token-5150"}' --expect 200 --check token missing
  control-automation state sql "SELECT json_extract(overrides, '\$.git_sync_token') AS t, instr(overrides, 'qa-remote-token-5150') AS leak FROM automation_git_sync_remote WHERE id = '$(echo "$RB" | tr -d -)'" --raw --check rows.0.t matches '^gAAAAA' --check rows.0.leak eq 0
  control-automation state sql "SELECT json_extract(overrides, '\$.git_sync_token') AS t FROM automation_git_sync_remote WHERE id = '$(echo "$RA" | tr -d -)'" --check rows.0.t eq null
  control-automation api PUT "/v1/git-sync/remotes/$RB" --json '{"token":null,"branch":null,"path":null}' --expect 200 --check branch eq main --check path eq automations --check label eq 'qa beta renamed'
  control-automation api GET "/v1/git-sync/remotes/$RB" --check branch eq main --check path eq automations
  ```
  The path is normalized on the way in; the rejected bodies changed nothing;
  A and the legacy status never moved. The token is a Fernet token in B's
  row only.
- **Pause one remote, check it, resume (`F18.pause-and-check`).**
  Requires: `F18.create-secondary`
  ```sh
  control-automation api PUT "/v1/git-sync/remotes/$RB" --json '{"enabled":false}' --expect 200 --check enabled eq false --check repo_url eq "$URL_B" --save F18.pause-and-check/pause
  control-automation api POST "/v1/git-sync/remotes/$RB/sync" --expect 503 --check detail eq 'Git sync is not enabled' --save F18.pause-and-check/sync-refused
  control-automation api GET /v1/git-sync/status --check enabled eq true
  control-automation api POST "/v1/git-sync/remotes/$RB/check" --json '{}' --expect 200 --check ok eq true --check branch_exists eq false --check detail eq null --save F18.pause-and-check/check
  control-automation api POST "/v1/git-sync/remotes/$RB/check" --json '{"repo_url":null}' --expect 200 --check ok eq false --check branch_exists eq false --check detail eq 'No repository URL is configured.' --save F18.pause-and-check/check-no-url
  control-automation api POST "/v1/git-sync/remotes/$RB/check" --json '{"repo_url":"/qa-nonexistent/repo.git"}' --expect 200 --check ok eq false --check detail matches '^git command failed \(128\): git ls-remote --heads -- /qa-nonexistent/repo.git main'
  control-automation api GET "/v1/git-sync/remotes/$RB" --check repo_url eq "$URL_B" --check enabled eq false
  control-automation api PUT "/v1/git-sync/remotes/$RB" --json '{"enabled":true}' --expect 200 --check enabled eq true
  control-automation api GET "/v1/git-sync/remotes/$RB" --check enabled eq true
  ```
  `{"repo_url":null}` asks what clearing the URL would leave: for a
  non-default remote that is no repository at all (it never inherits the
  env-level one). None of the checks saved anything.
- **Sync a secondary remote (`F18.sync-remote`).**
  Requires: `F18.adopt-unassigned`
  ```sh
  PREV_A=$(control-automation api GET "/v1/git-sync/remotes/$RA" --field last_synced_at)
  control-automation api POST "/v1/git-sync/remotes/$RB/sync" --expect 202 --check triggered eq true --save F18.sync-remote/trigger
  control-automation api GET "/v1/git-sync/remotes/$RB" --until-ok 60 --check last_synced_at exists --check sync_in_progress eq false --check last_error eq null --check dirty_count eq 0 --check automation_count eq 1 --save F18.sync-remote/remote
  control-automation api GET "/v1/git-sync/remotes/$RB" --check last_synced_commit eq "$(git -C "$B_B" rev-parse main)"
  control-automation exec --save F18.sync-remote/tree-b --expect-output automations/qa-beta-job/automation.yaml --expect-output automations/qa-beta-job/tarball/run.sh --reject-output qa-alpha-job -- git -C "$B_B" ls-tree -r --name-only main
  control-automation exec --reject-output qa-beta-job -- git -C "$B_A" ls-tree -r --name-only main
  test "$(git -C "$AUTOMATION_VERIFY_RUN/workspaces/git-sync/$RB" remote get-url origin)" = "$URL_B"
  control-automation api GET "/v1/git-sync/remotes/$RA" --check last_synced_at eq "$PREV_A"
  control-automation api GET /v1/git-sync/assignments --check automations.1.automation_id eq "$AM1" --check automations.1.remote_id eq "$RB" --check automations.1.dirty eq false
  control-automation api POST "/v1/git-sync/remotes/$RB/check" --json '{}' --expect 200 --check ok eq true --check branch_exists eq true
  ```
- **Move an automation between remotes (`F18.assign-move`).**
  Requires: `F18.sync-remote`
  ```sh
  control-automation api PUT "/v1/git-sync/assignments/$AM0" --json "{\"remote_id\":\"$RB\"}" --expect 200 --check automation_id eq "$AM0" --check remote_id eq "$RB" --check effective_remote_id eq "$RB" --check slug eq qa-alpha-job --check dirty eq true --save F18.assign-move/put
  control-automation api GET /v1/git-sync/assignments --check automations.0.automation_id eq "$AM0" --check automations.0.remote_id eq "$RB" --check automations.0.dirty eq true
  control-automation state sql "SELECT slug FROM automation_git_sync_removal WHERE remote_id = '$(echo "$RA" | tr -d -)'" --check count eq 1 --check rows.0.slug eq qa-alpha-job
  control-automation api GET /v1/git-sync/remotes --check remotes.0.automation_count eq 0 --check remotes.1.automation_count eq 2 --check unassigned_count eq 0
  control-automation api GET "/v1/git-sync/remotes/$RA" --check dirty_count eq 0
  control-automation api GET "/v1/git-sync/remotes/$RB" --check dirty_count eq 1
  PREV_A=$(control-automation api GET "/v1/git-sync/remotes/$RA" --field last_synced_at)
  control-automation api POST "/v1/git-sync/remotes/$RA/sync" --expect 202 --check triggered eq true
  control-automation api GET "/v1/git-sync/remotes/$RA" --until-ok 60 --check last_synced_at ne "$PREV_A" --check sync_in_progress eq false --check last_error eq null
  control-automation exec --save F18.assign-move/tree-a --reject-output qa-alpha-job -- git -C "$B_A" ls-tree -r --name-only main
  control-automation state sql "SELECT count(*) AS n FROM automation_git_sync_removal" --check rows.0.n eq 0
  PREV_B=$(control-automation api GET "/v1/git-sync/remotes/$RB" --field last_synced_at)
  control-automation api POST "/v1/git-sync/remotes/$RB/sync" --expect 202 --check triggered eq true
  control-automation api GET "/v1/git-sync/remotes/$RB" --until-ok 60 --check last_synced_at ne "$PREV_B" --check sync_in_progress eq false --check last_error eq null --check dirty_count eq 0
  control-automation exec --save F18.assign-move/tree-b --expect-output automations/qa-alpha-job/automation.yaml --expect-output automations/qa-alpha-job/tarball/run.sh --expect-output automations/qa-beta-job/automation.yaml -- git -C "$B_B" ls-tree -r --name-only main
  control-automation api PUT "/v1/git-sync/assignments/$AM0" --json "{\"remote_id\":\"$RB\"}" --expect 200 --check remote_id eq "$RB" --check dirty eq false
  control-automation state sql "SELECT count(*) AS n FROM automation_git_sync_removal" --check rows.0.n eq 0
  ```
  The move itself touches no repository: A's next cycle deletes
  `automations/qa-alpha-job/` from repository A (and consumes the removal
  row), and B's next cycle writes it to repository B. Re-assigning to the
  remote it is already on is a no-op (still clean, no removal).
- **The target already uses the slug (`F18.assign-slug-collision`).** A new automation whose name slugs to `qa-alpha-job` again, moved onto B where `qa alpha job` already lives.
  Requires: `F18.assign-move`
  ```sh
  AM2=$(control-automation fixture automation --kind echo --name 'QA Alpha Job!' --field id)
  control-automation api GET /v1/git-sync/assignments --check automations len-eq 3 --check automations.0.automation_id eq "$AM2" --check automations.0.remote_id eq "$RA" --check automations.0.slug eq qa-alpha-job
  control-automation api PUT "/v1/git-sync/assignments/$AM2" --json "{\"remote_id\":\"$RB\"}" --expect 200 --check remote_id eq "$RB" --check slug eq "qa-alpha-job-$(echo "$AM2" | tr -d - | cut -c1-8)" --check dirty eq true --save F18.assign-slug-collision/put
  control-automation state sql "SELECT slug FROM automation_git_sync_state WHERE automation_id = '$(echo "$AM2" | tr -d -)'" --check rows.0.slug eq "qa-alpha-job-$(echo "$AM2" | tr -d - | cut -c1-8)"
  control-automation api GET /v1/git-sync/assignments --check automations.1.automation_id eq "$AM0" --check automations.1.slug eq qa-alpha-job --check automations.1.remote_id eq "$RB"
  ```
  On A the plain slug was free (its previous owner moved away), so the new
  automation got `qa-alpha-job` at creation; B still has that slug, so the
  move renames it with the id suffix. Names sort in byte order, so the
  capitalized name lists first.
- **Unknown targets (`F18.assign-404`).**
  Requires: `F18.assign-move`
  ```sh
  control-automation api PUT "/v1/git-sync/assignments/$AM0" --json '{"remote_id":"00000000-0000-0000-0000-000000000f18"}' --expect 404 --check detail eq 'Git sync remote not found' --save F18.assign-404/remote
  control-automation api PUT /v1/git-sync/assignments/00000000-0000-0000-0000-000000000f18 --json "{\"remote_id\":\"$RB\"}" --expect 404 --check detail eq 'Automation not found' --save F18.assign-404/automation
  control-automation api PUT "/v1/git-sync/assignments/$AM0" --json '{"remote_id":"not-a-uuid"}' --expect 422 --check detail.0.loc.1 eq remote_id
  control-automation api PUT "/v1/git-sync/assignments/$AM0" --json '{}' --expect 422 --check detail.0.loc.1 eq remote_id
  control-automation state sql "SELECT remote_id, slug FROM automation_git_sync_state WHERE automation_id = '$(echo "$AM0" | tr -d -)'" --check rows.0.remote_id eq "$(echo "$RB" | tr -d -)" --check rows.0.slug eq qa-alpha-job
  control-automation state sql "SELECT count(*) AS n FROM automation_git_sync_removal" --check rows.0.n eq 0
  ```
- **Delete a remote (`F18.delete-remote`).** B goes; its two automations and its repository stay.
  Requires: `F18.assign-slug-collision`
  ```sh
  HEAD_B=$(git -C "$B_B" rev-parse main)
  control-automation api DELETE "/v1/git-sync/remotes/$RB" --expect 204 --save F18.delete-remote/delete
  control-automation api GET "/v1/git-sync/remotes/$RB" --expect 404 --check detail eq 'Git sync remote not found'
  control-automation api POST "/v1/git-sync/remotes/$RB/sync" --expect 404
  control-automation api GET /v1/git-sync/remotes --expect 200 --check remotes len-eq 1 --check remotes.0.id eq "$RA" --check remotes.0.automation_count eq 3 --check unassigned_count eq 3 --save F18.delete-remote/remotes
  control-automation api GET /v1/git-sync/assignments --expect 200 --check automations len-eq 3 --check automations.0.remote_id eq null --check automations.1.remote_id eq null --check automations.2.remote_id eq null --check automations.0.effective_remote_id eq "$RA" --check automations.1.effective_remote_id eq "$RA" --check automations.2.effective_remote_id eq "$RA" --check automations.1.dirty eq true --save F18.delete-remote/assignments
  control-automation state sql "SELECT count(*) AS n FROM automation_git_sync_state WHERE remote_id IS NULL AND dirty = 1" --check rows.0.n eq 3
  control-automation state sql "SELECT count(*) AS n FROM automation_git_sync_remote" --check rows.0.n eq 1
  test "$(git -C "$B_B" rev-parse main)" = "$HEAD_B"
  git -C "$B_B" cat-file -e main:automations/qa-alpha-job/automation.yaml
  control-automation api GET "/v1/$AM0" --expect 200 --check name eq 'qa alpha job'
  control-automation api GET "/v1/$AM1" --expect 200 --check name eq 'qa beta job'
  PREV_A=$(control-automation api GET "/v1/git-sync/remotes/$RA" --field last_synced_at)
  control-automation api POST "/v1/git-sync/remotes/$RA/sync" --expect 202 --check triggered eq true
  control-automation api GET "/v1/git-sync/remotes/$RA" --until-ok 60 --check last_synced_at ne "$PREV_A" --check sync_in_progress eq false --check last_error eq null --check dirty_count eq 0 --check automation_count eq 3 --save F18.delete-remote/adopted
  control-automation api GET /v1/git-sync/remotes --check unassigned_count eq 0
  control-automation api GET /v1/git-sync/assignments --check automations.0.remote_id eq "$RA" --check automations.1.remote_id eq "$RA" --check automations.2.remote_id eq "$RA" --check automations.0.dirty eq false
  control-automation exec --save F18.delete-remote/tree-a --expect-output automations/qa-alpha-job/automation.yaml --expect-output automations/qa-beta-job/automation.yaml --expect-output "automations/qa-alpha-job-$(echo "$AM2" | tr -d - | cut -c1-8)/automation.yaml" -- git -C "$B_A" ls-tree -r --name-only main
  test "$(git -C "$B_B" rev-parse main)" = "$HEAD_B"
  ```
  Repository B still has the last commit the service pushed; nothing is
  deleted from a repository the service no longer syncs. The three
  automations were unassigned and dirty, and A's next cycle adopted and
  exported them (slugs follow along).
- **The legacy routes create and drive the default (`F18.legacy-default`).** Delete the last remote, then configure through `PUT /config` only.
  Requires: `F18.delete-remote`
  ```sh
  control-automation api DELETE "/v1/git-sync/remotes/$RA" --expect 204
  control-automation api GET /v1/git-sync/remotes --check remotes len-eq 0 --check unassigned_count eq 3
  control-automation api GET /v1/git-sync/status --expect 200 --check enabled eq false --check repo_url eq '' --check last_synced_at eq null --check dirty_count eq 3 --save F18.legacy-default/status-none
  control-automation api GET /v1/git-sync/assignments --check automations.0.remote_id eq null --check automations.0.effective_remote_id eq null
  control-automation api POST /v1/git-sync/sync --expect 503 --check detail eq 'Git sync is not enabled'
  control-automation api POST /v1/git-sync/check --json '{}' --expect 200 --check ok eq false --check detail eq 'No repository URL is configured.'
  control-automation api PUT /v1/git-sync/config --json "{\"repo_url\":\"$URL_C\"}" --expect 200 --check enabled eq true --check repo_url eq "$URL_C" --check dirty_count eq 3 --save F18.legacy-default/config
  RD=$(control-automation api GET /v1/git-sync/remotes --expect 200 --check remotes len-eq 1 --check remotes.0.is_default eq true --check remotes.0.repo_url eq "$URL_C" --check remotes.0.label eq '' --check remotes.0.automation_count eq 3 --check unassigned_count eq 3 --field remotes.0.id)
  control-automation api GET "/v1/git-sync/remotes/$RD" --expect 200 --check is_default eq true --check repo_url eq "$URL_C" --check enabled eq true --save F18.legacy-default/remote
  control-automation api GET /v1/git-sync/assignments --check automations.0.remote_id eq null --check automations.0.effective_remote_id eq "$RD"
  control-automation api POST /v1/git-sync/sync --expect 202 --check triggered eq true
  control-automation api GET /v1/git-sync/status --until-ok 60 --check last_synced_at exists --check sync_in_progress eq false --check last_error eq null --check dirty_count eq 0 --save F18.legacy-default/status-synced
  control-automation api GET "/v1/git-sync/remotes/$RD" --check last_synced_commit eq "$(git -C "$B_C" rev-parse main)" --check dirty_count eq 0 --check automation_count eq 3
  control-automation api GET /v1/git-sync/remotes --check unassigned_count eq 0
  control-automation api GET /v1/git-sync/assignments --check automations.0.remote_id eq "$RD" --check automations.1.remote_id eq "$RD" --check automations.2.remote_id eq "$RD"
  control-automation exec --save F18.legacy-default/tree-c --expect-output automations/qa-alpha-job/automation.yaml --expect-output automations/qa-beta-job/automation.yaml --expect-output "automations/qa-alpha-job-$(echo "$AM2" | tr -d - | cut -c1-8)/automation.yaml" -- git -C "$B_C" ls-tree -r --name-only main
  test "$(git -C "$AUTOMATION_VERIFY_RUN/workspaces/git-sync/$RD" remote get-url origin)" = "$URL_C"
  control-automation api PUT /v1/git-sync/config --json '{"branch":"qa-dev","label":"qa ignored"}' --expect 200 --check branch eq qa-dev
  control-automation api GET "/v1/git-sync/remotes/$RD" --check branch eq qa-dev --check label eq ''
  control-automation api PUT "/v1/git-sync/remotes/$RD" --json '{"branch":null}' --expect 200 --check branch eq main
  control-automation api GET /v1/git-sync/status --check branch eq main
  ```
  `PUT /config` created a new default remote (a new id, blank label) and
  `POST /sync` ran its cycle: the three automations orphaned by the deletes
  were adopted and exported to repository C. The two surfaces edit one row:
  a branch set through `/config` shows on the remote, and cleared through
  `/remotes/{id}` shows on `/status`. `/config` ignores `label`.

## Gotchas

- A remote's cycle outcome appears only in `GET /remotes/{id}` (or
  `GET /status` for the default): poll `last_synced_at` (`exists` the first
  time, `ne` the previous value after) together with `sync_in_progress eq
  false`. `--field last_synced_at` exits 1 while it is null, so capture
  `PREV_*` only from a remote that has synced.
- Adoption is a cycle-time side effect of the *default* remote only.
  Between a create, a delete or a promotion and that cycle,
  `GET /assignments` shows `remote_id:null` with `effective_remote_id`
  pointing at the default, and `automation_count`/`dirty_count` on the
  default already include those rows. A non-default remote never adopts.
- Automation CRUD assigns the automation to whichever remote is the default
  *at that moment*; promoting another remote later moves nothing already
  assigned. Deleting the default leaves the org without one (no automatic
  successor), the legacy status reads unconfigured, and new automations stay
  unassigned until a remote is promoted or `PUT /config` creates a default.
- Deleting a remote never touches its repository, but the next default cycle
  re-exports the orphaned automations into *its* repository, so the same
  automation can end up in two repositories (B keeps the old copy in
  `F18.delete-remote`). Re-adding the old repository as a remote imports
  its directories as *new* automations (the import runs before adoption and
  matches directories to state rows by slug within the remote), so prefer
  moving automations with `PUT /assignments` over deleting and re-creating
  a remote.
- Slugs are unique per remote, not per org: a move renames only when the
  target already uses the slug, and the state row's slug changes
  permanently. The removal row names the *old* slug.
- A pending removal is skipped (not applied) when the slug is live again on
  that remote by the time its cycle runs, so a directory that was re-taken
  is never deleted. The removal row is consumed either way.
- `is_default:false` is never accepted on the default (422) and is a no-op
  elsewhere. `label` is ignored by `PUT /config`, and `is_default` is not a
  `/config` field.
- `POST /remotes/check` resolves the body over the env-level defaults when
  the org has no remotes yet (local mode: the would-be default inherits
  `AUTOMATION_GIT_SYNC_*`), and over the neutral defaults once it has any.
  `POST /remotes/{id}/check` resolves it over that remote's stored config,
  so `{}` checks the saved repository.
- Each remote clones into `workspaces/git-sync/<remote id>`; a checkout made
  before remotes existed (at `workspaces/git-sync` itself) is moved under
  the default remote's id on its first cycle. Deleted remotes leave their
  checkouts on disk.
- `GET /assignments` is ordered by name in the database's collation (byte
  order on SQLite: capitals first), then by id. The recipes pick names so
  the order is fixed; do not rely on indexes with free-form names.
- The 409 compares every remote in every org; a cross-org collision needs a
  seeded row (`F13.repo-conflict`), a same-org one needs only two remotes.
