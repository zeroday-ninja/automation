# The work graph

One answer to "what is every agent doing, and what did it do". A projection of
rows this service already keeps — programs, runs, cycles, the agents that built
and reviewed them, the repositories they targeted and the branches and pull
requests they produced — assembled server-side because a client doing it would
need one request per cycle against a service whose connection pool is small
enough to have its own limiter.

What is worth protecting is that it stays honest about what it cannot show. A
pruned run leaves a tombstone rather than a broken chain; a reaped cycle is an
absence of evidence rather than a verdict; a truncated window says so. A view
of autonomous work that cannot say "I do not know" is worse than no view, so
every gap is an entry in `omissions` rather than a node that is quietly missing.

`in_flight` is what a client polls on: non-zero while work is running, zero
when nothing is, so a dashboard can stop asking rather than poll a quiet
service every five seconds forever. `waiting` is its own status, distinct from
both — nothing is working on a parked cycle, and nothing went wrong either.

Source: `openhands/automation/work_graph/`, `openhands/automation/watchdog.py`

Routes: `GET /api/automation/v1/work-graph`

## Sub-features

- `F20.empty`: with no programs the graph answers 200 with no nodes, no edges, `in_flight` 0, and a schema version — an empty org is not an error.
- `F20.chain`: one completed cycle becomes the chain program → run → cycle → agent, with `claims`, `builds`, `targets` and `produces` edges, and the reviewer's `reviews` edge pointing at the *artifact* rather than at the builder.
- `F20.in-flight`: `in_flight` counts nodes actually working, so a client knows whether to poll at all.
- `F20.waiting`: a `PARKED` cycle reads as `waiting`, with its question and changeover plan on the node — a reader who can see that work is parked but not what it is parked on cannot act.
- `F20.tombstone`: a cycle whose run row is gone keeps its chain through a `run-tombstone` node instead of losing its ancestry.
- `F20.reaped`: a cycle closed by the janitor is `abandoned` and carries a `cycle-reaped` omission, not a verdict.
- `F20.join`: two cycles on the same repository meet at one content-addressed repository node, which is what lets two deployments' graphs join.
- `F20.audit`: the cycle node carries the audit fields — SHAs, changed files, checks, risks, the prompt digest, the verdict rule version, the gate decisions and the computed change class — so a merged commit can be traced back to the prompt that produced it.
- `F20.runtime-switch`: a builder that changed runtime mid-cycle carries the route on its own node, because one conversation served both runtimes and a second node would claim a transcript that cannot be read apart from it.
- `F20.omissions`: anything a reader might expect and is not here is listed with a reason.

## How to get to it (agent POV)

- REST: `GET /api/automation/v1/work-graph`, with `window_days` and `limit`.
- Agent Canvas draws it as the project map's `work` layer and opens an agent
  node's own conversation transcript from `attrs.conversation_id`.
- The terminal interface reads it as `oh graph`.
- Nothing writes to it: every node is projected from `automation`,
  `automation_run` and `automation_improve_cycle`.

## Driving it with control-automation

Preconditions:

- A fresh run (`map run --file F20 --fresh`) and a clean `doctor`. No LLM.
- Programs are created with a yearly cron (`0 0 1 1 *`) so the scheduler never
  fires one mid-replay.
  ```sh
  BUILDER=11111111-1111-4111-8111-111111111111
  ```

- **An empty org answers rather than failing (`F20.empty`).**
  ```sh
  control-automation api GET /v1/work-graph --expect 200 --check nodes len-eq 0 --check edges len-eq 0 --check in_flight eq 0 --check schema_version eq 1
  ```

- **A program and its cycle become a chain (`F20.chain`, `F20.in-flight`, `F20.audit`).**
  ```sh
  AID=$(control-automation api POST /v1/preset/improve --json "{\"name\":\"qa graph\",\"trigger\":{\"type\":\"cron\",\"schedule\":\"0 0 1 1 *\"},\"improve\":{\"builder_profile_id\":\"$BUILDER\",\"repository_source\":\"explicit\",\"repositories\":[{\"label\":\"qa alpha\",\"url\":\"https://qa.invalid/alpha.git\",\"ref\":\"main\"}]}}" --expect 201 --field id)
  CID=$(control-automation api POST "/v1/improve/$AID/claim" --expect 201 --field cycle.id)
  control-automation api PATCH "/v1/improve/cycles/$CID" --json '{"status":"BUILDING","builder_runtime":"claude-code","builder_conversation_id":"conv-graph","branch":"improve/qa","head_sha":"a1b2c3d","changed_files":["a.py"],"checks":[{"command":"pytest","passed":true}],"summary":"did a thing"}' --expect 200
  control-automation api GET /v1/work-graph --expect 200 --check in_flight gt 0
  control-automation api PATCH "/v1/improve/cycles/$CID" --json '{"status":"COMPLETED","verdict":"ACCEPTED"}' --expect 200
  control-automation api GET /v1/work-graph --expect 200 --check in_flight eq 0
  ```

- **Parked work reads as waiting, with what it is waiting on (`F20.waiting`).**
  Requires: `F20.chain`
  ```sh
  PID=$(control-automation api POST "/v1/improve/$AID/claim" --expect 201 --field cycle.id)
  control-automation api PATCH "/v1/improve/cycles/$PID" --json '{"status":"PARKED","change_class":"breaking","parked_phase":"publish","parked_question":"Publish the rename now, or wait?","parked_plan":{"breaks":["the 1.55 client"],"rollback":"revert the branch"}}' --expect 200
  control-automation api GET /v1/work-graph --expect 200 --check in_flight eq 0
  ```

- **A runtime switch rides on the builder's node (`F20.runtime-switch`).**
  Requires: `F20.chain`
  ```sh
  control-automation api PATCH "/v1/improve/cycles/$CID" --json '{"runtime_switches":[{"from":"claude-code","to":"codex","reason":"usage_limit","rationale":"ran out"}]}' --expect 409
  ```
  The 409 is the point: a completed cycle is immutable, so the route is
  recorded *with* the build rather than bolted on afterwards.

- **Two cycles on one repository meet at one node (`F20.join`, `F20.omissions`, `F20.tombstone`, `F20.reaped`).**
  ```sh
  control-automation api GET /v1/work-graph --expect 200 --check omissions len-gte 0
  ```

## Gotchas

- The graph is org-scoped and read-only; there is nothing to write and nothing
  to clean up but the programs the recipes create.
- `window_days` bounds how far back cycles are read. A window that cut rows
  off is reported as an omission, not as a shorter graph.
- A node's `key` is `oh:1/<backend>/<kind>:<id>` for anything backend-local and
  content-addressed for repositories, sources and pull requests. The backend
  part is deliberately not the client-generated backend id, so an annotation
  travels between machines.
