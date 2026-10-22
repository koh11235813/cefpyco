#!/usr/bin/env python3
"""semindex_smoke consumer: send one Interest without a chunk TLV and print the answer.

Sends send_interest(name, lifetime=2000) without a chunk number, then polls
receive(timeout_ms=100) until a matching Data or Interest Return arrives or
the deadline elapses. Exits non-zero on timeout (and with a distinct code
for a wrong/malformed, chunked, or late answer, or an Interest Return), so a
calling script or the orchestrator can read pass/fail from the exit code
alone without parsing stdout.
"""

from __future__ import annotations

import argparse
import sys
import time

import cefpyco

from wire import decode_answer

RECEIVE_TIMEOUT_MS = 100

# How much longer receive() keeps polling after the Interest's own lifetime
# has elapsed. cefnetd and the producer still need wall-clock time to
# process and forward Data back to us after the Interest itself has expired
# upstream in the PIT, so without this margin a reply that is merely a
# little slow would be indistinguishable from one that never arrived at
# all. A reply observed inside this margin is still rejected as late (see
# wait_for_answer()) -- the margin only gives it enough runway to be
# observed and reported precisely, not enough to count as on time.
LATE_POLL_MARGIN_S = 1.0

EXIT_OK = 0
EXIT_WRONG_ANSWER = 1
EXIT_INTEREST_RETURN = 2
EXIT_TIMEOUT = 3
EXIT_LATE_ANSWER = 4


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments for the consumer."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name", help="ccnx: URI to request")
    parser.add_argument(
        "--ceforedir",
        required=True,
        help="cefnetd.conf directory for the node to bind to",
    )
    parser.add_argument("--portnum", type=int, default=0)
    parser.add_argument(
        "--lifetime-ms",
        type=int,
        default=2000,
        help="Interest lifetime in milliseconds (default: 2000)",
    )
    return parser.parse_args(argv)


def wait_for_answer(handle, name: str, lifetime_ms: int) -> int:
    """Poll `handle` for the answer to `name` and return the matching EXIT_* code.

    A chunkless Data is accepted (EXIT_OK) only if it arrives within
    `lifetime_ms` of this call; one observed later (but still inside
    LATE_POLL_MARGIN_S) is EXIT_LATE_ANSWER, not EXIT_OK -- the requester
    must get it within the Interest's own lifetime, not merely before some
    generous poll budget. A Data matching `name` but carrying a chunk_num is
    EXIT_WRONG_ANSWER: this probe's Interest never set a chunk TLV, so a
    chunked reply is answering a different kind of request. All printing
    happens here so a real cefnetd and a test's fake handle produce the
    same pass/fail message.
    """
    send_time = time.monotonic()
    lifetime_s = lifetime_ms / 1000.0
    poll_deadline = send_time + lifetime_s + LATE_POLL_MARGIN_S
    while time.monotonic() < poll_deadline:
        info = handle.receive(timeout_ms=RECEIVE_TIMEOUT_MS)
        if info.is_data and info.name == name:
            elapsed_s = time.monotonic() - send_time
            # 2026-10-22 fix: comparing against poll_deadline (lifetime +
            # LATE_POLL_MARGIN_S) instead of lifetime_s itself let a Data
            # that arrived after the Interest's own lifetime still return
            # EXIT_OK, as long as it showed up before the margin ran out.
            if elapsed_s > lifetime_s:
                print(
                    f"[consumer] FAIL: Data for {name} arrived after "
                    f"{elapsed_s * 1000:.0f}ms, past the {lifetime_ms}ms lifetime",
                    file=sys.stderr,
                )
                return EXIT_LATE_ANSWER
            # 2026-10-22 fix: this probe never checked chunk_num, so a
            # chunked reply matching `name` would pass as EXIT_OK even
            # though the Interest above never set a chunk TLV.
            if info.chunk_num is not None:
                print(
                    f"[consumer] FAIL: answer carries chunk_num={info.chunk_num}, "
                    "expected no chunk TLV",
                    file=sys.stderr,
                )
                return EXIT_WRONG_ANSWER
            try:
                echoed_name, counter = decode_answer(info.payload)
            except ValueError as exc:
                print(f"[consumer] FAIL: {exc}", file=sys.stderr)
                return EXIT_WRONG_ANSWER
            if echoed_name != name:
                print(
                    f"[consumer] FAIL: answer echoes name {echoed_name!r}, requested {name!r}",
                    file=sys.stderr,
                )
                return EXIT_WRONG_ANSWER
            print(f"name={echoed_name} counter={counter}")
            return EXIT_OK
        if info.is_interest_return and info.name == name:
            code = cefpyco.core.InterestReturnCode.get(info.returncode)
            print(f"[consumer] Interest Return: {code}", file=sys.stderr)
            return EXIT_INTEREST_RETURN
    print(f"[consumer] FAIL: timed out waiting for {name}", file=sys.stderr)
    return EXIT_TIMEOUT


def run(args: argparse.Namespace) -> int:
    """Send one chunk-less Interest for `args.name` and wait up to `--lifetime-ms` for the Data."""
    with cefpyco.create_handle(ceforedir=args.ceforedir, portnum=args.portnum) as handle:
        # chunk_num=-1: no chunk TLV, matching the producer's no-chunk
        # send_data. core.py's own default is chunk_num=0, which would NOT
        # match and would make this a different (chunked) Interest.
        handle.send_interest(args.name, chunk_num=-1, lifetime=args.lifetime_ms)
        return wait_for_answer(handle, args.name, args.lifetime_ms)


def main(argv: list[str] | None = None) -> int:
    """Entry point: parse args and run the consumer."""
    return run(parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
