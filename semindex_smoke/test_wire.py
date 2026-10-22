"""Unit tests for the pure logic in semindex_smoke: no cefnetd, no root required.

These cover the parts of the ticket's smoke scripts that do not depend on a
running cefnetd: the wire format producer/consumer agree on (wire.py), the
strictly-increasing counter check state_freshness.py relies on, and the
byte-diff helper chunk_fetch.py uses to report a mismatch. The cefnetd-facing
parts of producer.py/consumer.py/chunk_fetch.py are exercised by
RUNBOOK.md's single-host scenario, which needs root and is not run here.

Run with: .venv/bin/python3 -m pytest semindex_smoke/test_wire.py -v
"""

import pytest

from chunk_fetch import first_difference
from state_freshness import is_fresh, request_name
from wire import MAX_PAYLOAD_BYTES, decode_answer, encode_answer


def test_encode_decode_round_trip():
    payload = encode_answer("ccnx:/semidx/g1/h0/rec/h0/3", 7)
    name, counter = decode_answer(payload)
    assert name == "ccnx:/semidx/g1/h0/rec/h0/3"
    assert counter == 7


def test_encode_rejects_oversized_payload():
    # +1 over the limit on a per-character-is-one-byte ASCII name makes this
    # exact: pad the name until the encoded JSON crosses MAX_PAYLOAD_BYTES.
    huge_name = "x" * MAX_PAYLOAD_BYTES
    with pytest.raises(ValueError) as excinfo:
        encode_answer(huge_name, 0)
    assert "exceeds cefpyco's" in str(excinfo.value)


def test_decode_rejects_non_json_payload():
    with pytest.raises(ValueError) as excinfo:
        decode_answer(b"not json")
    assert "not a valid semindex_smoke answer" in str(excinfo.value)


def test_decode_rejects_missing_field():
    with pytest.raises(ValueError) as excinfo:
        decode_answer(b'{"name": "ccnx:/a"}')
    assert "not a valid semindex_smoke answer" in str(excinfo.value)


def test_decode_rejects_wrong_field_type():
    with pytest.raises(ValueError) as excinfo:
        decode_answer(b'{"name": "ccnx:/a", "counter": "7"}')
    assert "wrong field types" in str(excinfo.value)


def test_is_fresh_first_counter_has_no_predecessor():
    assert is_fresh(0, None) is True


def test_is_fresh_strictly_increasing_passes():
    assert is_fresh(5, 4) is True


def test_is_fresh_repeat_fails():
    # The exact case the ticket calls out: "never a repeat of an older
    # answer". A repeat is the same counter, not just a smaller one.
    assert is_fresh(4, 4) is False


def test_is_fresh_decrease_fails():
    assert is_fresh(3, 4) is False


def test_request_name_includes_prefix_and_index():
    name = request_name("ccnx:/semidx/g1/h0", 2)
    assert name.startswith("ccnx:/semidx/g1/h0/sv/2/")


def test_request_name_is_monotonic_across_calls():
    # The point of the epoch_ms suffix: a name built later must never sort
    # equal-or-before one built earlier for the same prefix and req_index,
    # or a Content Store could legitimately answer a later request from an
    # earlier request's cached Data. At millisecond resolution two calls
    # made in the same millisecond can collide; sleeping past one
    # millisecond boundary is what makes this assertion exact rather than
    # flaky.
    import time

    before = request_name("ccnx:/semidx/g1/h0", 0)
    time.sleep(0.002)
    after = request_name("ccnx:/semidx/g1/h0", 0)
    assert after > before


def test_first_difference_equal_bytes():
    assert first_difference(b"abc", b"abc") == 3


def test_first_difference_at_start():
    assert first_difference(b"xbc", b"abc") == 0


def test_first_difference_one_is_prefix_of_other():
    assert first_difference(b"ab", b"abc") == 2
