# syntax=docker/dockerfile:1
#
# PII Redactor — container image, targeted at Hugging Face Spaces (Docker SDK).
#
# Spaces contract: listen on 0.0.0.0:7860, run as a non-root user, and keep all
# writes inside the user's home.  The image is large (~2 GB) because the quality
# claims in the README depend on real models: en_core_web_lg is 445 MB and the
# OCR stack another ~180 MB.  Build with --build-arg SPACY_MODEL=en_core_web_sm
# for a ~450 MB lighter image at a real cost in recall.

FROM python:3.12-slim

ARG SPACY_MODEL=en_core_web_lg

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/home/user \
    PII_SPACY_MODEL=${SPACY_MODEL}

# libgl/libglib are needed by opencv (pulled in by rapidocr) even headless.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Hugging Face Spaces runs the container as uid 1000.
RUN useradd -m -u 1000 user
USER user
ENV PATH="/home/user/.local/bin:${PATH}"
WORKDIR /home/user/app

# Dependencies first so edits to the source do not invalidate the model layer.
COPY --chown=user requirements.txt ./
RUN pip install --user -r requirements.txt

COPY --chown=user pii_redactor/ ./pii_redactor/
COPY --chown=user webapp/ ./webapp/
COPY --chown=user examples/ ./examples/
COPY --chown=user evaluation/ ./evaluation/
COPY --chown=user redact.py app.py README.md ./

# Build the library's sample documents.  All values in them are invented; the
# prospectus is never bundled, because it embeds photographs of real PAN and
# Aadhaar cards and this image is published.
RUN python examples/make_samples.py

EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD curl -fsS http://localhost:7860/api/health || exit 1

CMD ["uvicorn", "webapp.app:app", "--host", "0.0.0.0", "--port", "7860", "--timeout-keep-alive", "75"]
