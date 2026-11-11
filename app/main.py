"""PayMeBot: invoice-to-cash agent for freelancers. FastAPI server."""
import asyncio
import os
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()

from . import agent as agent_mod  # noqa: E402
from .agent import run_agent  # noqa: E402
from .paypal import MockPayPal, PayPalError, make_client  # noqa: E402
from .reminders import policy  # noqa: E402

paypal = make_client()
events = deque(maxlen=50)  # recent activity, newest first
auto = {"enabled": False, "last_run": None}
AUTO_CHECK_SECONDS = 60
CHASE_PROMPT = ("Check all my invoices. Send a polite reminder for each unpaid sent invoice whose reminder_state "
                "is ready. Then tell me in short which clients you reminded and which you skipped.")


async def run_chase(source: str) -> dict:
    """Send reminders for eligible invoices. The AI model is called only if there is work to do."""
    invoices = await paypal.list_invoices()
    ready = policy.eligible(invoices)
    open_count = sum(1 for i in invoices if i.get("reminder_state"))
    if not ready:
        if open_count:
            reply = (f"No reminders sent. {open_count} unpaid invoice(s) are in cooldown or at the reminder limit. "
                     "See the Next reminder column.")
        else:
            reply = "No reminders sent. There are no unpaid sent invoices."
        return {"reply": reply, "steps": [], "history": []}
    out = await run_agent(paypal, CHASE_PROMPT)
    log_steps(source, out["steps"], invoices)
    return out


async def auto_chase_loop():
    while True:
        await asyncio.sleep(AUTO_CHECK_SECONDS)
        if not auto["enabled"]:
            continue
        try:
            auto["last_run"] = datetime.now(timezone.utc).isoformat()
            await run_chase("auto-chase")
        except Exception as e:  # keep the loop alive
            log("auto-chase", f"Run failed: {type(e).__name__}")


@asynccontextmanager
async def lifespan(_app):
    task = asyncio.create_task(auto_chase_loop())
    yield
    task.cancel()


app = FastAPI(title="PayMeBot", lifespan=lifespan)
STATIC = Path(__file__).resolve().parent.parent / "static"


def pp_http(e: PayPalError) -> HTTPException:
    return HTTPException(502, e.as_dict())


def log_steps(source: str, steps: list, known: list | None = None):
    """Write agent actions to the activity list in plain words: client name and invoice number, no IDs."""
    by_id = {i["id"]: i for i in (known or [])}
    for s in steps:  # learn names from what the tools returned
        r = s.get("result") or {}
        for inv in r.get("invoices", []) if isinstance(r.get("invoices"), list) else []:
            by_id[inv["id"]] = inv
        if r.get("id") and r.get("number"):
            by_id[r["id"]] = r

    def who(invoice_id):
        inv = by_id.get(invoice_id)
        if not inv:
            return "an invoice"
        return f"invoice {inv['number']} ({inv.get('client_name') or 'client'})"

    for s in steps:
        tool, r, args = s["tool"], s.get("result") or {}, s.get("args") or {}
        if tool in ("list_invoices", "get_invoice"):
            continue
        if r.get("error"):
            what = {"create_invoice": "create an invoice", "send_invoice": f"send {who(args.get('invoice_id'))}",
                    "send_reminder": f"send a reminder for {who(args.get('invoice_id'))}"}.get(tool, tool)
            log(source, f"Could not {what}. Error code: {r.get('code') or 'unknown'}")
        elif tool == "create_invoice":
            log(source, f"Draft invoice {r.get('number')} created for {r.get('client_name')}, "
                        f"{r.get('currency')} {r.get('total', 0):.2f}")
        elif tool == "send_invoice":
            log(source, f"Invoice {r.get('number')} sent to {r.get('client_name')}, "
                        f"{r.get('currency')} {r.get('total', 0):.2f}")
        elif tool == "send_reminder":
            if r.get("reminder_sent"):
                log(source, f"Reminder sent for {who(args.get('invoice_id'))}")
            else:
                log(source, f"Reminder skipped for {who(args.get('invoice_id'))}: anti-spam policy")


def log(kind: str, text: str):
    events.appendleft({"time": datetime.now(timezone.utc).isoformat(), "kind": kind, "text": text})


class AgentRequest(BaseModel):
    message: str
    history: list = []
    currency: str = "USD"


@app.post("/api/agent")
async def agent(req: AgentRequest):
    if not req.message.strip():
        raise HTTPException(400, "Message is empty")
    try:
        cur = req.currency.strip().upper()
        out = await run_agent(paypal, req.message.strip(), req.history,
                              cur if len(cur) == 3 and cur.isalpha() else "USD")
    except Exception as e:  # show the cause in the UI instead of a bare 500
        raise HTTPException(500, {"error": "Something went wrong in the app.", "code": "APP_ERROR",
                                  "raw": f"{type(e).__name__}: {e}"})
    log_steps("agent", out["steps"])
    return out


@app.get("/api/invoices")
async def invoices():
    try:
        return {"invoices": [policy.annotate(i) for i in await paypal.list_invoices()]}
    except PayPalError as e:
        raise pp_http(e)


@app.post("/api/invoices/{invoice_id}/send")
async def send(invoice_id: str):
    try:
        inv = await paypal.send_invoice(invoice_id)
    except PayPalError as e:
        raise pp_http(e)
    policy.record_sent(invoice_id)
    log("you", f"Invoice {inv['number']} sent to {inv['client_name']}")
    return inv


@app.delete("/api/invoices/{invoice_id}")
async def delete(invoice_id: str):
    try:
        out = await paypal.delete_invoice(invoice_id)
    except PayPalError as e:
        raise pp_http(e)
    log("you", "Draft invoice deleted")
    return out


@app.post("/api/chase")
async def chase():
    """The agent finds unpaid invoices and sends reminders, inside the anti-spam policy."""
    try:
        return await run_chase("agent")
    except PayPalError as e:
        raise pp_http(e)


class AutoChase(BaseModel):
    enabled: bool


@app.post("/api/autochase")
async def set_autochase(body: AutoChase):
    auto["enabled"] = body.enabled
    log("you", f"Auto-chase turned {'on' if body.enabled else 'off'}")
    return auto


@app.post("/api/webhooks/paypal")
async def webhook(request: Request):
    event = await request.json()
    try:
        if not await paypal.verify_webhook(request.headers, event):
            raise HTTPException(400, "Webhook signature is not valid")
    except PayPalError as e:
        raise pp_http(e)
    kind = event.get("event_type", "")
    res = event.get("resource", {})
    inv = res.get("invoice", res)
    number = (inv.get("detail") or {}).get("invoice_number") or "unknown"
    name = ((inv.get("primary_recipients") or [{}])[0].get("billing_info") or {}).get("name") or {}
    client = (name.get("full_name") or f"{name.get('given_name', '')} {name.get('surname', '')}").strip()
    labels = {"INVOICING.INVOICE.PAID": "Payment received", "INVOICING.INVOICE.CANCELLED": "Invoice cancelled",
              "INVOICING.INVOICE.REFUNDED": "Payment refunded", "INVOICING.INVOICE.CREATED": "Invoice created",
              "INVOICING.INVOICE.SCHEDULED": "Invoice scheduled", "INVOICING.INVOICE.UPDATED": "Invoice updated"}
    log("paypal", f"{labels.get(kind, kind)}: invoice {number}" + (f" ({client})" if client else ""))
    return {"received": True}


@app.get("/api/events")
async def get_events():
    return {"events": list(events)}


@app.get("/api/config")
async def config():
    return {"support_url": os.getenv("SUPPORT_URL", "https://github.com/starlord-93/paypal-invoice-agent/issues/new"),
            "auto_chase": auto["enabled"], "cooldown_minutes": policy.cooldown_s / 60,
            "reminder_max": policy.max_reminders,
            "paypal_mode": "mock" if isinstance(paypal, MockPayPal) else os.getenv("PAYPAL_ENV", "sandbox"),
            "agent_mode": "mock" if os.getenv("AGENT_MOCK") == "1" else (agent_mod._working_model or agent_mod.model_chain()[0])}


@app.get("/api/mock/pay/{invoice_id}", response_class=HTMLResponse)
async def mock_pay(invoice_id: str):
    """Mock mode only: simulates the client payment."""
    if not isinstance(paypal, MockPayPal):
        raise HTTPException(404)
    try:
        inv = paypal.mark_paid(invoice_id)
    except PayPalError as e:
        raise HTTPException(404, str(e))
    log("paypal", f"Payment received: invoice {inv['number']} ({inv['client_name']})")
    return f"<h2>Mock payment complete for invoice {inv['number']}</h2><p>You can close this tab.</p>"


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
