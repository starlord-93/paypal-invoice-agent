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
    numbers = {i["id"]: i["number"] for i in invoices}
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
    for s in out["steps"]:
        if s["tool"] == "send_reminder":
            num = numbers.get(s["args"].get("invoice_id"), s["args"].get("invoice_id"))
            if (s["result"] or {}).get("reminder_sent"):
                log(source, f"Reminder sent for invoice {num}")
            else:
                log(source, f"Reminder skipped for invoice {num} (anti-spam policy)")
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


def log(kind: str, text: str):
    events.appendleft({"time": datetime.now(timezone.utc).isoformat(), "kind": kind, "text": text})


class AgentRequest(BaseModel):
    message: str
    history: list = []


@app.post("/api/agent")
async def agent(req: AgentRequest):
    if not req.message.strip():
        raise HTTPException(400, "Message is empty")
    try:
        out = await run_agent(paypal, req.message.strip(), req.history)
    except Exception as e:  # show the cause in the UI instead of a bare 500
        raise HTTPException(500, f"{type(e).__name__}: {e}")
    for s in out["steps"]:
        ok = "error" not in (s["result"] or {})
        log("agent", f"{s['tool']} {'succeeded' if ok else 'failed'}")
    return out


@app.get("/api/invoices")
async def invoices():
    try:
        return {"invoices": [policy.annotate(i) for i in await paypal.list_invoices()]}
    except PayPalError as e:
        raise HTTPException(502, str(e))


@app.post("/api/invoices/{invoice_id}/send")
async def send(invoice_id: str):
    try:
        inv = await paypal.send_invoice(invoice_id)
    except PayPalError as e:
        raise HTTPException(502, str(e))
    policy.record_sent(invoice_id)
    log("user", f"Invoice {inv['number']} sent")
    return inv


@app.post("/api/chase")
async def chase():
    """The agent finds unpaid invoices and sends reminders, inside the anti-spam policy."""
    try:
        return await run_chase("agent")
    except PayPalError as e:
        raise HTTPException(502, str(e))


class AutoChase(BaseModel):
    enabled: bool


@app.post("/api/autochase")
async def set_autochase(body: AutoChase):
    auto["enabled"] = body.enabled
    log("user", f"Auto-chase turned {'on' if body.enabled else 'off'}")
    return auto


@app.post("/api/webhooks/paypal")
async def webhook(request: Request):
    event = await request.json()
    try:
        if not await paypal.verify_webhook(request.headers, event):
            raise HTTPException(400, "Webhook signature is not valid")
    except PayPalError as e:
        raise HTTPException(502, str(e))
    kind = event.get("event_type", "")
    res = event.get("resource", {})
    inv = res.get("invoice", res)
    number = (inv.get("detail") or {}).get("invoice_number") or inv.get("id", "")
    log("paypal", f"{kind} for invoice {number}")
    return {"received": True}


@app.get("/api/events")
async def get_events():
    return {"events": list(events)}


@app.get("/api/config")
async def config():
    return {"auto_chase": auto["enabled"], "cooldown_minutes": policy.cooldown_s / 60,
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
    log("paypal", f"INVOICING.INVOICE.PAID for invoice {inv['number']}")
    return f"<h2>Mock payment complete for invoice {inv['number']}</h2><p>You can close this tab.</p>"


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
