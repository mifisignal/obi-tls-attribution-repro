// The service under test, Node.js arm.
//
// Mirrors app/app.py exactly: serves plaintext HTTP on :8080 (the INBOUND edge,
// from `loadgen`) and, for every inbound request, makes one OUTBOUND TLS call
// to `upstream:8443`.
//
// There is no CLIENT_MODE switch here because Node has no socket-BIO option to
// switch to. Node's TLS client is always TLSWrap, which drives OpenSSL through
// a pair of memory BIOs and hands the resulting ciphertext to the socket from
// the event loop, on a different call stack than the SSL_write that produced
// it. That is the same shape as CPython's CLIENT_MODE=asyncio arm, so this
// service is the Node equivalent of the reproducing case and has no control
// arm of its own -- app/app.py CLIENT_MODE=blocking remains the control.
//
// Node also bundles its own OpenSSL, statically linked into the `node` binary
// rather than loaded as libssl.so/libcrypto.so. That makes this arm a test of
// instrumentation attachment as much as of correlation.

'use strict';

const http = require('http');
const https = require('https');

const LISTEN_PORT = parseInt(process.env.LISTEN_PORT || '8080', 10);
const UPSTREAM_HOST = process.env.UPSTREAM_HOST || 'upstream';
const UPSTREAM_PORT = parseInt(process.env.UPSTREAM_PORT || '8443', 10);

// UPSTREAM_KEEPALIVE=true pools a fixed set of persistent TLS connections
// instead of opening one per request. That is the shape of the original
// real-world symptom: a long-lived pooled connection to a datastore, with one
// handshake at the start and thousands of application-data records after it.
const KEEPALIVE = (process.env.UPSTREAM_KEEPALIVE || 'false').toLowerCase() === 'true';
const POOL_SIZE = parseInt(process.env.UPSTREAM_POOL_SIZE || '16', 10);

// The upstream cert is self-signed and generated at image build time.
// Verifying it would require sharing the cert between two images and adds
// nothing to the reproduction -- the bug is about which peer a span is
// attributed to, not about certificate validation. Do not copy this into
// anything real.
//
const agent = KEEPALIVE
  ? new https.Agent({ keepAlive: true, maxSockets: POOL_SIZE, maxFreeSockets: POOL_SIZE })
  : new https.Agent({ keepAlive: false, maxSockets: Infinity });

const stats = { inbound: 0, outboundOk: 0, outboundErr: 0 };

function callUpstream() {
  return new Promise((resolve, reject) => {
    const req = https.request(
      {
        host: UPSTREAM_HOST,
        port: UPSTREAM_PORT,
        path: '/upstream',
        method: 'GET',
        agent,
        rejectUnauthorized: false,
        headers: {
          Accept: 'application/json',
          'User-Agent': 'app-outbound/1.0',
          // Without keepAlive, close each connection so every request produces
          // its own handshake, matching app.py's "Connection: close".
          ...(KEEPALIVE ? {} : { Connection: 'close' }),
        },
      },
      (res) => {
        let total = 0;
        res.on('data', (chunk) => {
          total += chunk.length;
        });
        res.on('end', () => resolve(total));
        res.on('error', reject);
      },
    );

    req.setTimeout(10000, () => req.destroy(new Error('upstream timeout')));
    req.on('error', reject);
    req.end();
  });
}

const server = http.createServer(async (req, res) => {
  stats.inbound += 1;

  try {
    const bytes = await callUpstream();
    stats.outboundOk += 1;
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ ok: true, upstreamBytes: bytes }));
  } catch (err) {
    stats.outboundErr += 1;
    res.writeHead(502, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ ok: false, error: String(err && err.message) }));
  }
});

// Match app.py: no artificial connection cap, the event loop is the point.
server.keepAliveTimeout = 5000;
server.maxRequestsPerSocket = 0;

setInterval(() => {
  console.log(
    `[app-node] inbound=${stats.inbound} outbound_ok=${stats.outboundOk} ` +
      `outbound_err=${stats.outboundErr}`,
  );
}, 10000).unref();

server.listen(LISTEN_PORT, '0.0.0.0', () => {
  console.log(
    `[app-node] node=${process.versions.node} openssl=${process.versions.openssl} ` +
      `listening on :${LISTEN_PORT}, upstream=${UPSTREAM_HOST}:${UPSTREAM_PORT} ` +
      `keepalive=${KEEPALIVE} pool=${KEEPALIVE ? POOL_SIZE : 0}`,
  );
});
