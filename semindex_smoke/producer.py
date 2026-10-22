#!/usr/bin/env python3
"""semindex_smoke producer: answer every Interest under a registered prefix.

register(prefix), then loop receive(timeout_ms=100) and answer each Interest
with send_data (no chunk TLV, chunk_num=-1) whose payload echoes the
Interest's own name plus a counter. The counter increases by one for every
Interest answered, regardless of name, and must never go backwards or repeat
across the whole run -- it is the signal a receiver uses to detect a stale
or replayed answer.

This is meant to run as a long-lived process for the duration of a RUNBOOK
scenario; stop it with SIGINT/SIGTERM.
"""

from __future__ import annotations

import argparse
import signal
import sys
from types import FrameType

import cefpyco

from wire import encode_answer

# map.md's cefpyco usage note: receive() blocks holding the GIL, and the C
# layer's receive buffer is a single process-wide static buffer, so this
# loop must stay single-threaded and poll with a short timeout instead of
# blocking indefinitely or handing receive() to a worker thread.
RECEIVE_TIMEOUT_MS = 100


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments for the producer."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--prefix",
        required=True,
        help="ccnx: URI prefix to register() and answer Interests under",
    )
    parser.add_argument(
        "--ceforedir",
        required=True,
        help="cefnetd.conf directory for the node to bind to (create_handle(ceforedir=...))",
    )
    parser.add_argument(
        "--portnum",
        type=int,
        default=0,
        help="cefnetd port override (0 = use PORT_NUM from --ceforedir's cefnetd.conf)",
    )
    parser.add_argument(
        "--expiry-ms",
        type=int,
        default=60_000,
        help=(
            "Content Object expiry, in milliseconds (cefpyco's send_data unit is "
            "milliseconds, not seconds like cefputfile's CLI; default: 60000 = 60s)"
        ),
    )
    parser.add_argument(
        "--cache-time-ms",
        type=int,
        default=-1,
        help=(
            "Recommended cache time, in milliseconds, or -1 to omit it and defer to "
            "csmgrd/cefnetd's own configured default (default: -1)"
        ),
    )
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    """Register `args.prefix` and answer Interests under it until signaled."""
    stop = {"flag": False}

    def _request_stop(_signum: int, _frame: FrameType | None) -> None:
        """Signal handler for SIGTERM/SIGINT: request a stop without raising."""
        # Set a flag rather than raising: the current receive()/send_data()
        # call is allowed to finish first, so SIGTERM can't land mid-call
        # across the Python/C boundary and leave cefpyco's handle in an
        # inconsistent state.
        stop["flag"] = True

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)

    counter = 0
    with cefpyco.create_handle(ceforedir=args.ceforedir, portnum=args.portnum) as handle:
        handle.register(args.prefix)
        print(f"[producer] registered {args.prefix}", file=sys.stderr, flush=True)
        while not stop["flag"]:
            info = handle.receive(timeout_ms=RECEIVE_TIMEOUT_MS)
            if not info.is_interest:
                continue
            payload = encode_answer(info.name, counter)
            handle.send_data(
                info.name,
                payload,
                chunk_num=-1,
                expiry=args.expiry_ms,
                cache_time=args.cache_time_ms,
            )
            print(f"[producer] answered {info.name} counter={counter}", file=sys.stderr, flush=True)
            counter += 1
    print("[producer] stopped", file=sys.stderr, flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point: parse args and run the producer loop."""
    return run(parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
