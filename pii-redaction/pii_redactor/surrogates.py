"""Surrogate generation — the "fake alternative" every detection is replaced with.

Two properties matter more than realism:

* **Consistency** — the same real value always becomes the same fake value, and
  related values stay related.  "Kushal Subbayya Hegde" becomes "John Q. Doe"
  on page 1 and on page 300; "Mr. Hegde" becomes "Mr. Doe"; his mailbox
  ``kushal.hegde@kshinternational.com`` becomes ``john.doe@<fake-company>.example.com``.
* **Determinism** — surrogates are derived from a hash of (seed, label, value),
  so re-running the tool on the same document produces byte-identical output and
  the mapping file can be regenerated at any time.

All generated domains sit under ``example.com`` (RFC 2606) so no surrogate can
ever collide with a live domain.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from faker import Faker

from .config import INDIAN_STATES, Policy
from .types import (
    ADDRESS,
    BANK_ACCOUNT,
    CREDIT_CARD,
    DOB,
    EMAIL,
    IP_ADDRESS,
    LOCATION,
    NATIONAL_ID,
    ORG,
    PERSON,
    PHONE,
    SSN,
    URL,
    Span,
    normalise,
)

# Role mailboxes: these local parts identify a function, not a person, and are
# kept so the redacted document still reads correctly.
ROLE_MAILBOXES = {
    "info", "support", "contact", "sales", "admin", "office", "help", "helpdesk",
    "care", "customercare", "customerservice", "service", "grievance", "grievances",
    "investor", "investors", "ipo", "cs", "compliance", "secretary", "hr", "careers",
    "legal", "finance", "accounts", "billing", "noreply", "no-reply", "mail", "enquiry",
    "enquiries", "team", "corp", "pro", "connect", "in",
}

FREE_MAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "yahoo.co.in", "hotmail.com", "outlook.com",
    "rediffmail.com", "aol.com", "icloud.com", "protonmail.com",
}

ORG_SUFFIX_RE = re.compile(
    r"(?i)\s+(private\s+limited|limited|ltd\.?|llp|inc\.?|incorporated|corporation|corp\.?|"
    r"plc|gmbh|company|chartered\s+accountants)$"
)

FAKE_ORG_HEADS = (
    "Arcadia", "Bluepeak", "Corvid", "Dunmore", "Everline", "Fairhaven", "Granite",
    "Harborview", "Ironwood", "Juniper", "Kestrel", "Lakeshore", "Meridian",
    "Northgate", "Oakfield", "Pinecrest", "Quarry", "Redstone", "Silverbrook",
    "Thornhill", "Umbra", "Vantage", "Westmark", "Yarrow", "Zenith",
)
FAKE_ORG_TAILS = (
    "Industries", "Systems", "Holdings", "Enterprises", "Technologies", "Partners",
    "Ventures", "Solutions", "Manufacturing", "Trading", "Capital", "Works",
)

# Invented regions, not real states.  A surrogate address ending in a real state
# name puts back the very thing the LOCATION pass just removed — and leaves the
# reader unable to tell a fake address from a real one.
INDIAN_STATE_POOL = (
    "Nordhaven State", "Westmarch", "Eastfield State", "Sundermere",
    "Highvale State", "Copperlake",
)

# Invented street / locality words.  Deliberately not drawn from a real-name
# provider: a "fake" address built out of real surnames can accidentally
# reintroduce a name that was just redacted.
FAKE_STREET_WORDS = (
    "Alder", "Birchwood", "Copperfield", "Dovecote", "Elmgrove", "Fernbank",
    "Goldcrest", "Hawthorn", "Ivybridge", "Larkspur", "Mapleton", "Nightingale",
    "Orchard", "Primrose", "Rosewood", "Sandalwood", "Tamarind", "Willowmere",
)
FAKE_LOCALITIES = (
    "Ashborne", "Brightvale", "Cedarhill", "Dunbarton", "Eastmere", "Fairwind",
    "Greenmoor", "Hillcrest", "Kingsford", "Lakewood", "Millfield", "Northaven",
)

# Second element for compound place names, so a document naming more localities
# than the pool holds still gets plausible names instead of "Eastmere 2".
# Purely topographic words: anything that also names an institution ("Bank",
# "House", "Park") produces sentences like "commercial banks in Eastmere Bank".
FAKE_PLACE_TAILS = (
    "Cross", "Halt", "Junction", "Meadows", "Reach", "Ridge", "Vale", "Wells",
    "Bridge", "Heath", "Hollow", "Moor", "Combe", "Dale", "Fell", "Knoll",
)


# The document writes suffixes in whatever case it likes ("LIMITED" on the cover
# page); surrogates always use the conventional spelling.
_SUFFIX_SPELLING = {
    "limited": "Limited", "private limited": "Private Limited", "ltd": "Ltd",
    "ltd.": "Ltd.", "llp": "LLP", "inc": "Inc", "inc.": "Inc.",
    "incorporated": "Incorporated", "corporation": "Corporation", "corp": "Corp",
    "corp.": "Corp.", "plc": "PLC", "gmbh": "GmbH", "company": "Company",
    "chartered accountants": "Chartered Accountants",
}


def _canonical_suffix(suffix: str) -> str:
    compact = " ".join(suffix.split())
    return _SUFFIX_SPELLING.get(compact.casefold(), compact.title())


def _seed_for(*parts: str, salt: int = 0) -> int:
    raw = "␟".join(parts).encode("utf-8")
    return int(hashlib.sha256(raw).hexdigest()[:12], 16) + salt


@dataclass
class SurrogateFactory:
    policy: Policy
    _faker: Faker = field(init=False)
    #: canonical original value -> fake value, per label (the audit mapping)
    mapping: dict[str, dict[str, str]] = field(init=False, default_factory=dict)
    _first_names: dict[str, str] = field(init=False, default_factory=dict)
    _surnames: dict[str, str] = field(init=False, default_factory=dict)
    _org_alias: dict[str, str] = field(init=False, default_factory=dict)
    _org_domain: dict[str, str] = field(init=False, default_factory=dict)
    _known_surnames: set[str] = field(init=False, default_factory=set)
    _known_first_names: set[str] = field(init=False, default_factory=set)
    _tag_counters: dict[str, int] = field(init=False, default_factory=dict)
    _tags: dict[str, str] = field(init=False, default_factory=dict)
    _locations: dict[str, str] = field(init=False, default_factory=dict)
    _used_org_cores: set = field(init=False, default_factory=set)
    #: set from the document's own addresses; decides the format of surrogates
    #: for address fragments that carry no country cue of their own
    _document_is_indian: bool = field(init=False, default=False)

    def __post_init__(self) -> None:
        self._faker = Faker(self.policy.faker_locale)
        self._faker_in = Faker("en_IN")

    def set_document_locale(self, places) -> None:
        """Pick one address format for the document, from the places it names."""
        if self.policy.faker_locale.endswith("_IN"):
            self._document_is_indian = True
            return
        for place in places:
            if normalise(place) in INDIAN_STATES:
                self._document_is_indian = True
                return

    # -- binding to the document's entity inventory -------------------------
    def bind(self, gazetteer) -> None:
        """Pre-compute surrogates for every known person and company."""
        for entity in gazetteer.people.values():
            tokens = entity.canonical.split()
            for index, token in enumerate(tokens):
                bare = token.strip(".").casefold()
                if not bare:
                    continue
                if index == len(tokens) - 1 and len(tokens) > 1:
                    self._known_surnames.add(bare)
                else:
                    self._known_first_names.add(bare)

        for entity in gazetteer.orgs.values():
            fake_full, fake_core = self._make_org(entity.canonical)
            for alias in entity.aliases:
                cleaned = " ".join(alias.split())
                if not cleaned:
                    continue
                key = cleaned.casefold()
                if key in self._org_alias:
                    continue
                if ORG_SUFFIX_RE.search(cleaned):
                    self._org_alias[key] = fake_full
                elif cleaned.isupper() and len(cleaned) <= 5:
                    self._org_alias[key] = "".join(w[0] for w in fake_core.split())[:len(cleaned)].upper()
                else:
                    self._org_alias[key] = fake_core
            # Index the company under its full slug *and* its first word so that
            # both nuvamawealthmanagement.com and nuvama.com map to one surrogate.
            core = ORG_SUFFIX_RE.sub("", entity.canonical)
            fake_slug = re.sub(r"[^a-z0-9]", "", fake_core.casefold())
            candidates = [core] + ([core.split()[0]] if core.split() else [])
            for candidate in candidates:
                slug = re.sub(r"[^a-z0-9]", "", candidate.casefold())
                if len(slug) >= 4:
                    self._org_domain.setdefault(slug, fake_slug)

    # -- public API ---------------------------------------------------------
    def replacement(self, span: Span) -> str:
        label, text = span.label, span.text
        if self.policy.mode == "tag":
            return self._tag(label, text)

        generator = {
            PERSON: self._person,
            ORG: self._org,
            EMAIL: self._email,
            PHONE: self._phone,
            ADDRESS: self._address,
            SSN: self._ssn,
            CREDIT_CARD: self._credit_card,
            DOB: self._dob,
            IP_ADDRESS: self._ip,
            URL: self._url,
            NATIONAL_ID: self._shape_preserving,
            BANK_ACCOUNT: self._shape_preserving,
            LOCATION: self._location,
        }.get(label, self._shape_preserving)

        value = generator(text)
        self.mapping.setdefault(label, {}).setdefault(" ".join(text.split()), value)
        return value

    def as_mapping(self) -> dict[str, dict[str, str]]:
        return self.mapping

    # -- generators ---------------------------------------------------------
    def _rand(self, *parts: str) -> Faker:
        self._faker.seed_instance(_seed_for(*parts, salt=self.policy.seed))
        return self._faker

    def _rand_in(self, *parts: str) -> Faker:
        self._faker_in.seed_instance(_seed_for(*parts, salt=self.policy.seed))
        return self._faker_in

    def _tag(self, label: str, text: str) -> str:
        key = f"{label}:{normalise(text)}"
        if key not in self._tags:
            self._tag_counters[label] = self._tag_counters.get(label, 0) + 1
            self._tags[key] = f"[{label}_{self._tag_counters[label]}]"
        self.mapping.setdefault(label, {}).setdefault(" ".join(text.split()), self._tags[key])
        return self._tags[key]

    # people ---------------------------------------------------------------
    def _fake_first(self, token: str) -> str:
        key = token.casefold()
        if key not in self._first_names:
            self._first_names[key] = self._rand("first", key).first_name()
        return self._first_names[key]

    def _fake_surname(self, token: str) -> str:
        key = token.casefold()
        if key not in self._surnames:
            self._surnames[key] = self._rand("last", key).last_name()
        return self._surnames[key]

    def _person(self, text: str) -> str:
        tokens = text.split()
        out: list[str] = []
        for index, token in enumerate(tokens):
            bare = token.strip(".,").casefold()
            is_initial = len(bare) == 1
            last_position = index == len(tokens) - 1 and len(tokens) > 1
            if bare in self._known_surnames and not is_initial:
                fake = self._fake_surname(bare)
            elif bare in self._known_first_names and not is_initial:
                fake = self._fake_first(bare)
            elif is_initial:
                fake = self._fake_first(bare)[0] + "."
            elif last_position:
                fake = self._fake_surname(bare)
            else:
                fake = self._fake_first(bare)
            if token.isupper():
                fake = fake.upper()
            out.append(fake)
        return " ".join(out)

    # companies ------------------------------------------------------------
    def _make_org(self, canonical: str) -> tuple[str, str]:
        """Invent a company name, guaranteed not to collide with another.

        Two real companies sharing one surrogate is worse than an ugly name: it
        tells the reader the issuer's auditor and its supplier are the same firm.
        On a clash the pool is re-rolled with a salt before falling back to a
        numeric suffix.
        """
        key = normalise(canonical)
        core = ""
        for attempt in range(24):
            faker = self._rand("org", key, str(attempt) if attempt else "")
            head = faker.random_element(FAKE_ORG_HEADS)
            tail = faker.random_element(FAKE_ORG_TAILS)
            core = f"{head} {tail}"
            if core not in self._used_org_cores:
                break
        core = self._unique(core, self._used_org_cores)
        self._used_org_cores.add(core)
        suffix_match = ORG_SUFFIX_RE.search(canonical)
        suffix = _canonical_suffix(suffix_match.group(1)) if suffix_match else "Limited"
        return f"{core} {suffix}", core

    def _org(self, text: str) -> str:
        key = " ".join(text.split()).casefold()
        if key in self._org_alias:
            fake = self._org_alias[key]
        else:
            fake_full, fake_core = self._make_org(text)
            fake = fake_full if ORG_SUFFIX_RE.search(text) else fake_core
            self._org_alias[key] = fake
        return fake.upper() if text.isupper() else fake

    # contact details ------------------------------------------------------
    def _email(self, text: str) -> str:
        local, _, domain = text.partition("@")
        domain = domain.casefold()
        root = domain.rsplit(".", 1)[0].split(".")[0]
        if domain in FREE_MAIL_DOMAINS:
            fake_domain = "example.com"
        else:
            slug = self._org_domain.get(re.sub(r"[^a-z0-9]", "", root))
            if slug is None:
                slug = re.sub(r"[^a-z0-9]", "", self._rand("domain", root).company().casefold())[:14]
                self._org_domain[re.sub(r"[^a-z0-9]", "", root)] = slug
            fake_domain = f"{slug}.example.com"

        parts = re.split(r"([._\-])", local)
        rebuilt = []
        for part in parts:
            bare = part.casefold()
            if part in "._-":
                rebuilt.append(part)
            elif bare in self._known_surnames:
                rebuilt.append(self._fake_surname(bare).casefold())
            elif bare in self._known_first_names:
                rebuilt.append(self._fake_first(bare).casefold())
            elif bare in self._org_domain:  # e.g. ksh.ipo@… -> <fakecompany>.ipo@…
                rebuilt.append(self._org_domain[bare])
            elif bare in ROLE_MAILBOXES or not bare.isalpha() or len(bare) <= 2:
                rebuilt.append(re.sub(r"\d+", lambda m: str(self._rand("num", m.group(0)).random_int(1, 99)), bare))
            else:
                # an unrecognised word in a mailbox is usually somebody's name
                rebuilt.append(self._fake_first(bare).casefold())
        return f"{''.join(rebuilt)}@{fake_domain}"

    def _phone(self, text: str) -> str:
        faker = self._rand("phone", re.sub(r"\D", "", text))
        digits_seen = 0
        out = []
        keep_prefix = text.strip().startswith("+")
        for index, char in enumerate(text):
            if char.isdigit():
                digits_seen += 1
                # keep the country code (first two digits after '+') recognisable
                if keep_prefix and digits_seen <= 2:
                    out.append(char)
                else:
                    out.append(str(faker.random_int(0, 9)))
            else:
                out.append(char)
        return "".join(out)

    # locations ------------------------------------------------------------
    def _address(self, text: str) -> str:
        compact = " ".join(text.split())
        indian = bool(
            re.search(
                r"(?i)india|maharashtra|karnataka|gujarat|pune|mumbai|delhi|bengaluru|"
                r"\bgat\b|village|taluka|tehsil|\bmarg\b|nagar|\bs\.?\s*no\b|\b\d{3}\s?\d{3}\b",
                compact,
            )
        )
        # Many address spans are fragments — "5th Floor, Gopal House" — that carry
        # no country cue at all.  Judging each one alone put US-format surrogates
        # ("Ashborne, WV 68460") in an Indian document, which is instantly visible
        # as a substitution.  The document as a whole decides the format instead.
        if not indian and self._document_is_indian:
            indian = True
        faker = self._rand_in("address", normalise(compact)) if indian else self._rand("address", normalise(compact))
        if indian:
            locality_used = {faker.random_element(FAKE_LOCALITIES)}
            pin = f"{faker.random_int(110, 699)} {faker.random_int(100, 999):03d}"
            surrogate = (
                f"{faker.random_int(1, 400)}, {faker.random_element(FAKE_STREET_WORDS)} "
                f"{faker.random_element(('Road', 'Marg', 'Lane'))}, "
                f"{faker.random_element(FAKE_LOCALITIES)}, "
                f"{faker.random_element([x for x in FAKE_LOCALITIES if x not in locality_used])}"
                f" – {pin}"
            )
            # Only carry a state and country if the original span had them.  The
            # cover page splits an address over two lines — street on one,
            # "Maharashtra, India" on the next — and appending a state to the
            # first half produced "…, Highvale State, India Hillcrest, India".
            if re.search(r"(?i)\b(?:" + "|".join(INDIAN_STATES) + r")\b", compact):
                surrogate += f", {faker.random_element(INDIAN_STATE_POOL)}"
            if re.search(r"(?i)\bindia\b", compact):
                surrogate += ", India"
            return surrogate
        return (
            f"{faker.random_int(10, 9800)} {faker.random_element(FAKE_STREET_WORDS)} "
            f"{faker.random_element(('Street', 'Avenue', 'Drive'))}, "
            f"{faker.random_element(FAKE_LOCALITIES)}, {faker.state_abbr()} {faker.random_int(10000, 99999)}"
        )

    # identifiers ----------------------------------------------------------
    def _ssn(self, text: str) -> str:
        return self._rand("ssn", text).ssn()

    def _credit_card(self, text: str) -> str:
        faker = self._rand("cc", re.sub(r"\D", "", text))
        digits = re.sub(r"\D", "", text)
        body = "4" + "".join(str(faker.random_int(0, 9)) for _ in range(len(digits) - 2))
        check = self._luhn_check_digit(body)
        fake_digits = body + str(check)
        out, index = [], 0
        for char in text:
            if char.isdigit():
                out.append(fake_digits[index])
                index += 1
            else:
                out.append(char)
        return "".join(out)

    @staticmethod
    def _luhn_check_digit(number: str) -> int:
        total, parity = 0, len(number) % 2
        for index, char in enumerate(number):
            digit = int(char)
            if index % 2 == parity:
                digit *= 2
                if digit > 9:
                    digit -= 9
            total += digit
        return (10 - total % 10) % 10

    def _ip(self, text: str) -> str:
        faker = self._rand("ip", text)
        if ":" in text:
            return faker.ipv6()
        return faker.ipv4_private()

    def _dob(self, text: str) -> str:
        faker = self._rand("dob", normalise(text))
        birth = faker.date_of_birth(minimum_age=25, maximum_age=75)
        if re.match(r"^\d{1,2}[/-]", text):
            separator = "/" if "/" in text else "-"
            return birth.strftime(f"%d{separator}%m{separator}%Y")
        if re.match(r"^\d", text):
            return birth.strftime("%d %B %Y")
        return birth.strftime("%B %-d, %Y")

    def _url(self, text: str) -> str:
        match = re.match(r"(?i)^(https?://)?(www\.)?([^/]+)(/.*)?$", text)
        scheme, www, host, path = (match.group(1) or "", match.group(2) or "", match.group(3), match.group(4) or "")
        root = re.sub(r"[^a-z0-9]", "", host.casefold().rsplit(".", 1)[0].split(".")[0])
        slug = self._org_domain.get(root)
        if slug is None:
            slug = re.sub(r"[^a-z0-9]", "", self._rand("domain", root).company().casefold())[:14]
            self._org_domain[root] = slug
        return f"{scheme}{www}{slug}.example.com{self._scrub_path(path)}"

    def _scrub_path(self, path: str) -> str:
        """Scrub identifiers out of the path and query of a URL.

        Replacing the host alone is not enough: ``/account/rashi-patil`` still
        names the customer, and so does ``?user=hegde``.  Every alphabetic word
        in the path is checked against the people and companies the document
        taught us, and replaced with the *same* surrogate the prose received —
        so the fake URL stays consistent with the fake name beside it.
        """
        if not path or path == "/":
            return path

        def swap(match: re.Match) -> str:
            word = match.group(0)
            key = word.casefold()
            if key in self._known_surnames:
                replacement = self._fake_surname(word)
            elif key in self._known_first_names:
                replacement = self._fake_first(word)
            elif key in self._org_alias:
                replacement = re.sub(r"[^A-Za-z0-9]", "", self._org_alias[key])
            else:
                return word
            if word.islower():
                return replacement.casefold()
            if word.isupper():
                return replacement.upper()
            return replacement

        return re.sub(r"[A-Za-z]{2,}", swap, path)

    def _location(self, text: str) -> str:
        """A stable invented place name, one per real place.

        Drawn from an invented word list rather than a real-place provider: a
        "fake" town that happens to be a real one near the issuer would put the
        reader back within a few miles of the original.
        """
        # A place span may carry the postal code that followed it; keep that
        # shape so the redacted address still reads as an address.
        pin_match = re.search(r"(\s*[–—-]?\s*)(\d{3}\s?\d{3})\s*$", text)
        core = text[: pin_match.start()] if pin_match else text
        key = normalise(core)
        if key not in self._locations:
            # The locality pool is smaller than the number of places a document
            # can name, so a clash is re-rolled as a compound ("Cedarhill Fernbank")
            # rather than suffixed with a digit — "Eastmere 2" reads as a bug, and
            # "commercial banks in Greenmoor 2" is not a sentence anyone believes.
            taken = set(self._locations.values())
            name = ""
            for attempt in range(40):
                faker = self._rand("place", key, str(attempt) if attempt else "")
                name = faker.random_element(FAKE_LOCALITIES)
                if attempt or len(core.split()) > 1:
                    name = f"{name} {faker.random_element(FAKE_PLACE_TAILS)}"
                if name not in taken:
                    break
            self._locations[key] = self._unique(name, taken)
        fake = self._locations[key]
        if core.isupper():
            fake = fake.upper()
        if pin_match:
            faker = self._rand("pin", key)
            digits = f"{faker.random_int(100, 899)} {faker.random_int(100, 999):03d}"
            fake = f"{fake}{pin_match.group(1)}{digits}"
        return fake

    @staticmethod
    def _unique(candidate: str, taken) -> str:
        """Keep two different real values from collapsing onto one surrogate.

        A collision invents a link that does not exist — two unrelated companies
        appearing to be the same one — so a suffix is added until the value is
        free.
        """
        existing = set(taken)
        if candidate not in existing:
            return candidate
        for index in range(2, 60):
            attempt = f"{candidate} {index}"
            if attempt not in existing:
                return attempt
        return candidate

    def _shape_preserving(self, text: str) -> str:
        """Replace digits with digits and letters with letters, keeping layout.

        Used for national IDs (PAN, DIN, CIN, GSTIN, passport) and bank account
        numbers: the surrogate stays format-valid for downstream parsers without
        carrying any of the original value.
        """
        faker = self._rand("shape", text)
        out = []
        for char in text:
            if char.isdigit():
                out.append(str(faker.random_int(0, 9)))
            elif char.isalpha():
                letter = faker.random_element("ABCDEFGHJKLMNPQRSTUVWXYZ")
                out.append(letter if char.isupper() else letter.lower())
            else:
                out.append(char)
        return "".join(out)
