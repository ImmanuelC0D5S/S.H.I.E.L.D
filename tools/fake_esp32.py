"""Stand-in sensor node: drive a running RPi service over the real link.

``tools/fake_esp32.py`` speaks PROTOCOL.md section 2 exactly as the ESP32 does —
same framing, same HMAC, same message types — so the RPi service can be
exercised end to end with no board, no Wi-Fi and no PIR wired up. It connects,
sends one ``trigger``, prints everything the RPi says back (``reply``,
``alarm``, ``silence``), answers ``alarm`` with ``alarm_ack`` and heartbeats
every 2 s, then leaves.

    python tools/fake_esp32.py --host 127.0.0.1 --tail 5
    python tools/fake_esp32.py --host 192.168.50.1 --tier high --sensors pir
    python tools/fake_esp32.py --host 127.0.0.1 --verbose

The key is ``SHIELD_LINK_KEY`` from the environment or ``.env`` — the same one
the ESP32 signs with. It is never printed.

The default host is the deployed one (``192.168.50.1``, the RPi's address on
``shield-net``). On anything else, pass ``--host 127.0.0.1`` *and* start the
service with a config whose ``link.host`` is loopback: binding ``192.168.50.1``
on a laptop that is not hosting that AP fails. See PROTOCOL.md 2.10.

Exit codes: 0 = a reply for this trigger arrived, 2 = no reply before the
timeout, 1 = could not connect or the link broke.
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rpi"))

from shield_rpi.link import (  # noqa: E402
    FrameDecoder,
    LinkError,
    encode_frame,
    new_message,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

#: PROTOCOL.md 2.8: a node that sends nothing for 6 s is judged down.
HEARTBEAT_S = 2.0

#: PROTOCOL.md 2.9: the RPi answers a trigger within this long.
DEFAULT_REPLY_TIMEOUT_S = 5.0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="192.168.50.1",
                        help="link host (default: the RPi on shield-net; use 127.0.0.1 on a laptop)")
    parser.add_argument("--port", type=int, default=8765, help="link port (PROTOCOL 2.1: 8765)")
    parser.add_argument("--key", default=None,
                        help="HMAC key; defaults to SHIELD_LINK_KEY from the environment or .env")
    parser.add_argument("--env-file", default=str(REPO_ROOT / ".env"),
                        help="dotenv file to read SHIELD_LINK_KEY from")
    parser.add_argument("--tier", choices=("low", "high"), default="low",
                        help="trigger tier (PROTOCOL 2.6). low unless a second sensor agreed.")
    parser.add_argument("--sensors", default="pir",
                        help="comma-separated sensor names, e.g. pir or pir,mmwave")
    parser.add_argument("--reply-timeout", type=float, default=DEFAULT_REPLY_TIMEOUT_S,
                        help="seconds to wait for the reply before giving up")
    parser.add_argument("--tail", type=float, default=0.0,
                        help="seconds to keep listening after the reply, to catch alarm/silence")
    parser.add_argument("--verbose", action="store_true", help="print hex frames too")
    return parser.parse_args(argv)


def _load_key(args: argparse.Namespace) -> str:
    """The link key, from --key, the environment, or .env. Never logged."""
    if args.key:
        return args.key
    if os.environ.get("SHIELD_LINK_KEY"):
        return os.environ["SHIELD_LINK_KEY"]

    env_path = Path(args.env_file)
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            name, _, value = stripped.partition("=")
            if name.strip() == "SHIELD_LINK_KEY" and value.strip():
                return value.strip()

    raise SystemExit(
        "SHIELD_LINK_KEY is not set. Export it, fill in .env, or pass --key. "
        "The RPi refuses unkeyed frames, so there is no unauthenticated mode."
    )


def say(text: str) -> None:
    print(f"[fake-esp32] {text}", flush=True)


def _describe(message: dict) -> str:
    """One line for whatever the RPi just said."""
    kind = message.get("type")
    if kind == "reply":
        return (f"<- reply status={message.get('status')} "
                f"confidence={message.get('confidence')} "
                f"trigger_id={message.get('trigger_id')}")
    if kind == "alarm":
        return f"<- ALARM reason={message.get('reason')} event_id={message.get('event_id')}"
    if kind == "silence":
        return f"<- silence reason={message.get('reason')} event_id={message.get('event_id')}"
    if kind == "heartbeat_ack":
        return f"<- heartbeat_ack echo={message.get('echo')}"
    return f"<- {kind}: {message}"


def run(args: argparse.Namespace) -> int:
    key = _load_key(args)
    sensors = [s.strip() for s in args.sensors.split(",") if s.strip()]
    trigger = new_message("trigger", tier=args.tier, sensors=sensors)

    say(f"connecting to {args.host}:{args.port} ...")
    try:
        sock = socket.create_connection((args.host, args.port), timeout=5.0)
    except OSError as exc:
        say(f"connect failed: {exc}")
        say("is the service running, and is its link.host reachable from here? (PROTOCOL 2.10)")
        return 1

    sock.settimeout(0.5)
    decoder = FrameDecoder(key)
    say(f"-> trigger id={trigger['id']} tier={args.tier} sensors={sensors}")
    sock.sendall(encode_frame(trigger, key))
    heartbeat_due = time.monotonic() + HEARTBEAT_S
    sent_at = time.monotonic()
    deadline = sent_at + args.reply_timeout
    tail_until: float | None = None
    reply_seen = False

    try:
        while True:
            now = time.monotonic()
            if reply_seen and tail_until is not None and now >= tail_until:
                break
            if not reply_seen and now >= deadline:
                say(f"no reply within {args.reply_timeout:g}s; the RPi did not answer "
                    f"trigger id={trigger['id']}")
                return 2

            if now >= heartbeat_due:
                heartbeat = new_message("heartbeat")
                sock.sendall(encode_frame(heartbeat, key))
                if args.verbose:
                    say(f"-> heartbeat id={heartbeat['id']}")
                heartbeat_due = now + HEARTBEAT_S

            try:
                chunk = sock.recv(4096)
            except TimeoutError:
                continue
            if not chunk:
                say("server closed the connection")
                return 1 if not reply_seen else 0

            if args.verbose:
                say(f"rx {len(chunk)} bytes: {chunk.hex()}")
            try:
                frames = decoder.feed(chunk)
            except LinkError as exc:
                say(f"protocol violation: {exc}")
                return 1

            for fault in decoder.take_faults():
                say(f"frame dropped: {fault}")

            for decoded in frames:
                message = decoded.message
                say(_describe(message))
                if message.get("type") == "reply":
                    reply_seen = True
                    tail_until = time.monotonic() + args.tail if args.tail else None
                    if not args.tail:
                        break
                elif message.get("type") == "alarm":
                    # Same answer the firmware gives (D-042): the RPi must know
                    # the buzzer actually started, not just that we heard it.
                    ack = new_message("alarm_ack", reason=message.get("reason", "unknown"),
                                      event_id=message.get("event_id", ""))
                    sock.sendall(encode_frame(ack, key))
                    say(f"-> alarm_ack id={ack['id']}")
                elif message.get("type") == "heartbeat_ack":
                    continue
                else:
                    # Unknown type: log it, do not answer. Same rule as the
                    # firmware and as service.py's own handler.
                    say(f"unhandled message type {message.get('type')!r}; ignored")
            else:
                continue
            break
    except (OSError, LinkError) as exc:
        say(f"link failed: {exc}")
        return 1
    finally:
        sock.close()

    if reply_seen:
        say(f"done after {time.monotonic() - sent_at:.1f}s")
        return 0
    return 2


def main(argv: list[str] | None = None) -> int:
    return run(parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
