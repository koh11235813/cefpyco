"""Fake-handle tests for the receive-side accept/reject logic: no cefnetd, no root required.

consumer.wait_for_answer(), state_freshness.validate_answer()/run(), and
chunk_fetch.fetch_chunk()/run() all decide whether a received packet counts
as a real pass, by inspecting timing and packet shape rather than just
"something with the right name arrived". Those decisions cannot be
exercised through test_wire.py's pure-value tests alone, since they depend
on a handle's receive()/send_interest() calls and (for consumer.py and
chunk_fetch.py) on elapsed wall-clock time -- so this module supplies
minimal fake handles and a controllable fake clock to drive them
deterministically.

Run with: .venv/bin/python3 -m pytest semindex_smoke/test_receive_guards.py -v
"""

from __future__ import annotations

import chunk_fetch
import consumer
import state_freshness
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
        """Start the fake clock at 0.0 simulated seconds."""
        self.now = 0.0


class _FakePacket:
    """A minimal stand-in for cefpyco's CcnPacketInfo, exposing only the attributes these scripts read."""

    def __init__(
        self,
        name: str = "",
        payload: bytes = b"",
        chunk_num: int | None = None,
        end_chunk_num: int | None = None,
        is_data: bool = False,
        is_interest_return: bool = False,
    ) -> None:
        """Set every attribute these scripts read off a real CcnPacketInfo, defaulting to an empty/no-op packet."""
        self.name = name
        self.payload = payload
        self.chunk_num = chunk_num
        self.end_chunk_num = end_chunk_num
        self.is_data = is_data
        self.is_interest_return = is_interest_return


class _HandleContext:
    """Context manager stand-in for `cefpyco.create_handle(...)`, yielding a given fake handle.

    run()-level tests monkeypatch `cefpyco.create_handle` with a callable
    returning one of these, so `with cefpyco.create_handle(...) as handle:`
    in the script under test gets the fake handle without a real cefnetd.
    """

    def __init__(self, handle) -> None:
        """Remember `handle` to hand back from `__enter__`."""
        self._handle = handle

    def __enter__(self):
        """Return the wrapped fake handle, standing in for a real `with cefpyco.create_handle(...)`."""
        return self._handle

    def __exit__(self, *exc_info) -> None:
        """No cleanup needed: the fake handle owns no real resource."""
        return None


class _NullHandle:
    """A handle stand-in for run()-level tests that stub out receive()/validation at a higher level.

    send_interest() is a no-op; these tests monkeypatch the helper
    functions run() calls (wait_for_data(), validate_answer()) rather than
    having this handle answer receive() itself.
    """

    def send_interest(self, *args, **kwargs) -> None:
        """Do nothing: this test drives behavior through a monkeypatched helper, not through send_interest()."""
        pass


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
        """Remember the shared clock, the one canned answer, and when (simulated) it arrives."""
        self._clock = clock
        self._answer = answer
        self._arrival_s = arrival_s
        self._poll_step_s = poll_step_s
        self._delivered = False

    def receive(self, timeout_ms: int) -> _FakePacket:
        """Advance the fake clock by one poll step and deliver the canned answer once `arrival_s` is reached."""
        self._clock.now += self._poll_step_s
        if not self._delivered and self._clock.now >= self._arrival_s:
            self._delivered = True
            return self._answer
        return _FakePacket()


# --- consumer.wait_for_answer(): timing and chunk-shape gate ---------------


def test_wait_for_answer_accepts_data_within_lifetime(monkeypatch):
    """A chunkless Data arriving before the Interest's lifetime elapses passes."""
    clock = _FakeClock()
    monkeypatch.setattr(consumer.time, "monotonic", lambda: clock.now)
    name = "ccnx:/semidx/smoke/a/hello"
    answer = _FakePacket(name=name, payload=encode_answer(name, 3), is_data=True)
    handle = _FakeHandle(clock, answer, arrival_s=1.9)
    assert consumer.wait_for_answer(handle, name, lifetime_ms=2000, send_time=clock.now) == consumer.EXIT_OK


def test_wait_for_answer_rejects_data_after_lifetime(monkeypatch):
    """A reply at simulated 2.5s is inside the 1.0s late-poll margin (so it
    is observed at all) but past the 2000ms lifetime the probe must be
    answered within -- this must not be scored as a pass.
    """
    clock = _FakeClock()
    monkeypatch.setattr(consumer.time, "monotonic", lambda: clock.now)
    name = "ccnx:/semidx/smoke/a/hello"
    answer = _FakePacket(name=name, payload=encode_answer(name, 3), is_data=True)
    handle = _FakeHandle(clock, answer, arrival_s=2.5)
    assert (
        consumer.wait_for_answer(handle, name, lifetime_ms=2000, send_time=clock.now)
        == consumer.EXIT_LATE_ANSWER
    )


def test_wait_for_answer_rejects_chunked_data(monkeypatch):
    """A Data matching the requested name but carrying a chunk_num is EXIT_WRONG_ANSWER."""
    clock = _FakeClock()
    monkeypatch.setattr(consumer.time, "monotonic", lambda: clock.now)
    name = "ccnx:/semidx/smoke/a/hello"
    answer = _FakePacket(name=name, payload=encode_answer(name, 3), chunk_num=0, is_data=True)
    handle = _FakeHandle(clock, answer, arrival_s=0.5)
    assert (
        consumer.wait_for_answer(handle, name, lifetime_ms=2000, send_time=clock.now)
        == consumer.EXIT_WRONG_ANSWER
    )


def test_wait_for_answer_times_out_when_nothing_arrives(monkeypatch):
    """No matching Data within the lifetime plus late-poll margin is EXIT_TIMEOUT."""
    clock = _FakeClock()
    monkeypatch.setattr(consumer.time, "monotonic", lambda: clock.now)
    name = "ccnx:/semidx/smoke/a/hello"
    # arrival_s beyond lifetime_ms + the late-poll margin: nothing is ever
    # delivered within the window wait_for_answer() polls.
    answer = _FakePacket(name=name, payload=encode_answer(name, 3), is_data=True)
    handle = _FakeHandle(clock, answer, arrival_s=10.0)
    assert (
        consumer.wait_for_answer(handle, name, lifetime_ms=2000, send_time=clock.now)
        == consumer.EXIT_TIMEOUT
    )


class _TimestampFakeHandle:
    """A handle stand-in that only exercises when consumer.run() reads the clock relative to send_interest().

    send_interest() itself advances the fake clock, standing in for the
    measurable wall-clock time a real send_interest() call takes. If
    run() read its start timestamp after calling send_interest() instead
    of before, the timestamp a test captures from the (monkeypatched)
    wait_for_answer() call would include this advance.
    """

    def __init__(self, clock: _FakeClock, advance_s: float) -> None:
        """Remember the shared clock and how far send_interest() advances it."""
        self._clock = clock
        self._advance_s = advance_s

    def send_interest(self, *args, **kwargs) -> None:
        """Advance the fake clock, standing in for send_interest()'s own wall-clock cost."""
        self._clock.now += self._advance_s


def test_run_takes_send_time_before_send_interest(monkeypatch):
    """consumer.run() must read its start timestamp before calling send_interest(), not after."""
    clock = _FakeClock()
    monkeypatch.setattr(consumer.time, "monotonic", lambda: clock.now)
    handle = _TimestampFakeHandle(clock, advance_s=0.5)
    monkeypatch.setattr(consumer.cefpyco, "create_handle", lambda **kwargs: _HandleContext(handle))

    captured_send_time = []

    def _fake_wait_for_answer(handle, name, lifetime_ms, send_time):
        """Stand in for wait_for_answer(): record the send_time run() passed in and report EXIT_OK."""
        captured_send_time.append(send_time)
        return consumer.EXIT_OK

    monkeypatch.setattr(consumer, "wait_for_answer", _fake_wait_for_answer)

    args = consumer.parse_args(["ccnx:/semidx/smoke/a/hello", "--ceforedir", "/tmp"])
    assert consumer.run(args) == consumer.EXIT_OK
    # If run() took its timestamp after send_interest() instead of before,
    # this would be 0.5 (the fake clock's advance), not 0.0.
    assert captured_send_time == [0.0]


# --- state_freshness.validate_answer(): shape and freshness gate ----------


def test_validate_answer_accepts_fresh_chunkless_data():
    """validate_answer() accepts a chunkless Data whose counter is fresh and whose name matches."""
    name = "ccnx:/semidx/g1/h0/sv/0/123"
    info = _FakePacket(name=name, payload=encode_answer(name, 5), is_data=True)
    ok, message, counter = validate_answer(info, name, last_counter=4)
    assert (ok, message, counter) == (True, None, 5)


def test_validate_answer_rejects_chunked_data():
    """validate_answer() rejects a Data carrying a chunk_num, with its exact error message."""
    name = "ccnx:/semidx/g1/h0/sv/0/123"
    info = _FakePacket(name=name, payload=encode_answer(name, 5), chunk_num=0, is_data=True)
    ok, message, counter = validate_answer(info, name, last_counter=4)
    assert (ok, counter) == (False, None)
    assert message == "answer carries chunk_num=0, expected no chunk TLV"


def test_validate_answer_rejects_non_fresh_counter():
    """validate_answer() rejects a counter that repeats the last one seen, with its exact error message."""
    name = "ccnx:/semidx/g1/h0/sv/0/123"
    info = _FakePacket(name=name, payload=encode_answer(name, 4), is_data=True)
    ok, message, counter = validate_answer(info, name, last_counter=4)
    assert (ok, counter) == (False, None)
    assert message == "counter 4 is not fresh (last counter seen was 4)"


# --- state_freshness.run(): timeout vs freshness-violation exit codes -----


def _state_freshness_args():
    """Build a minimal state_freshness argparse.Namespace for run()-level tests."""
    return state_freshness.parse_args(
        ["--prefix", "ccnx:/semidx/g1/h0", "--ceforedir", "/tmp", "--count", "1"]
    )


def test_run_returns_exit_timeout_when_no_data_arrives(monkeypatch):
    """A request with no Data at all (wait_for_data() returns None) is EXIT_TIMEOUT, not EXIT_FRESHNESS_VIOLATION."""
    monkeypatch.setattr(
        state_freshness.cefpyco, "create_handle", lambda **kwargs: _HandleContext(_NullHandle())
    )
    monkeypatch.setattr(state_freshness, "wait_for_data", lambda handle, name, lifetime_ms: None)
    assert state_freshness.run(_state_freshness_args()) == state_freshness.EXIT_TIMEOUT


def test_run_returns_exit_freshness_violation_on_stale_counter(monkeypatch):
    """A Data that arrives but fails validate_answer() is EXIT_FRESHNESS_VIOLATION, not EXIT_TIMEOUT."""
    monkeypatch.setattr(
        state_freshness.cefpyco, "create_handle", lambda **kwargs: _HandleContext(_NullHandle())
    )
    monkeypatch.setattr(state_freshness, "wait_for_data", lambda handle, name, lifetime_ms: _FakePacket())
    monkeypatch.setattr(
        state_freshness, "validate_answer", lambda info, name, last_counter: (False, "stale", None)
    )
    assert state_freshness.run(_state_freshness_args()) == state_freshness.EXIT_FRESHNESS_VIOLATION


def test_run_returns_exit_ok_when_every_request_is_fresh(monkeypatch):
    """A request whose Data passes validate_answer() still returns EXIT_OK after the exit-code split."""
    monkeypatch.setattr(
        state_freshness.cefpyco, "create_handle", lambda **kwargs: _HandleContext(_NullHandle())
    )
    monkeypatch.setattr(state_freshness, "wait_for_data", lambda handle, name, lifetime_ms: _FakePacket())
    monkeypatch.setattr(state_freshness, "validate_answer", lambda info, name, last_counter: (True, None, 1))
    assert state_freshness.run(_state_freshness_args()) == state_freshness.EXIT_OK


# --- chunk_fetch.fetch_chunk() / run(): per-chunk request/response loop ---


class _ChunkFakeHandle:
    """Hands back the canned Data for whichever chunk_num the last send_interest() asked for.

    fetch_chunk() sends one Interest per chunk_num, then polls receive()
    until a Data whose chunk_num matches arrives. This fake tracks only the
    most recently requested chunk_num and delivers that chunk's payload
    once; a request for a chunk_num outside `chunks` (or a second receive()
    before the next send_interest()) gets a no-op `_FakePacket`, which
    surfaces in the caller as a timeout rather than a wrong-chunk mixup.
    """

    def __init__(self, clock: _FakeClock, chunks: dict[int, bytes], end_chunk_num: int) -> None:
        """Remember the shared clock, the canned chunk payloads by chunk_num, and which one is last."""
        self._clock = clock
        self._chunks = chunks
        self._end_chunk_num = end_chunk_num
        self._pending_chunk_num: int | None = None

    def send_interest(self, name: str, chunk_num: int, lifetime: int) -> None:
        """Record which chunk_num the next receive() should answer."""
        self._pending_chunk_num = chunk_num

    def receive(self, timeout_ms: int) -> _FakePacket:
        """Advance the fake clock and deliver the pending chunk's Data once, then no-ops until the next request."""
        self._clock.now += 0.1
        chunk_num = self._pending_chunk_num
        if chunk_num is None or chunk_num not in self._chunks:
            return _FakePacket()
        self._pending_chunk_num = None
        end_chunk_num = chunk_num if chunk_num == self._end_chunk_num else None
        return _FakePacket(
            name="ccnx:/semidx/smoke/file",
            payload=self._chunks[chunk_num],
            chunk_num=chunk_num,
            end_chunk_num=end_chunk_num,
            is_data=True,
        )


def test_fetch_chunk_returns_the_matching_chunk(monkeypatch):
    """fetch_chunk() returns the Data whose chunk_num matches what it just requested."""
    clock = _FakeClock()
    monkeypatch.setattr(chunk_fetch.time, "monotonic", lambda: clock.now)
    name = "ccnx:/semidx/smoke/file"
    handle = _ChunkFakeHandle(clock, chunks={2: b"chunk-two"}, end_chunk_num=2)
    info = chunk_fetch.fetch_chunk(handle, name, chunk_num=2, lifetime_ms=2000)
    assert info is not None
    assert (info.chunk_num, info.payload) == (2, b"chunk-two")


def test_fetch_chunk_times_out_when_no_chunk_matches(monkeypatch):
    """fetch_chunk() returns None if no Data with the requested chunk_num ever arrives."""
    clock = _FakeClock()
    monkeypatch.setattr(chunk_fetch.time, "monotonic", lambda: clock.now)
    name = "ccnx:/semidx/smoke/file"
    # chunk 5 is never in this handle's canned chunks, so every receive() is a no-op _FakePacket.
    handle = _ChunkFakeHandle(clock, chunks={0: b"chunk-zero"}, end_chunk_num=0)
    info = chunk_fetch.fetch_chunk(handle, name, chunk_num=5, lifetime_ms=10)
    assert info is None


def test_run_reassembles_a_multi_chunk_file(monkeypatch, tmp_path):
    """run() fetches every chunk in order and passes when the reassembled bytes match the source file."""
    clock = _FakeClock()
    monkeypatch.setattr(chunk_fetch.time, "monotonic", lambda: clock.now)
    chunks = {0: b"hello-", 1: b"world!"}
    handle = _ChunkFakeHandle(clock, chunks=chunks, end_chunk_num=1)
    monkeypatch.setattr(chunk_fetch.cefpyco, "create_handle", lambda **kwargs: _HandleContext(handle))

    source_file = tmp_path / "source.bin"
    source_file.write_bytes(b"hello-world!")

    args = chunk_fetch.parse_args(["ccnx:/semidx/smoke/file", str(source_file), "--ceforedir", "/tmp"])
    assert chunk_fetch.run(args) == 0
