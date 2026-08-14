---
title: PII Redactor
emoji: 🛡️
colorFrom: blue
colorTo: purple
sdk: gradio
app_file: app.py
pinned: false
short_description: Redact PII in .docx - text, images and ID scans
---

# PII Redaction Tool

Reads a `.docx`, replaces every piece of personally identifiable information with a
**consistent fake alternative**, and writes a new `.docx` with the original
formatting, tables and numbering intact — **text and embedded images alike**.

Built for the attached *Red Herring Prospectus* (KSH International Limited, 4,231
paragraphs and table cells, ~452,000 characters, 8 embedded images), but nothing in
it is specific to that document.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install https://github.com/explosion/spacy-models/releases/download/en_core_web_lg-3.8.0/en_core_web_lg-3.8.0-py3-none-any.whl

python redact.py "examples/red_herring_prospectus.docx" \
    -o "output/Red Herring Prospectus - REDACTED.docx" \
    --mapping output/mapping.json --detections output/detections.csv
```

Runs in ~30 seconds on the prospectus (~19 s with `--no-ocr`, which skips the image
stage). Deliverables produced by exactly that command are in `output/`.

There is also a web UI — drag in a `.docx`, watch the real pipeline stages stream,
compare every embedded image before and after with a slider, and download the result:

```bash
docker build -t pii-redactor . && docker run --rm -p 7860:7860 pii-redactor
```

See [`DEPLOY.md`](DEPLOY.md) for Hugging Face Spaces, measured timings, and what the
web layer deliberately does not keep.

The nine PII types the brief requires — names, emails, phone numbers, company names,
addresses, SSNs, credit cards, dates of birth and IP addresses — are all detected,
plus URLs, national IDs (PAN/Aadhaar/CIN/DIN/GSTIN) and bank accounts. Anything the
document carries as a *picture* is covered too: scanned ID cards, signatures, company
logos and QR codes.

---

## Approach

**Hybrid: deterministic rules → document-level gazetteer → NER, in that order of trust.**

Neither regex nor NER alone is good enough. Regex cannot find "Rakhi Girija Shetty".
A general-purpose English NER model, run on Indian legal prose, mislabels people as
organisations, misses names in ALL-CAPS table cells, and confidently tags
"Audit Committee", "Equity Shares" and "Cap Price" as entities. So the tool uses
three layers, and each one constrains the next:

1. **Pattern detectors with validators** (`detectors.py`, `addresses.py`) — emails,
   phones, SSNs, credit cards (Luhn-checked), IPv4/IPv6 (octet-checked), PAN, CIN,
   GSTIN, Aadhaar, DIN, passport, IFSC, firm registration numbers, bank accounts,
   URLs, dates of birth, and postal addresses. These are high precision by
   construction: a 16-digit number is only a card if it passes Luhn; an
   Aadhaar-shaped number is only Aadhaar if the word "Aadhaar" is nearby.

2. **A document-level gazetteer** (`gazetteer.py`) — *this is the core of the design.*
   Pass 1 reads the entire document and builds a lexicon of the people and companies
   it talks about, using high-precision context ("Contact person: X", "being X",
   "X, aged", "allotted to X", a cell under a `Name` column, anything ending in
   `Limited`/`LLP`/`Trust`) plus NER candidates that survive filtering. Pass 2 then
   matches that lexicon **literally, everywhere**.

   The effect: a name has to be recognised *once*, in one favourable context, to be
   redacted *everywhere* — on the ALL-CAPS cover page, in table cells, in footnotes,
   inside email local parts. This is what turns a ~70%-recall model into a
   ~96%-recall system.

3. **spaCy NER** (`ner.py`) as a candidate generator only. Raw model output never
   reaches the output document unfiltered — it is passed through the same
   document-derived filters the gazetteer uses (below).

### Place names: the anchors that survive entity redaction

Redacting every person and company still leaves a document that names itself. A
manufacturer's set of plant towns — *Chakan, Supa, Taloja, Birdewadi, Khalumbre* —
identifies it as surely as its letterhead, and none of those words is a person, a
company or an address. They sit in narrative prose ("our Supa facility commissioned
a second line"), where no address detector can reach them.

So places are learned the same way people and companies are — **from the document
itself**. Every address the tool detects is decomposed into its components, and the
localities inside it (plus the word immediately before a PIN code, which is the city
by construction) become a place lexicon that is then matched everywhere, including in
prose. A postal code sitting immediately after a known place is absorbed into the
span, because six digits alone are unanchored but `Pune – 411 045` is an address.

The filters are stricter than for names, because a document's defined terms cluster
near addresses: a candidate is rejected if it contains any word the document also uses
in lower-case prose, any acronym, any defined term, or a leading article
(`the Cap Price`, `Designated RTA`, `our Supa Facility` → `Supa`). Countries are never
redacted — a prospectus that cannot say where it exports is useless, and a country
identifies nobody. `--keep-locations` turns the whole pass off.

### Telling names from jargon, without a dictionary

The biggest precision problem in a legal document is that its own defined terms are
capitalised exactly like names. The tool solves this with a signal taken from the
document itself: **it counts every word that appears in lower case in running prose.**

"committee", "allotment", "branch", "research" and "facility" all appear in lower
case somewhere in the prospectus; "Hegde", "Nuvama" and "Malvadkar" never do. So any
candidate entity containing a word the document also uses as an ordinary word is
rejected. This is a dictionary check that adapts to whatever document it is given.

Together with the stoplists below it cut total detections on the prospectus from
3,185 to 765 — the difference being almost entirely false positives such as
"Equity Share", "Audit Committee", "Bonus Issue" and 185 occurrences of "SEBI"
redacted as somebody's surname — while recall went *up*.

It is backed up by a curated stoplist of financial/legal defined terms
(`config.DEFINED_TERM_STOPLIST`) and an allowlist of public institutions
(`config.INSTITUTION_ALLOWLIST`).

### Consistent surrogates

Substitution is deterministic — `sha256(seed, type, value)` seeds a `Faker`
instance — so the same input always produces byte-identical output, and related
values stay related:

| original | surrogate |
|---|---|
| `Kushal Subbayya Hegde` | `Aguilar Susan Bartlett` |
| `Mr. Hegde` (same person, later) | `Mr. Bartlett` |
| `Rajesh Kushal Hegde` (his son) | `Evans Aguilar Bartlett` — family surname stays shared |
| `Sarthak.malvadkar@kshinterantional.com` | `jennifer.russo@<fake-company>.example.com` |
| `KSH International Limited` / `KSH` | `Redstone Works Limited` / `RW` |
| `+ 91 20 4505 3237` | `+ 91 25 2727 5384` — country code and grouping preserved |
| `U28129PN1979PLC141032` (CIN) | `H69800UV9398JLV276514` — format-valid, no real value |

Names are mapped **token by token** (first names to first names, surnames to
surnames), which is why partial mentions stay consistent with full ones. Generated
domains always sit under `example.com` (RFC 2606) so a surrogate can never collide
with a real domain. `--mode tag` switches to `[PERSON_1]`-style labels instead.

### Images: the PII no text scanner can see

A document's most sensitive content is often not text at all. This prospectus embeds
a **scanned PAN card** (face photo, PAN number, name, father's name, date of birth,
signature, QR) and a **scanned Aadhaar card** (face photo, name, DOB, the twelve-digit
Aadhaar number twice, full address, QR). No amount of paragraph scanning will ever
see them.

`images.py` closes that gap without inventing a second detection stack. It lifts
whatever it can out of each picture and pushes *that* through the same detectors,
gazetteer and NER the paragraphs go through — so an image is classified by the same
code that classifies a paragraph:

| Analyser | What it recovers |
|---|---|
| OCR (`rapidocr-onnxruntime`) | printed text inside the picture |
| Face detection (OpenCV Haar) | a face, which is biometric PII in its own right |
| Barcode decoding (OpenCV) | QR/barcode payloads, then treated as ordinary text |
| Ink morphology (Otsu + distance transform) | sparse, near-monochrome, thin even strokes: handwriting |

The spans that come back decide what happens:

| Evidence | Class | Replacement |
|---|---|---|
| `PERSON`/`NATIONAL_ID`/`DOB`/`ADDRESS` in the image, or a detected face | `IMAGE_ID_DOCUMENT` | opaque box at the original dimensions |
| A signature caption nearby, or handwriting-shaped ink | `IMAGE_SIGNATURE` | a synthetic squiggle, seeded from the surrogate name |
| A decodable QR/barcode | `IMAGE_CODE` | **a regenerated, still-scannable code pointing at the surrogate URL** |
| Only `ORG` evidence, or a company named in the caption | `IMAGE_LOGO` | a wordmark bearing the company's existing surrogate name |
| Nothing, but the picture has ink | `IMAGE_UNCLASSIFIED` | opaque box |
| Nothing, and no ink | *kept*, logged as reviewed | — |

Three decisions in there are deliberate and worth stating:

**Identity documents are replaced whole, never partially.** The QR on an Aadhaar or
PAN card re-encodes the entire record, and the photograph is PII by itself — so
boxing out the visible name and number would leak everything anyway through the code.

**No evidence means redact, not keep.** A signature yields almost no machine-readable
evidence; a keep-by-default rule is precisely how one survives redaction. The default
is therefore inverted: a picture is kept only when it can be affirmatively cleared.
`--keep-unclassified-images` restores the opposite trade.

**Surrogates stay consistent across media.** The QR in the redacted prospectus still
scans — to `https://arnoldhughes.example.com/…`, the same fake host the URL detector
assigned in the text. The KSH logo now reads `REDSTONE WORKS`, the same fake company
name that replaced "KSH International Limited" on all 284 of its textual mentions.

Every engine is optional and lazily imported. With none installed the stage degrades
to "replace every picture", which is still safe; `--no-ocr` skips it entirely.

### Writing the .docx back

Word splits a single word across several `<w:t>` runs whenever formatting or
spell-check state changes, so naive string replacement misses matches and destroys
styling. `docx_io.py` reconstructs each paragraph's text, runs detection on that, and
maps replacements back onto the individual runs — formatting, numbering, table
structure and styles are untouched. Coverage includes body, tables at any nesting
depth, headers, footers, footnotes/endnotes, text boxes, `HYPERLINK` field codes,
hyperlink relationship targets (a `mailto:` link keeps the real address even after
the visible text is replaced) and the document metadata (author/title).

---

## Policy choices (the deliberate ones)

| Decision | Choice | Why |
|---|---|---|
| Regulators, courts, stock exchanges (SEBI, RBI, BSE, NSE, RoC, ICAI) | **not** redacted | They identify nobody. Redacting them makes a prospectus unreadable without any privacy gain. `--redact-institutions` flips this. |
| Private companies (issuer, subsidiaries, banks, BRLMs, auditors, suppliers, customers) | redacted | The assignment lists company names as PII. |
| Dates | only **dates of birth** | A prospectus is wall-to-wall dates (board resolutions, filings, fiscal years). Only birth-anchored dates are personal. `--redact-all-dates` flips this. |
| Ticket / order / invoice numbers | **not** redacted | Explicitly called out as a precision trap in the brief; they identify a transaction, not a person. There is a unit test for this. |
| Statutory identifiers (PAN, CIN, DIN, GSTIN, SEBI reg. no., firm reg. no.) | redacted | They are direct identifiers of a person or a redacted company. |
| Standalone city/town/state names ("Pune", "Chakan", "Maharashtra") | **redacted**, learned from the document's own addresses | Reversed after an audit: a company's set of plant towns re-identifies it even when every person and company name has been replaced. `--keep-locations` restores the old behaviour. |
| Countries ("India", "Sweden", "United States") | **not** redacted | A country identifies nobody, and a prospectus that cannot say where it exports is unreadable. |
| Fragments of a redacted identifier ("Registration number: 141032") | redacted | The six-digit tail of a CIN is directly queryable against the companies registry. |
| Document metadata (author, title) | scrubbed | It leaks the author's name outside the visible text. |
| Embedded images | replaced unless affirmatively cleared | Signatures and ID scans produce almost no evidence; keep-by-default is how they survive. `--keep-unclassified-images` flips this. |
| URL paths and query strings | scrubbed, not just the host | `…/account/rashi-patil` names the customer even after the domain is faked. |

---

## What it gets wrong

Measured on a hand-annotated sample — full numbers and method in
[`evaluation/EVALUATION_REPORT.md`](evaluation/EVALUATION_REPORT.md).

**Precision 0.957 · Recall 0.957 · F1 0.957 · type-insensitive P/R 1.000 · token
accuracy 99.69% · 0 of 367 redacted values still present anywhere in the output · 0 of
8 output images leaking any redacted value when re-read with OCR.**

The type-insensitive row is the one that matters for privacy: **every annotated PII
value in the sample is replaced, and nothing outside the annotations is**. The two
false positives and two false negatives in the primary row are the same four items
counted twice — disagreements about which *label* a value got, not values that leaked.

> **Two corrections to earlier versions of this README.**
>
> 1. The "0 values still present" figure originally covered *text only*. Eight
>    embedded images — including scanned PAN and Aadhaar cards — were never inspected
>    and passed through byte-identical, so the claim of complete coverage was wrong.
> 2. A subsequent audit of the redacted output found the document still
>    **re-identifiable in under a minute**, despite healthy bulk-entity numbers. Three
>    independent anchors survived: the issuer's own domain on the cover page (split
>    across runs as `www.kshinternational. com`, so the URL pattern missed it), the
>    six-digit tail of its CIN printed as a bare "Registration number", and its entire
>    manufacturing footprint as bare place names in prose. Section *Place names* above
>    and the three detector fixes are the response; all are regression-tested.
>
> Both are recorded rather than quietly fixed, because "the bulk numbers look good"
> is exactly the reasoning that let them through.

False negatives seen:

* **`Gopal BO`** — names containing a short all-caps token are rejected, because that
  rule is what stops `Designated RTA Locations` and `FIG-OPS Department` from being
  redacted as people. A real trade-off, currently set in favour of precision.
* **Entities split across paragraph boundaries.** Word occasionally breaks a name in
  half (`KSH` / `Distriparks Private Limited` in adjacent paragraphs). There is a
  fix-up pass for adjacent pairs; three-way splits would still slip through.
* **Names that appear exactly once, in prose, that spaCy misses.** With no context
  pattern and no NER hit, nothing sees them. This is the residual risk of the design
  and the reason `--extra-terms` exists.

False positives seen:

* **Type confusion rather than over-redaction.** `Karunakar Hegde HUF` is redacted as
  a person, not an organisation. The value is still replaced consistently; only the
  label is wrong. Type-insensitively, precision on the sample is **1.000**.
* **Boundary drift on addresses.** A span may include or exclude a trailing
  ", Maharashtra, India". Harmless, but it is why the strict exact-boundary score
  (0.81 F1) is well below the overlap score.
* Earlier iterations over-redacted heavily (defined terms, `SEBI`, `Equity Share`);
  the filters described above were built in response and are covered by tests.

On images specifically:

* **Signature detection is heuristic.** The ink test will call some hand-drawn
  figures and annotations signatures. Under the inverted default that costs a
  needlessly replaced picture, never a leak — which is the trade it was chosen for.
* **Logo classification is best-effort.** OCR routinely fails on stylised wordmarks
  (`ICICI Securities` came back as `iICICIS Securities`), so those fall through to
  `IMAGE_UNCLASSIFIED`. They are still replaced; only the *appearance* of the
  replacement degrades from a wordmark to a plain box.
* **Vector embeddings (EMF/WMF/SVG) cannot be re-encoded** by Pillow. Rather than
  leave them, the drawing is deleted outright — always a safe redaction, if a blunt
  one. Undecodable or corrupt images take the same path, and a picture that throws
  during analysis is removed rather than trusted.

**Not handled:** tracked changes and comments beyond plain-text extraction; languages
other than English (Devanagari on the ID cards is only partly read — which does not
matter, because those images are replaced whole); embedded OLE objects such as an
attached spreadsheet, whose *contents* are not scanned; and steganography.

---

## Adding a new PII type

Four small steps, all local:

1. Add the label to `types.py` (`ALL_LABELS`, and a priority in `LABEL_PRIORITY`
   that decides who wins when spans overlap).
2. Emit it from a detector — a regex + validator in `detectors.py`, or a new module
   like `addresses.py` for anything needing span assembly.
3. Add a surrogate generator in `surrogates.py` (`_shape_preserving` is a sane
   default: it keeps the format and replaces the content).
4. Add a test in `tests/test_redactor.py`.

Nothing in the pipeline, the docx writer or the CLI needs to change — the label flows
through automatically, including `--types`.

---

## Layout

```
redact.py                      CLI entry point
Dockerfile                     container image (HF Spaces, port 7860)
webapp/
  app.py                       FastAPI: upload -> job -> SSE progress -> download
  jobs.py                      in-memory jobs, temp files, 15-minute TTL
  library.py                   the sample documents offered in the UI
  static/, templates/          the front end (no build step)
pii_redactor/
  config.py                    policy, allowlists, stoplists — every judgement call
  types.py                     Span/labels, overlap resolution
  detectors.py                 validator-backed pattern detectors
  addresses.py                 postal-code-anchored + line-level address detection
  gazetteer.py                 document-level entity inventory, alias expansion
  ner.py                       spaCy wrapper (optional; --no-ner runs rules only)
  images.py                    OCR/face/barcode/ink analysis of embedded pictures
  surrogates.py                deterministic consistent fake values
  docx_io.py                   run-level read/write, headers, tables, links, images, metadata
  pipeline.py                  two-pass orchestration + audit artefacts
evaluation/
  build_sample.py              stratified, system-independent sampling
  gold_spans.jsonl             hand annotations (110 blocks, 46 entities)
  evaluate.py                  P/R/F1, token accuracy, text + image leak checks
  reidentification_audit.py    greps the output for anchors that would name the subject
  reidentification_terms.json  those anchors, chosen by reading the source
  EVALUATION_REPORT.md         method, results, error analysis
examples/
  make_samples.py              builds the three synthetic library documents — a support
                               case file, an employment contract and an insurance claim,
                               each 185-292 blocks with tables, ID cards, signatures,
                               logos and QR codes
tests/test_redactor.py         29 unit tests (python tests/test_redactor.py)
output/                        redacted prospectus + mapping.json + detections.csv
```

`mapping.json` (original → surrogate, by type) and `detections.csv` (every span, with
the detector that fired and its confidence) exist for review: they are how a human
audits what the tool did.

> **They are a complete reversal key and are `.gitignore`d.** Together they undo the
> redaction entirely. An earlier revision committed both to git in the same directory
> as the deliverable, and `detections.csv` additionally quoted the OCR text of the ID
> cards it had just redacted — the image redaction succeeded and the audit file undid
> it. The image audit rows now record shape and counts only (`3 text line(s), 88
> chars; 1 face(s); 768x962 PNG`), never the recovered text.

`detections.csv` also carries `part` (body, header, footnotes…) and `find_in_output`.
The `block` column is an internal index over non-empty paragraphs and does **not**
correspond to anything visible in Word; searching the redacted document for the value
in `find_in_output` is how a reviewer actually locates a detection.

### CLI options

```
--mode fake|tag          realistic surrogates (default) or [PERSON_1] labels
--types PERSON,EMAIL,…   restrict to selected PII types
--seed N                 change the surrogate seed (output stays deterministic)
--locale en_IN           locale for generated names/companies
--no-ner                 rules + gazetteer only (no spaCy needed)
--model en_core_web_sm   smaller/faster spaCy model
--redact-institutions    also redact regulators, courts, exchanges
--redact-all-dates       treat every date as a date of birth
--extra-terms terms.json force literal strings into the gazetteer, e.g.
                         {"Jane Q. Public": "PERSON", "Acme Corp": "ORG"}
--keep-metadata          leave docx author/title alone
--keep-locations         do not redact learned place names
--no-ocr                 skip the image stage entirely (~11 s faster)
--keep-images            analyse and audit pictures but do not modify them
--redact-all-images      replace every picture regardless of what was found
--keep-unclassified-images
                         keep pictures that yielded no evidence (default: replace)
```

### Adding a new PII type that lives in an image

Nothing extra is needed. Image text flows through the same `detect` path as
paragraph text, so a new detector added in the four steps above is automatically
applied to OCR output. Only a genuinely new *image class* (say, a fingerprint) needs
a branch in `images.py`.
