#!/usr/bin/env python3
"""Minimal TLS HTTP upstream.

Serves HTTPS on :8443 using a self-signed certificate generated at container
build time. Deliberately boring: it exists only to be the *correct* peer of the
outbound TLS call that `app` makes, so that a mis-attributed client span is
obvious.

Uses a blocking thread-per-connection socket TLS server (socket BIO), because
the upstream side is not the side under test.
"""

import http.server
import os
import ssl
import socketserver

PORT = int(os.environ.get("UPSTREAM_PORT", "8443"))
CERT = os.environ.get("UPSTREAM_CERT", "/certs/upstream.pem")
KEY = os.environ.get("UPSTREAM_KEY", "/certs/upstream.key")


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "upstream/1.0"

    def do_GET(self):  # noqa: N802
        body = b'{"upstream":"ok","path":"%s"}\n' % self.path.encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        # One line per request, unbuffered, so `docker compose logs upstream`
        # can be used to confirm the outbound calls really landed here.
        print("upstream %s" % (fmt % args), flush=True)


class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=CERT, keyfile=KEY)

    httpd = ThreadedHTTPServer(("0.0.0.0", PORT), Handler)
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    print("upstream listening on https://0.0.0.0:%d" % PORT, flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
