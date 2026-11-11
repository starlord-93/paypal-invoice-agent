# PayMeBot: invoice-to-cash agent for freelancers

Freelancers lose time and money on billing. They write invoices late, forget to follow up, and get paid late.

PayMeBot removes that work. You tell it in one sentence what you did. An AI agent creates the invoice in PayPal, sends it to your client, shows the payment status, and sends reminders for unpaid invoices.

Built for the PayPal AI Hackathon.

## What it does

1. **Understands plain language.** Example: "Logo design for Ravi Kumar (ravi@example.com), 3 hours at $40. Send it, due Friday."
2. **Acts through PayPal.** The agent calls PayPal tools to create and send a real invoice (PayPal Invoicing API v2, sandbox).
3. **Shows the money.** A live table (AG Grid) shows each invoice, its status, the amount due and the pay link.
4. **Follows up.** One click on "Chase unpaid invoices" lets the agent find unpaid invoices and send a polite reminder for each.
5. **Receives PayPal webhooks.** Payment events show in the activity feed.

## How PayPal and AI are used

| Part | Technology |
|---|---|
| AI agent | Google Gemini with function calling. The model selects the tools. The server runs them. |
| Agent tools | `create_invoice`, `send_invoice`, `list_invoices`, `get_invoice`, `send_reminder` |
| PayPal | OAuth 2.0, Invoicing API v2 (create, send, list, remind), Webhooks with signature verification |
| UI | Plain HTML and JavaScript with AG Grid Community |
| Server | Python, FastAPI |
| Hosting | Render (`render.yaml` included) |

The app has no database. PayPal is the single source of truth for invoice data.

## Run it locally

Requirements: Python 3.10 or later.

```bash
git clone https://github.com/starlord-93/paypal-invoice-agent.git
cd paypal-invoice-agent
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

### Option A: quick look with no keys

Set `PAYPAL_MOCK=1` and `AGENT_MOCK=1` in `.env`. The app then uses in-memory invoices and a simple rule-based agent.

### Option B: full run with PayPal sandbox and Gemini

1. Go to the [PayPal Developer Dashboard](https://developer.paypal.com/dashboard/applications/sandbox) and create a sandbox REST app. Make sure that the **Invoicing** feature is on.
2. Put the Client ID and Secret in `.env` as `PAYPAL_CLIENT_ID` and `PAYPAL_CLIENT_SECRET`.
3. Get a Gemini API key at [Google AI Studio](https://aistudio.google.com/) and put it in `.env` as `GEMINI_API_KEY`.
4. Keep `PAYPAL_MOCK=0` and `AGENT_MOCK=0`.

Start the app:

```bash
uvicorn app.main:app --reload
```

Open http://localhost:8000.

### Test the full flow

1. Ask the agent to create and send an invoice. Use the email of your sandbox **personal** account as the client email.
2. Select "Pay link" in the table. Log in with the sandbox personal account and pay.
3. The status changes to PAID in the table.

### Webhooks (optional)

In the PayPal dashboard, add a webhook for your app with the URL `https://YOUR-HOST/api/webhooks/paypal` and the "Invoicing" events. Put the webhook ID in `PAYPAL_WEBHOOK_ID`. The server then verifies each webhook signature with PayPal.

## Deploy to Render

1. Create a new **Blueprint** on Render and select this repo. Render reads `render.yaml`.
2. Enter the values for `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET` and `GEMINI_API_KEY` when Render asks for them.

## Project structure

```
app/main.py     API routes and webhook endpoint
app/agent.py    Agent loop, tool definitions, system prompt
app/paypal.py   PayPal client and mock client
static/         Web UI
```

## Security notes

- Secrets are read from environment variables only. Do not commit `.env`.
- The agent sends an invoice only when the user asks for it. Drafts can also be sent with the "Send" button.
- This project is for the PayPal sandbox. Add user login before any production use.

## License

MIT. See [LICENSE](LICENSE).
