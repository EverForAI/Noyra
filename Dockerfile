# Multi-platform manifest digest for the explicitly pinned CPython 3.12.14
# Bookworm slim image. Update deliberately through the release checklist.
FROM python:3.12.14-slim-bookworm@sha256:a116514e19457bcb7af7efe9c3dd0b9b71e85b317694e7882a1c52aa15a78134

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    NOYRA_DATA_DIR=/var/lib/noyra \
    NOYRA_AT_REST_MODE=required \
    NOYRA_VOLUME_ENCRYPTION_BACKEND=attestation \
    NOYRA_VOLUME_ATTESTATION_PATH=/run/noyra/volume-attestation.json \
    NOYRA_BACKUP_KEYRING_PATH=/run/secrets/noyra-backup-keyring.json \
    NOYRA_HOST=127.0.0.1 \
    NOYRA_PORT=8765 \
    NOYRA_ALLOW_INSECURE_NON_LOOPBACK=false

ARG NOYRA_INSTALL_PROFILE=base
ENV NOYRA_INSTALL_PROFILE=${NOYRA_INSTALL_PROFILE}

RUN groupadd --system noyra && useradd --system --gid noyra --home /opt/noyra noyra
WORKDIR /opt/noyra

COPY pyproject.toml README.md requirements.lock requirements-cloud.lock ./
COPY src ./src
RUN python -m pip install --no-cache-dir --require-hashes -r requirements.lock \
    && if [ "$NOYRA_INSTALL_PROFILE" = "cloud" ]; then \
         python -m pip install --no-cache-dir --require-hashes -r requirements-cloud.lock; \
       elif [ "$NOYRA_INSTALL_PROFILE" != "base" ]; then \
         echo 'NOYRA_INSTALL_PROFILE must be base or cloud' >&2; exit 2; \
       fi \
    && python -m pip install --no-cache-dir --no-deps --no-build-isolation .

RUN install -d -o noyra -g noyra -m 0700 /var/lib/noyra
USER noyra
VOLUME ["/var/lib/noyra"]
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=3)"

CMD ["python", "-m", "noyra", "serve"]
