"""Verdict polling and buzzer escalation.

PROTOCOL.md 3.7. Polling is **event-scoped, not continuous**: it starts when an
alert is published, runs until the user answers, and then stops. There is no
background poll loop, so an idle sensor node costs zero uplink data.

The escalation ladder:

* ``known``   -> stand down. No buzzer.
* ``intruder``-> alarm.
* no answer by ``verdict.timeout_s`` -> alarm. Absence of an answer is not
  consent, and treating silence as "probably fine" is the failure mode the whole
  fail-safe rule exists to prevent.

Read failures are reported as *undecided*, never as a verdict. A dropped
connection must not be able to stand an alarm down.
"""

from __future__ import annotations

import enum
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from shield_rpi.alerts import SupabaseClient
from shield_rpi.config import VerdictConfig
from shield_rpi.link import LinkConnection, new_message

LOGGER = logging.getLogger(__name__)


class Escalation(enum.Enum):
    """What the escalation loop concluded."""

    STOOD_DOWN = "stood_down"
    ALARM_RAISED = "alarm_raised"
    GAVE_UP = "gave_up"
    LINK_LOST = "link_lost"


#: P2 stub. shield-app-prompt.md leaves the Twilio timeout "N = TBD, matching
#: the Telegram timeout". The hook is here so the timer lands in one place when
#: N is decided; nothing calls it yet.
def escalate_to_twilio(event_id: str) -> None:  # pragma: no cover - stub
    LOGGER.warning(
        "Twilio escalation is a P2 stub (N is TBD upstream); would SMS for %s", event_id
    )


@dataclass(frozen=True)
class VerdictResult:
    """Outcome of waiting for the user's answer."""

    escalation: Escalation
    verdict: str | None
    waited_s: float
    polls: int
    timed_out: bool


class VerdictWatcher:
    """Poll ``events.verdict`` until answered, then escalate or stand down.

    ``alarm`` and ``silence`` are sent as ``alarm``/``silence`` messages over the
    sensor link (PROTOCOL.md 2.4, added by DECISIONS.md D-042). They used to be
    HTTP ``POST /alarm`` in shield-esp32-prompt.md, which the reviewed TCP
    protocol has no room for; putting them on the same authenticated link means a
    node that cannot be reached cannot also be silenced.
    """

    def __init__(
        self,
        config: VerdictConfig,
        client: SupabaseClient,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.client = client
        self._clock = clock
        self._sleep = sleep

    def _send(
        self, connection: LinkConnection | None, message_type: str, **fields: object
    ) -> bool:
        if connection is None:
            LOGGER.warning(
                "no sensor link; %s could not be sent to the node", message_type
            )
            return False
        try:
            connection.send(new_message(message_type, **fields))
            return True
        except Exception as exc:  # noqa: BLE001 - link errors must not stop the loop
            LOGGER.warning("could not send %s to the sensor node: %s", message_type, exc)
            return False

    def watch(
        self, event_id: str, connection: LinkConnection | None = None
    ) -> VerdictResult:
        """Wait for a verdict on ``event_id`` and act on it."""
        started = self._clock()
        polls = 0
        LOGGER.info("watching %s for up to %.0fs", event_id, self.config.timeout_s)

        while True:
            elapsed = self._clock() - started
            if elapsed >= self.config.hard_stop_s:
                LOGGER.error(
                    "gave up on %s after %.0fs with no verdict", event_id, elapsed
                )
                self._send(connection, "alarm", reason="verdict_poll_hard_stop", event_id=event_id)
                escalate_to_twilio(event_id)
                return VerdictResult(
                    escalation=Escalation.GAVE_UP, verdict=None, waited_s=elapsed,
                    polls=polls, timed_out=True,
                )

            polls += 1
            verdict = self.client.fetch_verdict(event_id)

            if verdict == "known":
                LOGGER.info("user answered 'known' on %s after %.0fs", event_id, elapsed)
                self._send(connection, "silence", reason="verdict_known", event_id=event_id)
                return VerdictResult(
                    escalation=Escalation.STOOD_DOWN, verdict=verdict,
                    waited_s=elapsed, polls=polls, timed_out=False,
                )

            if verdict == "intruder":
                LOGGER.info("user answered 'intruder' on %s after %.0fs", event_id, elapsed)
                self._send(connection, "alarm", reason="verdict_intruder", event_id=event_id)
                escalate_to_twilio(event_id)
                return VerdictResult(
                    escalation=Escalation.ALARM_RAISED, verdict=verdict,
                    waited_s=elapsed, polls=polls, timed_out=False,
                )

            if elapsed >= self.config.timeout_s:
                # No answer. Silence is not consent.
                LOGGER.warning(
                    "no verdict for %s after %.0fs; escalating to the buzzer", event_id, elapsed
                )
                self._send(connection, "alarm", reason="verdict_timeout", event_id=event_id)
                escalate_to_twilio(event_id)
                return VerdictResult(
                    escalation=Escalation.ALARM_RAISED, verdict=None,
                    waited_s=elapsed, polls=polls, timed_out=True,
                )

            self._sleep(self.config.poll_interval_s)