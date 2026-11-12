"""Builds the AI integration prompt for the docs page (language + target aware). Only describes endpoints that really exist."""
LANGS = ["JavaScript", "TypeScript", "PHP", "Python", "Java", "Kotlin", "C#", "Go", "Ruby", "Rust", "Dart", "Swift", "Other language"]
TARGETS = {"website_existing": "Existing website / checkout", "website_new": "New website", "telegram": "Telegram bot",
           "discord": "Discord bot", "mobile": "Mobile app + backend"}
HINTS = {
    "JavaScript": "Use the runtime's built-in fetch (Node 18+) and the built-in crypto module (createHmac, timingSafeEqual). For webhooks read the RAW body (e.g. express.raw).",
    "TypeScript": "Use fetch (Node 18+) and the built-in crypto module with typed response interfaces. For webhooks read the RAW body (e.g. express.raw).",
    "PHP": "Use cURL for HTTP, hash_hmac + hash_equals for signatures, and file_get_contents('php://input') for the RAW webhook body.",
    "Python": "Use requests (or httpx), hmac + hashlib + hmac.compare_digest. Read the RAW webhook body (request.get_data / await request.body()).",
    "Java": "Use java.net.http.HttpClient, javax.crypto.Mac (HmacSHA256) and MessageDigest.isEqual. Read the RAW webhook body bytes.",
    "Kotlin": "Use the project's HTTP client (OkHttp/Ktor), javax.crypto.Mac (HmacSHA256) and MessageDigest.isEqual. Read the RAW webhook body.",
    "C#": "Use HttpClient, System.Security.Cryptography.HMACSHA256 and CryptographicOperations.FixedTimeEquals. Read the RAW webhook body.",
    "Go": "Use net/http, crypto/hmac + crypto/sha256 and hmac.Equal. Read the RAW webhook body with io.ReadAll.",
    "Ruby": "Use Net::HTTP (or Faraday), OpenSSL::HMAC and Rack::Utils.secure_compare. Read the RAW webhook body (request.body.read).",
    "Rust": "Use reqwest, the hmac + sha2 crates and constant-time comparison (subtle). Read the RAW webhook body bytes.",
    "Dart": "Use package:http and package:crypto (Hmac(sha256, ...)). Read the RAW webhook body.",
    "Swift": "Use URLSession and CryptoKit (HMAC<SHA256>, constant-time compare). Read the RAW webhook body.",
    "Other language": "Use the language's standard HTTP client and an HMAC-SHA256 implementation with constant-time comparison. Read the RAW webhook body.",
}
TARGET_TASKS = {
    "website_existing": "Add a 'Pay Now' flow to my EXISTING website/checkout: on click, my server creates the payment, then shows the returned QR (qr_url) and amount, or redirects to payment_url; the page polls my server for status; the order is marked paid only after verification.",
    "website_new": "Build a small NEW website with a checkout page (amount/order form, QR display with countdown, success and expired screens) plus the server routes that call the API and a webhook endpoint.",
    "telegram": "Build/extend a TELEGRAM BOT with a /buy command: create the payment, send the QR as a PNG photo (qr_png_url) with the exact amount, poll status (or use the webhook) and deliver the product once, only after SUCCESS.",
    "discord": "Build/extend a DISCORD BOT with a /pay command: create the payment, post the QR image (qr_png_url) and exact amount, poll status (or use the webhook) and deliver the role/product once, only after SUCCESS.",
    "mobile": "Add payments to my MOBILE APP through MY BACKEND: the backend creates the payment and returns payment_url/qr_png_url/amount to the app; the app shows it and asks the backend for status. The API key must never be inside the app.",
}
def build_prompt(base, lang, target, gateway="", details=""):
    base = base.rstrip("/"); api = base + "/api"
    lang = lang if lang in LANGS else "JavaScript"; target = target if target in TARGETS else "website_existing"
    gw = gateway if gateway in ("paytm", "fampay") else ""
    gwline = (f'Always send "gateway": "{gw}" (this merchant uses {gw} only; ignore every detail about the other gateway).' if gw
              else 'Make the gateway configurable (env PAYMENT_GATEWAY_GATEWAY = "paytm" or "fampay") and always send it explicitly.')
    langname = "my project's language" if lang == "Other language" else lang
    ex = "existing " if target == "website_existing" else ""
    out = f"""You are a senior {langname} engineer working directly inside my {ex}project. Implement a complete, production-ready PAYMENT GATEWAY integration for: {TARGETS[target]}. Inspect the repository first, then modify the real files; do not only give me a tutorial or isolated snippets. Preserve the existing architecture and UI. Make reasonable decisions without repeatedly asking me questions, and clearly report the files changed and how to test them when finished.

TASK
{TARGET_TASKS[target]}

OFFICIAL SOURCES
- Site URL: {base}
- API base URL: {api}
- Full documentation (plain text): {base}/docs-api.txt
- OpenAPI 3.1 specification: {base}/openapi.yaml
- Environment variables: PAYMENT_GATEWAY_API_KEY, PAYMENT_GATEWAY_WEBHOOK_SECRET, PAYMENT_GATEWAY_BASE_URL={api}

TECHNOLOGY
- Implementation language: {lang}.
- {HINTS[lang]}
- Use the project's existing server runtime, routing, persistence and configuration style. Read secrets only from environment variables, never hard-code them.

API SUMMARY (all amounts are rupees, INR)
- Auth: send the key in header "X-API-Key: KEY" (or "Authorization: Bearer KEY"). 60 requests/minute per key. Call the API from the SERVER only; never expose the key in browser JavaScript or inside a mobile app.
- Create payment: POST {api}/v1/payment/create  JSON {{"amount": 99, "order_id": "YOUR-UNIQUE-ID", "gateway": "paytm|fampay", "customer": {{}}}}
  Response fields: success, order_id, pay_id, status, amount, currency, gateway, transaction_id, payment_url (hosted QR page), qr_url (SVG), qr_png_url (PNG), upi_uri (raw UPI string), expires_at. Payments expire after 10 minutes. Re-sending the same order_id returns the same payment (safe retries).
- {gwline}
- FamPay note: the returned "amount" can include a unique paise adjustment (+0.00 to +0.99). The customer must pay EXACTLY the returned amount, so always display the response's amount, not the requested one.
- Check status: GET {api}/v1/payment/status/ORDER_ID (or POST {api}/v1/payment/verify {{"order_id": "..."}}). Statuses: PENDING, SUCCESS (the only paid state), FAILED, EXPIRED, REFUND_PENDING, REFUNDED. Poll every 3-5 seconds while the customer pays.
- Webhook: the merchant sets an https URL in the dashboard. Headers X-Payment-Gateway-Timestamp (unix seconds) and X-Payment-Gateway-Signature ("sha256=" + hex). Signature = HMAC_SHA256(PAYMENT_GATEWAY_WEBHOOK_SECRET, timestamp + "." + rawBody). Events: payment.success, payment.failed, payment.expired, payment.refunded, webhook.test. Payload fields: event, event_id, order_id, pay_id, transaction_id, amount, currency, status, gateway.

REQUIREMENTS
1. Generate a unique order_id for every purchase and store it with the expected amount.
2. Create the payment server-side and render the QR from the returned qr_url / qr_png_url (or draw one from upi_uri). Show the exact amount and a countdown to expires_at.
3. Verify server-side: mark an order paid ONLY when status == SUCCESS and the amount equals the amount returned at creation. Never trust redirects, screenshots or user-entered UTRs.
4. Implement the webhook endpoint: use the RAW body, check the signature in constant time, reject timestamps older than 5 minutes, deduplicate by event_id, reply 2xx quickly.
5. Make order fulfilment idempotent (the webhook and polling may both report SUCCESS; deliver once).
6. Handle errors: JSON error shape {{"success": false, "error": "..."}} with 400/401/403/404/429/502; show friendly messages and retry only safe operations.
7. Add a short README section listing the environment variables and how to test with a Rs 1 payment.
"""
    if details.strip(): out += "\nPROJECT DETAILS FROM THE OWNER\n" + details.strip()[:1500] + "\n"
    return out
