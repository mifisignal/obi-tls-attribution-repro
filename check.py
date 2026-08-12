#!/usr/bin/env python3
"""Tally OBI's outbound client spans by the peer it reports.

Usage:
    docker logs obi-repro-obi 2>&1 | python3 check.py

Every HTTPClient span emitted for `app` in this stack is the SAME outbound call:
app -> upstream:8443. There is exactly one outbound destination in the whole
system, so any HTTPClient span reporting a different peer is unambiguously the
bug -- OBI has attributed an outbound TLS client span to some other socket.

In practice the other socket is the INBOUND connection from loadgen that the
event loop happened to be serving at the moment the ciphertext hit the wire.
That shows up as a reversed edge: the span's "client" side is app's own
listening socket (app:8080) and its "server" side is loadgen's ephemeral port.

Watch the PORT, not the name. On a Docker bridge network reverse resolution can
still render the wrong address as the string "upstream", so a mis-attributed
span can read serverAddr=upstream with a serverPort that is a random ephemeral
port rather than 8443. The raw host/hostPort fields show the true tuple.
"""

import collections
import json
import os
import sys

UPSTREAM_PORT = os.environ.get("UPSTREAM_PORT", "8443")
APP_PORT = os.environ.get("LISTEN_PORT", "8080")


def spans(stream):
    for raw in stream:
        i = raw.find("[{")
        if i < 0:
            i = raw.find("{\"")
            if i < 0:
                continue
        try:
            doc = json.loads(raw[i:].strip())
        except json.JSONDecodeError:
            continue
        for s in (doc if isinstance(doc, list) else [doc]):
            if isinstance(s, dict):
                yield s


def main():
    kinds = collections.Counter()
    reported = collections.Counter()
    raw_tuple = collections.Counter()
    reversed_edge = 0
    total = 0
    example = None

    for s in spans(sys.stdin):
        kinds[s.get("type", "?")] += 1
        if s.get("type") != "HTTPClient":
            continue
        total += 1
        a = s.get("attributes", {}) or {}
        port = str(a.get("serverPort") or s.get("hostPort"))
        reported["%s:%s" % (a.get("serverAddr") or s.get("hostName"), port)] += 1
        if port != UPSTREAM_PORT:
            raw_tuple["client=%s:%s  server=%s:%s" % (
                s.get("peer"), s.get("peerPort"), s.get("host"), s.get("hostPort"))] += 1
            if str(s.get("peerPort")) == APP_PORT:
                reversed_edge += 1
            if example is None:
                example = s

    print("span types seen:")
    for k, v in kinds.most_common():
        print("  %-14s %d" % (k, v))

    if not total:
        print("\nNO HTTPClient SPANS CAPTURED -- cannot assess. Is OBI attached?")
        return 1

    wrong = total - reported.get("upstream:%s" % UPSTREAM_PORT, 0)
    # Anything not on the upstream port is wrong, regardless of resolved name.
    wrong = sum(n for k, n in reported.items() if not k.endswith(":" + UPSTREAM_PORT))

    print("\noutbound HTTPClient spans (total %d)" % total)
    print("  correct  -> upstream:%s      %6d  (%.1f%%)"
          % (UPSTREAM_PORT, total - wrong, 100.0 * (total - wrong) / total))
    print("  WRONG    -> some other peer  %6d  (%.1f%%)"
          % (wrong, 100.0 * wrong / total))
    print("  of which reversed edges (server side is app's own listener %s:%s): %d"
          % ("app", APP_PORT, reversed_edge))

    if wrong:
        print("\ntop mis-attributed raw socket tuples:")
        for k, n in raw_tuple.most_common(5):
            print("  %-52s %6d" % (k, n))
        print("\nBUG REPRODUCED. Example mis-attributed span:")
        print(json.dumps(example, indent=2))
    else:
        print("\nNo mis-attribution in this sample.")
        print("Check CONCURRENCY (the bug does not reproduce at 1) and CLIENT_MODE"
              " (only 'asyncio' reproduces). See README.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
