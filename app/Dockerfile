FROM python:3.12-slim

# No pip dependencies: stdlib asyncio only. The TLS client path under test is
# CPython's own ssl.MemoryBIO integration in asyncio.sslproto.

COPY app.py /srv/app.py

ENV PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["python", "/srv/app.py"]
