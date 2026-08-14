#!/usr/bin/env python3
"""Deploy the web UI on Modal.

Modal builds a container image much as the Dockerfile does, so unlike a 512 MB
PaaS instance this runs the **full-quality** pipeline: en_core_web_lg for NER and
the ONNX OCR stack for embedded images. Nothing about the application changes —
this file only describes the image and hands Modal the same FastAPI app that
`uvicorn webapp.app:app` serves locally.

    pip install modal
    modal setup                    # opens a browser once, to authenticate
    modal deploy modal_app.py

Deploying prints a public URL of the form
``https://<workspace>--pii-redactor-web.modal.run``.
"""

from __future__ import annotations

from pathlib import Path

import modal

HERE = Path(__file__).resolve().parent

# Built once and cached: later deploys that only touch source code reuse the
# dependency layer and take seconds rather than minutes.
image = (
    modal.Image.debian_slim(python_version="3.12")
    # opencv arrives with the OCR stack and needs these even headless
    .apt_install("libgl1", "libglib2.0-0")
    .pip_install_from_requirements(HERE / "requirements.txt")
    .add_local_dir(HERE / "pii_redactor", "/root/pii_redactor", copy=True)
    .add_local_dir(HERE / "webapp", "/root/webapp", copy=True)
    .add_local_dir(HERE / "examples", "/root/examples", copy=True)
    .workdir("/root")
    # Build the synthetic library documents at image-build time, exactly as the
    # Dockerfile does, so the first visitor does not wait for them.
    .run_commands("cd /root && python examples/make_samples.py")
)

app = modal.App("pii-redactor", image=image)


@app.function(
    cpu=2,
    memory=4096,           # spaCy lg + ONNX comfortably; the prospectus peaks well under this
    timeout=900,           # a 4,200-block document takes ~90s, with headroom
    scaledown_window=600,  # stay warm for 10 minutes between visits
    # Exactly one container, deliberately.  Jobs live in memory (jobs.py) and the
    # uploaded file sits in that container's temporary directory, so an upload
    # handled by one replica and a progress stream handled by another would look
    # like an expired job.  Concurrency below lets one container serve many
    # requests at once, which is what this workload actually needs.
    max_containers=1,
)
@modal.concurrent(max_inputs=8)
@modal.asgi_app()
def web():
    import sys

    sys.path.insert(0, "/root")
    from webapp.app import app as fastapi_app

    return fastapi_app
