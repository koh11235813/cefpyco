#!/usr/bin/env python3
"""semindex_smoke state_freshness: prove the producer never answers with a stale counter.

Ticket 01 smoke test (c): send N consumer-style requests whose names change
on every request (".../sv/<req>/<epoch_ms>") and assert each answer's
counter is strictly greater than every counter seen so far in this run. A
repeat or a decrease means either the producer answered from stale state
(e.g. cefpyco's receive() reusing its process-wide static buffer, see
map.md's cefpyco notes) or something replayed an old Data instead of the
producer's live answer. The ticket says a negative result here stops the
effort and goes back to the user, so this script fails loudly and exits
non-zero at the first such mismatch rather than continuing.
"""

from __future__ import annotations

import argparse
import sys
import time

import cefpyco

from wire import decode_answer

RECEIVE_TIMEOUT_MS = 100


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
        "--count", type=int, default=10, help="Number of requests to send (default: 10)"
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

    Pulled out of run() so the exact boundary this ticket cares about --
    a repeat (counter == last_counter) is a FAIL, not just a decrease -- has
    its own test instead of being exercised only end-to-end through cefnetd.
    """
    return last_counter is None or counter > last_counter


def run(args: argparse.Namespace) -> int:
    """Send `args.count` freshness probes and stop at the first non-increasing counter."""
    last_counter: int | None = None
    with cefpyco.create_handle(ceforedir=args.ceforedir, portnum=args.portnum) as handle:
        for req_index in range(args.count):
            name = request_name(args.prefix, req_index)
            deadline = time.monotonic() + args.lifetime_ms / 1000.0 + 1.0
            handle.send_interest(name, chunk_num=-1, lifetime=args.lifetime_ms)

            answer = None
            while time.monotonic() < deadline:
                info = handle.receive(timeout_ms=RECEIVE_TIMEOUT_MS)
                if info.is_data and info.name == name:
                    answer = info.payload
                    break
            if answer is None:
                print(
                    f"[state_freshness] FAIL: request {req_index} ({name}) timed out",
                    file=sys.stderr,
                )
                return 1

            try:
                echoed_name, counter = decode_answer(answer)
            except ValueError as exc:
                print(f"[state_freshness] FAIL: request {req_index}: {exc}", file=sys.stderr)
                return 1

            if echoed_name != name:
                print(
                    f"[state_freshness] FAIL: request {req_index}: requested {name!r}, "
                    f"answer echoes {echoed_name!r}",
                    file=sys.stderr,
                )
                return 1
            if not is_fresh(counter, last_counter):
                print(
                    f"[state_freshness] FAIL: request {req_index}: counter {counter} is not "
                    f"fresh (last counter seen was {last_counter})",
                    file=sys.stderr,
                )
                return 1

            print(f"[state_freshness] request {req_index}: counter={counter} OK")
            last_counter = counter

    print(f"[state_freshness] PASS: {args.count} requests, counters strictly increasing")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point: parse args and run the freshness probe."""
    return run(parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
