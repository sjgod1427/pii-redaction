"""Physical / mailing address detection.

Addresses have no single shape, so this works in two ways:

1. *Anchored* — find a postal code (Indian PIN or US ZIP) and grow the span
   left over comma-separated chunks that still look like address parts, then
   right over the state/country tail.
2. *Line-level* — a short, comma-heavy line full of address vocabulary and
   without prose verbs is an address even if the postal code sits on the next
   line (very common in the "Book Running Lead Managers" style blocks).
"""

from __future__ import annotations

import re
from typing import Iterator

from .config import ADDRESS_HINT_WORDS, INDIAN_STATES
from .types import ADDRESS, Span

# "Pune – 411 004" / "PIN: 410501" / "Mumbai - 400051"
# The dash before a PIN must separate words, not sit inside a reference code:
# "Pune – 411 004" is an address, "TKT-100294" and "NAV-HLT-4471209" are not.
# Blocking an upper-case letter or digit before the dash keeps "Pune-411004"
# working while rejecting every reference number in the corpus.
PIN_RE = re.compile(r"(?:(?<![A-Z0-9])[–—-]\s*|\bPIN\s*[:\-]?\s*)(\d{3}\s?\d{3})\b")
ZIP_RE = re.compile(r"\b[A-Z]{2}\s+\d{5}(?:-\d{4})?\b")

TAIL_WORDS = INDIAN_STATES | {"india", "usa", "united states", "uk", "singapore", "germany"}

PROSE_MARKERS = re.compile(
    r"(?i)\b(?:shall|will|may|has|have|were|was|are|is\s+(?:a|the)|pursuant|therefore|"
    r"accordingly|includes?|means|refers?|except|provided that)\b"
)

# Openers that reliably start a street address inside a longer sentence.
ADDRESS_OPENER_RE = re.compile(
    r"(?i)\b(?:gat\s+no\.?|survey\s+no\.?|s\.?\s*no\.?|plot\s+no\.?|khasra\s+no\.?|"
    r"village|door\s+no\.?|house\s+no\.?|flat\s+no\.?|shop\s+no\.?)\s*[:\-]?\s*\S"
)

BUILDING_RE = re.compile(r"(?:\b(?:no|gat|s\.?\s*no|plot|survey|flat|unit|door)\b\.?\s*)?\d+[/\-]?\d*")


_HINT_SINGLE = frozenset(w for w in ADDRESS_HINT_WORDS if w.isalpha())
_HINT_PHRASE = tuple(w for w in ADDRESS_HINT_WORDS if not w.isalpha())


def _hint_words(lowered: str) -> bool:
    """True when the chunk uses an address word as a word, not as a substring."""
    if set(re.findall(r"[a-z]+", lowered)) & _HINT_SINGLE:
        return True
    return any(re.search(rf"\b{re.escape(phrase)}", lowered) for phrase in _HINT_PHRASE)


def _chunk_is_address_like(chunk: str) -> bool:
    stripped = chunk.strip()
    if not stripped or len(stripped) > 90:
        return False
    # A chunk of running prose ("a company incorporated on July 30, 1979 under
    # the Companies Act") must not be swallowed into the address.
    if PROSE_MARKERS.search(stripped) or len(stripped.split()) > 8:
        return False
    lowered = stripped.casefold()
    # Hint words are matched as whole words.  A substring test here made
    # "recorded" an address because it contains "rd", and "post" matched inside
    # any word holding those letters — which let ordinary prose be swallowed
    # into an address span along with whatever PII was sitting in it.
    if _hint_words(lowered):
        return True
    if lowered in TAIL_WORDS:
        return True
    if BUILDING_RE.fullmatch(stripped.replace(" ", "")):
        return True
    # A chunk carrying digits is an address component ("940 Larch Street",
    # "Flat 702") only if it is not a sentence.  Counting lower-case words
    # separates "Pune – 411 004" from "the customer's SSN on file is 123-45-6789".
    if re.search(r"\d", stripped) and len(stripped.split()) <= 8:
        lowercase_words = [
            token for token in re.findall(r"[A-Za-z']+", stripped) if token[:1].islower()
        ]
        if len(lowercase_words) <= 1:
            return True
    # A short capitalised fragment such as "Pushpakamal" or "Bandra East".
    tokens = stripped.split()
    if 1 <= len(tokens) <= 5 and all(t[:1].isupper() or not t[:1].isalpha() for t in tokens):
        return True
    return False


# "… having its Registered Office at 11/3, …" — everything after "at" is the
# address and everything before it is prose.
ADDRESS_LEAD_IN_RE = re.compile(
    r"(?i)\b(?:office|premises|factory|plant|unit|branch|residence|situated|located|"
    r"registered|residing|resides?|lives?|based|delivered|shipped|dispatched)\s+"
    r"(?:at|in|to)\s+|\baddress\s*(?:at\s+|[:\-]\s*)"
)


def _expand_left(text: str, anchor: int, max_chunks: int = 9) -> int:
    """Walk backwards from a postal code over address-looking chunks."""
    head = text[:anchor]
    # never cross a label boundary such as "Registered Office:" or a cell break
    boundary = max(head.rfind(":"), head.rfind("|"), head.rfind("\n"), head.rfind("\t"))
    boundary = boundary + 1 if boundary >= 0 else 0
    lead_ins = list(ADDRESS_LEAD_IN_RE.finditer(text[:anchor]))
    if lead_ins:
        boundary = max(boundary, lead_ins[-1].end())
    head = text[boundary:anchor]

    start_offset = len(head)
    chunks = list(re.finditer(r"[^,]+", head))
    taken = 0
    for chunk in reversed(chunks):
        if taken >= max_chunks:
            break
        if not chunk.group(0).strip():
            continue  # empty piece left by ", " — not a reason to stop
        if not _chunk_is_address_like(chunk.group(0)):
            break
        start_offset = chunk.start()
        taken += 1
    if taken == 0:
        return anchor
    absolute = boundary + start_offset
    # trim leading punctuation/space
    while absolute < anchor and text[absolute] in " ,;-–—\t":
        absolute += 1
    return absolute


def _expand_right(text: str, anchor: int, max_chunks: int = 3) -> int:
    """Absorb the ", Maharashtra, India" tail that follows a postal code."""
    end = anchor
    for _ in range(max_chunks):
        step = re.match(r"\s*[,;]?\s*([^,;\n|\t]+)", text[end:])
        if not step:
            break
        candidate = step.group(1).strip().casefold().strip(".;")
        if candidate in TAIL_WORDS or candidate in INDIAN_STATES:
            end += step.end()
        else:
            break
    return end


def _looks_like_address_line(line: str) -> bool:
    stripped = line.strip()
    if not 12 <= len(stripped) <= 220:
        return False
    if PROSE_MARKERS.search(stripped):
        return False
    lowered = stripped.casefold()
    hits = sum(1 for word in ADDRESS_HINT_WORDS if re.search(rf"\b{re.escape(word)}\b", lowered))
    commas = stripped.count(",")
    has_number = bool(re.search(r"\d", stripped))
    if hits >= 2 and (commas >= 1 or has_number):
        return True
    if hits >= 1 and commas >= 2 and has_number:
        return True
    return False


def detect_addresses(text: str, section: str = "") -> Iterator[Span]:
    seen: list[tuple[int, int]] = []

    for match in list(PIN_RE.finditer(text)) + list(ZIP_RE.finditer(text)):
        anchor_start, anchor_end = match.start(), match.end()
        start = _expand_left(text, anchor_start)
        end = _expand_right(text, anchor_end)
        if end - start < 12:
            continue
        seen.append((start, end))
        yield Span(start, end, ADDRESS, text[start:end], "address:postal-anchor", 1.0)

    # Line-level fallback for address blocks split across paragraphs.
    offset = 0
    for line in text.split("\n"):
        line_start, line_end = offset, offset + len(line)
        offset = line_end + 1
        if any(s <= line_start and line_end <= e for s, e in seen):
            continue
        if any(line_start < e and s < line_end for s, e in seen):
            continue
        if _looks_like_address_line(line):
            stripped_start = line_start + (len(line) - len(line.lstrip()))
            stripped_end = line_end - (len(line) - len(line.rstrip()))
            yield Span(
                stripped_start,
                stripped_end,
                ADDRESS,
                text[stripped_start:stripped_end],
                "address:line-heuristic",
                0.7,
            )

    # Addresses that start mid-sentence ("His contact details are as set forth
    # below: Gat No. 11/3, 11/4, 11/5, Village Birdewadi") are found by their
    # opening marker and run to the end of the line.
    offset = 0
    for line in text.split("\n"):
        line_start = offset
        offset += len(line) + 1
        marker = ADDRESS_OPENER_RE.search(line)
        if not marker:
            continue
        start = line_start + marker.start()
        end = line_start + len(line.rstrip())
        if end - start < 15:
            continue
        if any(s < end and start < e for s, e in seen):
            continue
        tail = text[start:end]
        if sum(1 for word in ADDRESS_HINT_WORDS if re.search(rf"\b{re.escape(word)}\b", tail.casefold())) >= 1:
            seen.append((start, end))
            yield Span(start, end, ADDRESS, tail, "address:opener", 0.8)

    # A table cell sitting under an "Address" column is an address, full stop.
    if "address" in section.casefold() and text.strip() and not any(seen):
        stripped = text.strip()
        if len(stripped) > 15 and _looks_like_address_line(stripped):
            start = text.index(stripped)
            yield Span(start, start + len(stripped), ADDRESS, stripped, "address:column", 0.9)
