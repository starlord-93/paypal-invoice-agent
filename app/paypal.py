"""PayPal Invoicing v2 client (sandbox by default) with an offline mock mode."""
import asyncio
import os
import time
import uuid
from datetime import date

import httpx

SANDBOX = "https://api-m.sandbox.paypal.com"
LIVE = "https://api-m.paypal.com"


class PayPalError(Exception):
    """str(e) is the raw technical text. `friendly` is a short message for the end user."""

    def __init__(self, raw, friendly=None, code=None, debug_id=None, status=None):
        super().__init__(raw)
        self.friendly = friendly or "PayPal could not complete this action."
        self.code = code
        self.debug_id = debug_id
        self.status = status

    def as_dict(self) -> dict:
        return {"error": self.friendly, "code": self.code, "debug_id": self.debug_id,
                "status": self.status, "raw": str(self)}


class PayPalClient:
    def __init__(self):
        self.base = LIVE if os.getenv("PAYPAL_ENV", "sandbox") == "live" else SANDBOX
        self.client_id = os.getenv("PAYPAL_CLIENT_ID", "")
        self.secret = os.getenv("PAYPAL_CLIENT_SECRET", "")
        self.webhook_id = os.getenv("PAYPAL_WEBHOOK_ID", "")
        self._token = None
        self._token_exp = 0.0
        self.http = httpx.AsyncClient(timeout=30)
        self._pay_urls = {}

    async def _auth(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        if not (self.client_id and self.secret):
            raise PayPalError("PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET are not set",
                              friendly="The app is not connected to PayPal.", code="NOT_CONFIGURED")
        r = await self.http.post(
            f"{self.base}/v1/oauth2/token",
            auth=(self.client_id, self.secret),
            data={"grant_type": "client_credentials"},
        )
        if r.status_code != 200:
            raise PayPalError(f"PayPal auth failed ({r.status_code}): {r.text[:300]}",
                              friendly="PayPal did not accept the app credentials.", code="AUTH_FAILED",
                              status=r.status_code)
        body = r.json()
        self._token = body["access_token"]
        self._token_exp = time.time() + int(body.get("expires_in", 3000))
        return self._token

    async def _call(self, method: str, path: str, json=None, params=None, headers=None):
        token = await self._auth()
        h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        h.update(headers or {})
        r = await self.http.request(method, f"{self.base}{path}", json=json, params=params, headers=h)
        if r.status_code >= 400:
            friendly = code = debug_id = None
            try:
                err = r.json()
                first = (err.get("details") or [{}])[0]
                friendly = first.get("description") or err.get("message")
                code = first.get("issue") or err.get("name")
                debug_id = err.get("debug_id")
            except ValueError:
                pass
            raise PayPalError(f"PayPal {method} {path} failed ({r.status_code}): {r.text[:1500]}",
                              friendly=friendly, code=code or f"HTTP_{r.status_code}",
                              debug_id=debug_id, status=r.status_code)
        return r.json() if r.content else {}

    async def create_invoice(self, client_name, client_email, items, currency="USD",
                             due_date=None, note=None) -> dict:
        number = (await self._call("POST", "/v2/invoicing/generate-next-invoice-number"))["invoice_number"]
        parts = client_name.strip().split(" ", 1)
        detail = {"invoice_number": number, "currency_code": currency,
                  "invoice_date": date.today().isoformat()}
        if note:
            detail["note"] = note
        if due_date:
            detail["payment_term"] = {"due_date": due_date}
        body = {
            "detail": detail,
            "primary_recipients": [{"billing_info": {
                "name": {"given_name": parts[0], "surname": parts[1] if len(parts) > 1 else ""},
                "email_address": client_email,
            }}],
            "items": [{
                "name": i["name"],
                "quantity": str(i.get("quantity", 1)),
                "unit_amount": {"currency_code": currency, "value": f"{float(i['unit_amount']):.2f}"},
                "unit_of_measure": "QUANTITY",
            } for i in items],
        }
        invoicer_email = os.getenv("PAYPAL_INVOICER_EMAIL")
        if invoicer_email:
            body["invoicer"] = {"email_address": invoicer_email}
        inv = await self._call("POST", "/v2/invoicing/invoices", json=body,
                               headers={"Prefer": "return=representation"})
        return summarize(inv)

    async def send_invoice(self, invoice_id: str) -> dict:
        await self._call("POST", f"/v2/invoicing/invoices/{invoice_id}/send",
                         json={"send_to_recipient": True, "send_to_invoicer": False})
        return await self.get_invoice(invoice_id)

    async def get_invoice(self, invoice_id: str) -> dict:
        return summarize(await self._call("GET", f"/v2/invoicing/invoices/{invoice_id}"))

    async def list_invoices(self) -> list:
        data = await self._call("GET", "/v2/invoicing/invoices",
                                params={"page_size": 50, "total_required": "true", "fields": "all"})
        invoices = [summarize(i) for i in data.get("items", [])]
        # The list call does not return the pay link. Read it from the detail call for open invoices.
        open_ones = [i for i in invoices if not i["pay_url"]
                     and i["status"] in ("SENT", "PARTIALLY_PAID", "PAYMENT_PENDING")][:15]

        async def fill(inv):
            if inv["id"] not in self._pay_urls:
                try:
                    self._pay_urls[inv["id"]] = (await self.get_invoice(inv["id"]))["pay_url"]
                except (PayPalError, httpx.HTTPError):
                    return
            inv["pay_url"] = self._pay_urls[inv["id"]]

        await asyncio.gather(*(fill(i) for i in open_ones))
        return invoices

    async def delete_invoice(self, invoice_id: str) -> dict:
        """Deletes a draft invoice. PayPal does not delete sent invoices."""
        await self._call("DELETE", f"/v2/invoicing/invoices/{invoice_id}")
        return {"invoice_id": invoice_id, "deleted": True}

    async def send_reminder(self, invoice_id: str, subject: str, note: str) -> dict:
        await self._call("POST", f"/v2/invoicing/invoices/{invoice_id}/remind",
                         json={"subject": subject, "note": note})
        return {"invoice_id": invoice_id, "reminder_sent": True}

    async def verify_webhook(self, headers, event: dict) -> bool:
        """Verify the webhook signature with PayPal. Skipped if no webhook ID is set."""
        if not self.webhook_id:
            return True
        body = {
            "auth_algo": headers.get("paypal-auth-algo"),
            "cert_url": headers.get("paypal-cert-url"),
            "transmission_id": headers.get("paypal-transmission-id"),
            "transmission_sig": headers.get("paypal-transmission-sig"),
            "transmission_time": headers.get("paypal-transmission-time"),
            "webhook_id": self.webhook_id,
            "webhook_event": event,
        }
        res = await self._call("POST", "/v1/notifications/verify-webhook-signature", json=body)
        return res.get("verification_status") == "SUCCESS"


def summarize(inv: dict) -> dict:
    """Reduce a PayPal invoice object to the fields the UI and the agent use."""
    detail = inv.get("detail", {})
    rec = (inv.get("primary_recipients") or [{}])[0].get("billing_info", {})
    name = rec.get("name", {})
    amount = inv.get("amount", {})
    due = inv.get("due_amount", {})
    return {
        "id": inv.get("id"),
        "number": detail.get("invoice_number"),
        "status": inv.get("status"),
        "client_name": (name.get("full_name") or
                        f"{name.get('given_name', '')} {name.get('surname', '')}").strip(),
        "client_email": rec.get("email_address"),
        "currency": amount.get("currency_code") or detail.get("currency_code"),
        "total": float(amount.get("value", 0) or 0),
        "due_amount": float(due.get("value", 0) or 0),
        "invoice_date": detail.get("invoice_date"),
        "due_date": (detail.get("payment_term") or {}).get("due_date"),
        "pay_url": (detail.get("metadata") or {}).get("recipient_view_url"),
        "items": [{"name": i.get("name"), "quantity": i.get("quantity"),
                   "unit_amount": (i.get("unit_amount") or {}).get("value")}
                  for i in inv.get("items", [])],
    }


class MockPayPal:
    """In-memory stand-in. Lets you run the app with no PayPal credentials."""

    def __init__(self):
        self.invoices = {}
        self.n = 0

    async def create_invoice(self, client_name, client_email, items, currency="USD",
                             due_date=None, note=None):
        self.n += 1
        iid = "INV2-MOCK-" + uuid.uuid4().hex[:8].upper()
        total = sum(float(i["unit_amount"]) * float(i.get("quantity", 1)) for i in items)
        self.invoices[iid] = {
            "id": iid, "number": f"{self.n:04d}", "status": "DRAFT",
            "client_name": client_name, "client_email": client_email,
            "currency": currency, "total": round(total, 2), "due_amount": round(total, 2),
            "invoice_date": date.today().isoformat(), "due_date": due_date,
            "pay_url": None, "items": items,
        }
        return self.invoices[iid]

    async def send_invoice(self, invoice_id):
        inv = self._get(invoice_id)
        inv["status"] = "SENT"
        inv["pay_url"] = f"/api/mock/pay/{invoice_id}"
        return inv

    async def get_invoice(self, invoice_id):
        return self._get(invoice_id)

    async def list_invoices(self):
        return list(reversed(self.invoices.values()))

    async def send_reminder(self, invoice_id, subject, note):
        self._get(invoice_id)
        return {"invoice_id": invoice_id, "reminder_sent": True}

    async def verify_webhook(self, headers, event):
        return True

    async def delete_invoice(self, invoice_id):
        if self._get(invoice_id)["status"] != "DRAFT":
            raise PayPalError("Only a draft can be deleted", friendly="Only a draft invoice can be deleted.",
                              code="NOT_A_DRAFT")
        del self.invoices[invoice_id]
        return {"invoice_id": invoice_id, "deleted": True}

    def mark_paid(self, invoice_id):
        inv = self._get(invoice_id)
        inv["status"] = "PAID"
        inv["due_amount"] = 0.0
        return inv

    def _get(self, invoice_id):
        if invoice_id not in self.invoices:
            raise PayPalError(f"Invoice {invoice_id} not found", friendly="This invoice was not found.",
                              code="NOT_FOUND")
        return self.invoices[invoice_id]


def make_client():
    if os.getenv("PAYPAL_MOCK") == "1":
        return MockPayPal()
    return PayPalClient()
