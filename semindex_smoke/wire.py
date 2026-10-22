"""Wire-format helpers shared by the semindex_smoke producer/consumer/state_freshness scripts.

These scripts talk to each other over bare cefpyco send_data()/send_interest()
calls with no chunk TLV (ticket 01, smoke tests a-c). Both the producer side
and every consumer side need to agree on how the producer's per-Interest
counter and the Interest name it is answering get packed into the Content
Object payload. This module is the single place that agreement is written
down, so producer.py and consumer.py/state_freshness.py cannot drift apart
on the wire format by editing one script and forgetting the other.
"""

from __future__ import annotations

import json

# cefpyco's C layer frame buffer is CefpycoC_APIData_Len (65536) minus
# CefpycoC_Name_Len (CefC_NAME_MAXLEN, 1024) -- see
# cefpyco/src/cefpyco_c/include/cefpyco_def.h's CefpycoC_Payload_Len. cefpyco's
# send_data() never checks payload length itself (this is a known cefpyco
# footgun, see docs/wayfinder/semantic-index/map.md's cefpyco notes), so this
# layer enforces the bound before handing bytes to cefpyco, instead of letting
# an oversized payload overrun the shared C buffer.
MAX_PAYLOAD_BYTES = 65536 - 1024


def encode_answer(name: str, counter: int) -> bytes:
    """Pack a producer answer: the exact Interest name being answered, plus a counter.

    Echoing the requested name back (not just the counter) lets a receiver
    notice if it is ever handed an answer for the wrong request -- e.g. from
    cefpyco's receive() reusing its process-wide static receive buffer
    (another cefpyco footgun noted in map.md) -- instead of trusting that
    whatever arrived next must belong to the request just sent.

    Raises ValueError if the encoded payload would exceed MAX_PAYLOAD_BYTES.
    """
    payload = json.dumps(
        {"name": name, "counter": counter}, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise ValueError(
            f"encoded answer is {len(payload)} bytes, "
            f"exceeds cefpyco's {MAX_PAYLOAD_BYTES}-byte payload buffer"
        )
    return payload


def decode_answer(payload: bytes) -> tuple[str, int]:
    """Unpack a producer answer payload produced by encode_answer().

    Raises ValueError on anything that is not the expected
    {"name": str, "counter": int} object, so a malformed or unrelated
    payload fails loudly at the call site instead of being silently treated
    as a valid (name, counter) pair.
    """
    try:
        obj = json.loads(payload.decode("utf-8"))
        name = obj["name"]
        counter = obj["counter"]
    except Exception as exc:
        raise ValueError(f"payload is not a valid semindex_smoke answer: {payload!r}") from exc
    if not isinstance(name, str) or not isinstance(counter, int):
        raise ValueError(f"payload has wrong field types: {payload!r}")
    return name, counter
