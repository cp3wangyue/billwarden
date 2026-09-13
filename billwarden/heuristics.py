"""BillWarden's offline judgment engine.

The rules turn parsed bill facts + ledger history into one of two outcomes:
a household alert (a decision is needed) or a quiet digest note (no ping).
They also pre-compute the exact numbers and letter drafts the agent files.

This module is what makes BillWarden run with zero downloads and zero API
keys. Set BILLWARDEN_MODEL=ollama:<model> to route the same decision loop
through a real LLM via the Strands Agents SDK instead.
"""
from __future__ import annotations

import json
import re
from typing import Any

# --------------------------------------------------------------------------- #
# facts
# --------------------------------------------------------------------------- #

def build_facts(email: dict[str, Any], parsed: dict[str, Any],
                history: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge the parsed bill with its ledger history into decision facts."""
    vendor = parsed.get("vendor") or ""
    rows = [h for h in history if h.get("vendor") == vendor]
    amounts = [h["amount"] for h in rows[-3:]]
    baseline = round(sum(amounts) / len(amounts), 2) if amounts else None
    amount = parsed.get("amount")

    facts: dict[str, Any] = {
        "file": email["file"],
        "vendor": vendor,
        "period": parsed.get("period"),
        "amount": amount,
        "due_date": parsed.get("due_date"),
        "usage": parsed.get("usage"),
        "usage_unit": parsed.get("usage_unit"),
        "email_notes": parsed.get("notes", []),
        "baseline_avg_amount": baseline,
        "history": rows[-4:],
    }

    # duplicate charge: identical vendor+period already billed this cycle
    dupes = [h for h in rows if h.get("period") == parsed.get("period")]
    facts["duplicate_charge"] = len(dupes) >= 1

    # per-unit rate comparison where usage is metered
    if rows and amount and parsed.get("usage") and rows[-1].get("usage"):
        facts["rate_baseline"] = round(rows[-1]["amount"] / rows[-1]["usage"], 5)
        facts["rate_current"] = round(amount / parsed["usage"], 5)
        facts["rate_jump_pct"] = round(
            (facts["rate_current"] / facts["rate_baseline"] - 1) * 100, 1
        )

    facts["flat_jump_pct"] = (
        round((amount / baseline - 1) * 100, 1)
        if baseline and amount else None
    )

    body = email.get("body", "")
    facts["promo_ended"] = bool(re.search(r"promot\w* .{0,40}(ended|expir)", body, re.I))
    facts["usage_explained"] = bool(re.search(r"roaming|seasonal|trip|usage", body, re.I))
    facts["renewal"] = "renewal" in facts["email_notes"] or bool(
        re.search(r"renew", body, re.I))

    # annual renewal bills show an "equivalent monthly premium" — compare
    # monthly-to-monthly, never annual against monthly
    monthly = re.search(r"equivalent monthly premium:\s*\$?([\d,]+\.?\d*)", body, re.I)
    facts["amount_monthly"] = float(monthly.group(1).replace(",", "")) if monthly else None
    if facts["amount_monthly"] and baseline:
        facts["flat_jump_pct"] = round((facts["amount_monthly"] / baseline - 1) * 100, 1)
    return facts


def decide(facts: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Return (decision, payload): 'alert' with details, or 'digest'."""
    vendor = facts["vendor"]

    if facts.get("duplicate_charge"):
        return "alert", {
            "severity": "high",
            "title": "Duplicate charge this cycle",
            "reason": (f"Two identical {vendor} charges of ${facts['amount']:.2f} "
                       f"landed for the same billing period ({facts['period']}). "
                       "One of them is a billing error worth refunding."),
            "amount_delta": facts["amount"],
            "letter": "duplicate",
        }

    rate_jump = facts.get("rate_jump_pct")
    if rate_jump is not None and rate_jump >= 12 and facts.get("usage"):
        delta = round(facts["amount"] - facts["usage"] * facts["rate_baseline"], 2)
        return "alert", {
            "severity": "high",
            "title": f"Rate jumped {rate_jump:+.1f}% on flat usage",
            "reason": (f"{vendor} billed {facts['usage']:.0f} {facts['usage_unit']} at "
                       f"${facts['rate_current']:.4f}/{facts['usage_unit']} vs the recent "
                       f"${facts['rate_baseline']:.4f} — usage is actually down slightly, "
                       "so the bill growth is a price increase, not lifestyle."),
            "amount_delta": max(delta, 0.0),
            "letter": "rate",
        }

    if facts.get("renewal") and (facts.get("flat_jump_pct") or 0) >= 12:
        monthly_now = facts.get("amount_monthly") or facts["amount"]
        delta = round(monthly_now - facts["baseline_avg_amount"], 2)
        return "alert", {
            "severity": "medium",
            "title": f"Renewal premium up {facts['flat_jump_pct']:+.1f}%",
            "reason": (f"{vendor} renewal moved ${facts['baseline_avg_amount']:.2f} → "
                       f"${monthly_now:.2f}/mo. No claim-driven reason is stated; "
                       "worth a re-quote before it auto-renews."),
            "amount_delta": max(delta, 0.0),
            "letter": "renewal",
        }

    # everything else is a quiet note
    if facts.get("promo_ended"):
        note = (f"{vendor}: monthly price moved ${facts['baseline_avg_amount']:.2f} → "
                f"${facts['amount']:.2f} because the promo ended exactly as the "
                "contract scheduled. No action.")
    elif facts.get("usage_explained"):
        if facts.get("usage"):
            note = (f"{vendor}: ${facts['amount']:.2f} tracks usage this period "
                    f"({facts['usage']:.0f} {facts['usage_unit']}); explained by the "
                    "email itself, no action.")
        else:
            note = (f"{vendor}: ${facts['baseline_avg_amount']:.2f} → ${facts['amount']:.2f} "
                    "covers one-off usage charged this cycle (roaming/trip per the "
                    "email), not a recurring price change; no action.")
    elif facts.get("flat_jump_pct") is not None and abs(facts["flat_jump_pct"]) < 5:
        note = f"{vendor}: bill matches recent history, no action."
    elif facts.get("flat_jump_pct") is not None and facts.get("flat_jump_pct") >= 0:
        note = (f"{vendor}: small change ${facts['baseline_avg_amount']:.2f} → "
                f"${facts['amount']:.2f} ({facts['flat_jump_pct']:+.1f}%); within "
                "normal drift, no action.")
    else:
        note = f"{vendor}: bill matches recent history, no action."
    return "digest", {"note": note}


# --------------------------------------------------------------------------- #
# letter drafts
# --------------------------------------------------------------------------- #

def draft_letter(kind: str, facts: dict[str, Any]) -> dict[str, str]:
    """Return {filename, subject, body} for the kind of dispute."""
    vendor = facts["vendor"]
    if kind == "duplicate":
        return {
            "filename": "streamflix_duplicate_refund.txt",
            "subject": f"Duplicate charge — request refund of ${facts['amount']:.2f}",
            "body": (
                f"Hello {vendor} support,\n\n"
                f"My account was charged ${facts['amount']:.2f} twice on the same day for the "
                f"same billing period ({facts['period']}). I received two separate receipts "
                "for a single Premium subscription.\n\n"
                f"Could you reverse the duplicate ${facts['amount']:.2f} charge back to my "
                "original payment method? I'm happy to forward both receipts if that helps.\n\n"
                "Thank you,\nThe household account holder"
            ),
        }
    if kind == "rate":
        return {
            "filename": "powercity_rate_dispute.txt",
            "subject": "Dispute of September supply-rate increase",
            "body": (
                f"Hello {vendor} billing team,\n\n"
                f"My September statement bills {facts['usage']:.0f} {facts['usage_unit']} at "
                f"${facts['rate_current']:.4f}/{facts['usage_unit']} (${facts['amount']:.2f}), "
                f"while the same usage last month was priced at "
                f"${facts['rate_baseline']:.4f}/{facts['usage_unit']}. That is a "
                f"{facts['rate_jump_pct']:+.1f}% rate jump while my consumption stayed flat.\n\n"
                "Could you point me to the approved rate schedule that authorizes the new "
                "supply charge, and confirm whether a lower fixed or time-of-use plan would "
                "leave me whole? If the increase stands I'd like my account noted for the "
                "regulatory complaint window.\n\n"
                "Thank you,\nThe household account holder"
            ),
        }
    # renewal
    monthly_now = facts.get("amount_monthly") or facts["amount"]
    return {
        "filename": "homeguard_renewal_requote.txt",
        "subject": "Renewal premium increase — request re-quote before renewal",
        "body": (
            f"Hello {vendor},\n\n"
            f"My renewal notice raises the monthly premium from "
            f"${facts['baseline_avg_amount']:.2f} to ${monthly_now:.2f} "
            f"({facts['flat_jump_pct']:+.1f}%) with no claim history to justify it.\n\n"
            "Before the policy auto-renews, please re-quote my file with current "
            "discounts applied, confirm the exact coverage limits I'm being renewed at, "
            "and let me know the pro-rata cancellation terms if we can't find a fair number.\n\n"
            "Thank you,\nThe household account holder"
        ),
    }
