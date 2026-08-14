"""Redaction policy: what counts as PII in this run, and what explicitly does not.

Every judgement call the tool makes lives here so it can be reviewed and changed
without touching detector code.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .types import ALL_LABELS


# ---------------------------------------------------------------------------
# Organisations that are *institutions*, not identifiable private parties.
# By default these are NOT redacted: they carry no information about any
# individual or private company, and blanking them makes the document
# unreadable (every second sentence in a prospectus cites SEBI or the
# Companies Act).  Run with --redact-institutions to redact them anyway.
# ---------------------------------------------------------------------------
INSTITUTION_ALLOWLIST = {
    # regulators / government
    "securities and exchange board of india", "sebi", "reserve bank of india", "rbi",
    "registrar of companies", "roc", "ministry of corporate affairs", "mca",
    "government of india", "central government", "state government", "income tax department",
    "national company law tribunal", "nclt", "supreme court", "high court",
    "insurance regulatory and development authority", "irdai", "competition commission of india",
    "directorate general of foreign trade", "customs", "gst council",
    "central board of direct taxes", "employees provident fund organisation",
    "ministry of finance", "niti aayog", "united nations", "world bank",
    "international monetary fund", "world trade organization", "european union",
    # exchanges / market infrastructure
    "bse limited", "bse", "national stock exchange of india limited", "nse",
    "stock exchanges", "stock exchange", "nsdl", "cdsl",
    "national securities depository limited", "central depository services (india) limited",
    "clearing corporation", "indian clearing corporation limited",
    # standard-setters / indices
    "institute of chartered accountants of india", "icai",
    "institute of company secretaries of india", "icsi",
    "bureau of indian standards", "iso", "crisil ratings", "care ratings",
}

# Tokens that mark a string as a company name even when the NER model misses it.
# Only unambiguous legal suffixes belong here: "Company" and "Co." are too
# common in ordinary prose ("is our Company Secretary") to anchor a match.
ORG_LEGAL_SUFFIXES = (
    "Limited", "Ltd", "Ltd.", "Private Limited", "Pvt. Ltd.", "Pvt Ltd", "LLP",
    "Inc", "Inc.", "Incorporated", "Corporation", "Corp", "Corp.", "GmbH",
    "B.V.", "N.V.", "PLC", "S.A.", "Pte", "Pte. Ltd.", "LLC", "L.L.C.", "LP",
    "AB", "AG", "A/S", "Oy", "SpA", "S.p.A.", "SAS", "Pty", "Pty Ltd", "Sdn Bhd",
    "Chartered Accountants", "& Associates", "& Sons",
    # trusts and family vehicles are entities in their own right
    "Trust", "Foundation", "HUF",
)

# Suffixes stripped when deriving a company's short form ("KSH International
# Limited" -> "KSH International").
ORG_SUFFIXES = tuple(s.casefold() for s in ORG_LEGAL_SUFFIXES) + ("company", "co.")

# Words that a PERSON detection must not consist of — these are the
# false-positive drivers for NER on legal/financial prose.
PERSON_STOPWORDS = {
    "board", "company", "offer", "issuer", "promoter", "promoters", "director",
    "directors", "shareholder", "shareholders", "equity", "share", "shares",
    "bidder", "bidders", "allottee", "registrar", "auditor", "auditors",
    "chapter", "section", "schedule", "annexure", "regulation", "regulations",
    "act", "rules", "circular", "notification", "prospectus", "draft",
    "red herring prospectus", "book running lead managers", "anchor investor",
    "state", "india", "bharat", "rupees", "million", "crore", "lakh",
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december", "fiscal", "financial year",
    "gst", "pan", "din", "cin", "upi", "asba", "ipo", "nav", "ebitda",
}

# Capitalised *defined terms* — the vocabulary a prospectus (or any contract)
# invents for itself.  They are title-cased like names, repeat constantly, and
# are exactly what a NER model mistakes for organisations.
DEFINED_TERM_STOPLIST = {
    "asba", "asba account", "asba form", "asba forms", "allotment", "allotted",
    "allottee", "allottees", "anchor investor", "anchor investors", "bid", "bids",
    "bidder", "bidders", "bid amount", "bid lot", "bidding", "book building process",
    "book running lead manager", "book running lead managers", "basis of allotment",
    "cap price", "floor price", "price band", "cut-off price", "offer price",
    "draft red herring prospectus", "red herring prospectus", "prospectus",
    "equity share", "equity shares", "equity share capital", "preference shares",
    "board of directors", "board", "audit committee", "ipo committee",
    "nomination and remuneration committee", "stakeholders relationship committee",
    "corporate social responsibility committee", "risk management committee",
    "bonus issue", "fresh issue", "offer for sale", "net offer", "net proceeds",
    "general information document", "abridged prospectus", "bid cum application form",
    "designated intermediaries", "designated date", "designated stock exchange",
    "escrow account", "public offer account", "refund account", "sponsor bank",
    "self certified syndicate bank", "syndicate member", "syndicate members",
    "registrar to the offer", "registrar agreement", "underwriting agreement",
    "working day", "fiscal", "fiscals", "financial year", "restated financial information",
    "ebitda", "ebit", "cogs", "nav", "ronw", "eps", "pat", "gst", "ipo", "upi",
    "cin", "din", "pan", "dp id", "client id", "upi id", "upi mandate", "isin", "nach", "rtgs", "neft",
    "ifrs", "ind as", "us gaap", "femas", "fema", "fpi", "fii", "qib", "qibs",
    "hni", "nri", "nris", "oci", "rii", "hufs", "huf", "mutual funds", "insurance companies",
    "corporate office", "registered office", "compliance officer", "company secretary",
    "chief executive officer", "chief financial officer", "managing director",
    "independent director", "independent directors", "executive director",
    "whole-time director", "key managerial personnel", "senior management",
    "promoter", "promoters", "promoter group", "promoter selling shareholders",
    "group companies", "material subsidiary", "subsidiaries", "joint venture",
    "related party", "related parties", "statutory auditors", "peer review",
    "chartered accountants", "memorandum of association", "articles of association",
    "companies act", "sebi icdr regulations", "sebi listing regulations",
    "stock exchanges", "depositories", "depository participant",
    "continuous transposed conductors", "capital market division", "investment",
    "hindi", "english", "marathi", "india", "bharat",
    # Plant, product and offer vocabulary that NER reads as names of people or
    # companies.  Each of these was observed being redacted as PII.
    "green shoe option", "green shoe", "diesel generators", "diesel generator",
    "air conditioning", "air conditioner", "mega volt-amperes", "mega volt",
    "kilovolt", "kilovolts", "volt-amperes", "megawatt", "megawatts",
    "institute of chartered accountants", "refund bank", "specified locations",
    "cap price", "united states dollars", "sponsor banks", "escrow bank",
    "public offer", "offer agreement", "share escrow agent", "monitoring agency",
    "red herring", "private limited", "securities", "registrar", "syndicate",
    "investment", "branch", "branches", "department", "research", "trust",
    "locations", "facility", "chartered accountants", "scsb", "scsbs", "npci",
}

# Place names that appear inside addresses; used to grow an address span and to
# stop the NER model from labelling them as organisations.
INDIAN_STATES = {
    "maharashtra", "karnataka", "gujarat", "tamil nadu", "telangana", "kerala",
    "delhi", "haryana", "punjab", "rajasthan", "uttar pradesh", "madhya pradesh",
    "west bengal", "bihar", "odisha", "andhra pradesh", "goa", "assam",
    "jharkhand", "chhattisgarh", "uttarakhand", "himachal pradesh", "jammu and kashmir",
}

# Places that identify nobody and whose redaction only makes a document unreadable:
# countries, continents and the very largest metros used as market descriptors.
# Everything else learned from the document's own addresses IS redacted, because a
# set of small place names is a fingerprint — "Chakan, Supa, Taloja, Birdewadi"
# names one company even with every proper noun around it replaced.
PLACE_ALLOWLIST = {
    "india", "bharat", "asia", "europe", "africa", "america", "north america",
    "south america", "australia", "china", "japan", "usa", "united states",
    "united kingdom", "uk", "us", "germany", "france", "italy", "spain",
    "singapore", "dubai", "uae", "middle east", "far east", "european union",
}

# Countries and nationalities are never redacted as places: a prospectus that
# cannot say which country it exports to is useless, and a country identifies
# nobody.  Kept separate from PLACE_ALLOWLIST so it can be reused as a filter.
COUNTRY_WORDS = {
    "sweden", "norway", "denmark", "finland", "netherlands", "belgium",
    "switzerland", "austria", "poland", "portugal", "greece", "turkey",
    "russia", "ukraine", "canada", "mexico", "brazil", "argentina", "chile",
    "south africa", "egypt", "nigeria", "kenya", "israel", "saudi arabia",
    "qatar", "kuwait", "oman", "bahrain", "iran", "iraq", "pakistan",
    "bangladesh", "sri lanka", "nepal", "bhutan", "myanmar", "thailand",
    "vietnam", "malaysia", "indonesia", "philippines", "south korea", "korea",
    "taiwan", "hong kong", "new zealand", "ireland", "scotland", "wales",
    "united states of america", "u.s.", "u.s.a.", "america", "european",
    "american", "chinese", "japanese", "german", "british", "indian",
}

# Words that appear next to a place name in an address but are not themselves
# places; used when harvesting localities out of a detected address.
PLACE_NOISE_WORDS = {
    "village", "taluka", "tehsil", "district", "dist", "post", "near", "off",
    "opposite", "behind", "road", "marg", "street", "lane", "chowk", "phase",
    "plot", "gat", "survey", "floor", "wing", "tower", "block", "sector",
    "building", "premises", "compound", "estate", "industrial", "area", "park",
    "no", "nos", "and", "the", "at", "of", "state", "country", "pin", "pincode",
}

ADDRESS_HINT_WORDS = {
    "road", "rd", "street", "st", "marg", "lane", "nagar", "colony", "society",
    "sector", "block", "tower", "building", "bldg", "floor", "flat", "plot",
    "gat", "survey", "s. no", "village", "taluka", "tehsil", "district", "dist",
    "po", "p.o", "opposite", "near", "behind", "chowk", "cross", "phase",
    "industrial", "estate", "mida", "midc", "gidc", "apartment", "apartments",
    "bunglow", "bungalow", "house", "complex", "park", "avenue", "boulevard",
    "drive", "highway", "wing", "annexe", "premises", "compound", "layout",
}


# ---------------------------------------------------------------------------
# Image redaction
# ---------------------------------------------------------------------------

# Words that, in the text anchoring a picture, mark it as a signature block.
# A signature image carries almost no machine-readable evidence of its own, so
# the surrounding prose is the strongest signal available.
SIGNATURE_CONTEXT_CUES = (
    "signature", "signed by", "sd/-", "sd/", "authorised signatory",
    "authorized signatory", "for and on behalf of", "specimen signature",
    "in witness whereof", "yours faithfully", "yours sincerely",
    "thumb impression", "initials",
)

# Shape of a picture that is plausibly handwriting rather than print or artwork.
# Handwriting is sparse, near-monochrome and drawn with a thin, even stroke.
# These are deliberately wide: the cost of a wrong "signature" call is one
# needlessly replaced picture, while a miss leaks a person's actual signature.
SIGNATURE_INK = {
    "min_ink_ratio": 0.004,   # at least this fraction of pixels is ink
    "max_ink_ratio": 0.35,    # more than this is a photo or a solid graphic
    "max_saturation": 60.0,   # mean (max-min) channel spread; ink is grey/blue
    "max_stroke_fraction": 0.06,  # mean stroke width relative to image height
    "min_aspect": 0.8,        # signatures are wider than they are tall
}

# Pictures smaller than this in either dimension are bullets, rules and icons.
# They cannot legibly carry PII and redacting them mangles the layout.
MIN_IMAGE_DIMENSION = 32

# Longest side an image is scaled to before OCR.  Caps both runtime and memory
# on documents containing full-page scans.
OCR_MAX_DIMENSION = 1600


@dataclass
class Policy:
    """Everything tunable about a redaction run."""

    labels: tuple[str, ...] = ALL_LABELS
    #: replace with realistic fake values ("fake") or tags like [PERSON_3] ("tag")
    mode: str = "fake"
    #: locale used to invent surrogate people/companies/addresses
    faker_locale: str = "en_US"
    #: deterministic seed — same input + same seed => same output
    seed: int = 20260813
    #: redact regulators, courts, exchanges and other public institutions too
    redact_institutions: bool = False
    #: treat *every* date as a date of birth (default: only DOB-anchored dates)
    redact_all_dates: bool = False
    #: spaCy pipeline; en_core_web_lg is noticeably better on Indian names
    spacy_model: str = "en_core_web_lg"
    #: skip the NER stage entirely (regex + gazetteer only)
    disable_ner: bool = False
    #: extra names/companies the operator knows about, matched literally
    extra_terms: dict[str, str] = field(default_factory=dict)
    #: scrub author/title/company metadata from the .docx
    scrub_metadata: bool = True
    #: skip the image stage entirely (no OCR, pictures pass through untouched)
    disable_images: bool = False
    #: analyse and report on pictures but leave the bytes alone
    keep_images: bool = False
    #: replace every picture regardless of what was found in it
    redact_all_images: bool = False
    #: redact bare place names learned from the document's own addresses.
    #: On by default: a company's set of plant towns re-identifies it even when
    #: every company and person name around them has been replaced.
    redact_locations: bool = True
    #: keep pictures that yielded no evidence either way.  Off by default:
    #: a signature produces almost no evidence, so "no evidence -> keep" is
    #: precisely how a signature survives redaction.
    keep_unclassified_images: bool = False

    def wants(self, label: str) -> bool:
        return label in self.labels
