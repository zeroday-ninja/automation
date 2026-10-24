# Changes this project did not want

- **A new trigger kind for something a cron already expresses.** `Trigger` is a
  discriminated union threaded through schemas, the git-sync serializer,
  capabilities, draft validation, the TypeScript client and the Canvas trigger
  editor. Adding a member costs all of that.
- **Reimplementing an Agent Server contract.** If an endpoint is missing, it
  belongs in the SDK.
- **A "non-fatal" exception handler that only prints.** The next person to read
  the dashboard cannot see the log.
- **Guessing which of two conflicting records the user meant.** Keep both and
  say so.
