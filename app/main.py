"""PayMeBot: invoice-to-cash agent for freelancers. FastAPI server."""
import os
from collections import deque
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

app = FastAPI(title="PayMeBot")
paypal = make_client()
events = deque(maxlen=50)  # recent activity, newest first
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
        return {"invoices": await paypal.list_invoices()}
    except PayPalError as e:
        raise HTTPException(502, str(e))


@app.post("/api/invoices/{invoice_id}/send")
async def send(invoice_id: str):
    try:
        inv = await paypal.send_invoice(invoice_id)
    except PayPalError as e:
        raise HTTPException(502, str(e))
    log("user", f"Invoice {inv['number']} sent")
    return inv


@app.post("/api/chase")
async def chase():
    """One-click autonomous run: the agent finds unpaid invoices and sends reminders."""
    out = await run_agent(
        paypal,
        "Check all my invoices. Send a polite reminder for each sent invoice that is not paid. "
        "Then tell me which clients you reminded.")
    for s in out["steps"]:
        if s["tool"] == "send_reminder":
            log("agent", f"Reminder sent for {s['args'].get('invoice_id')}")
    return out


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
    return {"paypal_mode": "mock" if isinstance(paypal, MockPayPal) else os.getenv("PAYPAL_ENV", "sandbox"),
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
