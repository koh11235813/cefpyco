#!/usr/bin/env python3
"""semindex_smoke state_freshness: prove the producer never answers with a stale counter.

Sends N consumer-style requests whose names change on every request
(".../sv/<req>/<epoch_ms>") and asserts each answer's counter is strictly
greater than every counter seen so far in this run. A repeat or a decrease
means either the producer answered from stale state (e.g. cefpyco's
receive() reusing its process-wide static buffer, see map.md's cefpyco
notes) or something replayed an old Data instead of the producer's live
answer. A negative result here means the run cannot be trusted, so this
script fails loudly and exits non-zero at the first such mismatch rather
than continuing.
"""

from __future__ import annotations

import argparse
import sys
import time

import cefpyco

from wire import decode_answer

RECEIVE_TIMEOUT_MS = 100

EXIT_OK = 0
EXIT_FRESHNESS_VIOLATION = 1
# 2026-10-22 fix: a request that timed out (no Data at all) and one that
# got a Data back with a stale or malformed counter both used to return
# exit 1, so a caller could not tell "the producer never answered" from
# "the producer answered with a freshness violation" from the exit code
# alone. Giving timeout its own code, and the same numeric value
# consumer.py already uses for EXIT_TIMEOUT, means exit 3 means the same
# thing in both smoke scripts.
EXIT_TIMEOUT = 3


def _positive_int(value: str) -> int:
    """argparse type for --count: reject anything less than 1.

    2026-10-22 fix: --count 0 (or negative) used to send no requests and
    fall straight through to the final "PASS: N requests, counters
    strictly increasing" print with no request behind it to back that up.
    Rejected at argument-parsing time instead of silently producing
    unearned evidence.
    """
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError(f"--count must be a positive integer, got {value!r}")
    return parsed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments for the freshness probe."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--prefix",
        required=True,
        help="Producer's registered prefix, e.g. ccnx:/semidx/g1/h0",
    )
    parser.add_argument(
        "--ceforedir",
        required=True,
        help="cefnetd.conf directory for the node to bind to",
    )
    parser.add_argument("--portnum", type=int, default=0)
    parser.add_argument(
        "--count",
        type=_positive_int,
        default=10,
        help="Number of requests to send, at least 1 (default: 10)",
    )
    parser.add_argument("--lifetime-ms", type=int, default=2000)
    return parser.parse_args(argv)


def request_name(prefix: str, req_index: int) -> str:
    """Build the per-request state name '<prefix>/sv/<req_index>/<epoch_ms>'.

    The epoch_ms suffix guarantees every request names a Content Object the
    network has never seen before, so a Content Store hit is only possible
    if something answers with a name it was never asked for.
    """
    return f"{prefix}/sv/{req_index}/{int(time.time() * 1000)}"


def is_fresh(counter: int, last_counter: int | None) -> bool:
    """Return True if `counter` is strictly greater than `last_counter` (or there is none yet).

    Kept as its own function so the exact boundary that matters here -- a
    repeat (counter == last_counter) is a FAIL, not just a decrease -- has
    its own test instead of being exercised only end-to-end through
    cefnetd.
    """
    return last_counter is None or counter > last_counter


def wait_for_data(handle, name: str, lifetime_ms: int):
    """Poll `handle` up to `lifetime_ms` (+ margin) for the Data answering `name`.

    Margin beyond the Interest's own lifetime: cefnetd and the producer
    still need wall-clock time to process and forward the Data back to us
    after the Interest itself has expired upstream in the PIT. Returns the
    matching PacketInfo, or None on timeout. Whether that info is otherwise
    well-formed (right shape, right echoed name, fresh counter) is for the
    caller to decide -- this function only answers "did anything named
    `name` come back".
    """
    deadline = time.monotonic() + lifetime_ms / 1000.0 + 1.0
    while time.monotonic() < deadline:
        info = handle.receive(timeout_ms=RECEIVE_TIMEOUT_MS)
        if info.is_data and info.name == name:
            return info
    return None


def validate_answer(info, name: str, last_counter: int | None):
    """Check one probe's answer against every shape this script requires.

    Returns (True, None, counter) on success, or (False, message, None) on
    the first violated requirement: a chunk_num (synchronization traffic
    never carries a chunk TLV), a malformed payload, a name that does not
    match what was requested, or a counter that is not fresh. Centralizing
    these checks in one pure function (no handle, no clock) means each has
    its own test instead of only being exercised end-to-end through cefnetd.
    """
    # 2026-10-22 fix: the receive loop used to match on name alone, so a
    # chunked reply would pass as a valid freshness-probe answer even
    # though send_interest() above never sets a chunk TLV.
    if info.chunk_num is not None:
        return False, f"answer carries chunk_num={info.chunk_num}, expected no chunk TLV", None
    try:
        echoed_name, counter = decode_answer(info.payload)
    except ValueError as exc:
        return False, str(exc), None
    if echoed_name != name:
        return False, f"requested {name!r}, answer echoes {echoed_name!r}", None
    if not is_fresh(counter, last_counter):
        return (
            False,
            f"counter {counter} is not fresh (last counter seen was {last_counter})",
            None,
        )
    return True, None, counter


def run(args: argparse.Namespace) -> int:
    """Send `args.count` freshness probes and stop at the first non-increasing counter."""
    last_counter: int | None = None
    with cefpyco.create_handle(ceforedir=args.ceforedir, portnum=args.portnum) as handle:
        for req_index in range(args.count):
            name = request_name(args.prefix, req_index)
            handle.send_interest(name, chunk_num=-1, lifetime=args.lifetime_ms)

            info = wait_for_data(handle, name, args.lifetime_ms)
            if info is None:
                print(
                    f"[state_freshness] FAIL: request {req_index} ({name}) timed out",
                    file=sys.stderr,
                )
                return EXIT_TIMEOUT

            ok, message, counter = validate_answer(info, name, last_counter)
            if not ok:
                print(f"[state_freshness] FAIL: request {req_index}: {message}", file=sys.stderr)
                return EXIT_FRESHNESS_VIOLATION

            print(f"[state_freshness] request {req_index}: counter={counter} OK")
            last_counter = counter

    print(f"[state_freshness] PASS: {args.count} requests, counters strictly increasing")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """Entry point: parse args and run the freshness probe."""
    return run(parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
