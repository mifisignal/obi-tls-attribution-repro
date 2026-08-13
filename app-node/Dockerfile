FROM node:24-slim

# No npm dependencies: Node stdlib only. The TLS client path under test is
# Node's own TLSWrap, which drives the bundled OpenSSL through memory BIOs.
#
# Note that this image has no libssl.so / libcrypto.so at all -- Node links
# OpenSSL statically into the `node` binary and re-exports its symbols from the
# executable's own .dynsym. Instrumentation has to find them there.

COPY app.js /srv/app.js

ENV NODE_ENV=production
EXPOSE 8080
CMD ["node", "/srv/app.js"]
