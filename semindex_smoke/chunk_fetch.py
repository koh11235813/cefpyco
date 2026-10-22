#!/usr/bin/env python3
"""semindex_smoke chunk_fetch: fetch every chunk of a cefputfile-published object and verify it.

Ticket 01 smoke test (d): fetch chunks 0..end of a content object published
with cefputfile, one chunk at a time via send_interest(chunk_num=k), and
compare the reassembled bytes against the original source file byte for
byte.

end_chunk_num is reported only on the LAST chunk cefputfile emits: its main
loop (tools/cefputfile/cefputfile.c) sets end_chunk_num_f only when the
cumulative bytes sent reach the file size, and Cefore's own
cef_client_payload_get_with_info() (src/lib/cef_client.c) reports
end_chunk_num as -1 on every Data that lacks that TLV. So this script must
check info.end_chunk_num on every fetched chunk, not just the first one --
checking only chunk 0 would (incorrectly) treat a multi-chunk file as
finished after one chunk, since chunk 0 normally does not carry
end_chunk_num at all.
"""

from __future__ import annotations

import argparse
import sys
import time

import cefpyco

RECEIVE_TIMEOUT_MS = 100

# Guards against spinning forever if a Content Object is somehow published
# without end_chunk_num ever being reported (e.g. something other than
# cefputfile sent it). Any file this smoke test is meant for is far smaller
# than this many chunks.
MAX_CHUNKS = 100_000


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments for the chunk fetcher."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name", help="ccnx: URI the object was cefputfile-published under")
    parser.add_argument(
        "source_file",
        help="Path to the original file, to compare the fetched bytes against",
    )
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
        help="Interest lifetime per chunk, in milliseconds",
    )
    return parser.parse_args(argv)


def first_difference(a: bytes, b: bytes) -> int:
    """Return the index of the first byte where `a` and `b` differ.

    If one is a prefix of the other, returns the length of the shorter one
    (the point where the comparison runs out of bytes on one side). Pulled
    out of run() so the off-by-one-prone boundary cases -- equal inputs,
    one a strict prefix of the other, differing at index 0 -- have their own
    test instead of being exercised only end-to-end through cefnetd.
    """
    shortest = min(len(a), len(b))
    for i in range(shortest):
        if a[i] != b[i]:
            return i
    return shortest


def fetch_chunk(handle, name: str, chunk_num: int, lifetime_ms: int):
    """Send one Interest for `chunk_num` and wait up to `lifetime_ms` (+ margin) for its Data.

    Returns None on timeout so the caller can report which chunk went
    missing instead of raising past it.
    """
    deadline = time.monotonic() + lifetime_ms / 1000.0 + 1.0
    handle.send_interest(name, chunk_num=chunk_num, lifetime=lifetime_ms)
    while time.monotonic() < deadline:
        info = handle.receive(timeout_ms=RECEIVE_TIMEOUT_MS)
        if info.is_data and info.name == name and info.chunk_num == chunk_num:
            return info
    return None


def run(args: argparse.Namespace) -> int:
    """Fetch every chunk of `args.name` and compare the reassembled bytes with `args.source_file`."""
    with open(args.source_file, "rb") as f:
        expected = f.read()

    chunks: list[bytes] = []
    with cefpyco.create_handle(ceforedir=args.ceforedir, portnum=args.portnum) as handle:
        chunk_num = 0
        while chunk_num < MAX_CHUNKS:
            info = fetch_chunk(handle, args.name, chunk_num, args.lifetime_ms)
            if info is None:
                print(
                    f"[chunk_fetch] FAIL: chunk {chunk_num} of {args.name} timed out",
                    file=sys.stderr,
                )
                return 1
            chunks.append(info.payload)
            if info.end_chunk_num is not None and info.end_chunk_num == chunk_num:
                break
            chunk_num += 1
        else:
            print(
                f"[chunk_fetch] FAIL: {MAX_CHUNKS} chunks fetched without reaching end_chunk_num",
                file=sys.stderr,
            )
            return 1

    actual = b"".join(chunks)
    if actual != expected:
        first_diff = first_difference(actual, expected)
        print(
            f"[chunk_fetch] FAIL: {len(chunks)} chunks, {len(actual)} bytes fetched vs "
            f"{len(expected)} bytes expected; first difference at byte {first_diff}",
            file=sys.stderr,
        )
        return 1

    print(f"[chunk_fetch] PASS: {len(chunks)} chunks, {len(actual)} bytes match {args.source_file}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point: parse args and run the chunk fetcher."""
    return run(parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
