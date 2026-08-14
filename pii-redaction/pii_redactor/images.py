"""Redacting the pictures in a .docx.

Text scanning cannot see a scanned Aadhaar card, a handwritten signature or a QR
code that encodes a live URL — and those are among the most sensitive things a
document carries.  This module closes that gap without inventing a second
detection stack: it lifts whatever it can out of each picture (OCR text, decoded
barcode payloads, the prose surrounding the picture) and pushes that through the
*same* detectors, gazetteer and NER the paragraphs go through.  The spans that
come back decide what the picture is.

Four analysers feed the decision:

* **OCR** — printed text inside the image.
* **Faces** — a face is biometric PII in its own right, and is what separates a
  scanned ID card from a printed form.
* **Barcodes** — decoded and then treated as ordinary text, so a QR pointing at
  a company URL is caught by the URL detector.
* **Ink morphology** — sparse, near-monochrome, thin even strokes: handwriting.

Two design rules matter more than the individual heuristics:

1. **Identity documents are replaced whole, never partially.**  The QR on an
   Aadhaar or PAN card re-encodes the entire record and the photograph is itself
   PII, so boxing out the name and number would leak everything anyway.
2. **No evidence means redact, not keep.**  A signature yields almost no
   machine-readable evidence, so a keep-by-default rule is exactly how one
   survives.  ``--keep-unclassified-images`` restores the opposite trade.

Every engine is optional and imported lazily; with none of them installed the
stage degrades to "replace every picture", which is still safe.
"""

from __future__ import annotations

import hashlib
import io
import math
from dataclasses import dataclass, field

from .config import (
    MIN_IMAGE_DIMENSION,
    OCR_MAX_DIMENSION,
    SIGNATURE_CONTEXT_CUES,
    SIGNATURE_INK,
    Policy,
)
from .surrogates import ORG_SUFFIX_RE
from .types import (
    EMAIL,
    IDENTITY_EVIDENCE_LABELS,
    IMAGE_CODE,
    IMAGE_ID_DOCUMENT,
    IMAGE_LOGO,
    IMAGE_SIGNATURE,
    IMAGE_UNCLASSIFIED,
    ORG,
    PERSON,
    PHONE,
    URL,
    Span,
)

# Formats Pillow can write back.  Anything else (EMF/WMF metafiles, SVG) is
# redacted by deleting the drawing instead — see ImageRedactor.redact.
WRITABLE_FORMATS = {"PNG", "JPEG", "GIF", "BMP", "TIFF", "WEBP", "PPM"}

_INK = "#1d2733"
_PAPER = "#f4f5f7"


@dataclass
class ImageEvidence:
    """Everything the analysers could establish about one picture."""

    name: str = ""
    format: str = ""
    width: int = 0
    height: int = 0
    ocr_text: str = ""
    codes: list[str] = field(default_factory=list)
    faces: int = 0
    ink_ratio: float = 0.0
    saturation: float = 0.0
    stroke_fraction: float = 0.0
    readable: bool = True
    notes: list[str] = field(default_factory=list)

    @property
    def aspect(self) -> float:
        return self.width / self.height if self.height else 0.0

    def summary(self) -> str:
        """A description safe to write into the audit log.

        Deliberately does NOT quote the recovered text.  The whole point of
        replacing a scanned ID card is that the name, parent's name and date of
        birth on it stop existing outside the source document — writing them
        into detections.csv would hand them straight back.  Shape and counts are
        enough for a reviewer to judge the decision.
        """
        bits = []
        if self.faces:
            bits.append(f"{self.faces} face(s)")
        if self.ocr_text:
            lines = [line for line in self.ocr_text.splitlines() if line.strip()]
            bits.append(f"{len(lines)} text line(s), {len(self.ocr_text)} chars")
        if self.codes:
            bits.append(f"{len(self.codes)} decoded code(s)")
        if not self.readable:
            bits.append("undecodable")
        bits.append(f"{self.width}x{self.height} {self.format or '?'}")
        bits.append(f"ink={self.ink_ratio:.3f}")
        return "; ".join(bits)


@dataclass
class ImageDecision:
    """What the tool decided to do with a picture, and why."""

    name: str
    label: str
    evidence: ImageEvidence
    reason: str
    replaced: bool = False
    action: str = "kept"


class _Engines:
    """Lazily-loaded optional dependencies.

    Kept behind one object so the rest of the module never has to care whether
    OCR is installed, and so a missing engine degrades the *decision* rather
    than crashing the run.
    """

    def __init__(self) -> None:
        self._ocr = None
        self._cascade = None
        self._loaded = {"ocr": False, "cv": False}
        self.cv2 = None
        self.numpy = None

    @property
    def cv(self):
        if not self._loaded["cv"]:
            self._loaded["cv"] = True
            try:
                import cv2
                import numpy

                self.cv2, self.numpy = cv2, numpy
            except Exception:
                self.cv2 = self.numpy = None
        return self.cv2

    @property
    def ocr(self):
        if not self._loaded["ocr"]:
            self._loaded["ocr"] = True
            try:
                from rapidocr_onnxruntime import RapidOCR

                self._ocr = RapidOCR()
            except Exception:
                self._ocr = None
        return self._ocr

    @property
    def faces(self):
        if self._cascade is None and self.cv is not None:
            try:
                path = self.cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
                cascade = self.cv2.CascadeClassifier(path)
                self._cascade = cascade if not cascade.empty() else None
            except Exception:
                self._cascade = None
        return self._cascade

    @property
    def available(self) -> bool:
        return self.ocr is not None or self.cv is not None


class ImageRedactor:
    """Analyses and replaces every embedded picture in a document."""

    def __init__(self, policy: Policy, detect, surrogate) -> None:
        self.policy = policy
        self._detect = detect          # (text) -> list[Span]
        self._surrogate = surrogate    # (Span) -> str
        self.engines = _Engines()
        self.decisions: list[ImageDecision] = []
        self._cache: dict[str, ImageEvidence] = {}

    # -- analysis ----------------------------------------------------------
    def analyse(self, blob: bytes, name: str) -> ImageEvidence:
        digest = hashlib.sha256(blob).hexdigest()
        if digest in self._cache:
            cached = ImageEvidence(**{**self._cache[digest].__dict__, "name": name})
            return cached

        evidence = ImageEvidence(name=name)
        from PIL import Image

        try:
            with Image.open(io.BytesIO(blob)) as image:
                image.load()
                evidence.format = (image.format or "").upper()
                evidence.width, evidence.height = image.size
                working = self._prepare(image)
        except Exception as error:  # corrupt, truncated, or a vector metafile
            evidence.readable = False
            evidence.notes.append(f"could not decode ({type(error).__name__})")
            self._cache[digest] = evidence
            return evidence

        if min(evidence.width, evidence.height) < MIN_IMAGE_DIMENSION:
            evidence.notes.append("below minimum dimension")
            self._cache[digest] = evidence
            return evidence

        self._read_text(working, evidence)
        self._read_codes(working, evidence)
        self._count_faces(working, evidence)
        self._measure_ink(working, evidence)
        self._cache[digest] = evidence
        return evidence

    @staticmethod
    def _prepare(image):
        """Flatten to RGB and cap the longest side before any analysis."""
        from PIL import Image

        converted = image
        if converted.mode in ("RGBA", "LA", "P"):
            converted = converted.convert("RGBA")
            background = Image.new("RGBA", converted.size, (255, 255, 255, 255))
            converted = Image.alpha_composite(background, converted)
        converted = converted.convert("RGB")
        longest = max(converted.size)
        if longest > OCR_MAX_DIMENSION:
            scale = OCR_MAX_DIMENSION / longest
            new_size = (max(1, int(converted.width * scale)), max(1, int(converted.height * scale)))
            converted = converted.resize(new_size)
        return converted

    def _read_text(self, image, evidence: ImageEvidence) -> None:
        engine = self.engines.ocr
        if engine is None:
            evidence.notes.append("ocr unavailable")
            return
        try:
            result, _ = engine(self._to_array(image))
        except Exception as error:
            evidence.notes.append(f"ocr failed ({type(error).__name__})")
            return
        lines = [str(item[1]) for item in (result or []) if len(item) > 1 and item[1]]
        evidence.ocr_text = "\n".join(lines)

    def _read_codes(self, image, evidence: ImageEvidence) -> None:
        cv2 = self.engines.cv
        if cv2 is None:
            return
        array = self._to_array(image)[:, :, ::-1].copy()
        detector = cv2.QRCodeDetector()
        payloads: list[str] = []
        try:
            ok, decoded, _, _ = detector.detectAndDecodeMulti(array)
            if ok:
                payloads = [text for text in decoded if text]
        except Exception:
            pass
        if not payloads:
            try:
                text, _, _ = detector.detectAndDecode(array)
                if text:
                    payloads = [text]
            except Exception:
                pass
        evidence.codes = payloads

    def _count_faces(self, image, evidence: ImageEvidence) -> None:
        cascade = self.engines.faces
        if cascade is None:
            return
        cv2 = self.engines.cv
        try:
            grey = cv2.cvtColor(self._to_array(image), cv2.COLOR_RGB2GRAY)
            found = cascade.detectMultiScale(grey, scaleFactor=1.15, minNeighbors=6, minSize=(28, 28))
            evidence.faces = len(found)
        except Exception:
            evidence.faces = 0

    def _measure_ink(self, image, evidence: ImageEvidence) -> None:
        """Ink ratio, colourfulness and mean stroke width.

        Handwriting is sparse, near-grey and drawn with a thin, even stroke;
        photographs are dense, logos are saturated, printed text has many small
        components.  Stroke width comes from a distance transform, which is
        cheap and scale-aware.
        """
        cv2 = self.engines.cv
        if cv2 is None:
            return
        try:
            array = self._to_array(image)
            grey = cv2.cvtColor(array, cv2.COLOR_RGB2GRAY)
            _, binary = cv2.threshold(grey, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
            evidence.ink_ratio = float(binary.mean() / 255.0)
            channels = array.astype("float32")
            evidence.saturation = float((channels.max(axis=2) - channels.min(axis=2)).mean())
            if binary.any():
                distance = cv2.distanceTransform(binary, cv2.DIST_L2, 5)
                stroke = float(distance[binary > 0].mean() * 2.0)
                evidence.stroke_fraction = stroke / max(1, binary.shape[0])
        except Exception:
            pass

    @staticmethod
    def _to_array(image):
        import numpy

        return numpy.array(image)

    # -- classification ----------------------------------------------------
    def classify(self, evidence: ImageEvidence, context: str) -> tuple[str, str]:
        """Decide what a picture is, from evidence only.  Returns (label, reason)."""
        if self.policy.redact_all_images:
            return IMAGE_UNCLASSIFIED, "--redact-all-images"

        if min(evidence.width, evidence.height) and min(evidence.width, evidence.height) < MIN_IMAGE_DIMENSION:
            return "", "icon or rule, too small to carry PII"

        if not evidence.readable:
            # Cannot be inspected, therefore cannot be cleared.
            return IMAGE_UNCLASSIFIED, "image could not be decoded for inspection"

        # With no OCR and no vision library there is nothing to classify on:
        # ocr_text is empty and ink_ratio is 0.0 not because the picture is
        # blank but because nothing measured it.  Falling through would hit the
        # "blank -> keep" branch below and leave every image in place, which is
        # the one outcome this stage must never produce.
        if not self.engines.available:
            return IMAGE_UNCLASSIFIED, "no analysis engine available; redacted unexamined"

        inside = evidence.ocr_text + "\n" + "\n".join(evidence.codes)
        labels = self._labels(inside)

        if evidence.faces:
            return IMAGE_ID_DOCUMENT, f"contains {evidence.faces} detected face(s)"
        identity = labels & IDENTITY_EVIDENCE_LABELS
        # A lone PERSON hit on two words of text is far more likely to be a
        # wordmark than a scanned card, so it needs corroboration: either a
        # second kind of identifier, or enough text to be a document at all.
        if identity == {PERSON} and not _is_document_sized(evidence):
            identity = set()
        if identity:
            return IMAGE_ID_DOCUMENT, f"contains {', '.join(sorted(identity))} in image text"
        if evidence.codes:
            return IMAGE_CODE, "encodes a scannable payload"

        cue = self._signature_cue(context)
        if cue:
            return IMAGE_SIGNATURE, f"captioned {cue!r}"
        if self._looks_handwritten(evidence):
            return IMAGE_SIGNATURE, "ink morphology consistent with handwriting"

        if labels & {ORG, URL, EMAIL, PHONE}:
            return IMAGE_LOGO, f"contains {', '.join(sorted(labels & {ORG, URL, EMAIL, PHONE}))} in image text"
        if ORG in self._labels(context):
            return IMAGE_LOGO, "anchored to a company name in surrounding text"

        if _looks_like_a_mark(evidence):
            return IMAGE_LOGO, "small, saturated wordmark"

        # Only trust "blank" when something actually measured the ink.
        if evidence.ink_ratio < 0.002 and self.engines.cv is not None:
            return "", "blank or near-blank"
        if self.policy.keep_unclassified_images:
            return "", "no evidence found (--keep-unclassified-images)"
        return IMAGE_UNCLASSIFIED, "no evidence either way; redacted by default"

    def _spans(self, text: str) -> list[Span]:
        """Detect over the text as read, and over a title-cased copy.

        A wordmark is often set in lower case ("nuvama") or full caps, while the
        gazetteer learned the company from running prose ("Nuvama Wealth
        Management Limited").  Re-running on a title-cased copy costs one extra
        pass and recovers those matches; offsets are irrelevant here because only
        the labels and values are used.
        """
        if not text or not text.strip():
            return []
        spans = list(self._detect(text))
        variant = text.title()
        if variant != text:
            # Only companies are taken from the re-cased pass.  Title-casing turns
            # any two-word wordmark into something that looks like a person's
            # name ("nuvama" -> "Nuvama"), so accepting PERSON here would label
            # every logo an identity document.
            spans.extend(span for span in self._detect(variant) if span.label == ORG)
        return spans

    def _labels(self, text: str) -> set[str]:
        return {span.label for span in self._spans(text)}

    @staticmethod
    def _signature_cue(context: str) -> str:
        lowered = context.casefold()
        for cue in SIGNATURE_CONTEXT_CUES:
            if cue in lowered:
                return cue
        return ""

    @staticmethod
    def _looks_handwritten(evidence: ImageEvidence) -> bool:
        rules = SIGNATURE_INK
        if evidence.ocr_text.strip():
            return False  # confident printed text means it is not a signature
        return (
            rules["min_ink_ratio"] <= evidence.ink_ratio <= rules["max_ink_ratio"]
            and evidence.saturation <= rules["max_saturation"]
            and 0 < evidence.stroke_fraction <= rules["max_stroke_fraction"]
            and evidence.aspect >= rules["min_aspect"]
        )

    # -- replacement rendering ---------------------------------------------
    def render(self, label: str, evidence: ImageEvidence, context: str):
        """Build the picture that replaces the original, at identical size."""
        size = (max(evidence.width, 1), max(evidence.height, 1))
        if label == IMAGE_CODE:
            return self._render_code(size, evidence)
        if label == IMAGE_SIGNATURE:
            return self._render_signature(size, evidence, context)
        if label == IMAGE_LOGO:
            return self._render_logo(size, evidence, context)
        # ASCII only: the bundled PIL font has no em-dash and renders it as tofu.
        caption = "REDACTED - IDENTITY DOCUMENT" if label == IMAGE_ID_DOCUMENT else "REDACTED"
        return self._render_box(size, caption)

    def _render_box(self, size, caption: str):
        from PIL import Image, ImageDraw

        image = Image.new("RGB", size, _INK)
        draw = ImageDraw.Draw(image)
        draw.rectangle([(2, 2), (size[0] - 3, size[1] - 3)], outline="#5b6472", width=2)
        self._centre_text(draw, size, caption, "#e6e9ee")
        return image

    def _render_logo(self, size, evidence: ImageEvidence, context: str):
        """A neutral wordmark carrying the company's surrogate name.

        Using the surrogate the *text* already got means the logo and the prose
        agree: if "Nuvama" became "Redstone Works" on every page, its logo says
        "Redstone Works" too.
        """
        from PIL import Image, ImageDraw

        # A wordmark carries the company's name, not its legal form: "Redstone
        # Works", not "Redstone Works Limited" truncated to "REDSTONE WORKS LIMIT".
        name = ORG_SUFFIX_RE.sub("", self._surrogate_org(evidence, context) or "REDACTED").strip()
        image = Image.new("RGB", size, _PAPER)
        draw = ImageDraw.Draw(image)
        seed = int(hashlib.sha256(name.encode("utf-8")).hexdigest()[:6], 16)
        hue = ((seed % 300) / 300.0) * 360.0
        colour = _hsv_to_rgb(hue, 0.55, 0.62)
        radius = max(6, min(size) // 5)
        draw.ellipse(
            [(radius // 2, size[1] // 2 - radius), (radius // 2 + 2 * radius, size[1] // 2 + radius)],
            fill=colour,
        )
        self._centre_text(draw, size, name.upper(), _INK, left=radius // 2 + 2 * radius + 6)
        return image

    def _render_signature(self, size, evidence: ImageEvidence, context: str):
        """A synthetic squiggle, so the document still reads as signed.

        Deterministically derived from the surrogate name, so the same person
        signs the same way everywhere and re-runs are byte-identical.
        """
        from PIL import Image, ImageDraw

        name = self._surrogate_person(context) or "Redacted"
        image = Image.new("RGB", size, _PAPER)
        draw = ImageDraw.Draw(image)
        seed = int(hashlib.sha256(("sig" + name).encode("utf-8")).hexdigest()[:12], 16)
        width, height = size
        baseline = height * 0.62
        amplitude = height * 0.22
        # Four octaves with unrelated frequencies and seed-derived phases: one
        # smooth wave reads as a graph, several stacked read as handwriting.
        octaves = (
            (5.0, 1.00, seed % 97),
            (11.0, 0.55, seed // 97 % 89),
            (23.0, 0.28, seed // 8633 % 83),
            (47.0, 0.12, seed // 716539 % 79),
        )
        points = []
        steps = max(120, width // 2)
        for step in range(steps + 1):
            fraction = step / steps
            x = 0.08 * width + fraction * 0.84 * width
            wobble = sum(
                math.sin(fraction * frequency * math.pi + phase / 97.0 * math.tau) * amplitude * weight
                for frequency, weight, phase in octaves
            )
            taper = math.sin(fraction * math.pi) ** 0.35
            points.append((x, baseline - wobble * taper))
        draw.line(points, fill="#1c2b57", width=max(2, height // 40), joint="curve")
        draw.line(
            [(0.08 * width, baseline + amplitude * 0.9), (0.92 * width, baseline + amplitude * 0.9)],
            fill="#9aa3b0",
            width=1,
        )
        return image

    def _render_code(self, size, evidence: ImageEvidence):
        """Regenerate a *working* code that points at the surrogate URL."""
        from PIL import Image

        payload = evidence.codes[0] if evidence.codes else ""
        replacement = self._surrogate(Span(0, len(payload), URL, payload, "image-qr")) if payload else ""
        if not replacement:
            replacement = "https://example.com/redacted"
        try:
            import qrcode

            code = qrcode.make(replacement).convert("RGB")
            return code.resize(size)
        except Exception:
            return self._render_box(size, "REDACTED — CODE")

    # -- surrogate lookup ---------------------------------------------------
    def _surrogate_org(self, evidence: ImageEvidence, context: str) -> str:
        for text in (evidence.ocr_text, context):
            for span in self._spans(text or ""):
                if span.label == ORG:
                    return self._surrogate(span)
        return ""

    def _surrogate_person(self, context: str) -> str:
        for span in self._spans(context or ""):
            if span.label == PERSON:
                return self._surrogate(span)
        return ""

    @staticmethod
    def _centre_text(draw, size, text: str, colour: str, left: int = 0) -> None:
        from PIL import ImageFont

        font = ImageFont.load_default()
        try:
            box = draw.textbbox((0, 0), text, font=font)
            text_width, text_height = box[2] - box[0], box[3] - box[1]
        except Exception:
            text_width, text_height = len(text) * 6, 11
        available = size[0] - left
        if text_width > available and available > 0:
            keep = max(3, int(len(text) * available / max(1, text_width)) - 1)
            trimmed = text[:keep]
            # Prefer cutting at a word boundary: "REDSTONE WORKS" beats
            # "REDSTONE WORKS LIMIT".
            if " " in trimmed.strip():
                trimmed = trimmed.rsplit(" ", 1)[0]
            text = trimmed.strip()
            try:
                box = draw.textbbox((0, 0), text, font=font)
                text_width, text_height = box[2] - box[0], box[3] - box[1]
            except Exception:
                text_width = len(text) * 6
        x = left + max(0, (available - text_width) // 2)
        y = max(0, (size[1] - text_height) // 2)
        draw.text((x, y), text, fill=colour, font=font)

    # -- encoding ----------------------------------------------------------
    @staticmethod
    def encode(image, evidence: ImageEvidence) -> bytes | None:
        """Serialise the replacement in the original's format.

        The part name and content type are fixed by the package, so a PNG must
        stay a PNG.  Formats Pillow cannot write return ``None``, and the caller
        deletes the drawing instead.
        """
        target = evidence.format if evidence.format in WRITABLE_FORMATS else "PNG"
        buffer = io.BytesIO()
        candidate = image
        if target in ("JPEG", "BMP", "PPM") and candidate.mode != "RGB":
            candidate = candidate.convert("RGB")
        try:
            candidate.save(buffer, format=target)
        except Exception:
            try:
                buffer = io.BytesIO()
                candidate.convert("RGB").save(buffer, format="PNG")
            except Exception:
                return None
        return buffer.getvalue()

    # -- orchestration -----------------------------------------------------
    def redact(self, docx_file, refs=None) -> list[ImageDecision]:
        """Analyse and replace every picture; never raises for one bad image."""
        for ref in refs if refs is not None else docx_file.image_references():
            try:
                decision = self._redact_one(docx_file, ref)
            except Exception as error:  # a bad picture must not fail the document
                decision = ImageDecision(
                    name=ref.name,
                    label=IMAGE_UNCLASSIFIED,
                    evidence=ImageEvidence(name=ref.name, readable=False),
                    reason=f"analysis failed ({type(error).__name__}); removed to be safe",
                )
                try:
                    docx_file.remove_picture(ref)
                    decision.replaced, decision.action = True, "removed"
                except Exception:
                    decision.action = "FAILED"
            self.decisions.append(decision)
        return self.decisions

    def _redact_one(self, docx_file, ref) -> ImageDecision:
        blob = ref.part.blob
        evidence = self.analyse(blob, ref.name)
        context = ref.context
        label, reason = self.classify(evidence, context)

        decision = ImageDecision(name=ref.name, label=label, evidence=evidence, reason=reason)
        if not label:
            return decision
        if not self.policy.wants(label):
            decision.reason = f"{reason} (type not selected)"
            return decision
        if self.policy.keep_images:
            decision.reason = f"{reason} (--keep-images)"
            return decision

        replacement = self.render(label, evidence, context)
        encoded = self.encode(replacement, evidence)
        if encoded is None:
            removed = docx_file.remove_picture(ref)
            decision.replaced, decision.action = bool(removed), "removed"
            return decision
        ref.part._blob = encoded
        decision.replaced, decision.action = True, "replaced"
        return decision


def _is_document_sized(evidence: ImageEvidence) -> bool:
    """Enough text on the page to plausibly be a scanned document."""
    lines = [line for line in evidence.ocr_text.splitlines() if line.strip()]
    return len(lines) >= 4 and len(evidence.ocr_text) >= 30


def _looks_like_a_mark(evidence: ImageEvidence) -> bool:
    """A small, saturated graphic — a logo whose text OCR could not resolve.

    Logos are the one picture type that is routinely unreadable (stylised type,
    letters fused into a symbol), so shape is used where text failed.  Getting
    this wrong costs only the *appearance* of the replacement: an unclassified
    picture is replaced either way.
    """
    return (
        max(evidence.width, evidence.height) <= 600
        and evidence.saturation >= 25.0
        and evidence.ink_ratio >= 0.02
    )


def _hsv_to_rgb(hue: float, saturation: float, value: float) -> tuple[int, int, int]:
    import colorsys

    red, green, blue = colorsys.hsv_to_rgb((hue % 360) / 360.0, saturation, value)
    return int(red * 255), int(green * 255), int(blue * 255)
