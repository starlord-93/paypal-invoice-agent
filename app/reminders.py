"""Reminder policy: stops the app (and the AI agent) from spamming a client.

Rules, enforced on the server for every reminder:
- one reminder per invoice per cooldown period (also counted from the time the invoice was sent)
- a maximum number of reminders per invoice
"""
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

OPEN_STATUSES = ("SENT", "PARTIALLY_PAID")


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None


class ReminderPolicy:
    def __init__(self):
        self.cooldown_s = float(os.getenv("REMINDER_COOLDOWN_MINUTES", "60")) * 60
        self.max_reminders = int(os.getenv("REMINDER_MAX", "3"))
        self.path = Path(os.getenv("REMINDER_STORE", "reminders.json"))
        try:
            self.data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.data = {}

    def _save(self):
        try:
            self.path.write_text(json.dumps(self.data))
        except OSError:
            pass  # read-only disk: keep the state in memory

    def _entry(self, invoice_id):
        return self.data.setdefault(invoice_id, {"count": 0, "last": None, "sent_at": None})

    def record_sent(self, invoice_id):
        self._entry(invoice_id)["sent_at"] = time.time()
        self._save()

    def record_reminder(self, invoice_id):
        e = self._entry(invoice_id)
        e["count"] += 1
        e["last"] = time.time()
        self._save()

    def state(self, invoice_id) -> dict:
        e = self.data.get(invoice_id) or {"count": 0, "last": None, "sent_at": None}
        last_contact = max(e["last"] or 0, e["sent_at"] or 0)
        next_at = last_contact + self.cooldown_s if last_contact else 0
        if e["count"] >= self.max_reminders:
            state = "limit"
        elif next_at > time.time():
            state = "cooldown"
        else:
            state = "ready"
        return {"reminder_state": state, "reminder_count": e["count"], "reminder_max": self.max_reminders,
                "last_reminder": _iso(e["last"]), "next_reminder_at": _iso(next_at) if state == "cooldown" else None}

    def check(self, invoice_id):
        """Returns (allowed, reason)."""
        s = self.state(invoice_id)
        if s["reminder_state"] == "limit":
            return False, f"Reminder limit reached ({s['reminder_count']} of {self.max_reminders}). No more reminders for this invoice."
        if s["reminder_state"] == "cooldown":
            return False, f"Cooldown is active. The next reminder is allowed at {s['next_reminder_at']} (UTC)."
        return True, ""

    def annotate(self, inv: dict) -> dict:
        if inv.get("status") in OPEN_STATUSES and (inv.get("due_amount") or 0) > 0:
            inv.update(self.state(inv["id"]))
        else:
            inv.update({"reminder_state": None, "reminder_count": (self.data.get(inv["id"]) or {}).get("count", 0),
                        "reminder_max": self.max_reminders, "last_reminder": None, "next_reminder_at": None})
        return inv

    def eligible(self, invoices: list) -> list:
        return [i for i in invoices if self.annotate(i)["reminder_state"] == "ready"]


policy = ReminderPolicy()
