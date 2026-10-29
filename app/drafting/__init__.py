"""Drafting (milestone M6): corrected-claim builder with before/after diff, appeal letters from
templates plus claim facts and quoted note excerpts, fact check for model rewrites, optional PDF.

Modules:
- `corrected_claim`: `claim_to_export_dict`, `build_corrected_claim`, `corrected_claim_to_csv_rows`, `render_diff`
- `appeal`: `draft_appeal(ctx, classification, draft, guard_call=None) -> (letter_text, flags)`
- `factcheck`: `extract_facts`, `verify`
- `letters`: Jinja environment and block rendering for app/templates/letters/
- `pdf`: `letter_html`, `render_pdf` (WeasyPrint optional)
"""

from __future__ import annotations

__all__ = ["appeal", "corrected_claim", "factcheck", "letters", "pdf"]
