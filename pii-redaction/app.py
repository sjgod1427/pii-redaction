#!/usr/bin/env python3
"""Entry point for Hugging Face Spaces (Gradio SDK).

The Docker SDK requires a paid account, so the Space runs on the free Gradio
runtime instead: it installs `requirements.txt`, the apt packages in
`packages.txt`, and then executes this file. There is no build step, so the two
things the Dockerfile did at build time — fetching the spaCy model and
generating the sample documents — happen here instead.

The application itself is unchanged: this serves the same FastAPI app that
`uvicorn webapp.app:app` serves locally.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

PORT = int(os.environ.get("PORT", 7860))


def ensure_samples() -> None:
    """Generate the library documents if the image did not ship them.

    The prospectus is committed, but the three synthetic documents are built by
    a script so they are never stale relative to the generator.
    """
    expected = ["ticket_log.docx", "offer_letter.docx", "claim_form.docx"]
    if all((ROOT / "examples" / name).is_file() for name in expected):
        return
    try:
        from examples.make_samples import main as build

        build()
    except Exception as error:  # a missing sample must not stop the app booting
        print(f"warning: could not build sample documents ({error})", file=sys.stderr)


def main() -> None:
    ensure_samples()

    import uvicorn

    from webapp.app import app

    uvicorn.run(app, host="0.0.0.0", port=PORT, timeout_keep_alive=75)


if __name__ == "__main__":
    main()
