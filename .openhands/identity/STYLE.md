# How code and prose are written here

## Comments earn their place

A comment says **why**, never what. The bar: would a competent reader who has
the code in front of them still be missing this? If not, delete it. The best
comments in this repository name the failure the code is avoiding.

## Naming

Say the thing. `claim_next_cycle`, `mark_stale_runs`, `skip_pending_runs_for_
disabled_automation`. A name that needs a comment to explain it is the wrong
name.

## Errors

Every degraded path is recorded and surfaced. "Non-fatal" in a log line is a
smell: if it is worth printing, it is worth putting somewhere the user can see
it afterwards.

## Tests

Arrange, Act, Assert, with the comment on Assert saying what would be broken
if it failed. Test the property, not the transcript. A test that mirrors the
implementation's literals proves only that the file was copied correctly.

## Migrations

One head. Both `postgresql_where` and `sqlite_where` on a partial index: this
service claims PostgreSQL everywhere and is deployed on SQLite anyway.
