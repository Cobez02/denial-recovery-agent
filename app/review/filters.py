"""Jinja2 environment for the HTML pages: filters, globals, flash messages and the `render` helper.

Autoescaping is on for every template, so payer letters, note excerpts and anything a person typed
are rendered as text, never as markup. The plain-text letter templates in app/templates/letters
belong to the drafting module and use their own environment.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader

from app.auth.deps import csrf_token

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
FLASH_KEY = "flash"

# Service error codes (ValueError messages) -> what the reviewer reads.
ERROR_MESSAGES: dict[str, str] = {
    "note_excerpt_required": "A quoted note excerpt is required before a coding change can be approved.",
    "invalid_transition": "That action is not allowed while the item is in its current status.",
    "reason_required": "A reason is required.",
    "text_required": "Enter the note text first.",
    "suspicious_text": "The note text looks like planted instructions and was not attached.",
    "no_proposal": "This work item has no proposal to act on.",
    "nothing_changed": "No field was changed.",
    "invalid_path": "One of the corrected-claim fields is not editable.",
    "invalid_value": (
        "One of the corrected-claim values is not valid: amounts and units must be numbers, "
        "dates YYYY-MM-DD, and the procedure code cannot be blank."
    ),
    "no_corrected_claim": "This proposal has no corrected claim to edit.",
    "invalid_date": "Enter the date as YYYY-MM-DD.",
    "file_too_large": "The file is larger than the 20 MB upload limit.",
    "empty_file": "The uploaded file is empty.",
    "no_clinic": "No clinic is configured: set CLINIC_ID or create exactly one clinic.",
    "job_failed": "The import job failed; see the audit log for the error class.",
}


def today_utc() -> date:
    return datetime.now(UTC).date()


def money(value: Any) -> str:
    """Money with thousands separators and two decimals; empty string for None."""
    if value is None or value == "":
        return ""
    try:
        return f"${Decimal(str(value)):,.2f}"
    except (InvalidOperation, ValueError):
        return str(value)


def fmt_date(value: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def fmt_datetime(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    return str(value)


def label(value: Any) -> str:
    """'medical_necessity' -> 'Medical necessity'."""
    if value is None:
        return ""
    text = str(value).replace("_", " ").strip()
    return text[:1].upper() + text[1:] if text else ""


def pct(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return str(value)


def badge_class(badge: str | None) -> str:
    if badge is None:
        return ""
    if badge == "overdue":
        return "badge badge-overdue"
    if badge.startswith("<=14"):
        return "badge badge-warn"
    return "badge badge-soon"


def status_class(status: Any) -> str:
    return f"status status-{str(status or '').replace('_', '-')}"


def flag_class(flag: Any) -> str:
    text = str(flag or "")
    if text in ("injection_flagged", "model_draft_rejected"):
        return "badge badge-danger"
    if text in ("coding_change", "payer_change_unverified", "unsupported_coding_change"):
        return "badge badge-warn"
    return "badge badge-muted"


def error_text(code: Any) -> str:
    if code is None:
        return ""
    return ERROR_MESSAGES.get(str(code), str(code))


def days_until(deadline: date | None, today: date | None = None) -> int | None:
    if deadline is None:
        return None
    return (deadline - (today or today_utc())).days


def join_list(value: Any, sep: str = ", ") -> str:
    if value is None:
        return ""
    if isinstance(value, list | tuple):
        return sep.join(str(x) for x in value)
    return str(value)


def build_environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters.update(
        {
            "money": money,
            "fmt_date": fmt_date,
            "fmt_datetime": fmt_datetime,
            "label": label,
            "pct": pct,
            "badge_class": badge_class,
            "status_class": status_class,
            "flag_class": flag_class,
            "error_text": error_text,
            "days_until": days_until,
            "join_list": join_list,
        }
    )
    env.globals["today_utc"] = today_utc
    return env


templates = Jinja2Templates(env=build_environment())


def flash(request: Request, text: str, level: str = "info") -> None:
    """Queue a one-time message for the next rendered page (kept in the session cookie)."""
    messages = list(request.session.get(FLASH_KEY) or [])
    messages.append({"level": level, "text": str(text)[:500]})
    request.session[FLASH_KEY] = messages[-5:]


def pop_flashes(request: Request) -> list[dict[str, str]]:
    messages = request.session.pop(FLASH_KEY, None) or []
    return [m for m in messages if isinstance(m, dict)]


def render(
    request: Request,
    name: str,
    context: dict[str, Any] | None = None,
    *,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> Response:
    """Render a page or partial with the standard context (csrf token, current user, flashes)."""
    ctx: dict[str, Any] = dict(context or {})
    ctx.setdefault("csrf_token", csrf_token(request))
    ctx.setdefault("current_user", getattr(request.state, "user", None))
    ctx.setdefault("flashes", pop_flashes(request))
    ctx.setdefault("today", today_utc())
    ctx.setdefault("is_hx", bool(request.headers.get("HX-Request")))
    return templates.TemplateResponse(request, name, ctx, status_code=status_code, headers=headers)


__all__ = [
    "ERROR_MESSAGES",
    "TEMPLATES_DIR",
    "badge_class",
    "build_environment",
    "days_until",
    "error_text",
    "flash",
    "fmt_date",
    "fmt_datetime",
    "label",
    "money",
    "pct",
    "pop_flashes",
    "render",
    "templates",
    "today_utc",
]
