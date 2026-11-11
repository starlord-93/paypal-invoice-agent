"""The AI agent. Gemini decides which PayPal tools to call; this module runs them."""
import json
import os
import re
from datetime import date, timedelta

import httpx

from .paypal import PayPalError
from .reminders import policy

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
MAX_STEPS = 8

SYSTEM = """You are PayMeBot, an invoice-to-cash agent for freelancers. You act through PayPal tools.
Today is {today}.

Rules:
- When the user describes finished work, create a draft invoice with create_invoice.
  Calculate nothing yourself: give quantity and unit price per line item. PayPal calculates totals.
- Change relative dates ("Friday", "in 2 weeks") to YYYY-MM-DD. If no due date is given, use 14 days from today.
- Default currency is {currency} unless the user gives a different one.
- Create an invoice only when you have the client name, client email, and one or more line items with a price.
  If one of these is missing, ask one short question. Do not invent an email address or a price.
- Send an invoice (send_invoice) only when the user tells you to send it. "Bill", "send" and "invoice and send"
  all mean send. If the user only asks for a draft, do not send.
- For reminders: call list_invoices, select invoices with status SENT or PARTIALLY_PAID that are unpaid, and call
  send_reminder for each with a short, polite, specific note. Do not remind for DRAFT, PAID or CANCELLED invoices.
- Anti-spam policy: each invoice in list_invoices has a reminder_state. Call send_reminder only when it is "ready".
  "cooldown" means a reminder or the invoice itself was sent a short time ago. "limit" means the maximum is reached.
  If send_reminder returns skipped, do not try again. Tell the user which invoices were skipped and why.
- After tool calls, reply in 1 to 3 short sentences. State what you did, the invoice number, the total and the status.
- Never claim an action that a tool did not confirm.
- If a tool returns an error, explain it in one plain sentence for a non-technical user. Do not repeat raw
  technical text, IDs or URLs. The app shows the error code separately.
"""

TOOLS = [{
    "function_declarations": [
        {
            "name": "create_invoice",
            "description": "Create a draft PayPal invoice. Does not send it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "client_name": {"type": "string"},
                    "client_email": {"type": "string"},
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string", "description": "Work done, for example 'Logo design (hours)'"},
                                "quantity": {"type": "number"},
                                "unit_amount": {"type": "number", "description": "Price for one unit"},
                            },
                            "required": ["name", "quantity", "unit_amount"],
                        },
                    },
                    "currency": {"type": "string", "description": "ISO 4217 code, for example USD"},
                    "due_date": {"type": "string", "description": "YYYY-MM-DD"},
                    "note": {"type": "string", "description": "Short note to the client"},
                },
                "required": ["client_name", "client_email", "items"],
            },
        },
        {
            "name": "send_invoice",
            "description": "Send a draft invoice to the client through PayPal. The client gets a payment link.",
            "parameters": {"type": "object", "properties": {"invoice_id": {"type": "string"}},
                           "required": ["invoice_id"]},
        },
        {
            "name": "list_invoices",
            "description": "List recent invoices with status, client, total, amount due and due date.",
            "parameters": {"type": "object", "properties": {}},
        },
        {
            "name": "get_invoice",
            "description": "Get the current status and details of one invoice.",
            "parameters": {"type": "object", "properties": {"invoice_id": {"type": "string"}},
                           "required": ["invoice_id"]},
        },
        {
            "name": "send_reminder",
            "description": "Send a payment reminder for an unpaid, sent invoice.",
            "parameters": {
                "type": "object",
                "properties": {
                    "invoice_id": {"type": "string"},
                    "subject": {"type": "string"},
                    "note": {"type": "string", "description": "Polite reminder text to the client"},
                },
                "required": ["invoice_id", "subject", "note"],
            },
        },
    ]
}]


async def run_tool(paypal, name: str, args: dict) -> dict:
    try:
        if name == "create_invoice":
            return await paypal.create_invoice(
                args["client_name"], args["client_email"], args["items"],
                (args.get("currency") or "USD").upper(), args.get("due_date"), args.get("note"))
        if name == "send_invoice":
            inv = await paypal.send_invoice(args["invoice_id"])
            policy.record_sent(args["invoice_id"])
            return inv
        if name == "list_invoices":
            return {"invoices": [policy.annotate(i) for i in await paypal.list_invoices()]}
        if name == "get_invoice":
            return await paypal.get_invoice(args["invoice_id"])
        if name == "send_reminder":
            allowed, reason = policy.check(args["invoice_id"])
            if not allowed:
                return {"invoice_id": args["invoice_id"], "reminder_sent": False, "skipped": True, "reason": reason}
            out = await paypal.send_reminder(args["invoice_id"], args["subject"], args["note"])
            policy.record_reminder(args["invoice_id"])
            return out
        return {"error": f"Unknown tool {name}"}
    except PayPalError as e:
        return e.as_dict()
    except (KeyError, ValueError, TypeError) as e:
        return {"error": "The request was not complete.", "code": "BAD_TOOL_INPUT", "raw": f"{type(e).__name__}: {e}"}
    except httpx.HTTPError as e:
        return {"error": "PayPal did not answer. Try again in a moment.", "code": "NETWORK_ERROR",
                "raw": f"{type(e).__name__}: {e}"}


GEMINI_TIMEOUT = float(os.getenv("GEMINI_TIMEOUT", "25"))
_working_model = None  # the first model that answered; used first on later calls


def model_chain() -> list:
    names = [m.strip() for m in os.getenv(
        "GEMINI_MODEL", "gemini-flash-latest,gemini-3.8-flash,gemini-3.5-flash-lite,gemini-2.5-flash"
    ).split(",") if m.strip()]
    if _working_model in names:
        names.remove(_working_model)
        names.insert(0, _working_model)
    return names


async def _call_gemini(http, key: str, contents: list, currency: str = "USD"):
    """Try each model in the chain. Returns (response, problems)."""
    global _working_model
    problems = []
    for model in model_chain():
        try:
            r = await http.post(
                GEMINI_URL.format(model=model),
                headers={"x-goog-api-key": key},
                json={
                    "system_instruction": {"parts": [{"text": SYSTEM.format(today=date.today().isoformat(), currency=currency)}]},
                    "contents": contents,
                    "tools": TOOLS,
                    "generationConfig": {"temperature": 0.2},
                },
            )
        except httpx.HTTPError as e:
            problems.append(f"{model}: {type(e).__name__}")
            continue
        if r.status_code == 200:
            _working_model = model
            return r, problems
        problems.append(f"{model}: HTTP {r.status_code} {r.text[:160]}")
        if r.status_code in (400, 401, 403):  # bad request or bad key: another model will not help
            break
    return None, problems


async def run_agent(paypal, message: str, history: list | None = None, currency: str = "USD") -> dict:
    """Run one user turn. Returns the reply, the tool trace and the updated history."""
    if os.getenv("AGENT_MOCK") == "1":
        return await _mock_agent(paypal, message)

    key = os.getenv("GEMINI_API_KEY")
    if not key:
        return {"reply": "", "steps": [], "history": history or [],
                "error": {"error": "The AI assistant is not set up.", "code": "AI_NOT_CONFIGURED",
                          "raw": "GEMINI_API_KEY is not set on the server."}}
    contents = list(history or []) + [{"role": "user", "parts": [{"text": message}]}]
    steps = []

    async with httpx.AsyncClient(timeout=GEMINI_TIMEOUT) as http:
        for _ in range(MAX_STEPS):
            r, problems = await _call_gemini(http, key, contents, currency)
            if r is None:
                return {"reply": "", "steps": steps, "history": history or [],
                        "error": {"error": "The AI assistant did not answer. Try again in a moment.",
                                  "code": "AI_UNAVAILABLE", "raw": " | ".join(problems)}}
            cands = r.json().get("candidates") or []
            content = (cands[0].get("content") if cands else None) or {"role": "model", "parts": []}
            parts = content.get("parts") or []
            # Keep the model turn unchanged. Some models need their own parts sent back as-is.
            contents.append({"role": "model", "parts": parts})
            calls = [p["functionCall"] for p in parts if "functionCall" in p]
            if not calls:
                text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
                return {"reply": text or "Done.", "steps": steps, "history": contents}
            responses = []
            for c in calls:
                result = await run_tool(paypal, c["name"], c.get("args") or {})
                steps.append({"tool": c["name"], "args": c.get("args") or {}, "result": result})
                responses.append({"functionResponse": {"name": c["name"], "response": {"result": result}}})
            contents.append({"role": "user", "parts": responses})

    return {"reply": "I stopped because the task took too many steps.", "steps": steps, "history": contents}


async def _mock_agent(paypal, message: str) -> dict:
    """Rule-based stand-in for offline tests. Not used when a Gemini key is set."""
    steps = []
    low = message.lower()
    if "remind" in low or "chase" in low:
        invs = await paypal.list_invoices()
        steps.append({"tool": "list_invoices", "args": {}, "result": {"count": len(invs)}})
        n = 0
        for i in invs:
            if i["status"] in ("SENT", "PARTIALLY_PAID"):
                args = {"invoice_id": i["id"], "subject": f"Reminder: invoice {i['number']}",
                        "note": "A friendly reminder that this invoice is open."}
                res = await run_tool(paypal, "send_reminder", args)
                steps.append({"tool": "send_reminder", "args": args, "result": res})
                n += 1 if res.get("reminder_sent") else 0
        return {"reply": f"Sent {n} reminder(s).", "steps": steps, "history": []}
    email = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", message)
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:hours?|hrs?|x)\s*(?:at|@)?\s*\$?(\d+(?:\.\d+)?)", low)
    if not email or not m:
        return {"reply": "I need the client email and the work with a price.", "steps": [], "history": []}
    name = re.search(r"for ([A-Z][a-z]+(?: [A-Z][a-z]+)?)", message)
    args = {"client_name": name.group(1) if name else "Client", "client_email": email.group(0),
            "items": [{"name": message.split(" for ")[0][:60], "quantity": float(m.group(1)),
                       "unit_amount": float(m.group(2))}],
            "currency": "USD", "due_date": (date.today() + timedelta(days=14)).isoformat()}
    inv = await run_tool(paypal, "create_invoice", args)
    steps.append({"tool": "create_invoice", "args": args, "result": inv})
    if "send" in low or "bill" in low:
        inv = await run_tool(paypal, "send_invoice", {"invoice_id": inv["id"]})
        steps.append({"tool": "send_invoice", "args": {"invoice_id": inv["id"]}, "result": inv})
    return {"reply": f"Invoice {inv['number']} for {inv['currency']} {inv['total']:.2f} is {inv['status']}.",
            "steps": steps, "history": []}
