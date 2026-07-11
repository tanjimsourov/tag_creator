FROM python:3.12-slim

ARG INSTALL_LOCAL_AI=false

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TAG_CREATOR_CPU_THREADS=2 \
    OMP_NUM_THREADS=2 \
    OPENBLAS_NUM_THREADS=2 \
    MKL_NUM_THREADS=2 \
    VECLIB_MAXIMUM_THREADS=2 \
    NUMEXPR_NUM_THREADS=2 \
    TF_NUM_INTRAOP_THREADS=2 \
    TF_NUM_INTEROP_THREADS=2

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libsndfile1 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install from the pinned lock files for reproducible builds.
COPY requirements.lock requirements.lock
COPY requirements-ai.lock requirements-ai.lock
RUN pip install --upgrade pip \
    && pip install -r requirements.lock \
    && if [ "$INSTALL_LOCAL_AI" = "true" ]; then pip install -r requirements-ai.lock; fi

COPY pyproject.toml pyproject.toml
COPY tag_creator tag_creator
COPY scripts scripts
COPY docs docs
COPY README.md README.md

# Run as a non-root user and give it ownership of the writable volumes.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p data output models/local_ai media logs \
    && chown -R appuser:appuser /app
USER appuser

VOLUME ["/app/media", "/app/data", "/app/output", "/app/models/local_ai", "/app/logs"]

# Lightweight health check: config + package import must be valid.
HEALTHCHECK --interval=1m --timeout=15s --retries=3 \
    CMD python -m tag_creator --check-config || exit 1

# Optionally fetch open-source models at runtime (they live on a mounted volume):
#   docker run --rm --entrypoint python tag_creator scripts/download_models.py
ENTRYPOINT ["python", "-m", "tag_creator"]
CMD ["--input-dir", "media", "--report", "output/enrichment_report.csv", "--dry-run"]
