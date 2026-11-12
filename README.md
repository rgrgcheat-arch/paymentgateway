# Payment Gateway — Part 1
## Render deploy
1. Push to GitHub → Render "New Web Service" (or Blueprint using render.yaml). Build: `pip install -r requirements.txt`, Start: `gunicorn app:app -c gunicorn.conf.py` (or `uvicorn main:app --host 0.0.0.0 --port $PORT` — both work)
2. Env vars: SECRET_KEY (random, never change later or saved credentials become unreadable), ADMIN_EMAIL, ADMIN_PASSWORD (creates Owner on first boot), DATABASE_URL (Render Postgres internal URL).
3. Without DATABASE_URL it uses SQLite — Render free disk is wiped on every redeploy/sleep, so use Postgres.
4. Login as owner → /admin. Merchants: register → Gateways (Paytm staging creds) → Test → Enable → API Keys.
Free Render sleeps after 15 min idle; first request is slow.

## Part 2 additions
Optional env vars: SMTP_HOST, SMTP_PORT (587), SMTP_USER, SMTP_PASS, SMTP_FROM (email alerts + password reset); TELEGRAM_BOT_TOKEN (Telegram alerts; users add their chat ID in Settings).
Background worker (webhook retries, pending-payment reconciliation, refund status, subscription expiry) runs inside the web process. On Render free it only runs while the service is awake.
Plans (Free/Basic/Pro/Business/Lifetime) are seeded on first boot; edit them in Owner Panel → Plans.
Not yet done: maintenance mode, support tickets, IP whitelist, enforcing the plan's API-request limit, automatic collection of subscription payments (owner activates plans manually).

## Keep-alive
The service pings its own /healthz every 5 min (uses RENDER_EXTERNAL_URL, set by Render automatically) so the free instance does not go to sleep.

## Part 3 (final)
- Admin login: set ADMIN_USERNAME + ADMIN_EMAIL + ADMIN_PASSWORD (first boot) -> log in at /login with the username or email -> /admin.
- FamPay: Gateways -> FamPay (Shop name, UPI ID, Gmail, 16-digit App Password) -> Test Gmail -> Enable. Gmail needs 2-step verification + IMAP on.
- API: /api/create_order.php?amount=99&api_key=KEY , /api/verify.php?order_id=ORD..&api_key=KEY , /checkout.php?amount=99&api_key=KEY
- FamPay orders get a unique paise amount (+0..+99 paise) so each payment can be matched to its order.

## Part 4 (docs + theme)
- Docs page (/docs-api) now has 3 tabs: Getting started, Overview, AI integration prompt. Downloads: /openapi.yaml, /downloads/payment_gateway_telegram_bot.py, /docs-api.txt, and the prompt as .txt for the chosen language/target.
- Owner Panel -> "Public base URL": set your public Render link or domain; it is used in docs, OpenAPI, bot file and prompts (empty = automatic).
- Pink/white theme everywhere; dark mode is plum/pink.

## Part 5 (profile + merchant setup)
- /profile (menu: My profile): photo upload (JPG/PNG/WebP, max 2 MB, stored in the database so Render redeploys do not lose it), name, phone, business details, GSTIN, address, change password, theme. Username and login email are read-only.
- "Gateways" is now "Merchant setup": cards per gateway (Edit / Test Rs 1 / Pause / Delete) and one Add/Edit form with a Paytm/FamPay provider switch. The shop-name field is empty by default (placeholder "Your shop name").
- New columns are added to the existing database automatically on startup.

## Part 6 (plans per gateway, trial, auto-unlock, broadcast)
- Register: business name, contact name, work email, phone, password (8+, uppercase, number), terms. Username is auto-made from the email (login with email). Admin sees every field in Owner Panel -> user.
- Subscriptions (/subscription): 12-hour trial (one per account, price/hours editable), FamPay / Paytm plans (30/90/180/365 days). Only the gateway a user has bought is visible in Merchant setup, New payment and the API; an expired plan locks it again and stops new payments.
- Buying: tap Buy -> QR created with the OWNER's own FamPay/Paytm setup (log in as owner -> Merchant setup, test with Rs 1, enable) -> auto-verified -> plan unlocks, money goes to your bank. Owner Panel -> Plans & pricing -> "Where plan payments go" picks the gateway.
- Switching plans credits unused days: list price x remaining days / total days (Rs129 x 23/30 = Rs98.90). If the credit covers the new price the plan switches instantly.
- Owner Panel -> Plans & pricing: edit names, visibility, gateways, every tenure price, trial price/hours; assign plans by hand. Owner Panel -> Broadcast: message to all / paid / trial / expired / FamPay / Paytm users (shows in the bell + Notifications page).
- Existing merchants on the old Free plan are now locked until they buy/start trial (or you assign a plan). Paid legacy plans keep working until expiry.

## Part 7 (backup)
- Owner Panel -> Backup: download one .json.gz with every table; upload it to restore. "Add missing only" keeps current data and brings back missing users/records; "Replace everything" (type RESTORE) loads the backup exactly in one transaction (all-or-nothing). Columns that do not exist in the backup get defaults, so it works after you add new features. Use the same SECRET_KEY, otherwise saved gateway credentials / API keys cannot be decrypted. You are signed out after a restore.

## Part 8 (auto-backup every 12 hours)
- Owner Panel -> Backup -> "Automatic backup to Telegram": bot token (@BotFather), your chat ID, interval (default 12 h), ON/OFF. Press Start on the bot once.
- Alternative (survives even a wiped database): set Render env vars TELEGRAM_BOT_TOKEN and BACKUP_CHAT_ID; the panel fields override them if filled.
- "Send now (test)" sends a backup immediately. Failed sends retry every 10 min. The last result is shown on the page.
- Restore: download the .json.gz from Telegram -> Backup page -> Upload & restore.
