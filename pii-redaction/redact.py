#!/usr/bin/env python3
"""Command-line entry point for the PII redaction tool.

    python redact.py input.docx -o output.docx
    python redact.py input.docx -o out.docx --mode tag --types PERSON,EMAIL
    python redact.py input.docx -o out.docx --redact-institutions --no-ner
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pii_redactor import Policy, Redactor
from pii_redactor.types import ALL_LABELS


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Redact PII from a .docx document.")
    parser.add_argument("input", help="source .docx file")
    parser.add_argument("-o", "--output", help="destination .docx file", default=None)
    parser.add_argument(
        "--mode",
        choices=("fake", "tag"),
        default="fake",
        help="'fake' substitutes realistic surrogates (default); 'tag' writes [PERSON_1] style labels",
    )
    parser.add_argument(
        "--types",
        default=",".join(ALL_LABELS),
        help=f"comma-separated PII types to redact (default: all of {','.join(ALL_LABELS)})",
    )
    parser.add_argument("--seed", type=int, default=20260813, help="determinism seed")
    parser.add_argument("--locale", default="en_US", help="Faker locale for surrogate values")
    parser.add_argument("--model", default="en_core_web_lg", help="spaCy model name")
    parser.add_argument("--no-ner", action="store_true", help="disable the NER stage (rules only)")
    parser.add_argument(
        "--redact-institutions",
        action="store_true",
        help="also redact regulators, courts and stock exchanges (off by default)",
    )
    parser.add_argument(
        "--redact-all-dates",
        action="store_true",
        help="treat every date as a date of birth (default: only birth-anchored dates)",
    )
    parser.add_argument("--keep-metadata", action="store_true", help="do not scrub docx author/title metadata")
    parser.add_argument(
        "--keep-locations",
        action="store_true",
        help="do not redact place names learned from the document's addresses",
    )
    parser.add_argument(
        "--no-ocr",
        action="store_true",
        help="skip the image stage entirely (pictures pass through untouched)",
    )
    parser.add_argument(
        "--keep-images",
        action="store_true",
        help="analyse and audit pictures but do not modify them",
    )
    parser.add_argument(
        "--redact-all-images",
        action="store_true",
        help="replace every picture regardless of what was found in it",
    )
    parser.add_argument(
        "--keep-unclassified-images",
        action="store_true",
        help="keep pictures that yielded no evidence (default: replace them)",
    )
    parser.add_argument("--mapping", default=None, help="write the original->surrogate map here (JSON)")
    parser.add_argument("--detections", default=None, help="write a per-detection audit log here (CSV)")
    parser.add_argument(
        "--extra-terms",
        default=None,
        help="JSON file of {\"literal string\": \"PERSON|ORG\"} forced into the gazetteer",
    )
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    source = Path(args.input)
    if not source.exists():
        print(f"error: {source} not found", file=sys.stderr)
        return 2

    destination = Path(args.output) if args.output else source.with_name(source.stem + " - REDACTED.docx")

    extra_terms = {}
    if args.extra_terms:
        extra_terms = json.loads(Path(args.extra_terms).read_text(encoding="utf-8"))

    policy = Policy(
        labels=tuple(t.strip().upper() for t in args.types.split(",") if t.strip()),
        mode=args.mode,
        faker_locale=args.locale,
        seed=args.seed,
        redact_institutions=args.redact_institutions,
        redact_all_dates=args.redact_all_dates,
        spacy_model=args.model,
        disable_ner=args.no_ner,
        extra_terms=extra_terms,
        scrub_metadata=not args.keep_metadata,
        redact_locations=not args.keep_locations,
        disable_images=args.no_ocr,
        keep_images=args.keep_images,
        redact_all_images=args.redact_all_images,
        keep_unclassified_images=args.keep_unclassified_images,
    )

    redactor = Redactor(policy)
    if not redactor.ner.available and not policy.disable_ner and not args.quiet:
        print("warning: spaCy model unavailable — running with rules + gazetteer only", file=sys.stderr)
    if not policy.disable_images and not redactor.images.engines.available and not args.quiet:
        print(
            "warning: no OCR/vision engine available — every picture will be replaced unexamined",
            file=sys.stderr,
        )

    report = redactor.run(str(source), str(destination))

    if args.mapping:
        redactor.write_mapping(args.mapping)
    if args.detections:
        redactor.write_detections(args.detections)

    if not args.quiet:
        print(json.dumps(report.as_dict(), indent=2))
        print(f"\nredacted document written to: {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
