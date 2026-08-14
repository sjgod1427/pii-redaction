"""Two-pass redaction pipeline.

Pass 1 reads the whole document and learns who and what it talks about.
Pass 2 detects, resolves overlaps, generates surrogates and writes them back.
"""

from __future__ import annotations

import csv
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .addresses import detect_addresses
from .config import Policy
from .detectors import detect_dates_of_birth, detect_structured
from .docx_io import DocxFile
from .gazetteer import Gazetteer, _is_org_name
from .images import ImageRedactor
from .ner import NerTagger
from .surrogates import SurrogateFactory
from .types import EMAIL, ORG, URL, Detection, Span, merge_spans


@dataclass
class RunReport:
    counts: dict[str, int] = field(default_factory=dict)
    entities: dict[str, int] = field(default_factory=dict)
    paragraphs: int = 0
    paragraphs_changed: int = 0
    seconds: float = 0.0
    ner_model: str = ""
    images_scanned: int = 0
    images_replaced: int = 0
    image_counts: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "detections_by_type": dict(sorted(self.counts.items(), key=lambda kv: -kv[1])),
            "total_detections": sum(self.counts.values()),
            "entities_learned": self.entities,
            "paragraphs_scanned": self.paragraphs,
            "paragraphs_changed": self.paragraphs_changed,
            "images_scanned": self.images_scanned,
            "images_replaced": self.images_replaced,
            "images_by_type": dict(sorted(self.image_counts.items(), key=lambda kv: -kv[1])),
            "ner_model": self.ner_model,
            "runtime_seconds": round(self.seconds, 1),
        }


class Redactor:
    def __init__(self, policy: Policy) -> None:
        self.policy = policy
        self.gazetteer = Gazetteer(policy)
        self.surrogates = SurrogateFactory(policy)
        self.ner = NerTagger(policy)
        self.detections: list[Detection] = []
        self._block_parts: list[str] = []
        self.images = ImageRedactor(policy, self.detect_text, self.surrogates.replacement)

    def detect_text(self, text: str) -> list[Span]:
        """Run the full text stack over a standalone string.

        Used for text lifted out of pictures, so an image is classified by the
        same detectors, gazetteer and NER that classify a paragraph.
        """
        if not text or not text.strip():
            return []
        ner_spans = None
        if self.ner.available:
            try:
                ner_spans = list(self.ner.pipe([text]))[0]
            except Exception:
                ner_spans = None
        return self.detect(text, "", ner_spans)

    # -- detection ---------------------------------------------------------
    def detect(self, text: str, section: str = "", ner_spans: list[Span] | None = None) -> list[Span]:
        spans: list[Span] = []
        if not text.strip():
            return spans

        spans.extend(s for s in detect_structured(text, section) if self.policy.wants(s.label))
        spans.extend(
            s for s in detect_dates_of_birth(text, self.policy.redact_all_dates) if self.policy.wants(s.label)
        )
        spans.extend(s for s in detect_addresses(text, section) if self.policy.wants(s.label))
        spans.extend(self.gazetteer.spans(text))
        if self.policy.redact_locations:
            spans.extend(self.gazetteer.place_spans(text))

        for span in ner_spans or []:
            if not self.policy.wants(span.label):
                continue
            # Raw model output goes through the same document-level filters that
            # the gazetteer applied in pass 1 — otherwise every candidate the
            # gazetteer deliberately rejected would come back in through NER.
            if not self.gazetteer.accepts_ner_candidate(span.text, span.label):
                continue
            if span.label == ORG and not _is_org_name(span.text, self.policy):
                continue
            spans.append(span)

        return merge_spans(spans)

    # -- full document -----------------------------------------------------
    def run(self, source: str, destination: str, progress=None) -> RunReport:
        """Redact ``source`` into ``destination``.

        ``progress(stage, fraction, detail)`` is called at stage boundaries so a
        caller can report real work rather than an invented timer; it is optional
        and any exception it raises is ignored, since reporting must never be
        able to fail a redaction.
        """

        def report_progress(stage: str, fraction: float, detail: str = "") -> None:
            if progress is None:
                return
            try:
                progress(stage, fraction, detail)
            except Exception:
                pass

        started = time.time()
        report = RunReport(ner_model=self.policy.spacy_model if self.ner.available else "disabled")

        report_progress("read", 0.0, "opening document")
        docx_file = DocxFile(source)
        texts = [paragraph.text for paragraph in docx_file.paragraphs]
        self._block_parts = [paragraph.part for paragraph in docx_file.paragraphs]
        report.paragraphs = len(texts)

        # --- pass 1: learn the document's entities --------------------------
        report_progress("learn", 0.0, f"{len(texts)} blocks")
        ner_results = list(self.ner.pipe(texts))
        for text in texts:
            self.gazetteer.observe_vocabulary(text)
        for text, spans in zip(texts, ner_results):
            self.gazetteer.learn_block(text, spans)
        for header, cells in _table_rows(docx_file):
            self.gazetteer.learn_table_row(header, cells)
        if self.policy.extra_terms:
            self.gazetteer.add_manual(self.policy.extra_terms)
        self.gazetteer.finalise()
        # Decide one address format for the whole document before any surrogate is
        # generated, so fragments without a country cue match the rest.
        self.surrogates.set_document_locale(self.gazetteer.place_names.values())
        self.surrogates.bind(self.gazetteer)
        report.entities = self.gazetteer.summary()

        # Snapshot each picture's caption while the text is still the original.
        image_refs = [] if self.policy.disable_images else [
            ref.freeze() for ref in docx_file.image_references()
        ]

        # --- pass 2: detect, substitute, write back -------------------------
        report_progress(
            "detect", 0.0,
            f"{report.entities.get('people', 0)} people, {report.entities.get('companies', 0)} companies",
        )
        step = max(1, len(docx_file.paragraphs) // 20)
        for index, paragraph in enumerate(docx_file.paragraphs):
            if index % step == 0:
                report_progress("detect", index / max(1, len(docx_file.paragraphs)))
            spans = self.detect(texts[index], paragraph.section, ner_results[index])
            if not spans:
                continue
            edits = []
            for span in spans:
                replacement = self.surrogates.replacement(span)
                edits.append((span.start, span.end, replacement))
                self.detections.append(Detection(index, span, replacement))
                report.counts[span.label] = report.counts.get(span.label, 0) + 1
            if paragraph.apply(edits):
                report.paragraphs_changed += 1

        self._redact_across_paragraph_breaks(docx_file, report)
        self._redact_field_codes(docx_file)
        docx_file.rewrite_hyperlinks(self._rewrite_link)

        # --- pictures --------------------------------------------------------
        # Runs last so that logos and QR codes resolve to the same surrogates the
        # prose already used.
        if not self.policy.disable_images:
            report_progress("images", 0.0, f"{len(image_refs)} embedded")
            for decision in self.images.redact(docx_file, image_refs):
                report.images_scanned += 1
                if decision.replaced:
                    report.images_replaced += 1
                    report.image_counts[decision.label] = report.image_counts.get(decision.label, 0) + 1

        if self.policy.scrub_metadata:
            docx_file.scrub_metadata()

        report_progress("write", 0.0, "writing document")
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        docx_file.save(destination)
        report.seconds = time.time() - started
        report_progress("write", 1.0, f"{report.seconds:.1f}s")
        return report

    def _redact_across_paragraph_breaks(self, docx_file: DocxFile, report: RunReport) -> None:
        """Catch entities split over a paragraph boundary.

        Word sometimes breaks a name in half ("KSH" / "Distriparks Private
        Limited" in adjacent paragraphs of a table cell).  Each half looks
        harmless on its own, so the pair is re-scanned as one string and any
        match that straddles the join is redacted in both paragraphs.
        """
        paragraphs = docx_file.paragraphs
        for index in range(len(paragraphs) - 1):
            first, second = paragraphs[index], paragraphs[index + 1]
            head, tail = first.text, second.text
            if not head.strip() or not tail.strip() or len(head) + len(tail) > 400:
                continue
            joined = f"{head} {tail}"
            boundary = len(head)
            for span in self.gazetteer.spans(joined):
                if not (span.start < boundary < span.end):
                    continue
                replacement = self.surrogates.replacement(span)
                first.apply([(span.start, boundary, replacement)])
                second.apply([(0, span.end - boundary - 1, "")])
                self.detections.append(Detection(index, span, replacement))
                report.counts[span.label] = report.counts.get(span.label, 0) + 1

    def _redact_field_codes(self, docx_file: DocxFile) -> None:
        """Field instructions (e.g. HYPERLINK "mailto:…") hold PII too."""
        for node in docx_file.field_texts():
            text = node.text or ""
            if not text.strip():
                continue
            spans = merge_spans(list(detect_structured(text)))
            if not spans:
                continue
            for span in sorted(spans, key=lambda s: s.start, reverse=True):
                replacement = self.surrogates.replacement(span)
                text = text[: span.start] + replacement + text[span.end :]
            node.text = text

    def _rewrite_link(self, target: str) -> str:
        if target.lower().startswith("mailto:"):
            address = target[7:]
            return "mailto:" + self.surrogates.replacement(
                Span(0, len(address), EMAIL, address, "hyperlink")
            )
        if re.match(r"(?i)^https?://", target):
            return self.surrogates.replacement(Span(0, len(target), URL, target, "hyperlink"))
        return target

    # -- artefacts ---------------------------------------------------------
    def write_mapping(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(self.surrogates.as_mapping(), handle, indent=2, ensure_ascii=False, sort_keys=True)

    def _part_of(self, block_id: int) -> str:
        return self._block_parts[block_id] if 0 <= block_id < len(self._block_parts) else "?"

    def write_detections(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            # "block" is an internal index over non-empty paragraphs and does not
            # correspond to anything visible in Word, so "part" and "find_in_output"
            # are given alongside it: searching the redacted document for the
            # replacement string is how a reviewer actually locates a detection.
            writer.writerow(
                ["block", "part", "label", "original", "replacement", "find_in_output", "detector", "score"]
            )
            for item in self.detections:
                writer.writerow(
                    [
                        item.block_id,
                        self._part_of(item.block_id),
                        item.span.label,
                        " ".join(item.span.text.split()),
                        item.replacement,
                        " ".join(item.replacement.split())[:60],
                        item.span.source,
                        f"{item.span.score:.2f}",
                    ]
                )
            # Pictures are audited in the same file: one row per embedded image,
            # carrying the evidence that decided its fate so a reviewer can see
            # why a picture was replaced — or why it was left alone.
            for decision in self.images.decisions:
                writer.writerow(
                    [
                        decision.name,
                        "media",
                        decision.label or "IMAGE_CLEAN",
                        decision.evidence.summary(),
                        decision.action,
                        "",
                        f"image:{decision.reason}",
                        "1.00",
                    ]
                )


def _table_rows(docx_file: DocxFile):
    """Yield (header row text, [cell texts]) for every table row in the body."""
    from .docx_io import W, _cell_text

    for table in docx_file.document.element.body.iter(f"{W}tbl"):
        rows = table.findall(f"{W}tr")
        if len(rows) < 2:
            continue
        header = " | ".join(_cell_text(tc) for tc in rows[0].findall(f"{W}tc"))
        for row in rows[1:]:
            yield header, [_cell_text(tc) for tc in row.findall(f"{W}tc")]
