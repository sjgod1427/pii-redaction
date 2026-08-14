"""The library of documents offered in the UI.

Entries are resolved against the filesystem at request time, so a document that
is not shipped simply does not appear.  That is how the Red Herring Prospectus
is handled: it embeds photographs of a real PAN card and a real Aadhaar card, so
`.dockerignore` keeps it out of the published image and the library entry is
absent on a public deployment while still being available to anyone running the
tool locally against their own copy.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Sample:
    id: str
    name: str
    blurb: str
    highlights: tuple[str, ...]
    path: Path
    synthetic: bool = True

    def exists(self) -> bool:
        return self.path.is_file()

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "blurb": self.blurb,
            "highlights": list(self.highlights),
            "synthetic": self.synthetic,
            "kb": round(self.path.stat().st_size / 1024) if self.exists() else 0,
        }


SAMPLES: tuple[Sample, ...] = (
    Sample(
        id="ticket_log",
        name="Support ticket log",
        blurb="Three customer tickets. Exercises all nine required PII types, plus ticket, "
              "order and invoice numbers that must survive untouched.",
        highlights=("All 9 required types", "4 images", "Precision traps"),
        path=ROOT / "examples" / "ticket_log.docx",
    ),
    Sample(
        id="offer_letter",
        name="Employment offer letter",
        blurb="One employee described in depth — PAN, bank account, IFSC, personal address "
              "and a scanned ID card, signature block and acceptance QR code.",
        highlights=("Bank + IFSC", "ID card & signature", "QR code"),
        path=ROOT / "examples" / "offer_letter.docx",
    ),
    Sample(
        id="claim_form",
        name="Health insurance claim",
        blurb="Several people in one document: policyholder, nominee, treating physician. "
              "Aadhaar, card number and an IP address alongside medical context.",
        highlights=("Multiple people", "Aadhaar & card", "IP address"),
        path=ROOT / "examples" / "claim_form.docx",
    ),
    Sample(
        id="prospectus",
        name="Red Herring Prospectus",
        blurb="The real 128-page SEBI filing this tool was built for — 4,231 blocks, 8 embedded "
              "images including two scanned identity documents. The hardest case in the library "
              "and the slowest to run.",
        highlights=("4,231 blocks", "~80s run", "Real document"),
        path=ROOT / "examples" / "red_herring_prospectus.docx",
        synthetic=False,
    ),
)


def available() -> list[dict]:
    return [sample.as_dict() for sample in SAMPLES if sample.exists()]


def find(sample_id: str) -> Sample | None:
    for sample in SAMPLES:
        if sample.id == sample_id and sample.exists():
            return sample
    return None
