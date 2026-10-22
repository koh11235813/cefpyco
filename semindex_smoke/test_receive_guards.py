"""Fake-handle tests for the receive-side accept/reject logic: no cefnetd, no root required.

consumer.wait_for_answer() and state_freshness.validate_answer() both decide
whether a received packet counts as a real pass, by inspecting timing and
packet shape rather than just "something with the right name arrived". Those
decisions cannot be exercised through test_wire.py's pure-value tests alone,
since they depend on a handle's receive() calls and (for consumer.py) on
elapsed wall-clock time -- so this module supplies a minimal fake handle and
a controllable fake clock to drive them deterministically.

Run with: .venv/bin/python3 -m pytest semindex_smoke/test_receive_guards.py -v
"""

from __future__ import annotations

import consumer
from state_freshness import validate_answer
from wire import encode_answer


class _FakeClock:
    """A monotonic-clock stand-in that only advances when `_FakeHandle.receive()` is called.

    time.monotonic() itself does not block or consume wall-clock time; only
    the blocking receive() call does. Driving the simulated clock from
    receive() rather than from every time.monotonic() read keeps the
    advance-per-poll-iteration accounting exact instead of approximate.
    """

    def __init__(self) -> None:
        self.now = 0.0


class _FakePacket:
    """A minimal stand-in for cefpyco's CcnPacketInfo, exposing only the attributes these scripts read."""

    def __init__(
        self,
        name: str = "",
        payload: bytes = b"",
        chunk_num: int | None = None,
        is_data: bool = False,
        is_interest_return: bool = False,
    ) -> None:
        self.name = name
        self.payload = payload
        self.chunk_num = chunk_num
        self.is_data = is_data
        self.is_interest_return = is_interest_return


class _FakeHandle:
    """Delivers one canned packet at an exact simulated elapsed time; a no-op `_FakePacket` every call before that.

    `arrival_s` is seconds since the test's conceptual send_interest() (i.e.
    since the fake clock was at 0.0). Each receive() call advances the fake
    clock by `poll_step_s`, mirroring how a real receive(timeout_ms=...)
    call is what consumes wall-clock time while polling.
    """

    def __init__(
        self, clock: _FakeClock, answer: _FakePacket, arrival_s: float, poll_step_s: float = 0.1
    ) -> None:
        self._clock = clock
        self._answer = answer
        self._arrival_s = arrival_s
        self._poll_step_s = poll_step_s
        self._delivered = False

    def receive(self, timeout_ms: int) -> _FakePacket:
        self._clock.now += self._poll_step_s
        if not self._delivered and self._clock.now >= self._arrival_s:
            self._delivered = True
            return self._answer
        return _FakePacket()


# --- consumer.wait_for_answer(): timing and chunk-shape gate ---------------


def test_wait_for_answer_accepts_data_within_lifetime(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(consumer.time, "monotonic", lambda: clock.now)
    name = "ccnx:/semidx/smoke/a/hello"
    answer = _FakePacket(name=name, payload=encode_answer(name, 3), is_data=True)
    handle = _FakeHandle(clock, answer, arrival_s=1.9)
    assert consumer.wait_for_answer(handle, name, lifetime_ms=2000) == consumer.EXIT_OK


def test_wait_for_answer_rejects_data_after_lifetime(monkeypatch):
    # A reply at simulated 2.5s is inside the 1.0s late-poll margin (so it
    # is observed at all) but past the 2000ms lifetime the ticket requires
    # -- this must not be scored as a pass.
    clock = _FakeClock()
    monkeypatch.setattr(consumer.time, "monotonic", lambda: clock.now)
    name = "ccnx:/semidx/smoke/a/hello"
    answer = _FakePacket(name=name, payload=encode_answer(name, 3), is_data=True)
    handle = _FakeHandle(clock, answer, arrival_s=2.5)
    assert consumer.wait_for_answer(handle, name, lifetime_ms=2000) == consumer.EXIT_LATE_ANSWER


def test_wait_for_answer_rejects_chunked_data(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(consumer.time, "monotonic", lambda: clock.now)
    name = "ccnx:/semidx/smoke/a/hello"
    answer = _FakePacket(name=name, payload=encode_answer(name, 3), chunk_num=0, is_data=True)
    handle = _FakeHandle(clock, answer, arrival_s=0.5)
    assert consumer.wait_for_answer(handle, name, lifetime_ms=2000) == consumer.EXIT_WRONG_ANSWER


def test_wait_for_answer_times_out_when_nothing_arrives(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(consumer.time, "monotonic", lambda: clock.now)
    name = "ccnx:/semidx/smoke/a/hello"
    # arrival_s beyond lifetime_ms + the late-poll margin: nothing is ever
    # delivered within the window wait_for_answer() polls.
    answer = _FakePacket(name=name, payload=encode_answer(name, 3), is_data=True)
    handle = _FakeHandle(clock, answer, arrival_s=10.0)
    assert consumer.wait_for_answer(handle, name, lifetime_ms=2000) == consumer.EXIT_TIMEOUT


# --- state_freshness.validate_answer(): shape and freshness gate ----------


def test_validate_answer_accepts_fresh_chunkless_data():
    name = "ccnx:/semidx/g1/h0/sv/0/123"
    info = _FakePacket(name=name, payload=encode_answer(name, 5), is_data=True)
    ok, message, counter = validate_answer(info, name, last_counter=4)
    assert (ok, message, counter) == (True, None, 5)


def test_validate_answer_rejects_chunked_data():
    name = "ccnx:/semidx/g1/h0/sv/0/123"
    info = _FakePacket(name=name, payload=encode_answer(name, 5), chunk_num=0, is_data=True)
    ok, message, counter = validate_answer(info, name, last_counter=4)
    assert (ok, counter) == (False, None)
    assert message == "answer carries chunk_num=0, expected no chunk TLV"


def test_validate_answer_rejects_non_fresh_counter():
    name = "ccnx:/semidx/g1/h0/sv/0/123"
    info = _FakePacket(name=name, payload=encode_answer(name, 4), is_data=True)
    ok, message, counter = validate_answer(info, name, last_counter=4)
    assert (ok, counter) == (False, None)
    assert message == "counter 4 is not fresh (last counter seen was 4)"
