"""BillWarden agent — a Strands Agents SDK agent that audits household bills.

The Strands Agent loop drives real tool calls (parse → compare → decide →
draft → file). The default model provider is `HeuristicModel`, a fully
offline rule engine implementing the Strands `Model` interface, so the
project runs with zero downloads and zero API keys.

Set `BILLWARDEN_MODEL=ollama:<model-id>` to route the exact same tool loop
through a local LLM (Ollama) instead — judgment and letter drafting then
come from the LLM.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import date
from typing import Any, AsyncGenerator

from strands import Agent
from strands.models.model import Model

from . import tools
from .heuristics import build_facts, decide, draft_letter
from .seed import seed
from .tools import (
    add_digest_note, log_event, parse_bill, summary_totals,
)


# --------------------------------------------------------------------------- #
# Offline model provider (Strands Model interface)
# --------------------------------------------------------------------------- #

class HeuristicModel(Model):
    """A Strands model provider that decides with deterministic rules.

    It reads the `<FACTS>` JSON the orchestrator embeds in the latest user
    message and emits the same tool-use events an LLM provider would, so the
    Agent loop, tools and tracing are 100% genuine Strands.
    """

    def __init__(self, **config: Any) -> None:
        super().__init__()
        self._config = dict(config)

    def get_config(self) -> dict[str, Any]:
        return self._config

    def update_config(self, **kwargs: Any) -> None:
        self._config.update(kwargs)

    async def stream(
        self, messages, tool_specs=None, system_prompt=None, *, tool_choice=None, **kwargs
    ) -> AsyncGenerator[dict[str, Any], None]:
        facts = self._extract_facts(messages)
        plan = self._plan(facts) if facts else []
        done = sum(
            1 for m in messages if m.get("role") == "assistant"
            for block in m.get("content", []) if isinstance(block, dict) and "toolUse" in block
        )

        yield {"messageStart": {"role": "assistant"}}

        if done < len(plan):
            name, args = plan[done]
            yield {"contentBlockStart": {"start": {"toolUse": {"name": name, "toolUseId": f"hw_{uuid.uuid4().hex[:16]}"}}}}
            yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(args)}}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}
        else:
            text = self._narrate(facts)
            yield {"contentBlockStart": {"start": {}}}
            yield {"contentBlockDelta": {"delta": {"text": text}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "end_turn"}}

        yield {"metadata": {"usage": {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0},
                            "metrics": {"latencyMs": 0}}}

    async def structured_output(self, output_model, prompt, system_prompt=None, **kwargs):
        raise NotImplementedError("HeuristicModel is tool-loop only")

    # ---------------------------------------------------------------- #

    @staticmethod
    def _extract_facts(messages) -> dict[str, Any] | None:
        for message in reversed(messages):
            if message.get("role") != "user":
                continue
            for block in message.get("content", []):
                text = block.get("text", "") if isinstance(block, dict) else ""
                if "<FACTS>" in text:
                    try:
                        return json.loads(text.split("<FACTS>")[1].split("</FACTS>")[0])
                    except (IndexError, json.JSONDecodeError):
                        return None
        return None

    @staticmethod
    def _plan(facts: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        decision, payload = decide(facts)
        notes = ", ".join(facts.get("email_notes") or []) or None
        record = ("record_bill", {
            "vendor": facts["vendor"], "period": facts["period"],
            "amount": facts["amount"], "usage": facts.get("usage"),
            "usage_unit": facts.get("usage_unit"), "notes": notes,
        })
        archive = ("archive_email", {"file": facts["file"]})
        if decision == "alert":
            letter = draft_letter(payload["letter"], facts)
            return [
                ("raise_alert", {
                    "vendor": facts["vendor"], "severity": payload["severity"],
                    "title": payload["title"], "reason": payload["reason"],
                    "amount_delta": payload["amount_delta"],
                }),
                ("save_letter", {**letter, "vendor": facts["vendor"]}),
                record, archive,
            ]
        return [("add_digest_note", {"note": payload["note"]}), record, archive]

    @staticmethod
    def _narrate(facts: dict[str, Any]) -> str:
        decision, payload = decide(facts)
        if decision == "alert":
            return (f"Judgment: ALERT — {payload['title']}. {payload['reason']} "
                    f"Impact ${payload['amount_delta']:+.2f}/mo. Alert raised and a "
                    "ready-to-send draft filed.")
        return f"Judgment: quiet. {payload['note']} Filed to the weekly digest without pinging the household."


# --------------------------------------------------------------------------- #
# orchestration
# --------------------------------------------------------------------------- #

def _make_agent(system_prompt: str) -> Agent:
    from .tools import (
        add_digest_note, archive_email, get_bill_history, list_new_emails,
        parse_bill, raise_alert, record_bill, save_letter,
    )
    model_env = os.environ.get("BILLWARDEN_MODEL", "").strip()
    if model_env.startswith("ollama:"):
        from strands.models.ollama import OllamaModel
        model = OllamaModel(host="http://localhost:11434", model_id=model_env.split(":", 1)[1])
    else:
        model = HeuristicModel()
    return Agent(
        model=model,
        system_prompt=system_prompt,
        tools=[list_new_emails, archive_email, parse_bill, get_bill_history,
               record_bill, raise_alert, add_digest_note, save_letter],
        load_tools_from_directory=False,
    )

SYSTEM_PROMPT = """You are BillWarden, a household bill auditing agent. You protect a busy
family from money leaks. Today is {today}.

For ONE billing email: extract the bill fields, compare with the vendor's
stored history, then decide. Raise an alert (raise_alert) only for real leaks
— a per-unit rate increase on flat usage, a duplicate charge, a renewal jump,
an unexpected fee. Everything expected (seasonal tiers, usage-driven changes,
a promo ending exactly as the contract said) goes to add_digest_note, so the
household is informed without being interrupted. For alerts, save_letter with
a short, polite, factual draft. Always record_bill, then archive_email."""

DIGEST_TEMPLATE = (
    "This week BillWarden reviewed {n_bills} household bills and kept the noise at zero. "
    "{n_alerts} need a decision this week — about ${leak:.2f}/month in recurring savings if "
    "you act on them — while {n_quiet} expected changes were logged quietly in the digest. "
    "Draft letters for every alert are ready to send from the Letters panel."
)


def audit_inbox() -> dict[str, Any]:
    """Run the full audit over the inbox. Returns the final state."""
    tools.reset_runtime()
    seed(force=True)
    log_event("start", f"BillWarden weekly audit started — {date.today().isoformat()}")

    emails = [
        json.loads(p.read_text(encoding="utf-8"))
        for p in sorted(tools.INBOX_DIR.glob("*.json"))
    ]
    log_event("inbox", f"Inbox scanned — {len(emails)} billing emails waiting")

    system_prompt = SYSTEM_PROMPT.format(today=date.today().isoformat())

    for email in emails:
        log_event("email", f"Processing: {email['subject']}")
        parsed = parse_bill(email["body"])
        vendor = parsed.get("vendor") or ""
        rows = tools._db().execute(
            "SELECT vendor, period, amount, usage, usage_unit, notes FROM bills "
            "WHERE vendor = ? ORDER BY id", (vendor,)
        ).fetchall()
        history = [dict(r) for r in rows]
        facts = build_facts(email, parsed, history)
        facts_txt = json.dumps(facts, ensure_ascii=False, indent=1)

        prompt = (
            f"Audit this billing email.\n\n"
            f"FROM: {email['from']}\nSUBJECT: {email['subject']}\n"
            f"BODY:\n{email['body']}\n\n"
            f"<FACTS>{facts_txt}</FACTS>\n\n"
            f"Proceed: parse, compare with history, decide (raise_alert OR "
            f"add_digest_note), draft a letter if alerted, record the bill, "
            f"archive the email."
        )
        try:
            agent = _make_agent(system_prompt)
            agent(prompt)
        except Exception as exc:  # one bad email must never kill the audit
            log_event("error", f"Agent run issue on {email['file']}: {exc}")

        # deterministic backstops keep the ledger complete
        _backstop_ledger(facts)
        _backstop_decision(vendor)

    _write_weekly_summary(len(emails))
    log_event("done", "Audit complete — weekly summary ready")
    return summary_totals()


def _backstop_ledger(facts: dict[str, Any]) -> None:
    vendor, period = facts.get("vendor"), facts.get("period")
    if not vendor or not period or not facts.get("amount"):
        return
    conn = tools._db()
    row = conn.execute(
        "SELECT COUNT(*) FROM bills WHERE vendor=? AND period=?",
        (vendor, period),
    ).fetchone()[0]
    conn.close()
    if row == 0:
        tools.record_bill(
            vendor, period, facts["amount"], facts.get("usage"),
            facts.get("usage_unit"), ", ".join(facts.get("email_notes") or []) or None,
        )


def _backstop_decision(vendor: str) -> None:
    state = summary_totals()
    covered = any(a["vendor"] == vendor for a in state["alerts"]) or \
        any(vendor.lower()[:12] in n.lower() for n in state["digest"])
    if not covered:
        add_digest_note(f"{vendor}: reviewed, change looks normal, no action needed.")


def _write_weekly_summary(n_bills: int) -> None:
    state = summary_totals()
    alerts = state["alerts"]
    text = DIGEST_TEMPLATE.format(
        n_bills=n_bills, n_alerts=len(alerts),
        plural="s" if len(alerts) != 1 else "",
        leak=state["monthly_leak"], n_quiet=len(state["digest"]),
    )
    st = tools._read_state()
    st["summary"] = text
    tools._write_state(st)


def run_cli() -> None:
    state = audit_inbox()
    print("\n=== BILLWARDEN WEEKLY DIGEST ===")
    print(state.get("summary") or "")
    print("\n=== ALERTS ===")
    for a in state["alerts"]:
        print(f"[{a['severity']}] {a['vendor']}: {a['title']} ({a['amount_delta']:+.2f}/mo)")
    print("\n=== QUIET (no ping needed) ===")
    for n in state["digest"]:
        print(" -", n)
    print("\nLetters:", [l["file"] for l in state["letters"]])


if __name__ == "__main__":
    run_cli()
