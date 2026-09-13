"""Seed a realistic household billing inbox + 6 months of bill history.

The history is what lets the agent tell an expected change from a leak:
the same LLM judgment is applied to a price hike, a duplicate charge, a
renewal jump — and to three changes that are actually fine.
"""
from __future__ import annotations

import json
from pathlib import Path

from .tools import INBOX_DIR, PROCESSED_DIR, _db, _ensure_dirs

# --------------------------------------------------------------------------- #
# 6 months of history (Mar–Aug 2026)
# --------------------------------------------------------------------------- #

HISTORY = [
    # vendor, period, amount, usage, unit, notes
    ("PowerCity Electric", "Mar 2026", 142.10, 812, "kWh", None),
    ("PowerCity Electric", "Apr 2026", 138.44, 795, "kWh", None),
    ("PowerCity Electric", "May 2026", 126.20, 724, "kWh", None),
    ("PowerCity Electric", "Jun 2026", 121.80, 699, "kWh", None),
    ("PowerCity Electric", "Jul 2026", 139.35, 801, "kWh", None),
    ("PowerCity Electric", "Aug 2026", 151.02, 872, "kWh", None),
    ("NetLink Fiber",      "Mar 2026", 65.00,  None, None, "promo pricing $65"),
    ("NetLink Fiber",      "Apr 2026", 65.00,  None, None, None),
    ("NetLink Fiber",      "May 2026", 65.00,  None, None, None),
    ("NetLink Fiber",      "Jun 2026", 65.00,  None, None, None),
    ("NetLink Fiber",      "Jul 2026", 65.00,  None, None, None),
    ("NetLink Fiber",      "Aug 2026", 65.00,  None, None, None),
    ("StreamFlix",         "Mar 2026", 15.99,  None, None, None),
    ("StreamFlix",         "Apr 2026", 15.99,  None, None, None),
    ("StreamFlix",         "May 2026", 15.99,  None, None, None),
    ("StreamFlix",         "Jun 2026", 15.99,  None, None, None),
    ("StreamFlix",         "Jul 2026", 15.99,  None, None, None),
    ("StreamFlix",         "Aug 2026", 15.99,  None, None, None),
    ("HomeGuard Insurance","Mar 2026", 112.50, None, None, None),
    ("HomeGuard Insurance","Apr 2026", 112.50, None, None, None),
    ("HomeGuard Insurance","May 2026", 112.50, None, None, None),
    ("HomeGuard Insurance","Jun 2026", 112.50, None, None, None),
    ("HomeGuard Insurance","Jul 2026", 112.50, None, None, None),
    ("HomeGuard Insurance","Aug 2026", 112.50, None, None, None),
    ("MobileOne Wireless", "Mar 2026", 48.00,  None, None, None),
    ("MobileOne Wireless", "Apr 2026", 48.00,  None, None, None),
    ("MobileOne Wireless", "May 2026", 48.00,  None, None, None),
    ("MobileOne Wireless", "Jun 2026", 48.00,  None, None, None),
    ("MobileOne Wireless", "Jul 2026", 48.00,  None, None, None),
    ("MobileOne Wireless", "Aug 2026", 48.00,  None, None, None),
    ("AquaCity Water",     "Mar 2026", 31.60,  4100, "gal", None),
    ("AquaCity Water",     "Apr 2026", 29.85,  3800, "gal", None),
    ("AquaCity Water",     "May 2026", 33.40,  4300, "gal", None),
    ("AquaCity Water",     "Jun 2026", 44.10,  5900, "gal", None),
    ("AquaCity Water",     "Jul 2026", 51.75,  7050, "gal", None),
    ("AquaCity Water",     "Aug 2026", 49.20,  6650, "gal", None),
]

# --------------------------------------------------------------------------- #
# September 2026 inbox — 6 new emails, 3 need action, 3 are fine
# --------------------------------------------------------------------------- #

INBOX = [
    {
        "file": "powercity_sep.json",
        "from": "billing@powercityelectric.example",
        "subject": "Your September statement — PowerCity Electric",
        "received": "2026-09-11",
        "body": (
            "PowerCity Electric — Billing account: PowerCity Electric\n"
            "Billing period: Sep 1 - Sep 30 2026 (estimate)\n"
            "Amount due: $208.15\nDue by: October 3, 2026\n"
            "Usage this period: 858 kWh\n\n"
            "Dear customer, your monthly statement is ready. Effective this cycle, "
            "PowerCity adopted the new regional supply rate schedule. A rate change "
            "of our Standard Plan to $0.2426/kWh reflects updated capacity charges.\n\n"
            "You are enrolled in auto-pay. No action is needed unless you dispute this bill."
        ),
    },
    {
        "file": "netlink_sep.json",
        "from": "no-reply@netlinkfiber.example",
        "subject": "NetLink Fiber invoice — October",
        "received": "2026-09-12",
        "body": (
            "NetLink Fiber — Billing account: NetLink Fiber\n"
            "Billing period: Oct 2026\n"
            "Amount due: $70.00\nDue by: October 1, 2026\n\n"
            "Your 24-month promotional pricing has ended as scheduled in your service "
            "agreement. Your monthly rate returns to the standard $70.00. "
            "You are enrolled in auto-pay."
        ),
    },
    {
        "file": "streamflix_sep.json",
        "from": "receipts@streamflix.example",
        "subject": "Your StreamFlix receipt — $15.99",
        "received": "2026-09-12",
        "body": (
            "StreamFlix — Billing account: StreamFlix Premium\n"
            "Billing period: Sep 2026\n"
            "Amount due: $15.99\nDue by: September 12, 2026 (charged)\n\n"
            "Thanks for being a Premium member. This receipt covers your monthly "
            "subscription renewal."
        ),
    },
    {
        "file": "streamflix_sep_dupe.json",
        "from": "receipts@streamflix.example",
        "subject": "[Receipt] StreamFlix Premium — $15.99",
        "received": "2026-09-12",
        "body": (
            "StreamFlix — Billing account: StreamFlix Premium\n"
            "Billing period: Sep 2026\n"
            "Amount due: $15.99\nDue by: September 12, 2026 (charged)\n\n"
            "Thanks for being a Premium member. This receipt covers your monthly "
            "subscription renewal."
        ),
    },
    {
        "file": "homeguard_sep.json",
        "from": "policy@homeguardinsurance.example",
        "subject": "Your HomeGuard policy renews soon — new premium",
        "received": "2026-09-10",
        "body": (
            "HomeGuard Insurance — Billing account: HomeGuard Insurance\n"
            "Billing period: Oct 2026 - Sep 2027 annual renewal\n"
            "Amount due: $1647.00\nDue by: October 15, 2026\n"
            "(equivalent monthly premium: $137.25)\n\n"
            "Your policy renews automatically. After our annual review, your renewal "
            "premium reflects updated regional claim costs — a price increase from "
            "$112.50 to $137.25 per month. You may cancel within 30 days for a pro-rata refund."
        ),
    },
    {
        "file": "mobileone_sep.json",
        "from": "billing@mobileone.example",
        "subject": "MobileOne September bill — $63.50",
        "received": "2026-09-11",
        "body": (
            "MobileOne Wireless — Billing account: MobileOne Wireless\n"
            "Billing period: Sep 2026\n"
            "Amount due: $63.50\nDue by: October 5, 2026\n\n"
            "Your plan ($48.00) plus $15.50 of July international roaming usage "
            "charged on this cycle. See trip summary: 1.2 GB roaming, 48 minutes."
        ),
    },
    {
        "file": "aquacity_sep.json",
        "from": "service@aquacitywater.example",
        "subject": "AquaCity Water — September statement",
        "received": "2026-09-11",
        "body": (
            "AquaCity Water — Billing account: AquaCity Water\n"
            "Billing period: Sep 2026\n"
            "Amount due: $47.30\nDue by: October 8, 2026\n"
            "Usage this period: 6900 gal\n\n"
            "Seasonal rate tier applies during summer months. You are enrolled in auto-pay."
        ),
    },
]


def seed(force: bool = False) -> None:
    """(Re)create inbox emails and history DB."""
    _ensure_dirs()
    if force:
        for d in (INBOX_DIR, PROCESSED_DIR):
            for f in d.glob("*.json"):
                f.unlink()
    if force or not any(INBOX_DIR.glob("*.json")):
        for email in INBOX:
            (INBOX_DIR / email["file"]).write_text(
                json.dumps(email, ensure_ascii=False, indent=1), encoding="utf-8"
            )
    conn = _db()
    n = conn.execute("SELECT COUNT(*) FROM bills").fetchone()[0]
    if force:
        conn.execute("DELETE FROM bills")
        n = 0
    if n == 0:
        conn.executemany(
            "INSERT INTO bills(vendor, period, amount, usage, usage_unit, notes) VALUES(?,?,?,?,?,?)",
            HISTORY,
        )
    conn.commit()
    conn.close()


if __name__ == "__main__":
    seed(force=True)
    print("seeded:", len(INBOX), "inbox emails,", len(HISTORY), "history rows")
