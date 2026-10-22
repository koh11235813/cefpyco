"""Unit tests for the pure logic in semindex_smoke: no cefnetd, no root required.

These cover the parts of the smoke scripts that do not depend on a running
cefnetd: the wire format the answering and receiving sides agree on
(wire.py), the strictly-increasing counter check and request-count
validation state_freshness.py enforces, and the byte-diff helper
chunk_fetch.py uses to report a mismatch. The cefnetd-facing receive loops
are exercised by RUNBOOK.md's single-host scenario (needs root, not run
here) and, for their pure accept/reject logic, by test_receive_guards.py's
fake-handle tests.

Run with: .venv/bin/python3 -m pytest semindex_smoke/test_wire.py -v
"""

import argparse

import pytest

from chunk_fetch import first_difference
from state_freshness import _positive_int, is_fresh, request_name
from wire import MAX_PAYLOAD_BYTES, decode_answer, encode_answer


def test_encode_decode_round_trip():
    """encode_answer() then decode_answer() recovers the original name and counter."""
    payload = encode_answer("ccnx:/semidx/g1/h0/rec/h0/3", 7)
    name, counter = decode_answer(payload)
    assert name == "ccnx:/semidx/g1/h0/rec/h0/3"
    assert counter == 7


def test_encode_rejects_oversized_payload():
    """encode_answer() rejects a name whose encoded payload exceeds cefpyco's buffer, with the exact byte counts."""
    # A name of exactly MAX_PAYLOAD_BYTES ASCII characters, plus the 23
    # bytes of JSON structure encode_answer wraps it in
    # ('{"counter":0,"name":"' + ... + '"}'), crosses the limit by exactly
    # 23 bytes -- asserted exactly, not just "some error", per this repo's
    # exact-match assert policy.
    huge_name = "x" * MAX_PAYLOAD_BYTES
    with pytest.raises(ValueError) as excinfo:
        encode_answer(huge_name, 0)
    encoded_len = MAX_PAYLOAD_BYTES + 23
    assert str(excinfo.value) == (
        f"encoded answer is {encoded_len} bytes, "
        f"exceeds cefpyco's {MAX_PAYLOAD_BYTES}-byte payload buffer"
    )


def test_decode_rejects_non_json_payload():
    """decode_answer() rejects a payload that is not valid JSON, with its exact error message."""
    with pytest.raises(ValueError) as excinfo:
        decode_answer(b"not json")
    assert str(excinfo.value) == "payload is not a valid semindex_smoke answer: b'not json'"


def test_decode_rejects_missing_field():
    """decode_answer() rejects a payload missing a required field, with its exact error message."""
    with pytest.raises(ValueError) as excinfo:
        decode_answer(b'{"name": "ccnx:/a"}')
    assert str(excinfo.value) == (
        'payload is not a valid semindex_smoke answer: b\'{"name": "ccnx:/a"}\''
    )


def test_decode_rejects_wrong_field_type():
    """decode_answer() rejects a payload whose field has the wrong type, with its exact error message."""
    with pytest.raises(ValueError) as excinfo:
        decode_answer(b'{"name": "ccnx:/a", "counter": "7"}')
    assert str(excinfo.value) == (
        'payload has wrong field types: b\'{"name": "ccnx:/a", "counter": "7"}\''
    )


def test_decode_rejects_boolean_counter_true():
    """decode_answer() rejects a boolean counter even though bool is a subclass of int."""
    # bool is a subclass of int in Python, so a naive isinstance(counter,
    # int) check would accept this. JSON's `true`/`false` must not pass as
    # a valid counter value.
    payload = b'{"name": "ccnx:/a", "counter": true}'
    with pytest.raises(ValueError) as excinfo:
        decode_answer(payload)
    assert str(excinfo.value) == f"payload has wrong field types: {payload!r}"


def test_decode_rejects_boolean_counter_false():
    """decode_answer() rejects `false` the same way it rejects `true` for a counter."""
    payload = b'{"name": "ccnx:/a", "counter": false}'
    with pytest.raises(ValueError) as excinfo:
        decode_answer(payload)
    assert str(excinfo.value) == f"payload has wrong field types: {payload!r}"


def test_is_fresh_first_counter_has_no_predecessor():
    """is_fresh() accepts any counter when there is no prior counter to compare against."""
    assert is_fresh(0, None) is True


def test_is_fresh_strictly_increasing_passes():
    """is_fresh() accepts a counter strictly greater than the last one seen."""
    assert is_fresh(5, 4) is True


def test_is_fresh_repeat_fails():
    """is_fresh() rejects a repeated counter, not just a decreased one."""
    # A repeat is the same counter, not just a smaller one -- this is the
    # case this check exists for.
    assert is_fresh(4, 4) is False


def test_is_fresh_decrease_fails():
    """is_fresh() rejects a counter smaller than the last one seen."""
    assert is_fresh(3, 4) is False


def test_request_name_includes_prefix_and_index():
    """request_name() builds a name under the given prefix, tagged with the request index."""
    name = request_name("ccnx:/semidx/g1/h0", 2)
    assert name.startswith("ccnx:/semidx/g1/h0/sv/2/")


def test_request_name_is_monotonic_across_calls():
    """Two request_name() calls for the same prefix/index never produce the same or an out-of-order name."""
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
    """first_difference() returns the shared length when both inputs are identical."""
    assert first_difference(b"abc", b"abc") == 3


def test_first_difference_at_start():
    """first_difference() returns 0 when the inputs differ at the first byte."""
    assert first_difference(b"xbc", b"abc") == 0


def test_first_difference_one_is_prefix_of_other():
    """first_difference() returns the shorter length when one input is a strict prefix of the other."""
    assert first_difference(b"ab", b"abc") == 2


def test_positive_int_accepts_one():
    """_positive_int() accepts the smallest valid value, 1."""
    assert _positive_int("1") == 1


def test_positive_int_rejects_zero():
    """_positive_int() rejects 0 with its own exact error message."""
    with pytest.raises(argparse.ArgumentTypeError) as excinfo:
        _positive_int("0")
    assert str(excinfo.value) == "--count must be a positive integer, got '0'"


def test_positive_int_rejects_negative():
    """_positive_int() rejects a negative value with its own exact error message."""
    with pytest.raises(argparse.ArgumentTypeError) as excinfo:
        _positive_int("-1")
    assert str(excinfo.value) == "--count must be a positive integer, got '-1'"
