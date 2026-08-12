#!/usr/bin/env python3
"""Concurrent load generator.

CONCURRENCY is the single most important knob in this repository.

At CONCURRENCY=1 the bug DOES NOT REPRODUCE. The fault needs an inbound request
in flight on the event loop at the moment the outbound TLS ciphertext is written
to the socket; with one request at a time there is no inbound connection for the
eBPF fallback heuristic to latch onto, so the outbound span comes out looking
correct. A test that drives one request at a time reports green while the bug is
fully present.
"""

import asyncio
import os
import time

TARGET_HOST = os.environ.get("TARGET_HOST", "app")
TARGET_PORT = int(os.environ.get("TARGET_PORT", "8080"))
CONCURRENCY = int(os.environ.get("CONCURRENCY", "100"))
DURATION_S = float(os.environ.get("DURATION_S", "0"))  # 0 = run forever
WARMUP_DELAY_S = float(os.environ.get("WARMUP_DELAY_S", "5"))

_REQ = (
    "GET /work HTTP/1.1\r\n"
    f"Host: {TARGET_HOST}\r\n"
    "Connection: close\r\n"
    "User-Agent: loadgen/1.0\r\n"
    "\r\n"
).encode()

_stats = {"ok": 0, "err": 0}


async def one_request():
    reader, writer = await asyncio.open_connection(TARGET_HOST, TARGET_PORT)
    try:
        writer.write(_REQ)
        await writer.drain()
        await reader.read()
        _stats["ok"] += 1
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass


async def worker(stop_at: float | None):
    while stop_at is None or time.monotonic() < stop_at:
        try:
            await one_request()
        except Exception:  # noqa: BLE001 - keep hammering
            _stats["err"] += 1
            await asyncio.sleep(0.05)


async def reporter(stop_at: float | None):
    last = 0
    while stop_at is None or time.monotonic() < stop_at:
        await asyncio.sleep(5)
        done = _stats["ok"]
        print("loadgen concurrency=%d ok=%d err=%d rps=%.0f"
              % (CONCURRENCY, done, _stats["err"], (done - last) / 5.0), flush=True)
        last = done


async def main():
    print("loadgen waiting %.1fs for app/upstream..." % WARMUP_DELAY_S, flush=True)
    await asyncio.sleep(WARMUP_DELAY_S)
    print("loadgen -> http://%s:%d/work  CONCURRENCY=%d  DURATION_S=%s"
          % (TARGET_HOST, TARGET_PORT, CONCURRENCY,
             DURATION_S or "forever"), flush=True)
    if CONCURRENCY == 1:
        print("loadgen WARNING: CONCURRENCY=1 -- the bug does NOT reproduce at "
              "this setting. See README.", flush=True)

    stop_at = time.monotonic() + DURATION_S if DURATION_S > 0 else None
    tasks = [asyncio.create_task(worker(stop_at)) for _ in range(CONCURRENCY)]
    tasks.append(asyncio.create_task(reporter(stop_at)))
    await asyncio.gather(*tasks, return_exceptions=True)
    print("loadgen done ok=%d err=%d" % (_stats["ok"], _stats["err"]), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
