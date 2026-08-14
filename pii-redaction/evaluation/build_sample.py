#!/usr/bin/env python3
"""Draw a stratified, system-independent sample of blocks for manual annotation.

Sampling must not depend on what the redactor found, otherwise recall is
measured only where the tool already looked.  Blocks are therefore stratified by
*document section* (which is decided by the document's own headings, not by the
tool), with the PII-dense front matter deliberately over-sampled so that rare
types — phone numbers, national IDs, addresses — appear often enough to score.

    python evaluation/build_sample.py "../Red Herring Prospectus.docx" \
        --out evaluation/sample.jsonl --size 150
"""

from __future__ import annotations

import argparse
import json
import random
import re
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pii_redactor.docx_io import DocxFile  # noqa: E402

# Headings that open a section of the prospectus, mapped to a stratum name.
STRATA = [
    ("front_matter", re.compile(r"(?i)^(red herring prospectus|definitions and abbreviations)")),
    ("general_information", re.compile(r"(?i)^general information\b")),
    ("management", re.compile(r"(?i)^(our management|our promoters|promoter group)")),
    ("business", re.compile(r"(?i)^(our business|history and certain corporate matters|industry overview)")),
    ("financial", re.compile(r"(?i)^(financial|restated|other financial information|capital structure)")),
    ("offer_procedure", re.compile(r"(?i)^(offer procedure|offer structure|terms of the offer|general information document)")),
    ("legal_risk", re.compile(r"(?i)^(risk factors|outstanding litigation|government and other approvals)")),
]

# Blocks per stratum: PII lives in the front matter and management sections, so
# they get a larger share than their page count would suggest.
QUOTAS = {
    "front_matter": 25,
    "general_information": 25,
    "management": 30,
    "business": 20,
    "financial": 15,
    "offer_procedure": 20,
    "legal_risk": 15,
}


def classify(blocks: list[str]) -> list[str]:
    """Walk the document and tag each block with the section it sits in."""
    current = "front_matter"
    out = []
    for text in blocks:
        stripped = text.strip()
        if 3 < len(stripped) < 120:
            for name, pattern in STRATA:
                if pattern.match(stripped):
                    current = name
                    break
        out.append(current)
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("document")
    parser.add_argument("--out", default="evaluation/sample.jsonl")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--max-chars", type=int, default=600)
    parser.add_argument("--min-chars", type=int, default=40)
    args = parser.parse_args()

    docx_file = DocxFile(args.document)
    texts = [paragraph.text for paragraph in docx_file.paragraphs]
    sections = classify(texts)

    pools: dict[str, list[int]] = {name: [] for name in QUOTAS}
    for index, text in enumerate(texts):
        if not (args.min_chars <= len(text.strip()) <= args.max_chars):
            continue
        pools.setdefault(sections[index], []).append(index)

    rng = random.Random(args.seed)
    chosen: list[int] = []
    for stratum, quota in QUOTAS.items():
        pool = pools.get(stratum, [])
        chosen.extend(rng.sample(pool, min(quota, len(pool))))
    chosen.sort()

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        for index in chosen:
            handle.write(
                json.dumps(
                    {
                        "block_id": index,
                        "section": sections[index],
                        "text": texts[index],
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    print(f"wrote {len(chosen)} blocks to {args.out}")
    print({name: len(pool) for name, pool in pools.items()})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
