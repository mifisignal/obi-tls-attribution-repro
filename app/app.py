#!/usr/bin/env python3
"""The service under test.

Serves plaintext HTTP on :8080 (the INBOUND edge, from `loadgen`) and, for
every inbound request, makes an OUTBOUND TLS call to `upstream:8443`.

The whole point of this file is HOW the outbound TLS call is made.

  CLIENT_MODE=asyncio  (default, reproduces the bug)
      asyncio.open_connection(..., ssl=ctx). CPython's asyncio does TLS with
      ssl.MemoryBIO via SSLContext.wrap_bio -- a *memory BIO*. OpenSSL
      encrypts into a memory buffer; the resulting ciphertext is pushed to the
      kernel socket LATER, by the event loop, on a different call stack than
      the SSL_write that produced it.

  CLIENT_MODE=blocking (control arm, does NOT reproduce the bug)
      A plain blocking socket + ctx.wrap_socket() in a thread executor. This is
      a *socket BIO*: OpenSSL calls write(2) on the real fd from inside
      SSL_write itself, so the eBPF "sandwich" closes correctly and the peer is
      attributed correctly.

Running the same workload under both modes is the cleanest way to show that the
mis-attribution is caused by the BIO type, not by anything else in the app.
"""

import asyncio
import os
import socket
import ssl
import sys
import time

LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8080"))
UPSTREAM_HOST = os.environ.get("UPSTREAM_HOST", "upstream")
UPSTREAM_PORT = int(os.environ.get("UPSTREAM_PORT", "8443"))
CLIENT_MODE = os.environ.get("CLIENT_MODE", "asyncio").strip().lower()

# The upstream cert is self-signed and generated at image build time. Verifying
# it would require sharing the cert between two images and adds nothing to the
# reproduction -- the bug is about which peer a span is attributed to, not about
# certificate validation. Do not copy this into anything real.
_TLS = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
_TLS.check_hostname = False
_TLS.verify_mode = ssl.CERT_NONE

_REQ = (
    "GET /upstream HTTP/1.1\r\n"
    f"Host: {UPSTREAM_HOST}\r\n"
    "Connection: close\r\n"
    "Accept: application/json\r\n"
    "User-Agent: app-outbound/1.0\r\n"
    "\r\n"
).encode()

_stats = {"inbound": 0, "outbound_ok": 0, "outbound_err": 0}


async def call_upstream_asyncio() -> int:
    """Memory-BIO TLS. This is the code path that triggers the bug."""
    reader, writer = await asyncio.open_connection(
        UPSTREAM_HOST, UPSTREAM_PORT, ssl=_TLS
    )
    try:
        writer.write(_REQ)
        await writer.drain()
        data = await reader.read()
        return len(data)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (ssl.SSLError, OSError):
            pass


def _blocking_call() -> int:
    raw = socket.create_connection((UPSTREAM_HOST, UPSTREAM_PORT), timeout=10)
    with _TLS.wrap_socket(raw, server_hostname=UPSTREAM_HOST) as tls:
        tls.sendall(_REQ)
        total = 0
        while True:
            chunk = tls.recv(65536)
            if not chunk:
                break
            total += len(chunk)
        return total


async def call_upstream_blocking() -> int:
    """Socket-BIO TLS in a worker thread. Control arm: attributed correctly."""
    return await asyncio.get_running_loop().run_in_executor(None, _blocking_call)


CALL = call_upstream_asyncio if CLIENT_MODE == "asyncio" else call_upstream_blocking


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    """One inbound HTTP request -> one outbound TLS request.

    Note the ordering that matters for the bug: the inbound connection is still
    open and mid-request on this event-loop thread while the outbound TLS write
    happens. Under load that inbound peer is what the outbound span gets
    mislabelled with.
    """
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=15)
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError,
            asyncio.TimeoutError, ConnectionResetError):
        writer.close()
        return

    path = "/"
    try:
        path = head.split(b"\r\n", 1)[0].split(b" ")[1].decode()
    except (IndexError, UnicodeDecodeError):
        pass

    _stats["inbound"] += 1
    started = time.monotonic()

    if path in ("/healthz", "/stats"):
        body = ('{"inbound":%d,"outbound_ok":%d,"outbound_err":%d,"mode":"%s"}\n'
                % (_stats["inbound"], _stats["outbound_ok"],
                   _stats["outbound_err"], CLIENT_MODE)).encode()
        status = "200 OK"
    else:
        try:
            n = await CALL()
            _stats["outbound_ok"] += 1
            body = b'{"ok":true,"upstream_bytes":%d}\n' % n
            status = "200 OK"
        except Exception as exc:  # noqa: BLE001 - surface as 5xx, never crash
            _stats["outbound_err"] += 1
            body = ('{"ok":false,"error":%r}\n' % str(exc)).encode()
            status = "502 Bad Gateway"

    writer.write(
        ("HTTP/1.1 %s\r\nContent-Type: application/json\r\n"
         "Content-Length: %d\r\nConnection: close\r\n\r\n" % (status, len(body))
         ).encode() + body
    )
    try:
        await writer.drain()
    except (ConnectionResetError, BrokenPipeError):
        pass
    writer.close()

    if _stats["inbound"] % 200 == 0:
        print("app inbound=%d outbound_ok=%d outbound_err=%d last_ms=%.1f"
              % (_stats["inbound"], _stats["outbound_ok"], _stats["outbound_err"],
                 (time.monotonic() - started) * 1000), flush=True)


async def main():
    if CLIENT_MODE not in ("asyncio", "blocking"):
        print("CLIENT_MODE must be 'asyncio' or 'blocking'", file=sys.stderr)
        raise SystemExit(2)
    server = await asyncio.start_server(handle, "0.0.0.0", LISTEN_PORT, backlog=512)
    print("app listening on http://0.0.0.0:%d  CLIENT_MODE=%s  upstream=%s:%d"
          % (LISTEN_PORT, CLIENT_MODE, UPSTREAM_HOST, UPSTREAM_PORT), flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
