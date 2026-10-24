# OpenHands Automations

What this service is for, how it thinks, and what it will not do. Read this
before changing anything here; the rest of the set is beside it in
`.openhands/identity/`.

> Drafted by an agent from the code and its history. The **Current goals**
> section is the owner's to confirm or rewrite — everything else is a
> description of what the service already is.

## What it is

The service that decides **when work runs**. It stores automations, schedules
them, dispatches each run into a sandbox on an Agent Server, and keeps the
history. It does not run agents itself and it does not own the agent contract.

## Worldview

- **The schedule is a fact, not a suggestion.** Anything that decides whether
  work happens — a claim, a cap, a lease — is written to the database before
  an agent starts, so a crash cannot grant permission that was never given.
- **A run that failed and a run that found nothing are different outcomes.**
  Collapsing them is how a program disables itself for doing its job.
- **Prompts are files in the repository.** What an agent is told must be
  reviewable in a diff, next to the code that renders it.
- **The newest row is the cursor.** Derived state beats stored state wherever
  it can: there is then only one fact, and the dashboard reads the same rows
  the claim does.

## Boundaries

- No agent behaviour, no tool definitions, no conversation semantics. Those
  belong to the SDK; reimplementing them here is the thing this repository
  most has to avoid.
- No UI. The dashboard is Agent Canvas's.
- Nothing that silently drops a failure. A swallowed error becomes a recorded,
  surfaced one.

## Current goals

*Owner's section. The draft below is what the recent work implies.*

- Make continuous improvement safe to leave running: per-repository caps, one
  live run per automation, a global ceiling, and a kill switch with a reason.
- Make a runtime's exhaustion a scheduling input rather than an error, and let
  a cycle hand its work to the next runtime instead of restarting it.
- Make every routing decision a typed record with a required rationale.
