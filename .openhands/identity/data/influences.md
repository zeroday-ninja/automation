# Prior art, and what was taken from it

- **The HAZARD methodology's gates** (the owner's own writing): a failed gate
  routes work somewhere rather than ending it, and the routing vocabulary is
  closed — approve, cycle, return, archive, escalate. The verdict is data; the
  explanation is prose.
- **Cron semantics.** No catch-up, deliberately: a missed minute is skipped,
  never queued, which is what keeps a restart from stampeding.
- **Advisory locks over leases** where the work is short, and a real lease with
  a TTL where it is long. Git Sync uses the latter; the claim uses the former.
