"""A hybrid (rule + gazetteer + NER) PII redaction tool for Word documents."""

from .config import Policy
from .pipeline import Redactor, RunReport
from .types import ALL_LABELS, Span

__all__ = ["Policy", "Redactor", "RunReport", "Span", "ALL_LABELS"]
__version__ = "1.0.0"
