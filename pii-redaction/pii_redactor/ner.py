"""Thin wrapper around spaCy's named-entity recogniser.

The model is used as a *candidate generator* only: everything it proposes is
filtered by config rules and, for people and companies, is cross-checked
against the document-level gazetteer.  That keeps the well-known weaknesses of
a general-purpose English model on Indian names from leaking into the output.
"""

from __future__ import annotations

from typing import Iterable, Iterator

from .config import INSTITUTION_ALLOWLIST, INDIAN_STATES, Policy
from .types import ORG, PERSON, Span, normalise

# spaCy labels we care about -> our labels.  PLACE is not PII on its own (a
# city name tells you nothing); it is collected so that place names can be kept
# out of the person/company lexicon.
PLACE = "PLACE"
_LABEL_MAP = {"PERSON": PERSON, "ORG": ORG, "GPE": PLACE, "LOC": PLACE, "FAC": PLACE}


class NerTagger:
    """Loads a spaCy pipeline lazily; degrades to a no-op if it is unavailable."""

    def __init__(self, policy: Policy) -> None:
        self.policy = policy
        self._nlp = None
        self.available = False
        if policy.disable_ner:
            return
        try:  # pragma: no cover - depends on local install
            import spacy

            try:
                self._nlp = spacy.load(policy.spacy_model, exclude=["lemmatizer"])
            except OSError:
                self._nlp = spacy.load("en_core_web_sm", exclude=["lemmatizer"])
            self._nlp.max_length = 2_000_000
            self.available = True
        except Exception:  # pragma: no cover - spaCy not installed
            self.available = False

    def pipe(self, texts: Iterable[str]) -> Iterator[list[Span]]:
        """Yield spans per input text (empty lists when NER is unavailable)."""
        texts = list(texts)
        if not self.available:
            for _ in texts:
                yield []
            return
        for doc in self._nlp.pipe(texts, batch_size=64):
            spans: list[Span] = []
            for ent in doc.ents:
                label = _LABEL_MAP.get(ent.label_)
                if label is None:
                    continue
                text = ent.text.strip()
                if not text or len(text) < 3:
                    continue
                key = normalise(text)
                if label != PLACE:
                    if key in INSTITUTION_ALLOWLIST and not self.policy.redact_institutions:
                        continue
                    if key in INDIAN_STATES:
                        continue
                start = ent.start_char + (len(ent.text) - len(ent.text.lstrip()))
                spans.append(
                    Span(start, start + len(text), label, text, f"ner:{ent.label_}", 0.6)
                )
            yield spans
