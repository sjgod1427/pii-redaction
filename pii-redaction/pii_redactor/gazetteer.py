"""Document-level entity inventory.

Pass 1 of the pipeline reads the whole document and builds a lexicon of the
people and companies it mentions, using high-precision contextual patterns
("Contact person: X", "being <Name>", legal suffixes like "… Limited") plus
NER candidates that survive filtering.

Pass 2 then matches that lexicon literally against every block.  This is what
turns a 60-70% recall NER model into near-total recall: a name only has to be
recognised *once*, in one favourable context, to be redacted *everywhere* —
including the all-caps cover page, footnotes and table cells where NER is weak.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Iterator

from .config import (
    ADDRESS_HINT_WORDS,
    COUNTRY_WORDS,
    DEFINED_TERM_STOPLIST,
    INDIAN_STATES,
    INSTITUTION_ALLOWLIST,
    ORG_LEGAL_SUFFIXES,
    PERSON_STOPWORDS,
    PLACE_ALLOWLIST,
    PLACE_NOISE_WORDS,
    Policy,
)
from .types import LOCATION, ORG, PERSON, Span, normalise

NAME_TOKEN = r"[A-Z][A-Za-z'’\-]+|[A-Z]\."
NAME_RE = re.compile(rf"(?:{NAME_TOKEN})(?:\s+(?:{NAME_TOKEN})){{1,3}}")

CONTACT_RE = re.compile(r"(?i)\bcontact\s+person\s*[:\-]?\s*([^\n|]{3,120})")
TITLE_RE = re.compile(
    rf"\b(?:Mr|Ms|Mrs|Dr|Shri|Smt|Prof|Justice)\.?\s+({NAME_RE.pattern})"
)
BEING_RE = re.compile(rf"\bbeing\s+({NAME_RE.pattern})\b")
IS_OUR_RE = re.compile(rf"\b({NAME_RE.pattern})\s+is\s+(?:our|the)\b")
AGED_RE = re.compile(rf"\b({NAME_RE.pattern}),?\s+aged\b")
SIGNED_RE = re.compile(rf"(?i)\b(?:signed\s+by|sd/-|authorised\s+signatory)\s*[:\-]?\s*({NAME_RE.pattern})")
# "1,000 equity shares allotted to Karunakar N. Bhandary" / "held by X"
HELD_BY_RE = re.compile(
    rf"(?i)\b(?:allotted\s+to|transferred\s+(?:to|from|by)|acquired\s+(?:by|from)|held\s+by|"
    rf"in\s+favour\s+of|gifted\s+to|sold\s+to|nominee\s+of)\s+({NAME_RE.pattern})"
)

_SUFFIX_ALT = "|".join(
    sorted((re.escape(s) for s in ORG_LEGAL_SUFFIXES), key=len, reverse=True)
).replace(r"\ ", r"\s+")
# Case-sensitive on purpose: company names are capitalised, ordinary prose is not.
ORG_LEGAL_RE = re.compile(
    rf"\b([A-Z][\w&'’.\-]*(?:\s+(?:[A-Z0-9][\w&'’.\-]*|of|and|&|the|for|de|von))*?"
    rf"\s+(?:{_SUFFIX_ALT}))(?![\w])"
)
ORG_SUFFIX_STRIP_RE = re.compile(
    r"(?i)\s*\b(?:private\s+limited|limited|ltd\.?|llp|inc\.?|incorporated|corporation|"
    r"corp\.?|plc|gmbh|b\.v\.|n\.v\.|pte\.?|company|chartered\s+accountants|"
    r"&\s+associates|&\s+sons|trust|foundation|huf)\s*$"
)
# ("KSH") or (“Nuvama”) style short-form definitions
QUOTED_ALIAS_RE = re.compile(r"[(\[]?[“\"']([A-Z][A-Za-z&.\- ]{1,28})[”\"'][)\]]?")

# Words that a greedy company-name match tends to pick up in front of the real
# name ("Registered Office of our Company KSH International Limited").
LEADING_NOISE = {
    "the", "our", "of", "and", "by", "to", "for", "with", "at", "in", "from",
    "being", "formerly", "registered", "corporate", "office", "company",
    "escrow", "collection", "designated", "statutory", "namely", "erstwhile",
}

GENERIC_ORG_WORDS = {
    "the company", "our company", "company", "the issuer", "issuer", "the offer",
    "group companies", "subsidiary", "subsidiaries", "the board", "board",
    "promoter group", "the promoters", "our promoters", "the bank", "banks",
    "government", "the group",
}


def has_legal_suffix(text: str) -> bool:
    lowered = " ".join(text.split()).casefold().strip(" .,;:")
    return any(lowered.endswith(suffix.casefold().strip(".")) for suffix in ORG_LEGAL_SUFFIXES)


def _clean(text: str) -> str:
    return " ".join(text.replace(" ", " ").split()).strip(" ,.;:|-–—")


# Leading determiners and trailing generic nouns that get harvested along with a
# place name: "our Supa Facility" is the Supa plant, "the Cap Price" is neither.
_PLACE_LEAD_RE = re.compile(r"(?i)^(?:the|our|its|their|a|an|at|in|near|off)\s+")
_PLACE_TAIL_RE = re.compile(
    r"(?i)\s+(?:facility|facilities|plant|plants|unit|units|works|factory|office|"
    r"offices|branch|branches|division|locations|location|region|area|dollars?|"
    r"rupees?|bank|banks|premises|site|sites)$"
)


# A surname learned on its own ("Shetty") still matches inside a full name, and
# replacing only that half leaves the given name in place — "Narayna B. Shetty"
# became "Narayna B. Martin".  Given names and initials immediately before the
# match are pulled into the span so the whole name is replaced together.
_GIVEN_NAME_TAIL_RE = re.compile(r"(?:[A-Z][a-z'’\-]{1,20}|[A-Z]\.)(?:\s+(?:[A-Z][a-z'’\-]{1,20}|[A-Z]\.))*\s+$")
_HONORIFIC_RE = re.compile(r"(?i)(?:mr|mrs|ms|dr|shri|smt|prof|justice)\.?\s+$")


def _extend_over_given_names(text: str, start: int, max_tokens: int = 3) -> int:
    head = text[max(0, start - 60) : start]
    match = _GIVEN_NAME_TAIL_RE.search(head)
    if not match:
        return start
    candidate = match.group(0)
    if len(candidate.split()) > max_tokens:
        candidate = " ".join(candidate.split()[-max_tokens:]) + " "
    # Never swallow an honorific — "Mr. Hegde" should stay "Mr. <surrogate>".
    without_honorific = _HONORIFIC_RE.sub("", candidate)
    if not without_honorific.strip():
        return start
    return start - len(without_honorific)


_CONNECTIVES = {"of", "at", "for", "with", "from", "to", "in"}
_INITIAL_RE = re.compile(r"[A-Z]\.?")
_TRAILING_PIN_RE = re.compile(r"\s*[–—-]?\s*\d{3}\s?\d{3}(?!\d)")


def _outranks_place(name: str) -> bool:
    """True when a person/place clash should be resolved in favour of the person."""
    return len(name.split()) >= 3 and _is_person_name(name)


def _strip_place_affixes(name: str) -> str:
    previous = None
    while previous != name:
        previous = name
        name = _PLACE_LEAD_RE.sub("", name)
        name = _PLACE_TAIL_RE.sub("", name)
    return name.strip(" ,.-–—")


def _is_person_name(text: str) -> bool:
    text = _clean(text)
    tokens = text.split()
    if not 2 <= len(tokens) <= 4:
        return False
    if len(text) < 6 or len(text) > 60:
        return False
    lowered = text.casefold()
    if lowered in PERSON_STOPWORDS or lowered in INDIAN_STATES:
        return False
    if any(token.casefold().strip(".,") in PERSON_STOPWORDS for token in tokens):
        return False
    if any(suffix in lowered for suffix in ("limited", "ltd", "llp", "inc", "corporation", "& co")):
        return False
    for token in tokens:
        stripped = token.strip(".")
        if not stripped:
            return False
        if not (stripped[0].isupper() or stripped[0].isdigit()):
            return False
        if not re.fullmatch(r"[A-Za-z'’\-]+\.?", token):
            return False
    return True


def _is_org_name(text: str, policy: Policy) -> bool:
    text = _clean(text)
    if not 4 <= len(text) <= 90:
        return False
    lowered = text.casefold()
    if lowered in GENERIC_ORG_WORDS:
        return False
    if not policy.redact_institutions:
        if lowered in INSTITUTION_ALLOWLIST:
            return False
        if any(inst in lowered for inst in INSTITUTION_ALLOWLIST if len(inst) > 6):
            return False
    if re.search(r"(?i)\b(?:act|regulations?|rules|circular|guidelines|amendment)\b", lowered):
        return False
    if lowered in INDIAN_STATES:
        return False
    return bool(re.search(r"[A-Za-z]", text))


@dataclass
class Entity:
    kind: str
    canonical: str
    aliases: set[str] = field(default_factory=set)
    count: int = 0
    sources: set[str] = field(default_factory=set)

    @property
    def key(self) -> str:
        return normalise(self.canonical)


class Gazetteer:
    """Learns the document's people and companies, then matches them anywhere."""

    def __init__(self, policy: Policy) -> None:
        self.policy = policy
        self.people: dict[str, Entity] = {}
        self.orgs: dict[str, Entity] = {}
        self.places: set[str] = set()
        #: canonical place key -> display form, learned from the document's own
        #: addresses and from NER, then matched in narrative prose
        self.place_names: dict[str, str] = {}
        self._alias_index: dict[str, tuple[str, str]] = {}   # alias -> (kind, entity key)
        self._matcher: re.Pattern | None = None
        self._matcher_cs: re.Pattern | None = None
        self._place_matcher: re.Pattern | None = None
        self._ner_person_votes: Counter[str] = Counter()
        #: how often each word is seen in lower case in ordinary prose
        self._vocabulary: Counter[str] = Counter()
        self._short_forms: list[tuple[str, str]] = []  # (full org name, quoted short form)
        self.dropped: list[tuple[str, str, str]] = []  # (kind, name, reason)

    # -- document vocabulary ----------------------------------------------
    def observe_vocabulary(self, text: str) -> None:
        """Count lower-case words so common nouns can be told from names.

        "Committee", "Allotment" and "Facilities" appear in lower case
        elsewhere in the same document; "Hegde" and "Nuvama" never do.  This is
        a dictionary check that adapts to whatever document it is given.
        """
        stripped = re.sub(r"\S+@\S+|https?://\S+|www\.\S+", " ", text)
        self._vocabulary.update(re.findall(r"\b[a-z][a-z'\-]{2,}\b", stripped))

    def _is_common_word(self, token: str, threshold: int = 3) -> bool:
        return self._vocabulary[token.casefold().strip(".,")] >= threshold

    def _trim_common_head(self, name: str) -> str:
        """Drop leading prose glued to a company name by the suffix pattern.

        "Registered Office of our Company KSH International Limited"
        -> "KSH International Limited"
        """
        tokens = name.split()
        while len(tokens) > 2 and (tokens[0].islower() or normalise(tokens[0]) in LEADING_NOISE):
            tokens.pop(0)

        # "Rashi Patil of Sunrise Textiles Private Limited" is one person and one
        # company, and the legal-suffix pattern grabs both.  Cutting at a
        # connective is safe only when what follows still carries the suffix —
        # which is what separates this from "Bank of Baroda", where the tail
        # ("Baroda") does not.
        for index, token in enumerate(tokens[:-1]):
            if token.casefold() not in _CONNECTIVES:
                continue
            tail = tokens[index + 1:]
            if len(tail) >= 2 and has_legal_suffix(" ".join(tail)):
                tokens = tail
                break
        return " ".join(tokens)

    @staticmethod
    def _name_before_suffix(name: str) -> bool:
        """False for strings that are nothing but a legal suffix."""
        core = ORG_SUFFIX_STRIP_RE.sub("", name).strip(" ,.-–—")
        return len(core.split()) >= 1 and normalise(core) not in DEFINED_TERM_STOPLIST

    def _trim_common_tail(self, name: str) -> str:
        """Drop trailing common nouns glued on by a greedy pattern match.

        "Anand Soni Website" -> "Anand Soni"
        """
        tokens = name.split()
        while len(tokens) > 2 and (
            self._is_common_word(tokens[-1])
            or normalise(tokens[-1]) in INSTITUTION_ALLOWLIST
            or normalise(tokens[-1]) in DEFINED_TERM_STOPLIST
            or (tokens[-1].isupper() and len(tokens[-1]) <= 5)
        ):
            tokens.pop()
        return " ".join(tokens)

    # -- learning ----------------------------------------------------------
    def learn_block(self, text: str, ner_spans: Iterable[Span] = ()) -> None:
        # Locality names inside an address ("Deccan Gymkhana", "Bandra East")
        # look exactly like personal names to a NER model.  Anything sitting
        # inside a detected address is excluded from the name lexicon; the
        # address detector already covers it.
        from .addresses import detect_addresses

        address_ranges = [(s.start, s.end) for s in detect_addresses(text)]

        def inside_address(start: int, end: int) -> bool:
            return any(a_start <= start and end <= a_end for a_start, a_end in address_ranges)

        for regex in (TITLE_RE, BEING_RE, IS_OUR_RE, AGED_RE, SIGNED_RE, HELD_BY_RE):
            for match in regex.finditer(text):
                if not inside_address(match.start(1), match.end(1)):
                    self._add_person(match.group(1), source=regex.pattern[:16])

        for match in CONTACT_RE.finditer(text):
            for part in re.split(r"[/;,&]| and ", match.group(1)):
                name = NAME_RE.match(part.strip())
                if name:
                    self._add_person(name.group(0), source="contact-person")

        for match in ORG_LEGAL_RE.finditer(text):
            self._add_org(match.group(1), source="legal-suffix")
            # A short form defined right after the full name — ICICI Securities
            # Limited ("I-Sec") — is an alias for the same company.
            lookahead = text[match.end() : match.end() + 60]
            short = QUOTED_ALIAS_RE.search(lookahead)
            if short:
                self._short_forms.append((match.group(1), short.group(1)))

        # Harvest localities out of every address found in this block.  The
        # addresses are where the document itself tells us which places matter;
        # once learned, those names are redacted in narrative prose too, where no
        # address detector can reach them.
        for start, end in address_ranges:
            self._learn_places_in(text[start:end])

        for span in ner_spans:
            if span.label == "PLACE":
                self.places.add(normalise(span.text))
                self._add_place(span.text, source="ner")
                continue
            if inside_address(span.start, span.end):
                continue
            if span.label == PERSON:
                self._ner_person_votes[normalise(span.text)] += 1
                self._add_person(span.text, source="ner", tentative=True)
            elif span.label == ORG:
                # General-purpose English models routinely label Indian personal
                # names as organisations.  A candidate with no legal suffix that
                # parses as a name is treated as a (tentative) person instead.
                if not has_legal_suffix(span.text) and _is_person_name(span.text):
                    self._ner_person_votes[normalise(span.text)] += 1
                    self._add_person(span.text, source="ner", tentative=True)
                else:
                    self._add_org(span.text, source="ner", tentative=True)

    # -- places ------------------------------------------------------------
    def _learn_places_in(self, address: str) -> None:
        """Pull locality names out of one detected address.

        An address is a comma-separated list in which most components are places
        ("Village Birdewadi", "Chakan Taluka", "Khed", "Pune"). Numbers, street
        furniture and legal-entity words are dropped; whatever capitalised text
        survives is treated as a place name.  The word immediately before a PIN
        code is always taken, because that is the city by construction.
        """
        pin = re.search(r"\b\d{3}\s?\d{3}\b", address)
        if pin:
            before = address[: pin.start()].rstrip(" ,–—-")
            tail = re.findall(rf"(?:{NAME_TOKEN})(?:\s+(?:{NAME_TOKEN}))?$", before)
            for candidate in tail:
                self._add_place(candidate, source="address:pre-pin")

        for chunk in re.split(r"[,\n|]|\s+-\s+|–", address):
            words = [w for w in re.split(r"\s+", chunk.strip()) if w]
            keep: list[str] = []
            for word in words:
                bare = word.strip(".,()").casefold()
                if not bare or any(ch.isdigit() for ch in word):
                    keep = []
                    continue
                if bare in PLACE_NOISE_WORDS or bare in ADDRESS_HINT_WORDS:
                    if keep:
                        self._add_place(" ".join(keep), source="address:component")
                    keep = []
                    continue
                if word[:1].isupper():
                    keep.append(word.strip(".,()"))
                else:
                    if keep:
                        self._add_place(" ".join(keep), source="address:component")
                    keep = []
            if keep:
                self._add_place(" ".join(keep), source="address:component")

    def _add_place(self, raw: str, source: str) -> None:
        cleaned = _clean(raw)
        name = _strip_place_affixes(cleaned)
        if not name or len(name) < 4 or len(name.split()) > 3:
            return
        # Stripping a trailing noun can leave an ordinary word behind: "the Refund
        # Bank" becomes "Refund".  When trimming produced a single word, that word
        # must never appear in lower case anywhere in the document — a real
        # locality does not ("Supa", "Chakan"), a defined term does.
        if name != cleaned and len(name.split()) == 1 and self._is_common_word(name, threshold=1):
            return
        key = normalise(name)
        if not key or key in PLACE_ALLOWLIST or key in COUNTRY_WORDS:
            return
        if key in DEFINED_TERM_STOPLIST or key in INSTITUTION_ALLOWLIST:
            return
        if key in PERSON_STOPWORDS or key in self.people or key in self.orgs:
            return
        # Any token that is itself a defined term makes the whole thing a defined
        # term, not a place: "the Cap Price", "the Refund Bank".
        tokens = name.split()
        if any(normalise(t) in DEFINED_TERM_STOPLIST for t in tokens):
            return
        # Acronyms and abbreviations — "N.A", "U.S", "USD", "MoA", "CTS".
        if len(name) <= 4 and (name.isupper() or "." in name):
            return
        # A locality does not contain an acronym: "Designated RTA", "FIG-OPS
        # Department" are defined terms that happened to sit near an address.
        if any(t.isupper() and 2 <= len(t.strip(".")) <= 5 for t in tokens):
            return
        if re.fullmatch(r"(?:[A-Za-z]\.){1,3}[A-Za-z]?", name):
            return
        # A "place" containing any word the document also uses in ordinary
        # lower-case prose is a phrase, not a locality: "Designated RTA" is a
        # defined term.  Stricter than the person/company test on purpose —
        # real place names almost never reuse the document's own vocabulary.
        if any(self._is_common_word(token) for token in tokens):
            return
        if not re.fullmatch(r"[A-Za-z][A-Za-z'’\-. ]+", name):
            return
        # A three-part capitalised name harvested from an address is a person
        # who happens to live there, not a locality.  Route it to the right
        # lexicon so it is redacted under the right label.
        if len(tokens) >= 3 and _is_person_name(name):
            self._add_person(name, source=f"{source}:person")
            return
        self.place_names.setdefault(key, name)

    def _compile_places(self) -> None:
        names = [n for n in self.place_names.values() if len(n) >= 3]
        if not names:
            return
        names.sort(key=len, reverse=True)
        self._place_matcher = re.compile(
            r"(?<![\w@.])(" + "|".join(r"\s*".join(re.escape(p) for p in n.split()) for n in names) + r")(?![\w])",
            re.IGNORECASE,
        )

    def place_spans(self, text: str) -> Iterator[Span]:
        if self._place_matcher is None or not self.policy.wants(LOCATION):
            return
        for match in self._place_matcher.finditer(text):
            found = match.group(1)
            if found.islower():
                continue  # ordinary prose, not a proper noun
            # Swallow a postal code hanging off the place ("Pune – 411 045").
            # On its own a six-digit number is unanchored and unsafe to redact;
            # sitting immediately after a known locality, it is the PIN and it
            # pins the address down as precisely as the street would.
            end = match.end(1)
            tail = _TRAILING_PIN_RE.match(text, end)
            if tail:
                end = tail.end()
            yield Span(match.start(1), end, LOCATION, text[match.start(1) : end], "gazetteer:place", 0.9)

    def learn_table_row(self, header: str, cells: list[str]) -> None:
        """Use table headers as labels: a cell under "Name" is a name."""
        columns = [normalise(c) for c in header.split("|")]
        for index, cell in enumerate(cells):
            column = columns[index] if index < len(columns) else ""
            if not column:
                continue
            if re.search(r"\bname\b", column) and "company" not in column:
                self._add_person(cell, source="table:name-column")
            if "director" in column or "promoter" in column or "shareholder" in column:
                self._add_person(cell, source="table:role-column")

    def add_manual(self, terms: dict[str, str]) -> None:
        for term, kind in terms.items():
            if kind.upper() == PERSON:
                self._add_person(term, source="manual", force=True)
            else:
                self._add_org(term, source="manual", force=True)

    def _add_person(self, raw: str, source: str, tentative: bool = False, force: bool = False) -> None:
        name = _clean(raw)
        # strip honorifics and trailing role words
        name = re.sub(r"(?i)^(?:mr|ms|mrs|dr|shri|smt|prof|justice)\.?\s+", "", name)
        name = re.sub(r"(?i)\s+(?:is|was|and|the)$", "", name)
        if not force and not _is_person_name(name):
            return
        key = normalise(name)
        entity = self.people.get(key)
        if entity is None:
            entity = Entity(PERSON, name)
            self.people[key] = entity
        entity.count += 1
        entity.sources.add(source)
        if tentative:
            entity.sources.add("ner")

    def _add_org(self, raw: str, source: str, tentative: bool = False, force: bool = False) -> None:
        name = _clean(raw)
        # Drop leading connective words picked up by the pattern ("as Foo Ltd").
        while name and name.split()[0].islower():
            name = name.split(" ", 1)[1] if " " in name else ""
        if not force and not _is_org_name(name, self.policy):
            return
        key = normalise(name)
        entity = self.orgs.get(key)
        if entity is None:
            entity = Entity(ORG, name)
            self.orgs[key] = entity
        entity.count += 1
        entity.sources.add(source)

    # -- consolidation -----------------------------------------------------
    def _reject(self, kind: str, name: str, reason: str) -> None:
        self.dropped.append((kind, name, reason))

    def _looks_like_defined_term(self, name: str) -> bool:
        """True for the document's own jargon rather than a real-world entity."""
        lowered = normalise(name)
        if lowered in DEFINED_TERM_STOPLIST:
            return True
        tokens = name.split()
        # The head word of a real name is not a word the document uses in prose.
        if tokens and self._is_common_word(tokens[0]):
            return True
        return False

    def finalise(self, min_ner_votes: int = 1) -> None:
        """Drop weak candidates, then build alias tables and the matcher."""
        # -- people ---------------------------------------------------------
        for key, entity in list(self.people.items()):
            trimmed = self._trim_common_tail(entity.canonical)
            if trimmed != entity.canonical:
                if not _is_person_name(trimmed):
                    self._reject(PERSON, entity.canonical, "trimmed to non-name")
                    del self.people[key]
                    continue
                # re-key: the dictionary is indexed by the canonical form
                del self.people[key]
                entity.canonical = trimmed
                key = entity.key
                existing = self.people.get(key)
                if existing is not None:
                    existing.count += entity.count
                    existing.sources |= entity.sources
                    continue
                self.people[key] = entity

            reason = None
            if entity.sources <= {"ner"} and self._ner_person_votes[key] < min_ner_votes:
                reason = "single unconfirmed NER hit"
            elif any(self._is_common_word(token) for token in entity.canonical.split()):
                reason = "contains a word the document uses in prose"
            elif normalise(entity.canonical) in self.places and not _outranks_place(entity.canonical):
                # NER routinely tags Indian personal names as GPE/LOC.  A full
                # three-part name is a person even when the model disagrees;
                # a two-token hit ("Bandra East") really is a locality.
                reason = "place name"
            elif normalise(entity.canonical) in DEFINED_TERM_STOPLIST or any(
                normalise(token) in DEFINED_TERM_STOPLIST for token in entity.canonical.split()
            ):
                reason = "defined term"
            elif any(
                token.isupper() and len(token) <= 4 and not _INITIAL_RE.fullmatch(token)
                for token in entity.canonical.split()
            ):
                # "HUF" or "RTA" is an abbreviation; "K." in "Rupal K. Sancheti"
                # is a middle initial and perfectly ordinary in a name.
                reason = "contains an abbreviation, not a given name"
            if reason:
                self._reject(PERSON, entity.canonical, reason)
                del self.people[key]

        # -- companies ------------------------------------------------------
        for key, entity in list(self.orgs.items()):
            trimmed = self._trim_common_head(entity.canonical)
            if trimmed != entity.canonical:
                del self.orgs[key]
                entity.canonical = trimmed
                key = entity.key
                merged = self.orgs.get(key)
                if merged is not None:
                    merged.count += entity.count
                    merged.sources |= entity.sources
                    continue
                self.orgs[key] = entity

            reason = None
            if not self._name_before_suffix(entity.canonical):
                reason = "bare legal suffix, no name"
            elif self._looks_like_defined_term(entity.canonical) and not has_legal_suffix(entity.canonical):
                reason = "defined term / common-word head"
            elif normalise(entity.canonical) in self.places and not has_legal_suffix(entity.canonical):
                reason = "place name"
            elif entity.sources <= {"ner"} and not has_legal_suffix(entity.canonical):
                # An unsuffixed company that NER alone proposed is kept only if
                # the document repeats it and it reads like a proper name.
                tokens = entity.canonical.split()
                if entity.count < 3 or any(self._is_common_word(t) for t in tokens):
                    reason = "unconfirmed NER company"
                elif len(tokens) <= 2 and any(t.isupper() and len(t) <= 4 for t in tokens):
                    reason = "acronym pair, not a company name"
            if reason:
                self._reject(ORG, entity.canonical, reason)
                del self.orgs[key]

        self._attach_short_forms()
        self._build_aliases()
        self._compile()
        self._compile_places()

    def _attach_short_forms(self) -> None:
        """Register quoted short forms — ICICI Securities Limited ("I-Sec")."""
        for full, short in self._short_forms:
            entity = self.orgs.get(normalise(full))
            if entity is None:
                continue
            candidate = _clean(short)
            if len(candidate) < 3 or normalise(candidate) in DEFINED_TERM_STOPLIST:
                continue
            if any(self._is_common_word(token) for token in candidate.split()):
                continue
            entity.aliases.add(candidate)

    def _build_aliases(self) -> None:
        surname_owners: dict[str, set[str]] = defaultdict(set)
        for entity in self.people.values():
            tokens = entity.canonical.split()
            entity.aliases.add(entity.canonical)
            if len(tokens) >= 2:
                entity.aliases.add(f"{tokens[0]} {tokens[-1]}")
                surname = tokens[-1]
                if len(surname) >= 4 and surname.casefold() not in PERSON_STOPWORDS:
                    surname_owners[surname.casefold()].add(entity.key)
                # initial + surname, e.g. "K. Hegde"
                entity.aliases.add(f"{tokens[0][0]}. {tokens[-1]}")
                # a distinctive given name is also used on its own
                given = tokens[0]
                if len(given) >= 4 and given.casefold() not in PERSON_STOPWORDS:
                    entity.aliases.add(given)

        # Surnames are added as aliases even when several people share them:
        # the surrogate map translates a surname to a single fake surname, so
        # "Mr. Hegde" stays consistent for the whole family.
        for surname, owners in surname_owners.items():
            for owner in owners:
                self.people[owner].aliases.add(surname.title())

        for entity in self.orgs.values():
            entity.aliases.add(entity.canonical)
            core = ORG_SUFFIX_STRIP_RE.sub("", entity.canonical).strip()
            if len(core) >= 5 and core.casefold() not in GENERIC_ORG_WORDS:
                entity.aliases.add(core)
            first = core.split()[0] if core.split() else ""
            if first.isupper() and len(first) >= 3:  # e.g. "KSH"
                entity.aliases.add(first)
            elif len(first) >= 5 and not self._is_common_word(first, threshold=1):
                # a distinctive head word is used on its own: "Nuvama", "Hindalco"
                entity.aliases.add(first)

        for kind_map in (self.people, self.orgs):
            for entity in kind_map.values():
                for alias in entity.aliases:
                    cleaned = _clean(alias)
                    if not self._alias_is_safe(cleaned, entity):
                        continue
                    existing = self._alias_index.get(cleaned.casefold())
                    if existing and existing[1] != entity.key:
                        # ambiguous alias: keep the first owner, still redacted
                        continue
                    self._alias_index[cleaned.casefold()] = (entity.kind, entity.key)

    def _alias_is_safe(self, alias: str, entity: Entity) -> bool:
        """Reject aliases that would fire on ordinary words.

        This is the main precision guard: a surname like "Trust" or a company
        short form like "Securities" would otherwise match hundreds of times in
        running text.
        """
        if len(alias) < 3:
            return False
        lowered = normalise(alias)
        if lowered in DEFINED_TERM_STOPLIST or lowered in GENERIC_ORG_WORDS:
            return False
        if lowered in INSTITUTION_ALLOWLIST and not self.policy.redact_institutions:
            return False
        if entity.kind == PERSON and alias.isupper() and len(alias.split()) == 1:
            return False  # "SEBI" is an acronym that ended up as somebody's surname
        if lowered in self.places and not has_legal_suffix(alias):
            return False
        if len(alias.split()) == 1:
            if alias.isupper() and 3 <= len(alias) <= 5:
                return True  # "KSH": an acronym, matched case-sensitively
            # A single-word alias (a surname, or a company short form) has to be
            # a token the document never uses as an ordinary word.
            return len(alias) >= 4 and not self._is_common_word(alias, threshold=1)
        return True

    def _compile(self) -> None:
        long_aliases, acronyms = [], []
        for alias in self._alias_index:
            original = alias
            if len(original) <= 4 and original.upper() == original.upper():
                acronyms.append(original)
            long_aliases.append(original)
        long_aliases.sort(key=len, reverse=True)

        def to_pattern(alias: str) -> str:
            # \s* rather than \s+: documents converted from PDF routinely lose a
            # space inside a name ("KSHInfra Park 4 Private Limited").
            return r"\s*".join(re.escape(part) for part in alias.split())

        if long_aliases:
            self._matcher = re.compile(
                r"(?<![\w@.])(" + "|".join(to_pattern(a) for a in long_aliases) + r")(?![\w])",
                re.IGNORECASE,
            )
        acronyms = [a for a in acronyms if len(a) >= 3]
        if acronyms:
            self._matcher_cs = re.compile(
                r"(?<![\w@.])(" + "|".join(re.escape(a.upper()) for a in acronyms) + r")(?![\w])"
            )

    # -- matching ----------------------------------------------------------
    def spans(self, text: str) -> Iterator[Span]:
        for matcher, source in ((self._matcher, "gazetteer"), (self._matcher_cs, "gazetteer:acronym")):
            if matcher is None:
                continue
            for match in matcher.finditer(text):
                found = _clean(match.group(1))
                entry = self._alias_index.get(found.casefold())
                if entry is None:
                    continue
                # Matching is case-insensitive so that ALL-CAPS cover pages are
                # caught, but an all-lower-case hit is ordinary prose, not a name.
                if found.islower():
                    continue
                kind, _ = entry
                if not self.policy.wants(kind):
                    continue
                start, end = match.start(1), match.end(1)
                if kind == PERSON:
                    start = _extend_over_given_names(text, start)
                yield Span(start, end, kind, text[start:end], source, 0.95)

    def knows(self, text: str) -> bool:
        """True if this string is already a confirmed entity or alias."""
        key = normalise(text)
        return key in self.people or key in self.orgs or key in self._alias_index

    def accepts_ner_candidate(self, text: str, kind: str) -> bool:
        """Gate for a raw NER hit that pass 1 did not promote to an entity.

        Everything the gazetteer confirmed is already matched by ``spans``.
        This lets a *one-off* name through — a person mentioned exactly once,
        which never reaches the confirmation threshold — while still applying
        the same document-driven filters that rejected the false positives.
        """
        if self.knows(text):
            return True
        cleaned = _clean(text)
        lowered = normalise(cleaned)
        if lowered in DEFINED_TERM_STOPLIST or lowered in self.places:
            return False
        if lowered in INSTITUTION_ALLOWLIST and not self.policy.redact_institutions:
            return False
        tokens = cleaned.split()
        if any(normalise(token) in INSTITUTION_ALLOWLIST for token in tokens):
            return False
        if any(normalise(token) in DEFINED_TERM_STOPLIST for token in tokens):
            return False
        if any(self._is_common_word(token) for token in tokens):
            return False
        if kind == PERSON:
            # short all-caps fragments ("BO", "RTA") are abbreviations, not names
            if any(token.isupper() and len(token) <= 4 for token in tokens):
                return False
            return _is_person_name(cleaned)
        return has_legal_suffix(cleaned) and self._name_before_suffix(cleaned)

    # -- reporting ---------------------------------------------------------
    def summary(self) -> dict[str, int]:
        return {"people": len(self.people), "companies": len(self.orgs), "aliases": len(self._alias_index)}
