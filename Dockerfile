# Backend image: FastAPI + psycopg, no build toolchain needed at runtime
# (psycopg[binary] ships wheels, argon2-cffi has manylinux wheels).
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

# Dependencies first so code edits don't invalidate the wheel layer.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY web/ ./web/

# Non-root. Nothing in /srv needs to be writable at runtime.
RUN useradd --system --uid 10001 --no-create-home diet

# The Garmin raw-payload archive. Created here, owned by the runtime user, so
# that a fresh named volume mounted over it inherits that ownership -- Docker
# seeds an empty volume from the image, and a path that does not exist in the
# image becomes a root-owned mountpoint the container cannot write to.
RUN mkdir -p /var/log/diet/garmin && chown -R 10001:10001 /var/log/diet

USER 10001

EXPOSE 8080

# Cheap liveness probe: /api/health touches the pool but needs no session.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=4).status==200 else 1)"

# --forwarded-allow-ips is deliberately not "*": uvicorn then rewrites
# request.client.host from a header any client can send, which is what the
# login throttle counts against. It defaults to 127.0.0.1 and is overridden
# with FORWARDED_ALLOW_IPS for a real proxy.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
