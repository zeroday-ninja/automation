# Corrections, and the repeats they fail to prevent

A correction the user actually made is recorded without being asked for: an
answer to a parked cycle, a thumbs-down on something the dashboard shipped
itself, a change request. An agent's *guess* about their preferences is a
`soul_suggestion` on a cycle, which they accept or discard — deliberately a
different thing, because a correction that has to be accepted is a correction
you have to give twice.

A correction moves through four stages, and the later ones are what make it
stick: captured, scoped to what it applies to, injected into every cycle in
that scope, and mechanised into a check where it can be. A cycle that cannot
read its corrections fails rather than running without them.

The incidents are what make this a learning mechanism rather than a memory one.
The same correction arriving again is a defect of that pipeline, and the
incident names **which stage broke**: never captured, out of scope, in scope
but not injected, injected but ignored, or not mechanisable yet. A repeat with
no diagnosis is a complaint.

Source: `openhands/automation/improve/lessons.py`, `openhands/automation/improve/lesson_router.py`, `openhands/automation/models.py`, `migrations/versions/043_lessons.py`

Routes: `POST /api/automation/v1/lessons`, `GET /api/automation/v1/lessons`,
`POST /api/automation/v1/lessons/{lesson_id}/mechanise`,
`POST /api/automation/v1/lessons/{lesson_id}/retire`,
`GET /api/automation/v1/lessons/incidents`

## Sub-features

- `F21.capture`: `POST /v1/lessons` stores a correction with its scope, subject, trigger and the user's words verbatim, and answers `repeat: false`.
- `F21.scope-closed`: a scope outside `user|project|runtime|surface`, or a trigger outside the closed set, is refused 422 rather than stored as something nothing will ever read.
- `F21.list`: `GET /v1/lessons` answers every active correction with how often each has had to be given again; `include_retired=true` adds the retired ones.
- `F21.repeat`: the same correction in different words is not stored twice — it records an incident against the one that already existed and answers `repeat: true` with the stage that let it through.
- `F21.diagnosis`: the stage is computed from what the work reported having loaded, so "it ignored the correction" and "it never saw the correction" are different answers rather than one guess.
- `F21.mechanise`: `POST /v1/lessons/{id}/mechanise` records the hook, lint rule or test that now fails when the correction is broken; it refuses an empty `check_ref`, because `mechanised` with nothing named would make the next repeat diagnose itself against a check that does not exist.
- `F21.retire`: a retired correction stops applying and keeps its incidents — deleting it would delete the evidence that this mechanism was needed.
- `F21.incidents`: `GET /v1/lessons/incidents` answers every repeat with its stage, and reports every stage including the ones at zero, because a count of zero is information.
- `F21.resolution-capture`: answering a parked cycle files the answer as a project-scoped correction without being asked.
- `F21.acknowledgement`: an answer with nothing actionable in it ("yes, go ahead") is not stored, so the real corrections are not buried in a list a person has to read.
- `F21.org-scoped`: a correction belongs to the org that gave it; another org's reads answer none of it.

## How to get to it (agent POV)

- REST: the routes above. A cycle reads `GET /v1/lessons` before it assembles
  any prompt and *fails* if the read fails.
- Preset runs: `presets/improve/lessons.py` scopes and renders them, and the
  cycle reports `injected_lesson_ids` so a later repeat can be diagnosed.
- Agent Canvas captures a thumbs-down on an autonomously shipped feature as a
  `surface`-scoped correction.
- The improvement cycle's own answer to a parked change is captured by the
  service, not by the cycle.

## Driving it with control-automation

Preconditions:

- A fresh run (`map run --file F21 --fresh`) and a clean `doctor`. No LLM.
- No `git` and no profiles needed for the lesson routes themselves; the
  parked-resolution bullet reuses F19's program shape.
  ```sh
  BUILDER=11111111-1111-4111-8111-111111111111
  ```

- **Capture a correction (`F21.capture`, `F21.list`).**
  ```sh
  LID=$(control-automation api POST /v1/lessons --json '{"scope":"user","trigger":"chat_instruction","text":"Never add a dependency without checking its age.","why":"said once, in chat"}' --expect 201 --check repeat eq false --check lesson.scope eq user --check lesson.trigger eq chat_instruction --check lesson.repeats eq 0 --field lesson.id)
  control-automation api GET /v1/lessons --expect 200 --check total eq 1 --check lessons.0.text matches 'checking its age'
  ```

- **A scope or trigger outside the closed set is refused (`F21.scope-closed`).**
  ```sh
  control-automation api POST /v1/lessons --json '{"scope":"whenever","trigger":"comment","text":"something"}' --expect 422
  control-automation api POST /v1/lessons --json '{"scope":"user","trigger":"vibes","text":"something"}' --expect 422
  ```

- **The same correction twice is a repeat with a diagnosis (`F21.repeat`, `F21.diagnosis`, `F21.incidents`).**
  ```sh
  control-automation api POST /v1/lessons --json '{"scope":"user","trigger":"comment","text":"Do not add a dependency until you have checked how old it is."}' --expect 201 --check repeat eq true --check failed_stage eq not_injected
  control-automation api GET /v1/lessons --expect 200 --check total eq 1 --check lessons.0.repeats eq 1
  control-automation api GET /v1/lessons/incidents --expect 200 --check total eq 1 --check incidents.0.stage eq not_injected --check by_stage.ignored eq 0
  ```

- **Mechanise it, and refuse to pretend (`F21.mechanise`).**
  Requires: `F21.capture`
  ```sh
  control-automation api POST "/v1/lessons/$LID/mechanise" --json '{"check_ref":""}' --expect 422
  control-automation api POST "/v1/lessons/$LID/mechanise" --json '{"check_ref":"scripts/check-dependency-age.cjs"}' --expect 200 --check mechanised eq true --check check_ref matches 'check-dependency-age'
  ```

- **Retire it without forgetting it (`F21.retire`).**
  Requires: `F21.capture`
  ```sh
  control-automation api POST "/v1/lessons/$LID/retire" --expect 200 --check status eq retired
  control-automation api GET /v1/lessons --expect 200 --check total eq 0
  ```

- **Answering a parked cycle files the answer (`F21.resolution-capture`).**
  ```sh
  AID=$(control-automation api POST /v1/preset/improve --json "{\"name\":\"qa corrections\",\"trigger\":{\"type\":\"cron\",\"schedule\":\"0 0 1 1 *\"},\"improve\":{\"builder_profile_id\":\"$BUILDER\",\"repository_source\":\"explicit\",\"repositories\":[{\"label\":\"qa alpha\",\"url\":\"https://qa.invalid/alpha.git\"}]}}" --expect 201 --field id)
  CID=$(control-automation api POST "/v1/improve/$AID/claim" --expect 201 --field cycle.id)
  control-automation api PATCH "/v1/improve/cycles/$CID" --json '{"status":"PARKED","change_class":"breaking","parked_phase":"publish","parked_question":"Publish now?","injected_lesson_ids":[]}' --expect 200
  control-automation api POST "/v1/improve/cycles/$CID/resolve" --json '{"route":"cycle","resolution":"Wait for the client release before renaming a wire contract."}' --expect 200
  control-automation api GET /v1/lessons --expect 200 --check lessons.0.trigger eq parked_resolution --check lessons.0.scope eq project
  ```

- **An acknowledgement is not a correction (`F21.acknowledgement`).**
  Requires: `F21.resolution-capture`
  ```sh
  PID=$(control-automation api POST "/v1/improve/$AID/claim" --expect 201 --field cycle.id)
  control-automation api PATCH "/v1/improve/cycles/$PID" --json '{"status":"PARKED","parked_phase":"publish","parked_question":"And this one?"}' --expect 200
  control-automation api POST "/v1/improve/cycles/$PID/resolve" --json '{"route":"approve","resolution":"Yes, go ahead."}' --expect 200
  control-automation api GET /v1/lessons --expect 200 --check total eq 1
  ```

- **Corrections belong to the org that gave them (`F21.org-scoped`).**
  ```sh
  control-automation api GET /v1/lessons --expect 200 --check total eq 1
  ```

## Gotchas

- Matching is over *stemmed significant words* with the scaffolding stripped
  out, at a 0.5 threshold. Two statements of one rule share their nouns and
  verbs and differ in how the negation is phrased, so a threshold tuned for raw
  words would miss them — and the direction of error is deliberate: a miss
  gives one duplicate in a list, while a false match hides a real new
  correction behind an old one.
- An answer with fewer than four significant words is not a correction. That
  is what keeps "ok" and "not now" out of a list a person has to read.
- `injected_lesson_ids` absent and `injected_lesson_ids: []` mean different
  things, and the diagnosis depends on it: the first is "the work never said",
  the second is "the work loaded none".
