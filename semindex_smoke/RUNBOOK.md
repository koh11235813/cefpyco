# semindex_smoke RUNBOOK

Ticket 01's acceptance criteria (the semindex effort's
`docs/wayfinder/semantic-index/tickets/01-cefpyco-build-and-smoke.md`, in
the `cefore-semindex` repo, not this one) need a running `cefnetd` (and,
for item 5 and the CS_MODE=2 half of items
1-4, `csmgrd`). Both need root (Mininet-less here, but `cefnetdstart` still
wants to manage a UNIX domain socket and PID files under paths it does not
own). The commands below are written for the **orchestrator** to run; the
worker that wrote this RUNBOOK did not and could not run them.

**This RUNBOOK's single-host procedure (Modes A and B) is a preliminary
check only, not the ticket's acceptance evidence.** The ticket's own "Task"
section requires 1-5 proven on **ReCefore-provisioned Mininet hosts**, with
separate requester/producer cefnetd daemons and both a cache and a
non-cache node -- a single `cefnetd` on one host cannot show that
`create_handle(ceforedir=...)` actually picks among distinct daemons, or
that an Interest crosses a FIB route between them. See "Multi-host
follow-up" below for exactly what that needs and why it is not done here.

Every command below that starts or stops a daemon, or runs a
`semindex_smoke/*.py` script, is **root**. The scripts themselves only need
root insofar as they must reach a UNIX socket that `cefnetdstart` created as
root (see "Socket permissions" below) -- they do not touch Mininet or any
other root-only subsystem themselves.

## Why a temporary conf dir, not `/usr/local/cefore`

`/usr/local/cefore/cefnetd.conf` already has `CS_MODE=2` set system-wide
(presumably for other work on this host), and `/usr/local/cefore/csmgrd.conf`
defaults `CACHE_TYPE` to `filesystem`, which writes `csmgr_fsc_*` directories
under `CACHE_PATH` (`$CEFORE_DIR/cefore` by default, i.e. back into
`/usr/local/cefore` itself). Running this smoke test there would (a) leave
the system conf dir's `CS_MODE` as this smoke test needs it rather than as
whatever else on the host expects, and (b) scatter cache files into a shared
directory. A per-run temp copy avoids both and also makes the three
CS_MODE=2 / CS_MODE=0 / (later) multi-host runs trivially reproducible and
disposable:

```bash
# ORCHESTRATOR MUST RUN (root)
export SMOKE_CONF=$(mktemp -d /tmp/semindex-smoke-conf.XXXXXX)
cp -a /usr/local/cefore/. "$SMOKE_CONF"/
# Start from an empty static FIB: the system cefnetd.fib has only a
# commented-out example line, but copy-then-truncate makes "no stale
# routes" explicit rather than incidental.
: > "$SMOKE_CONF"/cefnetd.fib
# csmgrd's default CACHE_TYPE=filesystem would write csmgr_fsc_* under
# $SMOKE_CONF; memory cache avoids leaving cache files on disk for a
# throwaway smoke run.
echo "CACHE_TYPE=memory" >> "$SMOKE_CONF"/csmgrd.conf
```

Every daemon and every `semindex_smoke/*.py` invocation below takes
`-d "$SMOKE_CONF"` / `--ceforedir "$SMOKE_CONF"` so they all agree on which
cefnetd they mean.

## Socket permissions

`cefnetdstart` (run as root) creates `/tmp/cef_<port>.<LOCAL_SOCK_ID>` (a
UNIX domain socket; `LOCAL_SOCK_ID` defaults to `0`, so normally
`/tmp/cef_9695.0`) and owns it. The `semindex_smoke/*.py` scripts need to
`connect()` to that socket, so **run them with the same `sudo` as the
daemons**, not as an unprivileged user, as they already do in Mode A/B
below. This worker could not verify the socket's permissions (no root), so
check before assuming either way:

```bash
# ORCHESTRATOR: before starting anything -- Mininet hosts share /tmp, so a
# cefnetd left running from another scenario on this same host would
# collide with the smoke test's own cefnetd (same /tmp/cef_9695.0 path,
# same LOCAL_SOCK_ID default of 0). If pgrep finds one, stop it first or
# pick a different PORT_NUM/LOCAL_SOCK_ID in $SMOKE_CONF/cefnetd.conf.
pgrep -a cefnetd
pgrep -a csmgrd
```

```bash
# ORCHESTRATOR: right after cefnetdstart in Mode A/B below (the socket does
# not exist until cefnetdstart creates it), before running any smoke
# script:
ls -l /tmp/cef_9695.0
```

If that shows the socket is `root`-owned with no group/other write bit, the
smoke scripts must run under `sudo` too.

## Mode A: CS_MODE=2 (csmgrd present) -- single-host preliminary check for items 1-5

Run this block as a **script** (e.g. `bash mode_a.sh`), not pasted line by
line into an interactive shell: it relies on `trap ... EXIT` and `exit` to
stop and clean up the moment item 2 or 4 fails, per the ticket's acceptance
criteria (a negative result on either one stops the effort). Pasted into an
interactive shell, `exit` would instead close that shell.

Run it as `bash mode_a.sh`, **not** `sudo bash mode_a.sh`, and from the same
shell where the setup block above exported `$SMOKE_CONF`: `sudo` defaults to
`env_reset`, which would drop `$SMOKE_CONF` from the script's environment
entirely (every `-d "$SMOKE_CONF"` below would silently become `-d ""`).
Every privileged line inside the block already carries its own `sudo`, so
no outer one is needed.

```bash
# ORCHESTRATOR MUST RUN (root), from /home/lab_shared/cefpyco
PY=/home/lab_shared/cefore-semindex/.venv/bin/python3
sed -i 's/^CS_MODE=.*/CS_MODE=2/' "$SMOKE_CONF"/cefnetd.conf

# Runs on every exit from this script -- success, an explicit `exit` below
# on a failed item 2/4, or an interrupted orchestrator -- so a stopped
# effort never leaves cefnetd/csmgrd or a background producer/cefputfile
# running behind it. PRODUCER_PID/CEFPUTFILE_PID are cleared once each is
# stopped on its own success path, so cleanup() does not try to kill an
# already-dead PID.
PRODUCER_PID=""
CEFPUTFILE_PID=""
cleanup() {
    [ -n "$PRODUCER_PID" ] && sudo kill "$PRODUCER_PID" 2>/dev/null
    [ -n "$CEFPUTFILE_PID" ] && sudo kill "$CEFPUTFILE_PID" 2>/dev/null
    sudo cefnetdstop -d "$SMOKE_CONF" 2>/dev/null
    sudo csmgrdstop -d "$SMOKE_CONF" 2>/dev/null
}
trap cleanup EXIT

sudo csmgrdstart -d "$SMOKE_CONF"
sudo cefnetdstart -d "$SMOKE_CONF"
sleep 1
ls -l /tmp/cef_9695.0   # socket permissions check, see above
# csmgrstatus has no -d/conf-dir option (reference skill: csmgrstatus
# [uri] [-p port] [-h host]); it just proves csmgrd itself answers on its
# default port. What we actually need here -- that THIS cefnetd sees
# csmgrd -- is cefstatus's own "Cache Mode" line, which reads "External"
# once CS_MODE=2 has connected to csmgrd and "None" otherwise
# (src/cefnetd/cef_status.c; doc/Daemon.md).
sudo csmgrstatus
sudo cefstatus -d "$SMOKE_CONF" | grep "Cache Mode"   # expect "External"

# item 1 + 2 (csmgrd-backed half) + 3: start the producer in the
# background, registered under this csmgrd-backed node's prefix
sudo "$PY" semindex_smoke/producer.py \
    --prefix ccnx:/semidx/smoke/a --ceforedir "$SMOKE_CONF" \
    > /tmp/semindex-smoke-producer-a.log 2>&1 &
PRODUCER_PID=$!
sleep 1   # let register() land before the first Interest

# item 2 + 3: a longer name under the registered prefix must be answered
sudo "$PY" semindex_smoke/consumer.py \
    ccnx:/semidx/smoke/a/hello --ceforedir "$SMOKE_CONF"
CONSUMER_EXIT=$?
echo "consumer exit: $CONSUMER_EXIT"   # 0 = PASS (items 2 and 3 both hold)
if [ "$CONSUMER_EXIT" -ne 0 ]; then
    echo "[RUNBOOK] item 2/3 FAILED -- stopping per the ticket's acceptance criteria" >&2
    exit "$CONSUMER_EXIT"   # trap above still runs cleanup()
fi

# item 4: N requests with per-request names, counters must never repeat
sudo "$PY" semindex_smoke/state_freshness.py \
    --prefix ccnx:/semidx/smoke/a --ceforedir "$SMOKE_CONF" --count 20
FRESHNESS_EXIT=$?
echo "state_freshness exit: $FRESHNESS_EXIT"   # 0 = PASS
if [ "$FRESHNESS_EXIT" -ne 0 ]; then
    echo "[RUNBOOK] item 4 FAILED -- stopping per the ticket's acceptance criteria" >&2
    exit "$FRESHNESS_EXIT"
fi

sudo kill "$PRODUCER_PID"
wait "$PRODUCER_PID" 2>/dev/null
PRODUCER_PID=""   # already stopped; don't let cleanup() try to kill it again

# item 5: publish a multi-chunk file with cefputfile, then fetch it back
# chunk by chunk. Any file bigger than one block_size (default 1024 B) is
# enough to force more than one chunk.
head -c 5000 /dev/urandom > /tmp/semindex-smoke-src.bin
sudo cefputfile ccnx:/semidx/smoke/file -f /tmp/semindex-smoke-src.bin \
    -d "$SMOKE_CONF" -b 1024 &
CEFPUTFILE_PID=$!
sleep 1   # let the first Content Objects land in csmgrd's cache before asking for them
sudo "$PY" semindex_smoke/chunk_fetch.py \
    ccnx:/semidx/smoke/file /tmp/semindex-smoke-src.bin --ceforedir "$SMOKE_CONF"
CHUNK_FETCH_EXIT=$?
echo "chunk_fetch exit: $CHUNK_FETCH_EXIT"   # 0 = PASS (item 5)
sudo kill "$CEFPUTFILE_PID" 2>/dev/null
CEFPUTFILE_PID=""

exit "$CHUNK_FETCH_EXIT"   # trap above runs cleanup() on the way out either way
```

Item 5 only makes sense under CS_MODE=2 (or CS_MODE=1): `chunk_fetch.py`
asks for chunks that were already published, which requires something --
csmgrd's cache or cefnetd's own local cache -- to still be holding them when
the consumer asks. Under CS_MODE=0 there is nowhere for a published chunk to
live once `cefputfile` sends it, so leave `cefputfile`/`chunk_fetch.py` out
of the CS_MODE=0 run below rather than expecting them to pass.

## Mode B: CS_MODE=0 (no csmgrd) -- single-host preliminary check for items 1-4

This is the non-csmgrd half of item 2 (map.md's Fog item about whether a
node with no Content Store still delivers an Interest to its registered
app): same as Mode A but skip `csmgrdstart`/`csmgrstatus`/`cefstatus` and
`cefputfile`/`chunk_fetch.py`, and flip the conf. Run this as `bash
mode_b.sh` too, for the same reasons as Mode A (the `trap`/`exit` stop
guards need an unreset environment, and need a real shell rather than
being pasted statement by statement): the producer/consumer/state_freshness
sequence is identical, including the same `trap cleanup EXIT` and the same
`exit` on a failed item 2 or 4 -- substitute it in place of the comment
below rather than skipping the stop-on-failure guards because this mode's
own snippet looks shorter:

```bash
# ORCHESTRATOR MUST RUN (root)
sed -i 's/^CS_MODE=.*/CS_MODE=0/' "$SMOKE_CONF"/cefnetd.conf

PRODUCER_PID=""
cleanup() {
    [ -n "$PRODUCER_PID" ] && sudo kill "$PRODUCER_PID" 2>/dev/null
    sudo cefnetdstop -d "$SMOKE_CONF" 2>/dev/null
}
trap cleanup EXIT

sudo cefnetdstart -d "$SMOKE_CONF"
sleep 1
# ... producer.py / consumer.py / state_freshness.py exactly as in Mode A,
# including the CONSUMER_EXIT/FRESHNESS_EXIT checks and their `exit` on
# failure (there is no CEFPUTFILE_PID or item 5 in this mode) ...
exit 0   # trap above runs cleanup() on the way out either way
```

## How to read the result

| Ticket item | Script | Pass | Fail |
| --- | --- | --- | --- |
| 1. `create_handle` binds to a node's cefnetd | all four | process starts, `register()`/`send_interest()` don't raise | exception on `create_handle`/`register` |
| 2. app registered under a prefix receives a longer-name Interest (csmgrd node: Mode A; non-csmgrd node: Mode B) | consumer.py | exit 0 with `name=... counter=...` printed | exit 3 (timeout) -- see "Fog" below |
| 3. `send_data` (no chunk TLV) answers within `lifetime=2000` | consumer.py | exit 0 | exit 3 (timeout), exit 4 (Data arrived after the lifetime), or exit 1 (wrong/malformed/chunked answer) |
| 4. state-style names are never answered with an older object | state_freshness.py | exit 0, `PASS: N requests, counters strictly increasing` | exit 1, with the specific request index and counters printed -- **per the ticket, this stops the effort** |
| 5. `send_interest(chunk_num=k)` fetches all chunks of a cefputfile object | chunk_fetch.py | exit 0, `PASS: <n> chunks, <bytes> bytes match <file>` | exit 1, with the chunk number or first differing byte printed |

Run both Mode A and Mode B: together they are map.md's Fog question about
whether a csmgrd node and a non-csmgrd node both deliver an Interest to a
registered app, fully answered on a single host. What a single host cannot
answer is the cross-link case -- the requester on a different cefnetd than
the producer, reachable only through a FIB route -- see "Multi-host
follow-up" below.

## Fog: what the C source says, not yet confirmed live

These are source-level findings from reading the installed Cefore v0.12.0
source tree, cited so item 2's actual pass/fail can be explained rather than
just reported:

- **`register(prefix)` always becomes a prefix-match registration on the
  wire**, regardless of which opcode cefpyco's C layer passes in.
  `cef_client_prefix_reg()` (`src/lib/cef_client.c:616-617`) unconditionally
  sets `opt.app_reg_f = CefC_T_OPT_APP_REG_P` ("for partial match") whenever
  `func == CefC_T_OPT_APP_REG`, and cefpyco's `register()` always calls with
  that `func`. This answers map.md's "Fog" question about whether `register`
  does an exact or prefix match: it is always prefix, at the client-library
  level, independent of what cefpyco or the app passes.
- **The exact-match gate in `cefnetd` only applies to `from_pub_f` Interests.**
  `cef_netd.c:3677-3686`: `if (pm.org.from_pub_f) { ... cef_hash_tbl_item_check_exact(...) ... }`
  is the only place an app-registered prefix is required to match an
  Interest's name exactly; it is skipped entirely when `from_pub_f` is unset.
  None of `semindex_smoke`'s scripts call `build_msgorg(from_pub_f=1, ...)`,
  so their Interests should not hit this gate. If item 2 still fails, the
  next place to look is **not** this gate.
- If item 2 fails anyway, the FIB/PIT path itself (not the exact-match gate)
  is the remaining unknown -- live-test it before assuming a cefpyco bug.

## Build tree is load-bearing: do not delete it

`cefpyco`'s CMake build does not install self-contained shared libraries:
the installed `.../site-packages/cefpyco/libcefpyco.so` has a baked-in
`RUNPATH` (confirmed with `readelf -d`) pointing at
`/home/lab_shared/cefpyco/src/cefpyco_c` and
`/home/lab_shared/cefpyco/src/cefpyco_c/cefparse` for `libcefpyco_csrc.so`
and `libcefpyco_parse.so`. **Do not delete `/home/lab_shared/cefpyco`, and
do not run `build.bash -c` (clean) or `git clean` in it** after installing
into the venv, or `import cefpyco` will start failing with a missing shared
library even though the venv's own files are untouched. This is an upstream
cefpyco CMake property (no `install(TARGETS ... RPATH ...)`/`$ORIGIN`
handling), not something this ticket's one-line fix introduced.

A related, separate risk: `/home/lab_shared/cefore-semindex/pyproject.toml`
does not list `cefpyco` as a dependency (deliberately, per this ticket's
instructions -- it was not edited). Running `uv sync` in
`/home/lab_shared/cefore-semindex` would reconcile the venv against that
pyproject.toml and is likely to remove the `uv pip install`-ed `cefpyco`
package. Until semindex's own packaging ticket adds `cefpyco` as a
dependency (e.g. as a local path/wheel dependency), avoid `uv sync` there,
or re-run step 2 of this ticket's install (`uv pip install --python
.../.venv/bin/python3 dist/cefpyco-0.10.1.0-py3-none-any.whl`) after any
`uv sync`.

## Multi-host follow-up: REQUIRED before ticket 01 can be marked done

This RUNBOOK only covers a **single host, single cefnetd**, as a
preliminary check -- it does **not** satisfy ticket 01's acceptance
criteria by itself. Ticket 01's own "Task" section (in the
`cefore-semindex` repo) requires the same checks 1-5 on
**ReCefore-provisioned Mininet hosts**, each using its own real
`cefnetd.conf`, covering both a cache and a non-cache node and separate
requester/producer daemons, with the complete command output recorded.
Specifically still needed:

- the requester on a *different* host than the producer, reachable only via
  FIB routes -- this needs ticket 04's `prefix_routes:` (map.md) to exist
  so Interests actually cross a link instead of staying on one cefnetd.
- `host_apps:` (map.md, ticket 02) to launch `producer.py` as a long-lived
  per-host process with the node's own conf dir, instead of this RUNBOOK's
  manual `sudo ... &` backgrounding.
- distinct `LOCAL_SOCK_ID` per ReCefore host template (map.md's Fog item
  about h3+ not colliding) -- irrelevant on a single host, where there is
  only one cefnetd and one socket.

None of that is attempted here: it needs ticket 02's `host_apps` plumbing,
which does not exist yet, and real Mininet/root, which this worker does not
have. Whoever picks this up next should treat the single-host Mode A/B run
above as a quick sanity check before spending Mininet time, not as a
substitute for it.
