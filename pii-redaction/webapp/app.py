"""Web front end for the PII redaction tool.

Thin by design: it uploads a document, drives ``pii_redactor.Redactor`` on a
worker thread, streams the pipeline's own progress events to the browser, and
hands back the redacted file.  No detection logic lives here — the web layer
must never become a second, divergent implementation of the redaction rules.

Uploaded documents are treated as hostile-to-retain: they are held in a per-job
temporary directory, never logged, and deleted when the job expires.
"""

from __future__ import annotations

import base64
import io
import json
import re
import shutil
import sys
import threading
import time
import zipfile
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pii_redactor import Policy, Redactor  # noqa: E402
from pii_redactor.types import ALL_LABELS  # noqa: E402

from . import library  # noqa: E402
from .jobs import MAX_UPLOAD_BYTES, JobStore  # noqa: E402

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent

STAGES = [
    ("read", "Reading document"),
    ("learn", "Learning entities"),
    ("detect", "Detecting & substituting"),
    ("images", "Analysing images"),
    ("write", "Writing output"),
    ("verify", "Verifying no leaks"),
]

app = FastAPI(title="PII Redactor", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
store = JobStore()


@app.on_event("startup")
def _warm_models() -> None:
    """Load spaCy and the OCR engine before the first visitor arrives.

    Cold, the first request pays ~30s to load a 445 MB pipeline and the ONNX
    OCR models — which lands entirely on whoever clicks first.  Warming on a
    background thread moves that cost into container start-up, where the
    healthcheck's start-period already accounts for it.
    """

    def warm() -> None:
        try:
            redactor = Redactor(Policy())
            list(redactor.ner.pipe(["Warm up the pipeline."]))
            redactor.images.engines.ocr  # noqa: B018 - triggers lazy load
            redactor.images.engines.cv   # noqa: B018
        except Exception:
            pass  # a failed warm-up must never stop the server from serving

    threading.Thread(target=warm, daemon=True).start()


@app.on_event("shutdown")
def _cleanup() -> None:
    store.shutdown()


# --- pages ------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (BASE / "templates" / "index.html").read_text(encoding="utf-8")


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "labels": list(ALL_LABELS)}


# --- redaction --------------------------------------------------------------
@app.post("/api/redact")
async def redact(file: UploadFile) -> dict:
    name = (file.filename or "document.docx").strip()
    if not name.lower().endswith(".docx"):
        raise HTTPException(400, "Only .docx files are supported.")

    payload = await file.read()
    if len(payload) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"File is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
    if not payload.startswith(b"PK"):
        raise HTTPException(400, "That does not look like a .docx file.")

    job = store.create()
    source = job.path("source.docx")
    source.write_bytes(payload)

    options = {
        "mode": "fake",
        "no_ner": False,
        "images": True,
    }
    threading.Thread(target=_run_job, args=(job.id, name, options), daemon=True).start()
    return {"job": job.id, "name": name, "stages": [{"key": k, "label": v} for k, v in STAGES]}


@app.get("/api/library")
def library_list() -> dict:
    """Documents the visitor can run without supplying one of their own."""
    return {"samples": library.available()}


@app.post("/api/sample/{sample_id}")
def sample(sample_id: str) -> dict:
    """Run a bundled document — a real end-to-end run, not a replay."""
    chosen = library.find(sample_id)
    if chosen is None:
        raise HTTPException(404, "That document is not bundled in this deployment.")
    job = store.create()
    shutil.copyfile(chosen.path, job.path("source.docx"))
    threading.Thread(
        target=_run_job, args=(job.id, chosen.path.name, {"mode": "fake", "no_ner": False, "images": True}),
        daemon=True,
    ).start()
    return {"job": job.id, "name": chosen.name, "stages": [{"key": k, "label": v} for k, v in STAGES]}


@app.get("/api/library/{sample_id}/preview")
def library_preview(sample_id: str, limit: int = 160) -> dict:
    """Read a library document so it can be inspected before it is redacted."""
    chosen = library.find(sample_id)
    if chosen is None:
        raise HTTPException(404, "That document is not bundled in this deployment.")
    return _read_document(chosen.path, limit)


@app.get("/api/preview/{job_id}/{which}")
def preview(job_id: str, which: str, limit: int = 120) -> dict:
    """Readable rendering of a document, for the before/after viewer.

    Deliberately plain: paragraphs and pictures in order, no styling. The point
    is to let a reader compare what the two documents *say*, which is exactly
    what a reviewer needs and what a fidelity-preserving renderer would bury.
    """
    if which not in ("source", "redacted"):
        raise HTTPException(404, "No such view.")
    job = store.get(job_id)
    if job is None:
        raise HTTPException(404, "Unknown or expired job.")
    path = job.path("source.docx" if which == "source" else "redacted.docx")
    if not path.exists():
        raise HTTPException(404, "Not ready yet.")
    return {"which": which, **_read_document(path, limit)}


def _read_document(path: Path, limit: int) -> dict:
    from pii_redactor.docx_io import DocxFile

    blocks = []
    total = 0
    for paragraph in DocxFile(str(path)).paragraphs:
        text = " ".join(paragraph.text.split())
        if not text:
            continue
        total += 1
        if len(blocks) < limit:
            blocks.append({"text": text[:600]})

    return {
        "blocks": blocks,
        "total": total,
        "truncated": total > len(blocks),
        "images": _media_thumbnails(path, limit=8),
    }


def _media_thumbnails(path: Path, limit: int = 8) -> list[str]:
    try:
        from PIL import Image
    except Exception:
        return []
    out: list[str] = []
    try:
        with zipfile.ZipFile(path) as archive:
            for entry in sorted(archive.namelist()):
                if "/media/" not in entry or len(out) >= limit:
                    continue
                try:
                    with Image.open(io.BytesIO(archive.read(entry))) as image:
                        image = image.convert("RGB")
                        image.thumbnail((300, 300))
                        buffer = io.BytesIO()
                        image.save(buffer, format="JPEG", quality=70)
                    out.append(base64.b64encode(buffer.getvalue()).decode())
                except Exception:
                    continue
    except Exception:
        return []
    return out


def _run_job(job_id: str, name: str, options: dict) -> None:
    job = store.get(job_id)
    if job is None:
        return
    started = time.time()
    try:
        source = job.path("source.docx")
        destination = job.path("redacted.docx")

        policy = Policy(
            mode=options.get("mode", "fake"),
            disable_ner=bool(options.get("no_ner")),
            disable_images=not options.get("images", True),
        )
        redactor = Redactor(policy)

        def progress(stage: str, fraction: float, detail: str = "") -> None:
            job.emit("stage", stage=stage, fraction=round(fraction, 3), detail=detail)

        report = redactor.run(str(source), str(destination), progress=progress)

        job.emit("stage", stage="verify", fraction=0.0, detail="searching output")
        leaks = _leak_check(destination, redactor)
        job.emit("stage", stage="verify", fraction=1.0, detail="0 leaks" if not leaks else f"{len(leaks)} leaks")

        redactor.write_mapping(str(job.path("mapping.json")))
        redactor.write_detections(str(job.path("detections.csv")))

        result = {
            "name": name,
            "report": report.as_dict(),
            "images": _image_previews(source, destination, redactor),
            "samples": _detection_samples(redactor),
            "leaks": leaks,
            "seconds": round(time.time() - started, 1),
        }
        job.result = result
        job.finished = True
        job.emit("done", result=result)
    except Exception as error:  # surfaced to the browser, never a stack trace
        job.failed = f"{type(error).__name__}: {error}"
        job.finished = True
        job.emit("error", message=job.failed)


def _leak_check(destination: Path, redactor: Redactor) -> list[dict]:
    """Search the finished document for anything the tool claims to have replaced."""
    from pii_redactor.docx_io import DocxFile

    text = "\n".join(p.text for p in DocxFile(str(destination)).paragraphs)
    leaks = []
    for label, pairs in redactor.surrogates.as_mapping().items():
        for original in pairs:
            if len(original) < 5:
                continue
            pattern = re.escape(original).replace(r"\ ", r"\s+")
            if re.search(pattern, text):
                leaks.append({"type": label, "value": original[:60]})
    return leaks[:20]


def _image_previews(source: Path, destination: Path, redactor: Redactor, limit: int = 8) -> list[dict]:
    """Before/after thumbnails for each embedded picture."""
    try:
        from PIL import Image
    except Exception:
        return []

    def thumbnails(path: Path) -> dict[str, str]:
        out: dict[str, str] = {}
        try:
            with zipfile.ZipFile(path) as archive:
                for entry in archive.namelist():
                    if "/media/" not in entry:
                        continue
                    try:
                        with Image.open(io.BytesIO(archive.read(entry))) as image:
                            image = image.convert("RGB")
                            image.thumbnail((360, 360))
                            buffer = io.BytesIO()
                            image.save(buffer, format="JPEG", quality=72)
                        out["/" + entry] = base64.b64encode(buffer.getvalue()).decode()
                    except Exception:
                        continue
        except Exception:
            return {}
        return out

    before, after = thumbnails(source), thumbnails(destination)
    previews = []
    for decision in redactor.images.decisions[:limit]:
        key = decision.name if decision.name.startswith("/") else "/" + decision.name
        previews.append(
            {
                "name": key.rsplit("/", 1)[-1],
                "label": decision.label or "IMAGE_CLEAN",
                "action": decision.action,
                "reason": decision.reason,
                "evidence": decision.evidence.summary(),
                "before": before.get(key, ""),
                "after": after.get(key, ""),
            }
        )
    return previews


def _detection_samples(redactor: Redactor, per_label: int = 4) -> list[dict]:
    """A few original -> surrogate pairs per type, for the diff panel."""
    seen: dict[str, int] = {}
    samples = []
    for item in redactor.detections:
        label = item.span.label
        if seen.get(label, 0) >= per_label:
            continue
        original = " ".join(item.span.text.split())
        if not original:
            continue
        seen[label] = seen.get(label, 0) + 1
        samples.append({"label": label, "original": original[:70], "replacement": item.replacement[:70]})
    return samples


# --- progress stream --------------------------------------------------------
@app.get("/api/events/{job_id}")
def events(job_id: str) -> StreamingResponse:
    job = store.get(job_id)
    if job is None:
        raise HTTPException(404, "Unknown or expired job.")

    def stream():
        for event in job.history():
            yield _sse(event.kind, event.payload)
        while True:
            event = job.next_event(timeout=1.0)
            if event is None:
                if job.finished:
                    break
                yield ": keep-alive\n\n"
                continue
            yield _sse(event.kind, event.payload)
            if event.kind in ("done", "error"):
                break

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _sse(kind: str, payload: dict) -> str:
    return f"event: {kind}\ndata: {json.dumps(payload)}\n\n"


# --- downloads --------------------------------------------------------------
ARTIFACTS = {
    "document": ("redacted.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    "mapping": ("mapping.json", "application/json"),
    "detections": ("detections.csv", "text/csv"),
}


@app.get("/api/download/{job_id}/{artifact}")
def download(job_id: str, artifact: str):
    job = store.get(job_id)
    if job is None:
        raise HTTPException(404, "Unknown or expired job.")
    if artifact not in ARTIFACTS:
        raise HTTPException(404, "No such artifact.")
    filename, media_type = ARTIFACTS[artifact]
    path = job.path(filename)
    if not path.exists():
        raise HTTPException(404, "Not ready yet.")
    stem = Path(job.result.get("name", "document")).stem
    suffix = {"document": " - REDACTED.docx", "mapping": " - mapping.json", "detections": " - detections.csv"}[artifact]
    return FileResponse(path, media_type=media_type, filename=f"{stem}{suffix}")
