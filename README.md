# obi-tls-attribution-repro

A reproduction for
[open-telemetry/opentelemetry-ebpf-instrumentation#2998](https://github.com/open-telemetry/opentelemetry-ebpf-instrumentation/issues/2998).
Outbound TLS client spans get attached to the wrong socket. The span ends up
pointing at the caller instead of the server that was actually called.

## Nature of the bug

OBI has to work out which socket an OpenSSL `SSL*` belongs to. It watches for a
socket syscall that happens while an `SSL_read` or `SSL_write` uprobe is still
on the stack. If one shows up inside that window, OBI links the two together.

Some TLS stacks never do that. CPython `asyncio` (`ssl.MemoryBIO` through
`SSLContext.wrap_bio`) and Node.js `TLSWrap` encrypt into a memory buffer first.
The event loop sends those bytes later, from a different call stack. By then the
uprobe window has closed, so no syscall ever happens inside it.

When that happens, OBI guesses instead: It uses the last connection it saw on
that thread. On an event loop under load, that is usually the inbound request
the process is busy serving. OBI then preserves that guess for the rest of the
life of the `SSL*`.

The result is an outbound client span that wrongly carries the address of the
inbound caller.

## Components of this stack

| Service | What it does |
|---|---|
| `upstream` | A TLS server on `:8443` with a self-signed cert built into the image. It is the only correct peer for any outbound call here. |
| `app` | A CPython `asyncio` service. It serves plain HTTP on `:8080`, and makes one outbound TLS call to `upstream:8443` for each inbound request. |
| `app` (Node arm) | A Node.js service that does the same job, selected with a compose overlay. Node's TLS client is `TLSWrap`, which is also a memory BIO. |
| `loadgen` | Sends concurrent requests to `app`. |
| `obi` | The instrumentation being tested. It prints spans as JSON to stdout. |

`app` only ever calls the `upstream` service, so any client span that reports a
peer other than `upstream:8443` is wrong. This makes the output easy to check.

## Run it

```sh
docker compose up --build -d
sleep 90
docker logs obi-repro-obi 2>&1 | python3 check.py
```

`check.py` only uses the Python standard library. Shut the stack down with
`docker compose down -v`. If you are on Compose v1, you may need to use
`docker-compose` instead.

To test Grafana Beyla's vendored copy of OBI:

```sh
OBI_IMAGE=grafana/beyla:3.32.0 docker compose up --build -d
```

## The Node arm

The stack can run a Node.js service in place of the CPython one. Node uses its
own bundled OpenSSL, and its TLS client is `TLSWrap`, which is also a memory
BIO. It shows the same fault, and in these runs it showed it more strongly.

```sh
docker compose -f docker-compose.yml -f docker-compose.node.yml up --build -d
sleep 90
docker logs obi-repro-obi 2>&1 | python3 check.py
```

The overlay only swaps the build for the `app` service. Everything else stays
the same, so `check.py` needs no changes. `CLIENT_MODE` does nothing in this
arm, because Node has no socket-BIO client to switch to.

Node links OpenSSL statically into the `node` binary. On `node:24-slim` there is
no `libssl.so` and no `libcrypto.so` at all. `BIO_write`, `BIO_read`,
`SSL_set_bio` and `SSL_write` are exported from the executable itself, so
anything that looks only at shared libraries will find nothing.

## Pooled connections

By default each inbound request opens a new outbound TLS connection. Both arms
can instead reuse a small pool of long lived connections. That is closer to how
a service usually talks to a database or a cache, and it means one TLS handshake
at the start followed by thousands of application records.

```sh
UPSTREAM_KEEPALIVE=true docker compose -f docker-compose.yml -f docker-compose.node.yml up --build -d   # Node
CLIENT_MODE=asyncio-pooled docker compose up --build -d                                                 # CPython
```

`UPSTREAM_POOL_SIZE` sets the pool size and defaults to 16. You can tell the
pool is working from `check.py` output: the `DNS` span count drops to roughly
the pool size, instead of tracking the request count.

The two arms behave differently once pooled. The Node pooled arm still
reproduces the fault. The CPython pooled arm did not reproduce it in these runs,
so do not use it as a check. With a stable pool the first connection the thread
touches is the outbound one, the guess lands on it, and the guess is then kept
for the life of the connection.

## What to look for

`check.py` counts every outbound `HTTPClient` span and groups them by the peer
OBI reported. A run with the bug looks like this:

```
outbound HTTPClient spans (total 45721)
  correct  -> upstream:8443       40385  (88.3%)
  WRONG    -> some other peer      5336  (11.7%)
  of which reversed edges (server side is app's own listener app:8080): 5336

top mis-attributed raw socket tuples:
  client=172.20.0.3:8080  server=172.20.0.4:45962        1157
  client=172.20.0.3:8080  server=172.20.0.4:50606         975
```

`172.20.0.3` is `app`, `172.20.0.4` is `loadgen`, and `172.20.0.2` is
`upstream`. In every bad span, the client side is `app`'s own listening socket
and the server side is an ephemeral port on `loadgen`. The call to
`upstream:8443` has been recorded as a call back to the inbound caller, and
`upstream` does not show up in the span at all.

### A correct span, copied from a run

```json
{
  "type": "HTTPClient", "kind": "SPAN_KIND_CLIENT",
  "peer": "172.20.0.3", "peerPort": "58294",
  "host": "172.20.0.2", "hostPort": "8443",
  "peerName": "app", "hostName": "upstream",
  "duration": "268.417µs",
  "attributes": { "clientAddr": "app", "serverAddr": "upstream", "serverPort": "8443",
                  "method": "GET", "url": "/upstream", "status": "200" }
}
```

### An incorrect span, copied from a run

```json
{
  "type": "HTTPClient", "kind": "SPAN_KIND_CLIENT",
  "peer": "172.20.0.3", "peerPort": "8080",
  "host": "172.20.0.4", "hostPort": "46594",
  "peerName": "app", "hostName": "upstream",
  "duration": "6.601778ms",
  "attributes": { "serverAddr": "upstream", "serverPort": "46594",
                  "method": "GET", "url": "/upstream", "status": "200" }
}
```

Errors in the above span:
 
1. `serverAddr` still says `upstream`, even though the address it came from (`172.20.0.4`) is `loadgen`. 
2. `serverPort` is a random ephemeral port instead of `8443`.

The raw `host` and `hostPort` fields show the real tuple. 

## Concurrency matters

At `CONCURRENCY=1` the bug does not happen at all. To observe the fault, the
target app needs an inbound request in flight on the event loop at the same
moment the outbound TLS bytes go to the socket. With one request at a time there
is no inbound connection for the guess to land on, and every span comes out
correct.

This means a test that sends one request at a time will pass while the bug is
still there. The default here is `CONCURRENCY=150`.

This applies to the Node arm as well. Node runs one event loop, so the same
condition is needed: an inbound request in flight at the moment the outbound
ciphertext reaches the socket.

## Test results

Six runs, changing the app, the TLS stack and the concurrency level. Every row
below was measured. None are estimates.

| App and TLS client | `CONCURRENCY` | Instrumentation | Client spans | Wrong |
|---|---|---|---|---|
| CPython `asyncio` (memory BIO) | 150 | `otel/ebpf-instrument:latest` (v0.10.0) | 45,721 | 5,336 (11.7%), all reversed |
| CPython `asyncio` (memory BIO) | 150 | `grafana/beyla:3.32.0` | 27,568 | 3,090 (11.2%), all reversed |
| CPython `asyncio` (memory BIO) | 1 | `otel/ebpf-instrument:latest` | 62,898 | 0 |
| CPython `blocking` (socket BIO) | 150 | `otel/ebpf-instrument:latest` | 90,987 | 0 |
| Node `https` (memory BIO) | 150 | `otel/ebpf-instrument:latest` | 29,056 | 23,234 (80.0%), all reversed |
| Node `https` pooled (memory BIO) | 150 | `otel/ebpf-instrument:latest` | 38,698 | 9,723 (25.1%), all reversed |

The error rate moves around between runs. Repeats of the first row on the same
machine came out anywhere from 2.8% to 11.7%. That is expected for a race that
depends on what the event loop is doing at each outbound write. Treat any
non-zero count as a reproduction, and do not read the percentage as a measure
of how bad the bug is. The two zero rows were zero every time, over tens of
thousands of spans each.

The Node arm is worse than the CPython one, by a lot. The same caveat about
variance applies to it.

Those two zero rows above are the controls. Dropping concurrency to 1 makes the fault
go away. So does keeping the same concurrency and switching the same
application to a blocking socket-BIO TLS client, which is what
`CLIENT_MODE=blocking` does. That mode uses `socket` plus `ctx.wrap_socket()` in
a thread executor, where OpenSSL does the `write(2)` from inside `SSL_write`.
The application logic is the same in both arms, so the error tracks the TLS
stack.

```sh
CONCURRENCY=1 docker compose up --build -d               # no repro
CLIENT_MODE=blocking docker compose up --build -d        # no repro
```

### With a candidate fix

There is a candidate fix that correlates the `SSL*` to its socket by matching
the leading bytes of the TLS record, rather than guessing from thread activity.
It is not part of this repository. These are the same arms as above, measured on
the same machine, with that fix in place.

| App and TLS client | Stock | With the fix |
|---|---|---|
| CPython `asyncio` | 9.6% wrong | 0 |
| Node `https` | 80.0% wrong | 0 |
| Node `https` pooled | 25.1% wrong | 0 |

Read those zeros the same way as the zero rows above, and no more strongly. One
earlier CPython run under the fix left 3 wrong spans out of 40,724, which is
0.007% and not a clean zero. The fix also does nothing for a connection that was
already open before the instrumentation attached, because the setup call it
depends on has already happened by then.

## Environment

You need a kernel with BTF at `/sys/kernel/btf/vmlinux`. The `obi` container
runs privileged and shares `app`'s PID and network namespaces (`pid:
service:app`, `network_mode: service:app`).

This was built and tested on Docker Desktop on an arm64 macOS host, where eBPF
runs inside the Docker Desktop Linux VM. It worked with no special setup. The
VM kernel was `6.8.0-117-generic aarch64`, BTF was in place, and both
`otel/ebpf-instrument` and `grafana/beyla` publish `linux/arm64` images. The
same compose file should run on a Linux host without changes.

The one thing that did not work on macOS is the optional `bpftrace` script, and
only because of image architecture. See [`bpftrace/README.md`](bpftrace/README.md).

## Checking it without OBI

[`bpftrace/`](bpftrace/) has a standalone `bpftrace` script that shows the
binding failure on its own, with no OBI in the picture. It counts how many
socket syscalls happen inside each `SSL_write` or `SSL_write_ex` uprobe window,
which is the window OBI depends on. With a memory BIO the count is always zero.
With a socket BIO it is one. It is kept separate because it needs `bpftrace`
and some manual setup.

## Notes

- `app` does not verify the upstream certificate. This bug is about which peer
  a span gets attached to, and skipping verification saves sharing a cert
  between two images. Do not copy that setting into real code.
- `BIO_read` and `BIO_write` are exported by libcrypto, not libssl. If you probe
  them on libssl you will find nothing, with no error.
- CPython's `_ssl` module calls `SSL_write_ex`, not `SSL_write`. If you probe
  only `SSL_write`, a Python workload will look idle.

## Licence

Apache-2.0. See [LICENSE](LICENSE).
