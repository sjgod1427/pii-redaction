"""Pattern-based detectors for structured PII.

These handle the PII types that have a *shape* — emails, phone numbers, card
numbers, national IDs.  They are deliberately validator-backed (Luhn for cards,
octet ranges for IPs, digit counts for phones) so that precision stays high and
things like ticket numbers or share counts are not swept up.
"""

from __future__ import annotations

import re
from typing import Iterator

from .types import (
    BANK_ACCOUNT,
    CREDIT_CARD,
    DOB,
    EMAIL,
    IP_ADDRESS,
    NATIONAL_ID,
    PHONE,
    SSN,
    URL,
    Span,
)

# --- individual patterns ----------------------------------------------------

EMAIL_RE = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
)

URL_RE = re.compile(
    r"\b(?:https?://|www\.)[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?:/[^\s,;)\"']*)?"
)

# +91 20 4505 3237 / +1 (415) 555-0132 / 415-555-0132
INTL_PHONE_RE = re.compile(r"\+\s?\d{1,3}[\s\-.]?(?:\(?\d{2,5}\)?[\s\-.]?){1,4}\d{2,5}")
US_PHONE_RE = re.compile(r"\(?\b\d{3}\)?[\s\-.]\d{3}[\s\-.]\d{4}\b")
IN_MOBILE_RE = re.compile(r"\b[6-9]\d{4}[\s\-]?\d{5}\b")
PHONE_ANCHOR_RE = re.compile(
    r"(?i)\b(?:tel(?:ephone)?|phone|mobile|mob|fax|contact\s*(?:no|number)|helpline)\b"
    r"\s*(?:no\.?|number)?\s*[:.\-]?\s*([+\d][\d\s\-().]{6,20}\d)"
)

# Real top-level domains, used to confirm a whitespace-split match really is a
# host and not two ordinary words that happen to sit either side of a full stop.
KNOWN_TLDS = (
    "com", "net", "org", "edu", "gov", "info", "biz", "io", "co", "in", "uk",
    "us", "ai", "app", "dev", "me", "tv", "asia", "eu",
)
TLD_TAIL_RE = re.compile(
    r"(?i)\.(?:" + "|".join(KNOWN_TLDS) + r")(?:\.(?:in|uk|us|au|nz|za))?$"
)

# Whitespace-tolerant host: "kshinternational. com" is one host with a gap in it.
_GAP = r"\s{0,2}"
# A label may itself be split ("...co" + newline + "m"), so a gap is allowed
# between its characters.  That makes the pattern greedy, which `_trim_to_host`
# below undoes by walking the match back to the longest valid host.
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]|\s(?=[A-Za-z0-9]))*"
_HOST = rf"{_LABEL}(?:{_GAP}\.{_GAP}{_LABEL})+"
SPLIT_URL_RE = re.compile(
    rf"(?i)\b(?:https?://{_GAP}|www{_GAP}\.{_GAP}){_HOST}(?:/[^\s,;)\"']*)?"
)
SPLIT_EMAIL_RE = re.compile(
    rf"\b[A-Za-z0-9._%+-]+{_GAP}@{_GAP}{_HOST}"
)

SSN_RE = re.compile(r"\b(?!000|666|9\d\d)\d{3}[-\s](?!00)\d{2}[-\s](?!0000)\d{4}\b")

# "Registration number: 141032" — the six-digit tail of a CIN, printed on its
# own.  Its parent CIN is redacted everywhere else, so leaving the fragment is
# enough to look the company up in the companies registry.
REG_NUMBER_RE = re.compile(
    r"(?i)\b(?:registration|regn\.?|reg\.?|licence|license|membership)\s*(?:no\.?|number)?\s*"
    r"[:\-]?\s*([A-Z]{0,3}[-/]?\d{5,8}[A-Z]?)\b"
)

CARD_RE = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")

IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
IPV6_RE = re.compile(r"\b(?:[A-Fa-f0-9]{1,4}:){2,7}[A-Fa-f0-9]{1,4}\b")

PAN_RE = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")
CIN_RE = re.compile(r"\b[LU]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}\b")
SEBI_REG_RE = re.compile(r"\bIN[A-Z]\d{9}\b")
IFSC_RE = re.compile(r"\b[A-Z]{4}0[A-Z0-9]{6}\b")
GSTIN_RE = re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9]Z[A-Z0-9]\b")
AADHAAR_RE = re.compile(r"\b[2-9]\d{3}\s?\d{4}\s?\d{4}\b")
DIN_ANCHOR_RE = re.compile(r"(?i)\bDIN\s*[:\-]?\s*(\d{8})\b")
PASSPORT_RE = re.compile(r"(?i)\bpassport\s*(?:no\.?|number)?\s*[:\-]?\s*([A-PR-WY]\d{7})\b")
FIRM_REG_RE = re.compile(
    r"(?i)\b(?:firm\s+registration|registration)\s*(?:no\.?|number)?\s*[:\-]?\s*"
    r"([0-9]{6}[A-Z](?:\s*/\s*[A-Z]?[0-9]{6})?)\b"
)
BANK_ACCT_RE = re.compile(
    r"(?i)\b(?:a/?c|account)\s*(?:no\.?|number)?\s*[:\-]?\s*(\d[\d\s\-]{7,20}\d)\b"
)

MONTHS = (
    "January|February|March|April|May|June|July|August|September|October|"
    "November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept?|Oct|Nov|Dec"
)
DATE_RE = re.compile(
    rf"\b(?:(?:{MONTHS})\.?\s+\d{{1,2}},?\s+(?:19|20)\d{{2}}"
    rf"|\d{{1,2}}\s+(?:{MONTHS})\.?,?\s+(?:19|20)\d{{2}}"
    rf"|\d{{1,2}}[/-]\d{{1,2}}[/-](?:19|20)?\d{{2}})\b"
)
BIRTH_ANCHOR_RE = re.compile(r"(?i)\b(?:date\s+of\s+birth|d\.?o\.?b\.?|born\s+(?:on|in)|birthday)\b")

# Aadhaar-shaped digits show up in financial tables constantly, so the pattern
# only fires when the word Aadhaar is nearby.
AADHAAR_ANCHOR_RE = re.compile(r"(?i)\baadhaar|aadhar|uidai\b")


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value)


def _luhn_ok(number: str) -> bool:
    total, parity = 0, len(number) % 2
    for index, char in enumerate(number):
        digit = int(char)
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def _valid_ipv4(value: str) -> bool:
    parts = value.split(".")
    return len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 and (p == "0" or not p.startswith("0")) for p in parts)


def _plausible_phone(raw: str, anchored: bool, formatted: bool = False) -> bool:
    digits = _digits(raw)
    if not 8 <= len(digits) <= 15:
        return False
    if raw.strip().startswith("+") or anchored:
        return True
    # Without a "Telephone:" label the number has to carry its own evidence:
    # either a written-out grouping (415-555-0132) or an Indian mobile prefix.
    if formatted and len(digits) == 10:
        return True
    return len(digits) == 10 and digits[0] in "6789"


def _emit(match: re.Match, label: str, source: str, score: float = 1.0, group: int = 0) -> Span:
    return Span(match.start(group), match.end(group), label, match.group(group), source, score)


def _trim_to_host(candidate: str, label: str) -> str | None:
    """Cut a greedy match back to the longest prefix that is a real host.

    Allowing gaps inside a label lets the pattern run on past the address and
    into the next words ("...co m Telephone"). Walking the end backwards and
    keeping the first prefix whose de-spaced host ends in a known TLD recovers
    exactly "…co m" -> "kshinternational.com", and rejects the rest.
    """
    candidate = candidate.rstrip()
    for end in range(len(candidate), 3, -1):
        prefix = candidate[:end].rstrip(".,;:- \t\r\n")
        if not prefix:
            continue
        host = prefix.split("@")[-1] if label == EMAIL else prefix
        host = re.sub(r"(?i)^\s*https?:\s*//\s*", "", host)
        host = re.split(r"[/?#]", host)[0]
        if TLD_TAIL_RE.search("".join(host.split())):
            return prefix
    return None


def detect_split_contacts(text: str, max_gaps: int = 2) -> Iterator[Span]:
    """Find emails and URLs that Word has broken apart.

    Word splits a run wherever formatting or spell-check state changes, and a
    line break inside a long address leaves a literal space in the text:
    ``www.kshinternational. com``.  The plain patterns cannot match across that,
    which on this document left the issuer's own domain readable on the cover
    page while the same URL was redacted correctly elsewhere.

    The patterns below tolerate short gaps between the parts of a host.  Two
    guards keep them honest: a match must contain at least one and at most
    ``max_gaps`` whitespace characters, and the host must end in a real TLD —
    without which "visit the site. Company said" would join into a fake domain.
    """
    for regex, label, source in (
        (SPLIT_EMAIL_RE, EMAIL, "regex:email-split"),
        (SPLIT_URL_RE, URL, "regex:url-split"),
    ):
        for match in regex.finditer(text):
            original = _trim_to_host(match.group(0), label)
            if original is None:
                continue
            gaps = sum(1 for char in original if char.isspace())
            if gaps == 0 or gaps > max_gaps:
                continue  # no gap means the ordinary pass already has it
            yield Span(match.start(), match.start() + len(original), label, original, source, 0.85)


def detect_structured(text: str, section: str = "") -> Iterator[Span]:
    """Yield every pattern-based PII span found in ``text``."""

    for match in EMAIL_RE.finditer(text):
        yield _emit(match, EMAIL, "regex:email")

    for match in URL_RE.finditer(text):
        yield _emit(match, URL, "regex:url", 0.9)

    yield from detect_split_contacts(text)

    # --- phones -------------------------------------------------------------
    for match in PHONE_ANCHOR_RE.finditer(text):
        if _plausible_phone(match.group(1), anchored=True):
            yield _emit(match, PHONE, "regex:phone-anchored", 1.0, group=1)
    for regex, source, formatted in (
        (INTL_PHONE_RE, "regex:phone-intl", False),
        (US_PHONE_RE, "regex:phone-us", True),
        (IN_MOBILE_RE, "regex:phone-in-mobile", False),
    ):
        for match in regex.finditer(text):
            if _plausible_phone(match.group(0), anchored=False, formatted=formatted):
                yield _emit(match, PHONE, source, 0.9)

    # --- high-confidence identifiers ---------------------------------------
    for match in SSN_RE.finditer(text):
        yield _emit(match, SSN, "regex:ssn")

    for match in CARD_RE.finditer(text):
        digits = _digits(match.group(0))
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            yield _emit(match, CREDIT_CARD, "regex:card+luhn")

    for match in IPV4_RE.finditer(text):
        if _valid_ipv4(match.group(0)):
            yield _emit(match, IP_ADDRESS, "regex:ipv4")
    for match in IPV6_RE.finditer(text):
        yield _emit(match, IP_ADDRESS, "regex:ipv6", 0.8)

    for regex, source in (
        (PAN_RE, "regex:pan"),
        (CIN_RE, "regex:cin"),
        (SEBI_REG_RE, "regex:sebi-reg"),
        (IFSC_RE, "regex:ifsc"),
        (GSTIN_RE, "regex:gstin"),
    ):
        for match in regex.finditer(text):
            yield _emit(match, NATIONAL_ID, source)

    for match in DIN_ANCHOR_RE.finditer(text):
        yield _emit(match, NATIONAL_ID, "regex:din", 1.0, group=1)
    # A table cell that is nothing but eight digits, inside a table with a DIN
    # column, is a DIN.
    if "din" in section.casefold() and re.fullmatch(r"\s*\d{8}\s*", text):
        yield Span(0, len(text), NATIONAL_ID, text, "context:din-column", 0.9)

    for match in FIRM_REG_RE.finditer(text):
        yield _emit(match, NATIONAL_ID, "regex:firm-registration", 1.0, group=1)

    for match in REG_NUMBER_RE.finditer(text):
        yield _emit(match, NATIONAL_ID, "regex:registration-number", 0.95, group=1)

    for match in PASSPORT_RE.finditer(text):
        yield _emit(match, NATIONAL_ID, "regex:passport", 1.0, group=1)

    if AADHAAR_ANCHOR_RE.search(text):
        for match in AADHAAR_RE.finditer(text):
            yield _emit(match, NATIONAL_ID, "regex:aadhaar", 0.9)

    for match in BANK_ACCT_RE.finditer(text):
        yield _emit(match, BANK_ACCOUNT, "regex:bank-account", 0.9, group=1)


def detect_dates_of_birth(text: str, redact_all_dates: bool = False) -> Iterator[Span]:
    """Dates are only PII when they are somebody's date of birth.

    A prospectus is wall-to-wall dates (board resolutions, fiscal years, filing
    dates); redacting them all destroys the document for no privacy gain.  So by
    default a date has to sit next to a birth anchor to count.
    """
    anchors = [m.end() for m in BIRTH_ANCHOR_RE.finditer(text)]
    for match in DATE_RE.finditer(text):
        if redact_all_dates:
            yield _emit(match, DOB, "regex:date-all", 0.6)
            continue
        window_start = max(0, match.start() - 80)
        if any(window_start <= anchor <= match.start() for anchor in anchors) or BIRTH_ANCHOR_RE.search(
            text[window_start : match.start()]
        ):
            yield _emit(match, DOB, "regex:dob-anchored", 1.0)
