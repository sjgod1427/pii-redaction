#!/usr/bin/env python3
"""Search a redacted document for the things that would name its subject.

Span precision and recall answer "did the tool replace what we annotated?".  They
cannot answer the question a reviewer actually cares about — *can a reader still
work out who this is about?* — because anything the tool never looked at produces
neither a true positive nor a false negative.  It simply is not in the search
space.  An early revision of this tool scored 0.978/0.957 with a clean zero-leak
check while remaining re-identifiable in under a minute.

So this script takes the other side: a list of anchors that would give the subject
away, and greps the finished document for them.  Zero here means "none of the
anchors we thought of survived" — not "the document is anonymous".  It is a floor,
not a proof, and the anchor list is the part that needs human judgement.

    python evaluation/reidentification_audit.py \\
        "output/Red Herring Prospectus - REDACTED.docx" \\
        --terms evaluation/reidentification_terms.json

Exit status is 1 if any CRITICAL or HIGH anchor survived, so it can gate a build.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pii_redactor.docx_io import DocxFile  # noqa: E402


def load_terms(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def document_text(path: str) -> str:
    return "\n".join(paragraph.text for paragraph in DocxFile(path).paragraphs)


def count(haystack: str, needle: str) -> int:
    """Whole-word, case-insensitive count, tolerant of collapsed whitespace."""
    pattern = r"\b" + r"\s+".join(re.escape(part) for part in needle.split()) + r"\b"
    return len(re.findall(pattern, haystack, re.IGNORECASE))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("document", help="the redacted .docx to audit")
    parser.add_argument(
        "--terms",
        default=str(Path(__file__).with_name("reidentification_terms.json")),
        help="JSON of {severity: {anchor name: [terms]}}",
    )
    parser.add_argument("--json-out", default=None)
    args = parser.parse_args(argv)

    text = document_text(args.document)
    terms = load_terms(args.terms)

    results: dict[str, dict] = {}
    blocking = 0
    for severity, anchors in terms.items():
        if severity.startswith("_") or not isinstance(anchors, dict):
            continue  # commentary, not a severity band
        results[severity] = {}
        print(f"\n{severity}")
        for name, needles in anchors.items():
            hits = {needle: count(text, needle) for needle in needles}
            hits = {k: v for k, v in hits.items() if v}
            total = sum(hits.values())
            if total and severity in ("CRITICAL", "HIGH"):
                blocking += total
            results[severity][name] = {"total": total, "hits": hits}
            status = "PASS" if not total else "FAIL"
            detail = f"  {hits}" if hits else ""
            print(f"  {status}  {name:34} {total:5}{detail}")

    print("\n" + "=" * 66)
    print(f"surviving CRITICAL + HIGH anchors: {blocking}")
    print("note: zero means none of the listed anchors survived, not that the")
    print("      document is anonymous — the anchor list is a human judgement.")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(results, indent=2), encoding="utf-8")
    return 1 if blocking else 0


if __name__ == "__main__":
    raise SystemExit(main())
