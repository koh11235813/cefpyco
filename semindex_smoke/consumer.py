#!/usr/bin/env python3
"""semindex_smoke consumer: send one Interest without a chunk TLV and print the answer.

Ticket 01 smoke test (b): send_interest(name, lifetime=2000) without a chunk
number, then loop receive(timeout_ms=100) until the deadline elapses or a
matching Data or Interest Return arrives. Exits non-zero on timeout (and
with a distinct code for a wrong/malformed answer or an Interest Return),
so a calling script or the orchestrator can read pass/fail from the exit
code alone without parsing stdout.
"""

from __future__ import annotations

import argparse
import sys
import time

import cefpyco

from wire import decode_answer

RECEIVE_TIMEOUT_MS = 100

EXIT_OK = 0
EXIT_WRONG_ANSWER = 1
EXIT_INTEREST_RETURN = 2
EXIT_TIMEOUT = 3


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
        help="Interest lifetime in milliseconds (ticket 01 default: 2000)",
    )
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    """Send one chunk-less Interest for `args.name` and wait up to `--lifetime-ms` for the Data."""
    # Margin beyond the Interest's own lifetime: cefnetd and the producer
    # still need wall-clock time to process and forward the Data back to us
    # after the Interest itself has expired upstream in the PIT.
    deadline = time.monotonic() + args.lifetime_ms / 1000.0 + 1.0

    with cefpyco.create_handle(ceforedir=args.ceforedir, portnum=args.portnum) as handle:
        # chunk_num=-1: no chunk TLV, matching the producer's no-chunk
        # send_data. core.py's own default is chunk_num=0, which would NOT
        # match and would make this a different (chunked) Interest.
        handle.send_interest(args.name, chunk_num=-1, lifetime=args.lifetime_ms)
        while time.monotonic() < deadline:
            info = handle.receive(timeout_ms=RECEIVE_TIMEOUT_MS)
            if info.is_data and info.name == args.name:
                try:
                    name, counter = decode_answer(info.payload)
                except ValueError as exc:
                    print(f"[consumer] FAIL: {exc}", file=sys.stderr)
                    return EXIT_WRONG_ANSWER
                if name != args.name:
                    print(
                        f"[consumer] FAIL: answer echoes name {name!r}, requested {args.name!r}",
                        file=sys.stderr,
                    )
                    return EXIT_WRONG_ANSWER
                print(f"name={name} counter={counter}")
                return EXIT_OK
            if info.is_interest_return and info.name == args.name:
                code = cefpyco.core.InterestReturnCode.get(info.returncode)
                print(f"[consumer] Interest Return: {code}", file=sys.stderr)
                return EXIT_INTEREST_RETURN
    print(f"[consumer] FAIL: timed out waiting for {args.name}", file=sys.stderr)
    return EXIT_TIMEOUT


def main(argv: list[str] | None = None) -> int:
    """Entry point: parse args and run the consumer."""
    return run(parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
