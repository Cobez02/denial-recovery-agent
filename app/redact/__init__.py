"""Redaction layer (milestone M5): identifier registry, tokenizer/restore, scanner, injection detector.

Only `app.llm.guard` may send text to a model, and only after `redact` and a `scan` pass.
"""

from app.redact.injection import detect_injection, is_suspicious, wrap_untrusted
from app.redact.registry import IdentifierRegistry, date_formats, name_variants
from app.redact.scanner import RedactionError, mask_generic, scan
from app.redact.tokenizer import TOKEN_RE, redact, restore, tokenize

__all__ = [
    "IdentifierRegistry",
    "RedactionError",
    "TOKEN_RE",
    "date_formats",
    "detect_injection",
    "is_suspicious",
    "mask_generic",
    "name_variants",
    "redact",
    "restore",
    "scan",
    "tokenize",
    "wrap_untrusted",
]
