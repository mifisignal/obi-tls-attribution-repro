FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends openssl \
 && rm -rf /var/lib/apt/lists/*

# Self-signed cert baked at build time: no external network dependency, and no
# volume sharing needed (the client side does not verify -- see app/app.py).
RUN mkdir -p /certs \
 && openssl req -x509 -newkey rsa:2048 -nodes \
      -keyout /certs/upstream.key -out /certs/upstream.pem \
      -days 3650 -subj "/CN=upstream" \
      -addext "subjectAltName=DNS:upstream,DNS:localhost,IP:127.0.0.1"

COPY upstream.py /srv/upstream.py

ENV PYTHONUNBUFFERED=1
EXPOSE 8443
CMD ["python", "/srv/upstream.py"]
