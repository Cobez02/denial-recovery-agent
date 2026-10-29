"""Letter HTML and optional PDF rendering.

WeasyPrint is an optional dependency (the Docker image has it). When it cannot be imported or fails,
`render_pdf` returns None and the export package ships the HTML letter and notes that the PDF was
skipped. Only the exception class is logged: letter text never reaches a log.
"""

from __future__ import annotations

import html
import logging

log = logging.getLogger(__name__)

_CSS = (
    "body{font-family:Georgia,'Times New Roman',serif;font-size:12pt;line-height:1.4;color:#111;"
    "margin:1in;} h1{font-size:12pt;font-weight:normal;margin:0 0 1em 0;color:#444;} "
    "pre{font-family:inherit;font-size:inherit;white-space:pre-wrap;word-wrap:break-word;margin:0;}"
)


def letter_html(letter_text: str, title: str) -> str:
    """Simple standalone HTML: escaped text inside a pre-wrap block so line breaks are preserved."""
    safe_title = html.escape(title or "Appeal letter", quote=True)
    safe_text = html.escape(letter_text or "", quote=False)
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        f"<title>{safe_title}</title><style>{_CSS}</style></head>\n"
        f"<body><h1>{safe_title}</h1>\n<pre>{safe_text}</pre>\n</body></html>\n"
    )


def weasyprint_available() -> bool:
    try:
        import weasyprint  # noqa: F401
    except Exception:  # ImportError or a native-library failure at import time
        return False
    return True


def render_pdf(html_text: str) -> bytes | None:
    """PDF bytes via WeasyPrint, or None when it is unavailable or fails."""
    try:
        import weasyprint
    except Exception as exc:
        log.info("pdf rendering skipped: weasyprint unavailable (%s)", type(exc).__name__)
        return None
    try:
        return weasyprint.HTML(string=html_text).write_pdf()
    except Exception as exc:
        log.warning("pdf rendering failed: %s", type(exc).__name__)
        return None


__all__ = ["letter_html", "render_pdf", "weasyprint_available"]
