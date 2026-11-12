"""
PAYMENT GATEWAY - Telegram bot example (python-telegram-bot v20+)

Install:   pip install "python-telegram-bot[job-queue]>=20,<22" requests
Run:       export TELEGRAM_BOT_TOKEN=...   PAYMENT_GATEWAY_API_KEY=ZP_...
           python payment_gateway_telegram_bot.py
Optional:  PAYMENT_GATEWAY_BASE_URL (default {{base}}/api), PAYMENT_GATEWAY_GATEWAY (paytm | fampay)

Commands:  /start   /buy <amount>   e.g.  /buy 99
The bot creates the payment, sends the QR, polls the status and calls on_paid() ONCE after SUCCESS.
"""
import os, io, time, asyncio, logging
import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
API_KEY = os.environ["PAYMENT_GATEWAY_API_KEY"]
BASE = os.getenv("PAYMENT_GATEWAY_BASE_URL", "{{base}}/api").rstrip("/")
GATEWAY = os.getenv("PAYMENT_GATEWAY_GATEWAY", "paytm")
HEAD = {"X-API-Key": API_KEY}
log = logging.getLogger("payment-gateway-bot")
logging.basicConfig(level=logging.INFO)
delivered = set()          # idempotency: never deliver the same order twice (use a database in production)
tasks = set()


def create_payment(amount, order_id):
    r = requests.post(BASE + "/v1/payment/create", headers=HEAD, timeout=20,
                      json={"amount": amount, "order_id": order_id, "gateway": GATEWAY})
    data = r.json()
    if not r.ok or not data.get("success"):
        raise RuntimeError(data.get("error", "payment create failed"))
    return data


def get_status(order_id):
    r = requests.get(BASE + "/v1/payment/status/" + order_id, headers=HEAD, timeout=20)
    return r.json() if r.ok else {}


def qr_png(url):
    return io.BytesIO(requests.get(url, timeout=20).content)


async def on_paid(chat_id, order_id, amount, context):
    """TODO: deliver your product / add balance here. Called once per paid order."""
    await context.bot.send_message(chat_id, "Payment received: Rs %s. Thank you!" % amount)


async def watch(chat_id, order_id, expires_in, context):
    deadline = time.time() + expires_in + 15
    while time.time() < deadline:
        await asyncio.sleep(5)
        st = await asyncio.to_thread(get_status, order_id)
        status = st.get("status")
        if status == "SUCCESS":
            if order_id not in delivered:
                delivered.add(order_id)
                await on_paid(chat_id, order_id, st.get("amount"), context)
            return
        if status in ("FAILED", "EXPIRED"):
            await context.bot.send_message(chat_id, "Payment %s. Send /buy to try again." % status.lower())
            return
    await context.bot.send_message(chat_id, "Payment timed out. Send /buy to try again.")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Welcome! Use /buy <amount>, for example /buy 99")


async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(context.args[0])
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /buy <amount>   e.g. /buy 99")
        return
    order_id = "TG-%s-%d" % (update.effective_user.id, int(time.time()))
    try:
        p = await asyncio.to_thread(create_payment, amount, order_id)
    except Exception as e:
        await update.message.reply_text("Could not create payment: %s" % e)
        return
    png = await asyncio.to_thread(qr_png, p["qr_png_url"])
    await update.message.reply_photo(png, caption="Pay exactly Rs %s within 10 minutes\n%s" % (p["amount"], p["payment_url"]))
    t = asyncio.create_task(watch(update.effective_chat.id, p["order_id"], 600, context))
    tasks.add(t); t.add_done_callback(tasks.discard)


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("buy", buy))
    app.run_polling()


if __name__ == "__main__":
    main()
