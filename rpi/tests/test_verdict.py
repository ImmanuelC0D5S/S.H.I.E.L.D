"""Verdict polling and buzzer escalation. ``docs/PROTOCOL.md`` 3.7.

The escalation ladder has one rule that must not be broken: **silence is not
consent.** A dropped network read must never be able to stand an alarm down, and
no answer at all must escalate rather than stand down. Both are tested here
because both are the kind of thing that "looks fine" in a demo.
"""

from __future__ import annotations

import pytest
from shield_rpi.config import VerdictConfig
from shield_rpi.link import FrameDecoder
from shield_rpi.verdict import Escalation, VerdictWatcher

KEY = "k" * 64


class ScriptedClient:
    """Returns a scripted sequence of verdicts, then keeps returning the last."""

    def __init__(self, script: list[str | None]) -> None:
        self.script = script
        self.calls = 0

    def fetch_verdict(self, event_id: str) -> str | None:
        self.calls += 1
        if self.calls <= len(self.script):
            return self.script[self.calls - 1]
        return self.script[-1] if self.script else None


class RecordingConnection:
    """Captures the frames the watcher sends to the sensor node."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send(self, message: dict) -> None:
        self.sent.append(message)


def watch(script, *, connection=None, timeout_s=60.0, interval=2.0, hard_stop=600.0):
    clock = [0.0]
    slept: list[float] = []

    client = ScriptedClient(script)
    watcher = VerdictWatcher(
        VerdictConfig(
            timeout_s=timeout_s, poll_interval_s=interval, hard_stop_s=hard_stop
        ),
        client,  # type: ignore[arg-type]
        clock=lambda: clock[0],
        sleep=lambda seconds: (slept.append(seconds), clock.__setitem__(0, clock[0] + seconds)),
    )
    result = watcher.watch("evt-1", connection)
    return result, client, slept


# -- the two verdicts -------------------------------------------------------


def test_known_stands_the_alarm_down():
    connection = RecordingConnection()
    result, _, _ = watch(["known"], connection=connection)

    assert result.escalation is Escalation.STOOD_DOWN
    assert result.verdict == "known"
    assert result.timed_out is False
    assert [m["type"] for m in connection.sent] == ["silence"]
    assert connection.sent[0]["reason"] == "verdict_known"


def test_intruder_raises_the_alarm():
    connection = RecordingConnection()
    result, _, _ = watch([None, "intruder"], connection=connection)

    assert result.escalation is Escalation.ALARM_RAISED
    assert result.verdict == "intruder"
    assert result.timed_out is False
    assert [m["type"] for m in connection.sent] == ["alarm"]
    assert connection.sent[0]["reason"] == "verdict_intruder"


# -- silence is not consent -------------------------------------------------


def test_no_answer_escalates_rather_than_standing_down():
    connection = RecordingConnection()
    result, _, _ = watch([None], connection=connection, timeout_s=60.0, interval=2.0)

    assert result.escalation is Escalation.ALARM_RAISED
    assert result.verdict is None
    assert result.timed_out is True
    assert len(connection.sent) == 1
    assert connection.sent[0]["type"] == "alarm"
    assert connection.sent[0]["reason"] == "verdict_timeout"
    assert connection.sent[0]["event_id"] == "evt-1"


def test_a_read_failure_is_undecided_not_a_verdict():
    """A network blip must not be readable as the user saying "known"."""
    client = ScriptedClient([None])
    client.fetch_verdict = lambda _id: None  # type: ignore[method-assign]

    connection = RecordingConnection()
    result, _, _ = watch([None], connection=connection, timeout_s=10.0, interval=2.0)
    assert result.escalation is Escalation.ALARM_RAISED
    assert result.verdict is None


def test_the_poll_actually_repeats():
    _, client, slept = watch([None, None, "known"], timeout_s=60.0, interval=2.0)
    assert client.calls == 3
    assert slept == [2.0, 2.0], "one sleep between each pair of polls"


def test_polling_stops_immediately_once_answered():
    """PROTOCOL.md 3.7 stop condition: one poll after the verdict is enough."""
    _, client, _ = watch(["known"])
    assert client.calls == 1


# -- hard stop --------------------------------------------------------------


def test_the_hard_stop_alarms_even_though_no_verdict_arrived():
    """When the hard stop and the timeout coincide, the hard stop is what runs.

    Reporting "the system gave up" to a node that has no way to distinguish it
    from "no answer" would be worse than reporting an alarm, so the loop checks
    the hard stop first.
    """
    connection = RecordingConnection()
    result, _, _ = watch(
        [None], connection=connection, timeout_s=6.0, interval=2.0, hard_stop=6.0
    )
    assert result.escalation is Escalation.GAVE_UP
    assert connection.sent[0]["type"] == "alarm"
    assert connection.sent[0]["reason"] == "verdict_poll_hard_stop"


def test_config_refuses_a_hard_stop_shorter_than_the_timeout():
    """Otherwise the hard stop would abort polling while the user is still being
    asked, which reads to them as the system giving up."""
    from shield_rpi.config import ConfigError

    with pytest.raises(ConfigError, match="verdict.hard_stop_s"):
        VerdictConfig(timeout_s=120.0, poll_interval_s=2.0, hard_stop_s=60.0)


# -- link handling ----------------------------------------------------------


def test_the_frame_sent_to_the_node_is_well_formed_and_authenticated():
    """The buzzer command rides the same HMAC link as the triggers, so it is
    framed exactly like one (DECISIONS.md D-042). A node that would reject a
    trigger must not reject an alarm."""
    from shield_rpi.link import encode_frame

    connection = RecordingConnection()
    watch(["intruder"], connection=connection)

    message = connection.sent[0]
    frame = encode_frame(message, KEY)

    (decoded,) = FrameDecoder(KEY).feed(frame)
    assert decoded.message["type"] == "alarm"
    assert decoded.message["event_id"] == "evt-1"
    assert decoded.message["reason"] == "verdict_intruder"
    assert len(message["id"]) == 8
    assert message["sent_at"].endswith("Z")


def test_escalation_without_a_link_is_logged_not_crashed():
    """A node that cannot be reached must not take down the wait loop."""
    result, _, _ = watch(["intruder"], connection=None)
    assert result.escalation is Escalation.ALARM_RAISED


def test_a_broken_connection_does_not_stop_the_watch_loop():
    class Broken:
        def send(self, _message: dict) -> None:
            raise OSError("link down")

    result, _, _ = watch(["intruder"], connection=Broken())
    assert result.escalation is Escalation.ALARM_RAISED


# -- P2 stub ----------------------------------------------------------------


def test_the_twilio_hook_exists_and_is_inert():
    """shield-app-prompt.md leaves N TBD. The hook must be present so the timer
    lands in one place when N is decided, and must not send anything yet."""
    from shield_rpi.verdict import escalate_to_twilio

    escalate_to_twilio("evt-1")  # must not raise


# -- framing sanity ---------------------------------------------------------


def test_alarm_message_fits_the_frame_budget():
    """PROTOCOL.md 2.3 caps a body at 8192 bytes; an alarm carries an event id
    and a reason and must never approach that."""
    import json

    connection = RecordingConnection()
    watch(["intruder"], connection=connection)
    body = json.dumps(connection.sent[0], separators=(",", ":")).encode()
    assert len(body) < 512, f"alarm body is {len(body)} bytes"


def test_watcher_uses_the_configured_interval():
    _, _, slept = watch([None, None, "known"], timeout_s=60.0, interval=5.0)
    assert slept == [5.0, 5.0]


@pytest.mark.parametrize("verdict", ["known", "intruder"])
def test_either_verdict_is_consumed_in_a_single_poll(verdict):
    connection = RecordingConnection()
    result, client, _ = watch([verdict], connection=connection)
    assert client.calls == 1
    assert result.verdict == verdict
    assert (connection.sent[0]["type"] == "silence") == (verdict == "known")