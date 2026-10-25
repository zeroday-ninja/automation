"""Which runtime actually ran a conversation.

A conversation changes runtime underneath itself when the SDK fails over on a
usage limit. The profile says what the cycle began on; only the conversation's
own events say what produced the work. Everything downstream -- the audit
trail, the reviewer choice, the ledger that decides which subscription to
spend next -- reads the second one.
"""

from openhands.automation.presets.improve.context import trace_runtime


NOW = "2026-10-24T09:00:00+00:00"


def _switch(
    came_from: str,
    went_to: str,
    *,
    reason: str = "usage_limit",
    at: str = "2026-10-24T08:00:00",
    hint: dict | None = None,
) -> dict:
    event = {
        "kind": "ACPRuntimeSwitchEvent",
        "from_server": came_from,
        "to_server": went_to,
        "to_model": None,
        "reason": reason,
        "detail": "you've hit your usage limit",
        "timestamp": at,
    }
    if hint is not None:
        event["reset_hint"] = hint
    return event


def test_the_runtime_that_finished_is_the_one_recorded() -> None:
    trace = trace_runtime(
        [_switch("claude-code", "codex")],
        started_on="claude-code",
        finished=True,
        conversation_id="c1",
        observed_at=NOW,
    )

    assert trace["ran"] == "codex"
    assert trace["route"] == ["claude-code", "codex"]
    assert [(s["from"], s["to"], s["reason"]) for s in trace["switches"]] == [
        ("claude-code", "codex", "usage_limit")
    ]


def test_an_abandoned_runtime_is_reported_spent_and_the_finisher_healthy() -> None:
    trace = trace_runtime(
        [
            _switch(
                "claude-code",
                "codex",
                hint={
                    "at": "2026-10-24T12:00:00+00:00",
                    "source": "resets 12pm (UTC)",
                    "confidence": "exact",
                },
            )
        ],
        started_on="claude-code",
        finished=True,
        conversation_id="c1",
        observed_at=NOW,
    )

    assert [(o["runtime"], o["outcome"]) for o in trace["observations"]] == [
        ("claude-code", "failed_over_from"),
        ("codex", "ok"),
    ]
    spent = trace["observations"][0]
    assert spent["reset_at"] == "2026-10-24T12:00:00+00:00"
    assert spent["reset_source"] == "resets 12pm (UTC)"
    assert spent["conversation_id"] == "c1"


def test_a_chain_that_runs_out_leaves_no_runtime_looking_healthy() -> None:
    trace = trace_runtime(
        [
            _switch("claude-code", "codex"),
            {
                "kind": "ConversationErrorEvent",
                "code": "ACPUsageLimitChainExhausted",
                "detail": "Every ACP runtime in the failover chain...",
                "timestamp": "2026-10-24T08:30:00",
            },
        ],
        started_on="claude-code",
        finished=False,
        conversation_id="c1",
        observed_at=NOW,
    )

    assert trace["ran"] == "codex"
    assert [(o["runtime"], o["outcome"]) for o in trace["observations"]] == [
        ("claude-code", "failed_over_from"),
        ("codex", "exhausted"),
    ]


def test_a_manual_switch_moves_the_route_without_blaming_a_runtime() -> None:
    trace = trace_runtime(
        [_switch("claude-code", "codex", reason="manual")],
        started_on="claude-code",
        finished=True,
        conversation_id="c1",
        observed_at=NOW,
    )

    # Nothing was learned about claude-code's usage: the user moved, the
    # runtime did not refuse.
    assert [(o["runtime"], o["outcome"]) for o in trace["observations"]] == [
        ("codex", "ok")
    ]
    assert trace["switches"][0]["reason"] == "manual"


def test_a_conversation_that_never_switched_reports_its_own_runtime_ok() -> None:
    trace = trace_runtime(
        [{"kind": "MessageEvent", "source": "agent"}],
        started_on="codex",
        finished=True,
        conversation_id="c1",
        observed_at=NOW,
    )

    assert trace["ran"] == "codex"
    assert trace["switches"] == []
    assert [(o["runtime"], o["outcome"]) for o in trace["observations"]] == [
        ("codex", "ok")
    ]


def test_events_that_could_not_be_read_are_degraded_not_silent() -> None:
    trace = trace_runtime(
        None,
        started_on="codex",
        finished=True,
        conversation_id="c1",
        observed_at=NOW,
    )

    # The distinction that matters: unreadable events must not be reported as
    # "nothing switched", because that is a claim about what happened.
    assert trace["degraded"]
    assert trace["ran"] == "codex"

    # And the failure is recorded rather than only logged. It earns no `ok` —
    # that would clear a real exhaustion on the strength of the one thing
    # nobody observed — but it does earn a `degraded` observation, because
    # `misattributed_cycles` treats the switch trail as the authority on which
    # runtime finished the work and has to be able to see that this trail
    # could not be read.
    assert [item["outcome"] for item in trace["observations"]] == ["degraded"]
    assert trace["observations"][0]["evidence"] == trace["degraded"]
    assert trace["observations"][0]["runtime"] == "codex"
