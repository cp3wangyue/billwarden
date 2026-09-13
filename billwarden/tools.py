"""BillWarden deterministic tools.

Every tool is a plain, testable function exposed to the Strands agent via
the @tool decorator. All money math and history lookups are deterministic
on purpose: the LLM never does arithmetic, it only *judges* and *drafts*.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

from strands import tool

DATA_DIR = Path(__file__).resolve().parent / "data"
DB_PATH = DATA_DIR / "history.db"
STATE_DIR = DATA_DIR / "runtime"
INBOX_DIR = DATA_DIR / "inbox"
PROCESSED_DIR = DATA_DIR / "processed"
LETTERS_DIR = DATA_DIR / "letters"
EVENTS_PATH = STATE_DIR / "events.jsonl"
STATE_PATH = STATE_DIR / "state.json"


def _ensure_dirs() -> None:
    for d in (DATA_DIR, STATE_DIR, INBOX_DIR, PROCESSED_DIR, LETTERS_DIR):
        d.mkdir(parents=True, exist_ok=True)


def log_event(kind: str, message: str, **extra: Any) -> None:
    """Append a UI event so the dashboard can show the agent working live."""
    _ensure_dirs()
    event = {"ts": date.today().isoformat(), "kind": kind, "message": message, **extra}
    with EVENTS_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def reset_runtime() -> None:
    """Clear runtime artifacts (events, alerts, letters, ledger) between runs."""
    _ensure_dirs()
    for p in [EVENTS_PATH, STATE_PATH]:
        if p.exists():
            p.unlink()
    for f in LETTERS_DIR.glob("*.txt"):
        f.unlink()
    conn = _db()
    conn.execute("DELETE FROM bills")
    conn.commit()
    conn.close()


# --------------------------------------------------------------------------- #
# Email / inbox tools
# --------------------------------------------------------------------------- #

@tool
def list_new_emails() -> list[dict[str, str]]:
    """Return the household billing emails waiting in the inbox.

    Each item contains `file`, `from`, `subject`, `received` and the full
    `body`. The inbox stays untouched; emails are archived only after the
    agent explicitly calls `archive_email`.
    """
    _ensure_dirs()
    emails = []
    for path in sorted(INBOX_DIR.glob("*.json")):
        emails.append(json.loads(path.read_text(encoding="utf-8")))
    log_event("inbox", f"Inbox scanned — {len(emails)} billing emails waiting")
    return emails


@tool
def archive_email(file: str) -> str:
    """Move an email file from the inbox to the processed archive.

    Args:
        file: file name of the email as returned by `list_new_emails`.
    """
    src = INBOX_DIR / Path(file).name
    dst = PROCESSED_DIR / Path(file).name
    if src.exists():
        src.replace(dst)
        log_event("inbox", f"Archived {Path(file).name}")
        return f"archived {file}"
    return f"file not found: {file}"


@tool
def parse_bill(text: str) -> dict[str, Any]:
    """Extract the key fields of a billing email into structured JSON.

    Parses vendor, billing period, amount due, due date and usage numbers
    from an email body using deterministic rules (no LLM guessing).

    Args:
        text: full body text of the billing email.
    """
    result: dict[str, Any] = {
        "vendor": None, "period": None, "amount": None,
        "due_date": None, "usage": None, "usage_unit": None, "notes": [],
    }

    vendor_match = re.search(r"Billing account:\s*(.+)", text)
    if vendor_match:
        result["vendor"] = vendor_match.group(1).strip()

    amount_match = re.search(r"(?:Amount due|Total due|New balance)[:\s]*\$?\s*([\d,]+\.\d{2})", text)
    if amount_match:
        result["amount"] = float(amount_match.group(1).replace(",", ""))

    period_match = re.search(r"Billing period[:\s]*([A-Za-z0-9 ,/-]+)", text)
    if period_match:
        result["period"] = period_match.group(1).strip()

    due_match = re.search(r"(?:Due by|Due date)[:\s]*([A-Za-z]+ \d{1,2}, \d{4})", text)
    if due_match:
        result["due_date"] = due_match.group(1)

    usage_match = re.search(r"Usage this period[:\s]*([\d,\.]+)\s*(kWh|GB|MB|gal|minutes|m³)", text, re.I)
    if usage_match:
        result["usage"] = float(usage_match.group(1).replace(",", ""))
        result["usage_unit"] = usage_match.group(2).lower()

    for note_pat in (r"late fee", r"price increase", r"rate change", r"promotion", r"promo",
                     r"renewal", r"auto-pay", r"roaming", r"seasonal", r"duplicate"):
        if re.search(note_pat, text, re.I):
            result["notes"].append(note_pat)

    return result


# --------------------------------------------------------------------------- #
# History / ledger tools
# --------------------------------------------------------------------------- #

def _db() -> sqlite3.Connection:
    _ensure_dirs()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE IF NOT EXISTS bills(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vendor TEXT NOT NULL,
            period TEXT NOT NULL,
            amount REAL NOT NULL,
            usage REAL,
            usage_unit TEXT,
            notes TEXT
        )"""
    )
    return conn


@tool
def get_bill_history(vendor: str) -> list[dict[str, Any]]:
    """Return the stored billing history (past periods) for one vendor.

    Args:
        vendor: vendor name as shown on the bill (e.g. "PowerCity Electric").
    """
    conn = _db()
    rows = conn.execute(
        "SELECT vendor, period, amount, usage, usage_unit, notes FROM bills "
        "WHERE vendor = ? ORDER BY id", (vendor,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@tool
def record_bill(vendor: str, period: str, amount: float,
                usage: float | None = None, usage_unit: str | None = None,
                notes: str | None = None) -> str:
    """Store a processed bill in the household ledger so future audits can compare.

    Args:
        vendor: vendor name exactly as billed.
        period: billing period label (e.g. "Aug 2026").
        amount: amount due in USD.
        usage: usage number for the period, if the bill reports one.
        usage_unit: unit for the usage value (kWh, GB, gal, minutes).
        notes: free-form notes worth keeping for next audit.
    """
    conn = _db()
    conn.execute(
        "INSERT INTO bills(vendor, period, amount, usage, usage_unit, notes) VALUES(?,?,?,?,?,?)",
        (vendor, period, amount, usage, usage_unit, notes),
    )
    conn.commit()
    conn.close()
    log_event("ledger", f"Ledger updated — {vendor} {period}: ${amount:.2f}")
    return "recorded"


# --------------------------------------------------------------------------- #
# Output tools (alerts, digest, letters)
# --------------------------------------------------------------------------- #

@tool
def raise_alert(vendor: str, severity: str, title: str, reason: str,
                amount_delta: float) -> str:
    """Raise a household alert that requires a human decision.

    Use this ONLY when the household genuinely needs to act (a price hike,
    a duplicate charge, a suspicious fee). Expected seasonal or usage-driven
    changes belong in `add_digest_note` instead.

    Args:
        vendor: vendor the alert is about.
        severity: "high" for money leaking now, "medium" for upcoming renewals.
        title: short alert headline shown on the dashboard.
        reason: 1-3 sentence explanation grounded in the parsed bill + history.
        amount_delta: how many dollars per month this issue costs vs before
            (positive means extra cost).
    """
    _ensure_dirs()
    state = _read_state()
    state.setdefault("alerts", []).append({
        "vendor": vendor, "severity": severity, "title": title,
        "reason": reason, "amount_delta": round(amount_delta, 2),
    })
    _write_state(state)
    log_event("alert", f"ALERT [{severity}] {vendor}: {title} (${amount_delta:+.2f}/mo)")
    return "alert raised"


@tool
def add_digest_note(note: str) -> str:
    """Add a quiet, no-action-needed line to the weekly digest.

    Use this for expected changes (seasonal usage, promo ending per
    contract) so the household stays informed without being interrupted.

    Args:
        note: one sentence summary, include vendor and numbers.
    """
    _ensure_dirs()
    state = _read_state()
    state.setdefault("digest", []).append(note)
    _write_state(state)
    log_event("digest", f"Digest note: {note}")
    return "digest updated"


@tool
def save_letter(filename: str, vendor: str, subject: str, body: str) -> str:
    """Save a ready-to-send draft letter/email about a billing issue.

    Args:
        filename: short file name ending in .txt (e.g. powercity_dispute.txt).
        vendor: vendor the letter is addressed to.
        subject: email subject line for the draft.
        body: the full letter body, polite and factual, referencing bill
              numbers, periods and amounts.
    """
    _ensure_dirs()
    path = LETTERS_DIR / Path(filename).name
    path.write_text(f"To: {vendor}\nSubject: {subject}\n\n{body}\n", encoding="utf-8")
    state = _read_state()
    state.setdefault("letters", []).append({"file": path.name, "vendor": vendor, "subject": subject})
    _write_state(state)
    log_event("letter", f"Draft saved: {subject} → {vendor}")
    return f"saved {path.name}"


# --------------------------------------------------------------------------- #
# state helpers
# --------------------------------------------------------------------------- #

def _read_state() -> dict[str, Any]:
    _ensure_dirs()
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"alerts": [], "digest": [], "letters": []}


def _write_state(state: dict[str, Any]) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def summary_totals() -> dict[str, Any]:
    """Convenience helper used by the dashboard header."""
    state = _read_state()
    leak = sum(a.get("amount_delta", 0.0) for a in state.get("alerts", []))
    return {
        "alerts": state.get("alerts", []),
        "digest": state.get("digest", []),
        "letters": state.get("letters", []),
        "monthly_leak": round(leak, 2),
        "summary": state.get("summary", ""),
    }
