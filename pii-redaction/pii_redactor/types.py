"""Core data types shared by every stage of the pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


# --- PII labels -------------------------------------------------------------
# Adding a new PII type means: (1) add a label here, (2) emit it from a
# detector, (3) give it a surrogate generator in surrogates.py.  Nothing else
# in the pipeline needs to change.

PERSON = "PERSON"
EMAIL = "EMAIL"
PHONE = "PHONE"
ORG = "ORG"
ADDRESS = "ADDRESS"
SSN = "SSN"
CREDIT_CARD = "CREDIT_CARD"
DOB = "DOB"
IP_ADDRESS = "IP_ADDRESS"
URL = "URL"
NATIONAL_ID = "NATIONAL_ID"  # PAN, Aadhaar, DIN, CIN, passport, SEBI reg. no.
BANK_ACCOUNT = "BANK_ACCOUNT"
#: A bare place name in running prose — the city, town or village a company
#: operates from.  Not identifying on its own, but a set of them ("Chakan",
#: "Supa", "Taloja") names the issuer as surely as its own letterhead.
LOCATION = "LOCATION"

# --- image labels -----------------------------------------------------------
# Embedded pictures carry PII that no amount of text scanning will ever see: a
# scanned ID card, a handwritten signature, a QR code encoding a live URL, a
# company logo.  They are classified by running the *text* stack above over
# whatever can be lifted out of the picture, so an image is handled by the same
# detectors as a paragraph.
IMAGE_ID_DOCUMENT = "IMAGE_ID_DOCUMENT"
IMAGE_SIGNATURE = "IMAGE_SIGNATURE"
IMAGE_CODE = "IMAGE_CODE"          # QR / barcode
IMAGE_LOGO = "IMAGE_LOGO"
IMAGE_UNCLASSIFIED = "IMAGE_UNCLASSIFIED"

IMAGE_LABELS = (
    IMAGE_ID_DOCUMENT,
    IMAGE_SIGNATURE,
    IMAGE_CODE,
    IMAGE_LOGO,
    IMAGE_UNCLASSIFIED,
)

TEXT_LABELS = (
    PERSON,
    EMAIL,
    PHONE,
    ORG,
    ADDRESS,
    SSN,
    CREDIT_CARD,
    DOB,
    IP_ADDRESS,
    URL,
    NATIONAL_ID,
    BANK_ACCOUNT,
    LOCATION,
)

ALL_LABELS = TEXT_LABELS + IMAGE_LABELS

#: Finding any of these *inside* a picture means the picture is an identity
#: document — a scan of a card, passport or certificate belonging to a person.
IDENTITY_EVIDENCE_LABELS = frozenset(
    {PERSON, NATIONAL_ID, DOB, ADDRESS, SSN, CREDIT_CARD, BANK_ACCOUNT}
)

# When two detections overlap, the label with the higher priority wins.
# Structured, validator-backed types beat model-guessed types.
LABEL_PRIORITY = {
    CREDIT_CARD: 100,
    SSN: 100,
    IP_ADDRESS: 95,
    EMAIL: 90,
    NATIONAL_ID: 85,
    BANK_ACCOUNT: 85,
    PHONE: 80,
    URL: 75,
    DOB: 70,
    ADDRESS: 60,
    PERSON: 50,
    ORG: 40,
    LOCATION: 30,   # loses to everything: a place inside an address is an address
}


@dataclass(frozen=True)
class Span:
    """A detected piece of PII inside one text block."""

    start: int
    end: int
    label: str
    text: str
    source: str = ""          # which detector produced it (for auditing)
    score: float = 1.0        # confidence, used to break overlap ties

    def __len__(self) -> int:
        return self.end - self.start

    def overlaps(self, other: "Span") -> bool:
        return self.start < other.end and other.start < self.end

    def key(self) -> tuple[str, str]:
        """Identity used for consistent surrogate assignment."""
        return (self.label, normalise(self.text))


@dataclass
class Detection:
    """A span plus the surrogate that replaced it — one row of the audit log."""

    block_id: int
    span: Span
    replacement: str


def normalise(text: str) -> str:
    """Casefold + collapse whitespace/punctuation noise for entity matching."""
    text = text.replace("’", "'").replace("‘", "'")
    text = " ".join(text.split())
    return text.strip(" .,:;–—-").casefold()


def merge_spans(spans: Iterable[Span]) -> list[Span]:
    """Resolve overlaps.

    Longest span wins first — an address that contains a city and a person's
    name should be redacted as one address, not shredded into pieces.  Ties go
    to the higher-priority label (validator-backed types beat model guesses),
    then to the higher-scoring detector.
    """
    ordered = sorted(
        spans,
        key=lambda s: (
            -len(s),
            -LABEL_PRIORITY.get(s.label, 0),
            -s.score,
            s.start,
        ),
    )
    kept: list[Span] = []
    for span in ordered:
        if any(span.overlaps(k) for k in kept):
            continue
        kept.append(span)
    return sorted(kept, key=lambda s: s.start)
