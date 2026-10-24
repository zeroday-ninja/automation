# Decisions, and approaches already tried

Append-only. Each entry is dated and says what was tried, what happened, and
what would have to change for it to work. Do not rediscover these.

## 2026-10-21 — Chaining cycles on completion was rejected

The completion callback is best-effort, so a run whose callback never lands
breaks the chain forever, and a chain has no natural floor: a cycle that fails
in ten seconds re-fires immediately. Adding a minimum gap reinvents the cron.
A one-minute cron with a one-live-run guard does the same job with existing
machinery.

## 2026-10-22 — A crashed reviewer must not veto a verdict

Counting a reviewer that never judged as INCONCLUSIVE let one broken runtime
hold every cycle hostage. A failed review is recorded and shown, and does not
vote.

## 2026-10-22 — Reading the builder from the config was wrong

The preset read `config["builder_profile_id"]` instead of the claim's builder,
so a rotated builder reviewed its own work. The claim is the only place that
knows whose turn it was.

## 2026-10-24 — The per-automation KV store is the wrong home for scheduling state

It is scoped per automation where scheduling memory is org-wide, it is
encrypted with a key derived from the session key so rotating that key bricks
it, and a Fernet blob cannot be queried by state.
