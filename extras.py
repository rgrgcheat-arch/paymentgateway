"""Part 2: webhooks, refunds, subscriptions, team, notifications, CSV, password reset, background worker."""
import os, io, csv, json, hmac, hashlib, time, secrets, threading, socket, ipaddress, smtplib
from urllib.parse import urlparse
from datetime import timedelta
from email.message import EmailMessage
import requests
from fastapi import APIRouter, Request, Depends, HTTPException, Form
from fastapi.responses import Response
from itsdangerous import URLSafeTimedSerializer, BadSignature
from sqlalchemy import select, func, delete
from sqlalchemy.exc import IntegrityError
from db import SessionLocal, get_db
from models import (User, ApiKey, GatewayCred, Txn, Plan, Subscription, WebhookEndpoint, WebhookDelivery, Refund,
                     UserSettings, Notification, now)
import security as sec, providers as gw
from core import render, flash, go, check_csrf, audit, current, owner_only, api_user, base_url, cfg_for, _lookup, refresh, tx_json

router = APIRouter()
BACKOFF = [60, 300, 1800, 7200, 21600, 43200]

# ---------- plans ----------
DEFAULT_PLANS = [("Free", 0, 0, 1000, 100, 1, "paytm,fampay", "Community"), ("Basic", 29900, 30, 10000, 1000, 2, "paytm,fampay", "Email"),
                 ("Pro", 99900, 30, 100000, 10000, 5, "paytm,fampay", "Priority"), ("Business", 299900, 30, 1000000, 100000, 20, "paytm,fampay", "Dedicated"),
                 ("Lifetime", 999900, 0, 1000000, 100000, 20, "paytm,fampay", "Priority")]
def plan_for(db, uid):
    s = db.scalar(select(Subscription).where(Subscription.user_id == uid, Subscription.status == "active").order_by(Subscription.id.desc()))
    if s and (s.end is None or s.end > now()): return db.get(Plan, s.plan_id), s
    if s: s.status = "expired"; db.commit()
    return db.scalar(select(Plan).where(Plan.name == "Free")), None
def max_keys(db, uid): return plan_for(db, uid)[0].max_keys
def check_plan(db, user, gateway):
    if user.role == "owner": return  # the owner collects plan payments and tests on own gateways without a plan
    p, _ = plan_for(db, user.id)
    if gateway not in [g.strip() for g in (p.gateways or "").split(",")]: raise HTTPException(403, "No active plan for this gateway. Buy or renew a plan on the Subscriptions page.")
    m0 = now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if db.scalar(select(func.count()).where(Txn.user_id == user.id, Txn.created >= m0)) >= p.txn_limit:
        raise HTTPException(403, "Monthly transaction limit reached for your plan")

# ---------- notifications ----------
def send_mail(to, subj, body):
    host = os.getenv("SMTP_HOST")
    if not host: return False
    try:
        m = EmailMessage(); m["From"] = os.getenv("SMTP_FROM", os.getenv("SMTP_USER", "")); m["To"] = to; m["Subject"] = subj; m.set_content(body)
        with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", "587")), timeout=10) as s:
            s.starttls()
            if os.getenv("SMTP_USER"): s.login(os.getenv("SMTP_USER"), os.getenv("SMTP_PASS", ""))
            s.send_message(m)
        return True
    except Exception as e: print("mail error", e); return False
def _send(tg, em, kind, msg):
    tok = os.getenv("TELEGRAM_BOT_TOKEN")
    if tg and tok:
        try: requests.post(f"https://api.telegram.org/bot{tok}/sendMessage", json={"chat_id": tg, "text": msg}, timeout=8)
        except Exception: pass
    if em: send_mail(em, f"Payment Gateway: {kind}", msg)
def notify(db, uid, kind, msg):
    s, u = db.get(UserSettings, uid), db.get(User, uid)
    if not u: return
    db.add(Notification(user_id=uid, kind=kind, message=msg[:300])); db.commit()
    threading.Thread(target=_send, args=(s.telegram_chat_id if s and s.notify_telegram and s.telegram_chat_id else None,
                                         u.email if (s is None or s.notify_email) else None, kind, msg), daemon=True).start()

# ---------- webhooks ----------
def safe_url(url):
    p = urlparse(url or "")
    if p.scheme not in ("http", "https") or not p.hostname: return False
    if os.getenv("RENDER") and p.scheme != "https": return False
    try:
        for a in socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80)):
            ip = ipaddress.ip_address(a[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast: return False
    except Exception: return False
    return True
def sign(secret, ts, body): return hmac.new(secret.encode(), f"{ts}.{body}".encode(), hashlib.sha256).hexdigest()
def _new_delivery(db, uid, event_id, event, data):
    d = WebhookDelivery(user_id=uid, event_id=event_id, event=event, payload=json.dumps({"event": event, "event_id": event_id, **data}))
    db.add(d)
    try: db.commit(); return d
    except IntegrityError: db.rollback(); return None  # duplicate event protection
def enqueue(db, uid, event_id, event, data):
    ep = db.scalar(select(WebhookEndpoint).where(WebhookEndpoint.user_id == uid, WebhookEndpoint.enabled == True))  # noqa
    return _new_delivery(db, uid, event_id, event, data) if ep else None
def deliver(db, d):
    ep = db.scalar(select(WebhookEndpoint).where(WebhookEndpoint.user_id == d.user_id))
    ok = False
    if not ep or not safe_url(ep.url): d.last_error = "Endpoint missing or URL not allowed"
    else:
        ts = str(int(time.time())); secret = sec.dec(ep.secret_enc)["s"]
        try:
            r = requests.post(ep.url, data=d.payload, timeout=10, allow_redirects=False, headers={
                "Content-Type": "application/json", "X-Payment-Gateway-Timestamp": ts, "X-Payment-Gateway-Signature": "sha256=" + sign(secret, ts, d.payload)})
            d.last_code = r.status_code; ok = 200 <= r.status_code < 300; d.last_error = "" if ok else f"HTTP {r.status_code}"
        except Exception as e: d.last_error = str(e)[:200]
    d.attempts += 1; failed = False
    if ok: d.status = "DELIVERED"
    elif d.attempts >= (ep.max_retries if ep else 1): d.status = "FAILED"; failed = True
    else: d.next_try = now() + timedelta(seconds=BACKOFF[min(d.attempts - 1, len(BACKOFF) - 1)])
    db.commit()
    if failed and not d.event_id.startswith("test:"): notify(db, d.user_id, "webhook_failed", f"Webhook {d.event} ({d.event_id}) failed after {d.attempts} attempts: {d.last_error}")
    return d

# ---------- payment status changes ----------
EVENTS = {"SUCCESS": "payment.success", "FAILED": "payment.failed", "EXPIRED": "payment.expired", "REFUNDED": "payment.refunded"}
def on_change(db, t):
    if (t.merchant_order_id or "").startswith("SUB-"):  # a plan purchase paid on the owner's gateway: unlock the plan, no merchant webhooks
        from billing import on_sub_txn; on_sub_txn(db, t); return
    if '"test": true' in (t.customer or ""): return  # Rs 1 gateway test payments never trigger webhooks/alerts
    ev = EVENTS.get(t.status)
    if not ev: return
    enqueue(db, t.user_id, f"{t.pay_id}:{ev}", ev, {"order_id": t.merchant_order_id or t.pay_id, "pay_id": t.pay_id, "transaction_id": t.provider_ref,
            "amount": t.amount / 100, "currency": t.currency, "status": t.status, "gateway": t.gateway})
    if t.status == "SUCCESS": notify(db, t.user_id, "payment_success", f"Payment received: ₹{t.amount/100:.2f} for {t.merchant_order_id or t.pay_id}")
    elif t.status == "FAILED": notify(db, t.user_id, "payment_failed", f"Payment failed: {t.merchant_order_id or t.pay_id}")

# ---------- refunds ----------
def sync_refund_status(db, t):
    rs = db.scalars(select(Refund).where(Refund.txn_id == t.id)).all()
    paid = sum(r.amount for r in rs if r.status == "SUCCESS"); old = t.status
    t.status = "REFUNDED" if paid >= t.amount else ("REFUND_PENDING" if any(r.status == "PENDING" for r in rs) else "SUCCESS")
    if t.status != old: t.updated = now(); db.commit(); on_change(db, t)
    else: db.commit()
def do_refund(db, user, t, amount=None):
    if t.user_id != user.id: raise HTTPException(404, "Order not found")
    if t.status not in ("SUCCESS", "REFUND_PENDING"): raise HTTPException(400, "Only paid orders can be refunded")
    used = db.scalar(select(func.coalesce(func.sum(Refund.amount), 0)).where(Refund.txn_id == t.id, Refund.status.in_(("PENDING", "SUCCESS"))))
    amount = amount or (t.amount - used)
    if amount <= 0 or amount + used > t.amount: raise HTTPException(400, "Invalid refund amount")
    cfg, env = cfg_for(db, t.user_id, t.gateway)
    if not cfg: raise HTTPException(400, "Gateway not enabled")
    rid = "RF" + secrets.token_hex(8).upper()
    try: res = gw.get(t.gateway).refund(cfg, env, t.pay_id, t.provider_ref, rid, amount)
    except Exception as e: raise HTTPException(502, str(e)[:200])
    if res["state"] == "FAILED": raise HTTPException(400, f"Provider rejected refund: {res['msg']}")
    r = Refund(txn_id=t.id, user_id=user.id, refund_id=rid, amount=amount, status=res["state"]); db.add(r); db.flush()
    audit(db, user.id, "refund", f"{t.pay_id} {amount}"); sync_refund_status(db, t); return r
def refresh_refund(db, r):
    t = db.get(Txn, r.txn_id); cfg, env = cfg_for(db, t.user_id, t.gateway)
    if not cfg: return
    res = gw.get(t.gateway).refund_status(cfg, env, t.pay_id, r.refund_id)
    if res["state"] != r.status: r.status = res["state"]; db.flush(); sync_refund_status(db, t)
@router.post("/api/v1/payment/refund")
async def api_refund(request: Request, u=Depends(api_user), db=Depends(get_db)):
    b = await request.json(); t = _lookup(db, u, str(b.get("order_id", "")))
    amt = round(float(b["amount"]) * 100) if b.get("amount") else None
    r = do_refund(db, u, t, amt)
    return {"success": True, "refund_id": r.refund_id, "refund_status": r.status, "amount": r.amount / 100, "order_status": t.status}
@router.post("/api/v1/payment/verify")
async def api_verify_post(request: Request, u=Depends(api_user), db=Depends(get_db)):
    b = await request.json(); return {"success": True, **tx_json(refresh(db, _lookup(db, u, str(b.get("order_id", "")))), request)}
@router.post("/transactions/{pay_id}/refund")
def ui_refund(pay_id: str, request: Request, amount: str = Form(""), csrf: str = Form(""), u=Depends(current), db=Depends(get_db)):
    check_csrf(request, csrf); t = db.scalar(select(Txn).where(Txn.pay_id == pay_id, Txn.user_id == u.id))
    if not t: raise HTTPException(404)
    try: do_refund(db, u, t, round(float(amount) * 100) if amount.strip() else None); flash(request, "Refund requested.")
    except HTTPException as e: flash(request, str(e.detail), "err")
    return go("/transactions")

# ---------- CSV ----------
@router.get("/transactions/export.csv")
def export_csv(status: str = "", gateway: str = "", search: str = "", u=Depends(current), db=Depends(get_db)):
    q = select(Txn).where(Txn.user_id == u.id)
    if status: q = q.where(Txn.status == status)
    if gateway: q = q.where(Txn.gateway == gateway)
    if search: q = q.where(Txn.pay_id.contains(search) | Txn.merchant_order_id.contains(search))
    esc = lambda v: ("'" + v) if isinstance(v, str) and v and v[0] in "=+-@" else v
    buf = io.StringIO(); buf.write("\ufeff"); w = csv.writer(buf)
    w.writerow(["Order ID", "Pay ID", "Customer", "Amount", "Currency", "Gateway", "Status", "Provider Ref", "Created (UTC)"])
    for t in db.scalars(q.order_by(Txn.id.desc()).limit(10000)):
        try: cn = json.loads(t.customer).get("name", "")
        except Exception: cn = ""
        w.writerow([esc(x) for x in [t.merchant_order_id or "", t.pay_id, cn, f"{t.amount/100:.2f}", t.currency, t.gateway, t.status, t.provider_ref, t.created.isoformat()]])
    return Response(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=transactions.csv"})

# ---------- webhook UI + API ----------
@router.get("/webhooks")
def wh_page(request: Request, u=Depends(current), db=Depends(get_db)):
    ep = db.scalar(select(WebhookEndpoint).where(WebhookEndpoint.user_id == u.id))
    ds = db.scalars(select(WebhookDelivery).where(WebhookDelivery.user_id == u.id).order_by(WebhookDelivery.id.desc()).limit(30)).all()
    return render(request, "webhooks.html", u, ep=ep, ds=ds, newsecret=request.session.pop("wsecret", None))
@router.post("/webhooks/save")
def wh_save(request: Request, url: str = Form(), max_retries: int = Form(5), enabled: str = Form("1"), csrf: str = Form(""), u=Depends(current), db=Depends(get_db)):
    check_csrf(request, csrf); url = url.strip()
    if not safe_url(url): flash(request, "URL must be a public https address.", "err"); return go("/webhooks")
    ep = db.scalar(select(WebhookEndpoint).where(WebhookEndpoint.user_id == u.id)); mr = max(1, min(10, max_retries))
    if ep: ep.url, ep.max_retries, ep.enabled = url, mr, enabled == "1"
    else:
        s = secrets.token_hex(24); db.add(WebhookEndpoint(user_id=u.id, url=url, secret_enc=sec.enc({"s": s}), max_retries=mr, enabled=enabled == "1")); request.session["wsecret"] = s
    db.commit(); flash(request, "Webhook saved."); return go("/webhooks")
@router.post("/webhooks/regenerate")
def wh_regen(request: Request, csrf: str = Form(""), u=Depends(current), db=Depends(get_db)):
    check_csrf(request, csrf); ep = db.scalar(select(WebhookEndpoint).where(WebhookEndpoint.user_id == u.id))
    if ep:
        s = secrets.token_hex(24); ep.secret_enc = sec.enc({"s": s}); request.session["wsecret"] = s; audit(db, u.id, "webhook_secret_rotate"); db.commit()
        notify(db, u.id, "secret_rotated", "Your webhook signing secret was regenerated.")
    return go("/webhooks")
def _send_test(db, u):
    ep = db.scalar(select(WebhookEndpoint).where(WebhookEndpoint.user_id == u.id))
    if not ep: raise HTTPException(400, "Configure a webhook URL first")
    return deliver(db, _new_delivery(db, u.id, "test:" + secrets.token_hex(6), "webhook.test", {"message": "Test event from Payment Gateway"}))
@router.post("/webhooks/test")
def wh_test(request: Request, csrf: str = Form(""), u=Depends(current), db=Depends(get_db)):
    check_csrf(request, csrf)
    try: d = _send_test(db, u); flash(request, f"Test {d.status.lower()} (HTTP {d.last_code or '-'} {d.last_error})", "ok" if d.status == "DELIVERED" else "err")
    except HTTPException as e: flash(request, e.detail, "err")
    return go("/webhooks")
@router.post("/webhooks/{did}/retry")
def wh_retry(did: int, request: Request, csrf: str = Form(""), u=Depends(current), db=Depends(get_db)):
    check_csrf(request, csrf); d = db.get(WebhookDelivery, did)
    if d and d.user_id == u.id and d.status == "FAILED": d.status, d.attempts, d.next_try = "PENDING", 0, now(); db.commit()
    return go("/webhooks")
@router.post("/api/v1/webhooks/test")
def api_wh_test(u=Depends(api_user), db=Depends(get_db)):
    d = _send_test(db, u); return {"success": d.status == "DELIVERED", "status": d.status, "http_status": d.last_code, "error": d.last_error}

# ---------- settings ----------
@router.get("/settings")
def settings(request: Request, u=Depends(current)): return go("/notifications")  # settings page removed from the app
@router.post("/settings")
def settings_save(request: Request, telegram_chat_id: str = Form(""), notify_email: str = Form(""), notify_telegram: str = Form(""), csrf: str = Form(""), u=Depends(current), db=Depends(get_db)):
    check_csrf(request, csrf); cid = telegram_chat_id.strip()
    if cid and not cid.lstrip("-").isdigit(): flash(request, "Telegram chat ID must be numeric.", "err"); return go("/settings")
    s = db.get(UserSettings, u.id)
    if not s: s = UserSettings(user_id=u.id); db.add(s)
    s.telegram_chat_id, s.notify_email, s.notify_telegram = cid, bool(notify_email), bool(notify_telegram and cid)
    db.commit(); flash(request, "Settings saved."); return go("/settings")

# ---------- subscriptions ----------
@router.get("/subscription/{sid}/receipt")
def receipt(sid: int, request: Request, u=Depends(current), db=Depends(get_db)):
    s = db.get(Subscription, sid)
    if not s or (s.user_id != u.id and u.role != "owner"): raise HTTPException(404)
    return render(request, "receipt.html", u, s=s, plan=db.get(Plan, s.plan_id), buyer=db.get(User, s.user_id))
# ---------- team (seller / reseller) ----------
def can_manage(u, t): return t.role != "owner" and (u.role == "owner" or (u.role == "seller" and t.parent_id == u.id))
@router.get("/team")
def team(request: Request, u=Depends(current), db=Depends(get_db)):
    if u.role not in ("owner", "seller"): raise HTTPException(403, "Not allowed")
    q = select(User).where(User.role.in_(("seller", "reseller")) if u.role == "owner" else User.parent_id == u.id)
    rows = db.scalars(q.order_by(User.id.desc())).all(); ids = [r.id for r in rows] or [0]
    st = {i: (c, v) for i, c, v in db.execute(select(Txn.user_id, func.count(), func.coalesce(func.sum(Txn.amount), 0)).where(Txn.user_id.in_(ids), Txn.status == "SUCCESS").group_by(Txn.user_id))}
    return render(request, "team.html", u, rows=rows, st=st)
@router.post("/team/create")
def team_create(request: Request, name: str = Form(), username: str = Form(), email: str = Form(), phone: str = Form(), password: str = Form(), role: str = Form("reseller"),
                csrf: str = Form(""), u=Depends(current), db=Depends(get_db)):
    check_csrf(request, csrf)
    if u.role == "seller": role = "reseller"
    elif u.role != "owner" or role not in ("seller", "reseller"): raise HTTPException(403)
    username, email = username.strip().lower(), email.strip().lower(); err = None
    if len(password) < 8: err = "Password must be 8+ characters."
    elif not username.replace("_", "").isalnum() or len(username) < 3: err = "Invalid username."
    elif db.scalar(select(User).where(User.username == username)): err = "Username taken."
    elif db.scalar(select(User).where(User.email == email)): err = "Email already registered."
    if err: flash(request, err, "err"); return go("/team")
    nu = User(name=name.strip(), username=username, email=email, phone=phone.strip(), pw_hash=sec.hash_pw(password), role=role, parent_id=u.id if u.role == "seller" else None)
    db.add(nu); db.flush(); audit(db, u.id, "team_create", f"{role} {username}"); db.commit(); flash(request, f"{role.capitalize()} created."); return go("/team")
@router.post("/team/{uid}/toggle")
def team_toggle(uid: int, request: Request, csrf: str = Form(""), u=Depends(current), db=Depends(get_db)):
    check_csrf(request, csrf); t = db.get(User, uid)
    if not t or not can_manage(u, t): raise HTTPException(403)
    t.status = "suspended" if t.status == "active" else "active"; audit(db, u.id, "user_toggle", f"{uid}->{t.status}"); db.commit(); return go("/team")
@router.post("/team/{uid}/delete")
def team_delete(uid: int, request: Request, csrf: str = Form(""), u=Depends(current), db=Depends(get_db)):
    check_csrf(request, csrf); t = db.get(User, uid)
    if not t or not can_manage(u, t): raise HTTPException(403)
    if db.scalar(select(func.count()).where(Txn.user_id == uid)) or db.scalar(select(func.count()).where(User.parent_id == uid)):
        flash(request, "Account has transactions or sub-accounts — suspend it instead.", "err"); return go("/team")
    for M in (ApiKey, GatewayCred, WebhookDelivery, WebhookEndpoint, Notification, UserSettings, Subscription): db.execute(delete(M).where(M.user_id == uid))
    db.delete(t); audit(db, u.id, "user_delete", str(uid)); db.commit(); flash(request, "Account deleted."); return go("/team")

# ---------- password reset ----------
ser = URLSafeTimedSerializer(sec.SECRET, salt="pwreset")
@router.get("/forgot")
def forgot_page(request: Request): return render(request, "forgot.html")
@router.post("/forgot")
def forgot(request: Request, email: str = Form(), csrf: str = Form(""), db=Depends(get_db)):
    check_csrf(request, csrf)
    if not sec.rate_ok(f"forgot:{request.client.host if request.client else '?'}", 5, 3600): flash(request, "Too many requests.", "err"); return go("/forgot")
    u = db.scalar(select(User).where(User.email == email.strip().lower()))
    if u and u.status == "active":
        link = f"{base_url(request)}/reset/{ser.dumps({'u': u.id, 'h': u.pw_hash[-12:]})}"
        if not send_mail(u.email, "Reset your Payment Gateway password", f"Open this link within 1 hour to reset your password:\n{link}"):
            flash(request, "Email service is not configured on this server (SMTP_* variables).", "err"); return go("/forgot")
    flash(request, "If that email exists, a reset link has been sent."); return go("/login")
def _tok(db, token):
    try: d = ser.loads(token, max_age=3600)
    except BadSignature: return None
    u = db.get(User, d["u"]); return u if u and u.pw_hash[-12:] == d["h"] else None
@router.get("/reset/{token}")
def reset_page(token: str, request: Request, db=Depends(get_db)):
    if not _tok(db, token): flash(request, "Link invalid or expired.", "err"); return go("/forgot")
    return render(request, "reset.html", token=token)
@router.post("/reset/{token}")
def reset(token: str, request: Request, password: str = Form(), confirm: str = Form(), csrf: str = Form(""), db=Depends(get_db)):
    check_csrf(request, csrf); u = _tok(db, token)
    if not u: flash(request, "Link invalid or expired.", "err"); return go("/forgot")
    if len(password) < 8 or password != confirm: flash(request, "Passwords must match and be 8+ characters.", "err"); return go(f"/reset/{token}")
    u.pw_hash = sec.hash_pw(password); audit(db, u.id, "password_reset"); db.commit(); flash(request, "Password updated. Please log in."); return go("/login")

# ---------- startup + background worker ----------
def worker():
    last = 0
    while True:
        try:
            with SessionLocal() as db:
                for d in db.scalars(select(WebhookDelivery).where(WebhookDelivery.status == "PENDING", WebhookDelivery.next_try <= now()).limit(20)).all(): deliver(db, d)
                if time.time() - last > 15:  # fast reconciliation so bots/websites get their webhook within seconds of payment
                    last = time.time()
                    for t in db.scalars(select(Txn).where(Txn.status == "PENDING", Txn.created >= now() - timedelta(days=1)).order_by(Txn.id.desc()).limit(40)).all(): refresh(db, t)  # reconciliation
                    for r in db.scalars(select(Refund).where(Refund.status == "PENDING").limit(20)).all(): refresh_refund(db, r)
                    for s in db.scalars(select(Subscription).where(Subscription.status == "active", Subscription.end != None)).all():  # noqa
                        if s.end <= now(): s.status = "expired"; db.commit(); notify(db, s.user_id, "subscription_expired", "Your subscription has expired. You are now on the Free plan.")
                        elif s.end - now() < timedelta(days=3) and not s.notified: s.notified = True; db.commit(); notify(db, s.user_id, "subscription_expiring", f"Your subscription expires on {s.end:%d %b %Y}.")
        except Exception as e: print("worker error:", e)
        time.sleep(5)
_started = False
def start():
    global _started
    with SessionLocal() as db:
        if not db.scalar(select(func.count()).select_from(Plan)):
            for n, pr, d, rq, tx, k, g, sp in DEFAULT_PLANS: db.add(Plan(name=n, price=pr, duration_days=d, req_limit=rq, txn_limit=tx, max_keys=k, gateways=g, support=sp))
            db.commit()
    if not _started: _started = True; threading.Thread(target=worker, daemon=True).start()
