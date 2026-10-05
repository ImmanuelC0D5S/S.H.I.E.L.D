"""Sensor-link tests. ``docs/PROTOCOL.md`` section 2.10.

Host-side socket tests, no hardware. Section 2.10 specifies what must be
covered; each test below names the clause it pins.

The two mistakes this file exists to prevent are both silent. XOR-style checks
and ``==`` on a tag both *work* - they just stop being forgery-resistant, and
nothing about a passing smoke test would tell you. So the tag tests flip single
bits rather than checking that a wrong key fails.
"""

from __future__ import annotations

import json
import socket
import struct
import threading
import time
from pathlib import Path

import pytest
from shield_rpi.link import (
    FLAG_JSON,
    HEADER_BYTES,
    MAX_BODY,
    TAG_BYTES,
    FrameDecoder,
    LinkHealth,
    LinkServer,
    ProtocolViolationError,
    TagMismatchError,
    compute_tag,
    encode_frame,
    new_message,
)

KEY = "k" * 64
OTHER_KEY = "z" * 64


def _body(message: dict) -> bytes:
    return json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


# -- 2.3 framing ------------------------------------------------------------


def test_frame_round_trip_preserves_len_flags_and_tag():
    message = new_message("trigger", tier="high", sensors=["pir", "mmwave"])
    frame = encode_frame(message, KEY)

    declared = int.from_bytes(frame[:4], "big")
    assert declared == len(frame) - HEADER_BYTES - TAG_BYTES
    assert frame[4] == FLAG_JSON

    decoder = FrameDecoder(KEY)
    (decoded,) = decoder.feed(frame)
    assert decoded.message["type"] == "trigger"
    assert decoded.message["v"] == 2
    assert decoded.message["tier"] == "high"
    assert decoded.message["sensors"] == ["pir", "mmwave"]


def test_tag_covers_len_flags_and_body_exactly():
    """PROTOCOL.md 2.5: the tag is over ``len || flags || body``, nothing else."""
    body = _body({"v": 2, "type": "heartbeat"})
    prefix = len(body).to_bytes(4, "big") + bytes([FLAG_JSON])
    assert compute_tag(KEY, prefix, body) == compute_tag(KEY, prefix, body)
    # Changing anything inside the covered range changes the tag.
    assert compute_tag(KEY, prefix, body) != compute_tag(KEY, prefix, body + b" ")
    assert compute_tag(KEY, prefix, body) != compute_tag(
        KEY, (len(body) + 1).to_bytes(4, "big") + bytes([FLAG_JSON]), body
    )
    assert compute_tag(KEY, prefix, body) != compute_tag(KEY, prefix[:-1] + b"\x00", body)


# -- 2.5 test vector --------------------------------------------------------
#: PROTOCOL.md 2.5.1. Fixed so the ESP32 sketch's ``hmacTag()``, ``link.py`` and
#: ``tools/fake_esp32.py`` can be compared byte for byte without a board.
VECTOR_KEY = "00112233445566778899aabbccddeeff"
VECTOR_BODY = (
    b'{"v":2,"type":"trigger","id":"cafe0001","ts_ms":1234567,'
    b'"sent_at":"2026-10-01T09:14:03.512Z","tier":"high","sensors":["pir","mmwave"]}'
)
VECTOR_PREFIX = bytes.fromhex("0000008601")
VECTOR_TAG = bytes.fromhex("ec9773c93f151c953b74aea31f529427")


def test_protocol_test_vector_tag():
    """2.5.1: compute the documented frame instead of trusting it."""
    assert len(VECTOR_BODY) == 134
    assert len(VECTOR_BODY).to_bytes(4, "big") + bytes([FLAG_JSON]) == VECTOR_PREFIX
    assert compute_tag(VECTOR_KEY, VECTOR_PREFIX, VECTOR_BODY) == VECTOR_TAG


def test_protocol_test_vector_is_the_documented_one():
    """The constants above and the ones printed in PROTOCOL.md are the same ones."""
    document = Path(__file__).resolve().parents[2] / "docs" / "PROTOCOL.md"
    text = document.read_text(encoding="utf-8")
    assert VECTOR_KEY in text, "test key no longer documented"
    assert VECTOR_PREFIX.hex() in text, "prefix no longer documented"
    assert VECTOR_TAG.hex() in text, "tag no longer documented"


def test_encode_frame_reproduces_the_documented_frame():
    """Serialising the vector's message must yield exactly the documented bytes."""
    assert encode_frame(json.loads(VECTOR_BODY), VECTOR_KEY) == (
        VECTOR_PREFIX + VECTOR_BODY + VECTOR_TAG
    )


def test_decoder_accepts_the_documented_frame():
    """The frame the RPi prints must also be the frame the RPi parses."""
    (decoded,) = FrameDecoder(VECTOR_KEY).feed(VECTOR_PREFIX + VECTOR_BODY + VECTOR_TAG)
    assert decoded.message["type"] == "trigger"
    assert decoded.message["id"] == "cafe0001"
    assert decoded.message["sensors"] == ["pir", "mmwave"]


def test_flipped_bit_in_len_is_rejected():
    frame = bytearray(encode_frame(new_message("trigger"), KEY))
    frame[3] ^= 0x01
    decoder = FrameDecoder(KEY)
    assert decoder.feed(bytes(frame)) == []
    assert decoder.fault_count == 1


def test_flipped_bit_in_flags_is_rejected():
    frame = bytearray(encode_frame(new_message("trigger"), KEY))
    frame[4] ^= 0x01
    decoder = FrameDecoder(KEY)
    assert decoder.feed(bytes(frame)) == []
    assert decoder.fault_count == 1


@pytest.mark.parametrize("offset", [5, 9, 14])
def test_single_flipped_bit_in_body_is_rejected(offset):
    frame = bytearray(encode_frame(new_message("trigger"), KEY))
    frame[offset] ^= 0x01
    decoder = FrameDecoder(KEY)
    assert decoder.feed(bytes(frame)) == [], "a forged frame must never be returned"
    assert decoder.fault_count == 1
    assert isinstance(decoder.faults[0], TagMismatchError)


def test_a_bad_frame_does_not_hide_the_good_frame_behind_it():
    """The frame boundary survives a bad tag, because ``len`` is range-checked
    before the tag is examined. Dropping the rest of the buffer would let one
    forged frame blind the node for the rest of the packet."""
    good = encode_frame(new_message("heartbeat"), KEY)
    bad = bytearray(encode_frame(new_message("trigger"), KEY))
    bad[8] ^= 0xFF

    decoder = FrameDecoder(KEY)
    frames = decoder.feed(bytes(bad) + good)

    assert [f.message["type"] for f in frames] == ["heartbeat"]
    assert decoder.fault_count == 1


def test_each_forged_frame_counts_individually():
    """Otherwise a peer could pack a hundred forgeries into one packet and be
    judged as a single attempt against the three-fault limit."""
    bad = bytearray(encode_frame(new_message("trigger"), KEY))
    bad[8] ^= 0xFF
    decoder = FrameDecoder(KEY)
    assert decoder.feed(bytes(bad) * 5) == []
    assert decoder.fault_count == 5


def test_take_faults_clears_them():
    decoder = FrameDecoder(KEY)
    frame = bytearray(encode_frame(new_message("trigger"), KEY))
    frame[8] ^= 0xFF
    decoder.feed(bytes(frame))
    assert len(decoder.take_faults()) == 1
    assert decoder.fault_count == 0
    assert decoder.take_faults() == []


def test_wrong_key_is_rejected():
    decoder = FrameDecoder(OTHER_KEY)
    assert decoder.feed(encode_frame(new_message("trigger"), KEY)) == []
    assert isinstance(decoder.faults[0], TagMismatchError)


def test_tag_check_happens_before_json_parse():
    """PROTOCOL.md 2.5: an unauthenticated body is never parsed.

    A malformed body with a bad tag must be recorded as a tag failure, not a JSON
    error. If the order ever inverts, a hostile frame reaches the JSON decoder -
    which is exactly what the ordering forbids.
    """
    hostile = b"{not json at all"
    prefix = len(hostile).to_bytes(4, "big") + bytes([FLAG_JSON])
    frame = prefix + hostile + b"\x00" * TAG_BYTES

    decoder = FrameDecoder(KEY)
    assert decoder.feed(frame) == []
    assert isinstance(decoder.faults[0], TagMismatchError)


def test_oversized_len_raises_because_the_stream_cannot_be_trusted():
    """PROTOCOL.md 2.3: a body larger than 8192 is a violation, not a big message.

    Unlike a bad tag this one is fatal rather than counted, because there is no
    magic byte to resynchronise on and the receiver must close instead.
    """
    frame = (MAX_BODY + 1).to_bytes(4, "big") + bytes([FLAG_JSON]) + b"\x00" * 32
    with pytest.raises(ProtocolViolationError, match="exceeds"):
        FrameDecoder(KEY).feed(frame)


def test_authenticated_but_malformed_json_is_a_protocol_violation():
    body = b"{not json at all"
    prefix = len(body).to_bytes(4, "big") + bytes([FLAG_JSON])
    frame = prefix + body + compute_tag(KEY, prefix, body)
    decoder = FrameDecoder(KEY)
    assert decoder.feed(frame) == []
    assert isinstance(decoder.faults[0], ProtocolViolationError)
    assert "not valid JSON" in str(decoder.faults[0])


def test_non_json_flag_is_rejected():
    body = _body({"v": 2, "type": "trigger"})
    prefix = len(body).to_bytes(4, "big") + bytes([0x00])
    frame = prefix + body + compute_tag(KEY, prefix, body)
    decoder = FrameDecoder(KEY)
    assert decoder.feed(frame) == []
    assert isinstance(decoder.faults[0], ProtocolViolationError)


def test_wrong_protocol_version_is_rejected():
    body = _body({"v": 1, "type": "trigger"})
    prefix = len(body).to_bytes(4, "big") + bytes([FLAG_JSON])
    frame = prefix + body + compute_tag(KEY, prefix, body)
    decoder = FrameDecoder(KEY)
    assert decoder.feed(frame) == []
    assert "unsupported protocol version" in str(decoder.faults[0])


def test_encode_refuses_oversized_body():
    with pytest.raises(ProtocolViolationError, match="max is"):
        encode_frame({"v": 2, "type": "trigger", "pad": "x" * (MAX_BODY + 10)}, KEY)


# -- 2.3 reassembly ---------------------------------------------------------


def test_frame_split_across_reads_is_reassembled():
    """On a real socket a frame routinely arrives in pieces. Dropping the tail
    would make every large message look like a desync and close the link."""
    frame = encode_frame(new_message("trigger", sensors=["pir"]), KEY)
    decoder = FrameDecoder(KEY)

    collected = []
    for index in range(0, len(frame), 7):
        collected.extend(decoder.feed(frame[index : index + 7]))

    assert len(collected) == 1
    assert collected[0].message["type"] == "trigger"
    assert decoder.pending == 0


def test_two_frames_in_one_chunk_yield_two():
    frame = encode_frame(new_message("heartbeat"), KEY)
    decoder = FrameDecoder(KEY)
    assert len(decoder.feed(frame + frame)) == 2


def test_incomplete_frame_buffers_without_raising():
    frame = encode_frame(new_message("trigger"), KEY)
    decoder = FrameDecoder(KEY)
    assert decoder.feed(frame[:-1]) == []
    assert decoder.pending == len(frame) - 1
    assert len(decoder.feed(frame[-1:])) == 1


# -- 2.4 envelope -----------------------------------------------------------


def test_new_message_envelope_shape():
    message = new_message("reply", trigger_id="a3f19c02", status=2, confidence=82)
    assert message["v"] == 2
    assert message["type"] == "reply"
    assert len(message["id"]) == 8
    int(message["id"], 16)  # hex, 8 chars
    assert isinstance(message["ts_ms"], int)
    assert message["sent_at"].endswith("Z")
    assert message["trigger_id"] == "a3f19c02"
    assert message["status"] == 2


def test_message_ids_are_unique():
    assert len({new_message("trigger")["id"] for _ in range(200)}) == 200


# -- 2.8 liveness -----------------------------------------------------------


def test_health_reports_down_before_any_frame():
    assert LinkHealth().alive is False


def test_health_goes_down_after_the_heartbeat_timeout():
    health = LinkHealth()
    health.note_frame(now=time.monotonic() - 7.0)
    assert health.alive is False


def test_health_logs_one_warning_per_down_transition():
    """PROTOCOL.md 2.8: log once per transition, not once per poll."""
    health = LinkHealth()
    health.last_frame_at = time.monotonic() - 10.0

    for _ in range(5):
        assert health.check() is False
    assert health.transitions == 1

    health.note_frame()
    assert health.check() is True

    health.last_frame_at = time.monotonic() - 10.0
    assert health.check() is False
    assert health.transitions == 2


# -- 2.7 server / client ----------------------------------------------------


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class Harness:
    """A real LinkServer on loopback plus a raw socket client."""

    def __init__(self, grace: float = 1.0) -> None:
        self.port = _free_port()
        self.received: list[dict] = []
        self.server = LinkServer(
            "127.0.0.1", self.port, KEY, frame_timeout_s=1.0, reconnect_grace_s=grace
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever, args=(self._handler,), daemon=True
        )

    def _handler(self, message: dict, connection) -> None:
        """Mirror the real dispatch: ack heartbeats, reply to triggers."""
        self.received.append(message)
        if message.get("type") == "heartbeat":
            connection.send(new_message("heartbeat_ack", echo=message.get("id")))
            return
        connection.send(
            new_message(
                "reply",
                trigger_id=message.get("id"),
                status=2,
                confidence=message.get("confidence", 0),
            )
        )

    def __enter__(self) -> Harness:
        self.server.bind()
        self.thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.server.shutdown()

    def connect(self) -> socket.socket:
        sock = socket.socket()
        sock.settimeout(2.0)
        sock.connect(("127.0.0.1", self.port))
        return sock


def _read_reply(sock: socket.socket, key: bytes = KEY.encode()) -> dict:
    decoder = FrameDecoder(key)
    deadline = time.time() + 2.0
    while time.time() < deadline:
        chunk = sock.recv(4096)
        if not chunk:
            break
        frames = decoder.feed(chunk)
        if frames:
            return frames[0].message
    raise AssertionError("no reply received")


def test_trigger_gets_a_reply_over_a_real_socket():
    with Harness() as h, h.connect() as sock:
        sock.sendall(encode_frame(new_message("trigger", tier="high"), KEY))
        reply = _read_reply(sock)
        assert reply["type"] == "reply"
        assert reply["status"] == 2


def test_heartbeat_is_acknowledged_immediately():
    """PROTOCOL.md 2.8: heartbeat_ack is immediate."""
    with Harness() as h, h.connect() as sock:
        sock.sendall(encode_frame(new_message("heartbeat"), KEY))
        reply = _read_reply(sock)
        assert reply["type"] == "heartbeat_ack"


def test_unknown_message_type_does_not_kill_the_link():
    with Harness() as h, h.connect() as sock:
        sock.sendall(encode_frame(new_message("nonsense"), KEY))
        # The handler replies to anything; what matters is the server is
        # still serving afterwards.
        _read_reply(sock)
        sock.sendall(encode_frame(new_message("heartbeat"), KEY))
        assert _read_reply(sock)["type"] == "heartbeat_ack"


def test_duplicate_trigger_is_answered_not_dropped():
    """PROTOCOL.md 2.7: every valid frame is dispatched and answered."""
    with Harness() as h, h.connect() as sock:
        frame = encode_frame(new_message("trigger"), KEY)
        sock.sendall(frame)
        first = _read_reply(sock)
        sock.sendall(frame)
        second = _read_reply(sock)
        assert first["trigger_id"] == second["trigger_id"]


def test_server_survives_a_dropped_mid_frame():
    """A peer vanishing mid-frame is a network event, not a protocol violation."""
    with Harness() as h:
        sock = h.connect()
        frame = encode_frame(new_message("trigger"), KEY)
        sock.sendall(frame[: len(frame) // 2])
        sock.close()

        with h.connect() as second:
            second.sendall(encode_frame(new_message("heartbeat"), KEY))
            assert _read_reply(second)["type"] == "heartbeat_ack"


def test_a_node_that_reconnects_immediately_is_not_refused():
    """PROTOCOL.md 2.7 step 3 reconnects from 500 ms upward. A strict
    "one connection at a time" rule would refuse that reconnect while the server
    is still reading the dead socket, and the node would have to sit out a full
    backoff step for no reason."""
    with Harness(grace=1.0) as h:
        first = h.connect()
        frame = encode_frame(new_message("trigger"), KEY)
        first.sendall(frame[: len(frame) // 2])
        first.close()

        with h.connect() as second:
            second.sendall(encode_frame(new_message("heartbeat"), KEY))
            assert _read_reply(second)["type"] == "heartbeat_ack"
        assert h.server.stats.rejected == 0


def test_a_graceful_node_still_loses_to_a_live_one():
    """The grace window delays the rejection, it does not permit two sensors."""
    with Harness(grace=0.25) as h:
        first = h.connect()
        try:
            first.sendall(encode_frame(new_message("heartbeat"), KEY))
            _read_reply(first)

            intruder = h.connect()
            intruder.settimeout(3.0)
            try:
                assert intruder.recv(4096) == b""
            except (TimeoutError, ConnectionResetError, OSError):
                pass  # a reset is an equally valid rejection
            finally:
                intruder.close()
            assert h.server.stats.rejected >= 1
        finally:
            first.close()


def test_three_tag_failures_close_the_connection():
    """PROTOCOL.md 2.7 step 5: at three faults on a connection, close it."""
    with Harness() as h:
        sock = h.connect()
        bad = bytearray(encode_frame(new_message("trigger"), KEY))
        bad[8] ^= 0xFF

        for _ in range(3):
            try:
                sock.sendall(bytes(bad))
            except OSError:
                break

        # The server closes; the client sees EOF rather than a fourth reply.
        sock.settimeout(2.0)
        deadline = time.time() + 2.0
        closed = False
        while time.time() < deadline:
            try:
                if sock.recv(4096) == b"":
                    closed = True
                    break
            except (TimeoutError, OSError):
                closed = True
                break
        assert closed, "server should have closed the connection after three faults"
        assert h.server.stats.faults >= 3


def test_second_connection_is_rejected_and_closed():
    """PROTOCOL.md 2.7: one sensor node is expected."""
    with Harness(grace=0.25) as h:
        first = h.connect()
        try:
            first.sendall(encode_frame(new_message("heartbeat"), KEY))
            _read_reply(first)

            intruder = h.connect()
            intruder.settimeout(2.0)
            try:
                assert intruder.recv(4096) == b""
            except (TimeoutError, ConnectionResetError, OSError):
                pass  # a reset is an equally valid rejection
            finally:
                intruder.close()
            assert h.server.stats.rejected >= 1
        finally:
            first.close()


def test_frame_is_well_formed_on_the_wire():
    """Pin the exact bytes so a change to the framing cannot pass unnoticed."""
    message = new_message("trigger", tier="low", sensors=["pir"])
    message["id"] = "a3f19c02"
    message["ts_ms"] = 1234567
    message["sent_at"] = "2026-10-01T09:14:03.512Z"

    frame = encode_frame(message, KEY)
    assert len(frame) == HEADER_BYTES + len(_body(message)) + TAG_BYTES
    assert struct.unpack(">I", frame[:4])[0] == len(_body(message))
    assert frame[4] == 1
    assert frame[-TAG_BYTES:] == compute_tag(KEY, frame[:HEADER_BYTES], _body(message))