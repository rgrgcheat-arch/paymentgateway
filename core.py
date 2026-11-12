import os, io, re, json, secrets
from datetime import timedelta, datetime
from fastapi import FastAPI, Request, Depends, HTTPException, Form, Header
from fastapi.responses import RedirectResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import select, func
from sqlalchemy.orm import Session
import segno
from db import Base, engine, SessionLocal, get_db
from models import User, GatewayCred, ApiKey, Txn, AuditLog, Subscription, LoginLog, Setting, now
import security as sec
import providers as gw
import time as _time

app = FastAPI(title="Payment Gateway")
app.add_middleware(SessionMiddleware, secret_key=sec.SECRET, https_only=bool(os.getenv("RENDER")), same_site="lax", max_age=86400 * 7)
_HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIRS = [os.path.join(_HERE, "templates"), _HERE]  # works with a templates/ folder OR html files placed next to main.py
_NEED = ["base", "newpayment", "home", "login", "register", "dashboard", "transactions", "gateways", "keys", "docs", "checkout", "admin", "forgot", "reset", "webhooks", "settings", "subscription", "receipt", "admin_subs", "team", "admin_user"]
_missing = [n + ".html" for n in _NEED if not any(os.path.exists(os.path.join(d, n + ".html")) for d in TEMPLATE_DIRS)]
if _missing: raise ImportError("Missing template files (upload them to GitHub): " + ", ".join(_missing))
templates = Jinja2Templates(directory=TEMPLATE_DIRS)
templates.env.filters["rs"] = lambda p: f"₹{(p or 0)/100:,.2f}"
from datetime import timezone as _tz, timedelta as _td
try:
    from zoneinfo import ZoneInfo as _ZI
    _LOCAL_TZ = _ZI(os.getenv("DISPLAY_TZ", "Asia/Kolkata"))
except Exception: _LOCAL_TZ = _tz(_td(hours=5, minutes=30))  # IST fallback when the server has no tz database
def ist(d):
    """Stored times are UTC; this converts for display (default Asia/Kolkata, change with env DISPLAY_TZ)."""
    if not d: return d
    try: return d.replace(tzinfo=_tz.utc).astimezone(_LOCAL_TZ).replace(tzinfo=None)
    except Exception: return d
templates.env.filters["ist"] = ist

def _gate_ok(request, db, u, g):
    from billing import allowed
    if g in allowed(db, u): return True
    flash(request, "This gateway is locked. Buy or renew a plan to unlock it.", "err"); return False
def _startup_inner():
    Base.metadata.create_all(engine)
    try:  # add new columns to existing tables (create_all does not alter tables)
        from sqlalchemy import inspect, text
        want = {"api_keys": [("key_enc", "TEXT DEFAULT ''")],
                "users": [("business_name", "VARCHAR(120) DEFAULT ''"), ("business_type", "VARCHAR(40) DEFAULT ''"), ("gstin", "VARCHAR(20) DEFAULT ''"),
                          ("address", "TEXT DEFAULT ''"), ("photo_b64", "TEXT DEFAULT ''"), ("photo_mime", "VARCHAR(20) DEFAULT ''"), ("photo_v", "INTEGER DEFAULT 0"),
                          ("trial_used", "BOOLEAN DEFAULT FALSE"), ("notif_seen", "TIMESTAMP"), ("terms_at", "TIMESTAMP")],
                "plans": [("code", "VARCHAR(20) DEFAULT ''"), ("prices", "TEXT DEFAULT ''"), ("hours", "INTEGER DEFAULT 0"), ("active", "BOOLEAN DEFAULT TRUE"), ("sort", "INTEGER DEFAULT 0")],
                "transactions": [("msg_key", "VARCHAR(64) DEFAULT ''")],
                "subscriptions": [("value", "INTEGER DEFAULT 0"), ("days", "INTEGER DEFAULT 0"), ("source", "VARCHAR(12) DEFAULT 'admin'")]}
        for tbl, cols in want.items():
            have = [c["name"] for c in inspect(engine).get_columns(tbl)]
            for name, ddl in cols:
                if name not in have:
                    with engine.begin() as cn: cn.execute(text(f"ALTER TABLE {tbl} ADD COLUMN {name} {ddl}"))
    except Exception as e: print("migration error", e)
    em, pw = (os.getenv("ADMIN_EMAIL") or "admin@rgcheat.local"), (os.getenv("ADMIN_PASSWORD") or "909090")
    un = (os.getenv("ADMIN_USERNAME") or "RGCHEAT").lower()  # default admin login: RGCHEAT / 909090 (override with ADMIN_USERNAME / ADMIN_PASSWORD env vars)
    if pw:  # env is the source of truth for the owner login: created on first boot, updated if you change the env vars
        with SessionLocal() as db:
            o = db.scalar(select(User).where(User.role == "owner").order_by(User.id))
            if not o:
                db.add(User(name="Owner", username=un, email=em.lower(), phone="", pw_hash=sec.hash_pw(pw), role="owner"))
            else:
                o.username = un
                if not sec.check_pw(pw, o.pw_hash): o.pw_hash = sec.hash_pw(pw)
            db.commit()
    from extras import start
    start()
    from billing import seed_plans
    with SessionLocal() as db: seed_plans(db)

# ---------- helpers ----------
_SET = {}  # tiny per-process cache (30s) so an admin change reaches every gunicorn worker quickly
def get_setting(key, default=""):
    c = _SET.get(key)
    if c and _time.time() - c[1] < 5: return c[0] or default
    try:
        with SessionLocal() as d: r = d.get(Setting, key); v = r.value if r else ""
    except Exception: return default
    _SET[key] = (v, _time.time()); return v or default
def set_setting(db, key, value):
    r = db.get(Setting, key)
    if r: r.value = value
    else: db.add(Setting(key=key, value=value))
    db.commit(); _SET.pop(key, None)
def base_url(request):
    """Public URL used in the docs, downloads (txt / yaml / bot), payment links and QR urls.
    1) the URL saved by the owner in Owner Panel (always wins)  2) the address the visitor actually used  3) Render's own URL."""
    v = get_setting("base_url").strip().rstrip("/")
    if v.lower().endswith("/api"): v = v[:-4].rstrip("/")  # the docs add /api themselves
    if v: return v
    try:
        h = request.headers; host = (h.get("x-forwarded-host") or h.get("host") or "").split(",")[0].strip()
        if host: return f"{(h.get('x-forwarded-proto') or ('https' if os.getenv('RENDER') else request.url.scheme)).split(',')[0].strip()}://{host}"
    except Exception: pass
    return os.getenv("RENDER_EXTERNAL_URL") or str(request.base_url).rstrip("/")
def render(request, name, user=None, **ctx):
    s = request.session
    if "csrf" not in s: s["csrf"] = secrets.token_hex(16)
    bell = 0
    if user:
        try:
            from models import Notification
            with SessionLocal() as _d: bell = _d.scalar(select(func.count()).where(Notification.user_id == user.id, Notification.created > (user.notif_seen or datetime(1970, 1, 1)))) or 0
        except Exception: bell = 0
    ctx.update(request=request, user=user, csrf=s["csrf"], flash=s.pop("flash", None), base=base_url(request), bell=bell)
    return templates.TemplateResponse(request, name, ctx)
def flash(request, msg, kind="ok"): request.session["flash"] = {"m": msg, "k": kind}
def go(url): return RedirectResponse(url, status_code=303)
def check_csrf(request, token):
    if not token or not secrets.compare_digest(token, request.session.get("csrf", "")): raise HTTPException(400, "Bad CSRF token")
def audit(db, uid, action, detail=""): db.add(AuditLog(user_id=uid, action=action, detail=detail[:300]))
def current(request: Request, db: Session = Depends(get_db)):
    u = db.get(User, request.session.get("uid")) if request.session.get("uid") else None
    if not u or u.status != "active": request.session.clear(); raise HTTPException(303, headers={"Location": "/login"})
    return u
def owner_only(u=Depends(current)):
    if u.role != "owner": raise HTTPException(403, "Owner only")
    return u
def optional_user(request: Request, db: Session = Depends(get_db)):
    uid = request.session.get("uid"); return db.get(User, uid) if uid else None

# ---------- public + auth ----------
@app.get("/")
def home(request: Request, u=Depends(optional_user)): return go("/dashboard" if u else "/login")  # no landing page: straight to sign in / create account
@app.get("/login")
def login_page(request: Request): return render(request, "login.html")
@app.post("/login")
def login(request: Request, identity: str = Form(), password: str = Form(), csrf: str = Form(""), db: Session = Depends(get_db)):
    check_csrf(request, csrf)
    ip = request.client.host if request.client else "?"
    if not sec.rate_ok(f"login:{ip}:{identity.lower()}", 5, 300):
        flash(request, "Too many attempts. Try again in 5 minutes.", "err"); return go("/login")
    u = db.scalar(select(User).where((User.email == identity.lower()) | (User.username == identity.lower())))
    if not u or not sec.check_pw(password, u.pw_hash):
        db.add(LoginLog(user_id=u.id if u else None, identity=identity[:120], ip=ip, ok=False)); db.commit()
        flash(request, "Invalid credentials.", "err"); return go("/login")
    db.add(LoginLog(user_id=u.id, identity=identity[:120], ip=ip, ok=True))
    if u.status != "active": flash(request, "Account suspended.", "err"); return go("/login")
    request.session.clear(); request.session["uid"] = u.id; audit(db, u.id, "login", ip); db.commit()
    return go("/admin" if u.role == "owner" else "/dashboard")
@app.get("/register")
def register_page(request: Request): return render(request, "register.html")
@app.post("/register")
def register(request: Request, business_name: str = Form(""), name: str = Form(""), email: str = Form(""), phone: str = Form(""), password: str = Form(""),
             terms: str = Form(""), csrf: str = Form(""), db: Session = Depends(get_db)):
    check_csrf(request, csrf)
    bn, nm, email, ph = business_name.strip()[:120], name.strip()[:120], email.strip().lower(), re.sub(r"[\s\-()]", "", phone.strip())
    err = None
    if not bn: err = "Enter your business name."
    elif not nm: err = "Enter the contact person's full name."
    elif not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) or len(email) > 190: err = "Enter a valid work email."
    elif not re.fullmatch(r"\+?\d{10,15}", ph): err = "Enter a valid phone number (10-15 digits)."
    elif len(password) < 8 or not re.search(r"[A-Z]", password) or not re.search(r"\d", password): err = "Password needs 8+ characters, an uppercase letter and a number."
    elif not terms: err = "Please accept the Terms and Privacy Policy."
    elif db.scalar(select(User).where(User.email == email)): err = "Email already registered. Please sign in."
    if err: flash(request, err, "err"); return render(request, "register.html", None, form=dict(business_name=bn, name=nm, email=email, phone=phone.strip()))
    base_un = re.sub(r"[^a-z0-9_]", "", email.split("@")[0])[:20]
    if len(base_un) < 3: base_un = "user"
    username = base_un
    while db.scalar(select(User).where(User.username == username)): username = f"{base_un[:20]}{secrets.randbelow(9000) + 1000}"
    u = User(name=nm, username=username, email=email, phone=ph, pw_hash=sec.hash_pw(password), business_name=bn, terms_at=now()); db.add(u); db.flush()
    audit(db, u.id, "register", bn); db.commit()
    try:
        from extras import notify
        ow = db.scalar(select(User).where(User.role == "owner").order_by(User.id))
        if ow: notify(db, ow.id, "new_user", f"New user: {bn} ({email}, {ph})")
    except Exception as e: print("owner notify error", e)
    request.session.clear(); request.session["uid"] = u.id
    return go("/subscription")
@app.get("/terms")
def terms_page(request: Request, u=Depends(optional_user)): return render(request, "legal.html", u, kind="terms")
@app.get("/privacy")
def privacy_page(request: Request, u=Depends(optional_user)): return render(request, "legal.html", u, kind="privacy")
@app.post("/logout")
def logout(request: Request, csrf: str = Form("")):
    check_csrf(request, csrf); request.session.clear(); return go("/")
@app.get("/docs-api.txt")  # downloadable plain-text docs (AI-ready)
def docs_txt(request: Request):
    body = templates.get_template("docs_api.txt").render(base=base_url(request))
    return Response(body, media_type="text/plain; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="payment-gateway-api-docs.txt"'})
@app.get("/openapi.yaml")
def openapi_yaml(request: Request):
    body = templates.get_template("openapi.yaml").render(base=base_url(request))
    return Response(body, media_type="application/yaml; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="payment-gateway-openapi.yaml"'})
@app.get("/downloads/payment_gateway_telegram_bot.py")
def dl_tgbot(request: Request):
    body = templates.get_template("payment_gateway_telegram_bot.py").render(base=base_url(request))
    return Response(body, media_type="text/x-python; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="payment_gateway_telegram_bot.py"'})
@app.get("/docs-api/prompt")
def docs_prompt(request: Request, lang: str = "JavaScript", target: str = "website_existing", gateway: str = "", details: str = ""):
    from docs_gen import build_prompt
    return Response(build_prompt(base_url(request), lang, target, gateway, details), media_type="text/plain; charset=utf-8")
@app.get("/docs-api")
def docs(request: Request, u=Depends(optional_user)):
    from docs_gen import LANGS, TARGETS
    return render(request, "docs.html", u, langs=LANGS, targets=TARGETS)
@app.get("/docs.php")
def docs_php(): return go("/docs-api")
@app.post("/admin/settings")
def admin_settings(request: Request, base_url_: str = Form("", alias="base_url"), csrf: str = Form(""), u=Depends(owner_only), db: Session = Depends(get_db)):
    check_csrf(request, csrf)
    v = base_url_.strip().rstrip("/")
    if v and not (v.startswith("https://") or v.startswith("http://")): flash(request, "Public URL must start with https://", "err"); return go("/admin")
    set_setting(db, "base_url", v); audit(db, u.id, "setting_base_url", v); db.commit()
    flash(request, "Public base URL saved." if v else "Public base URL cleared (automatic detection)."); return go("/admin")

# ---------- dashboard ----------
@app.get("/dashboard")
def dashboard(request: Request, u=Depends(current), db: Session = Depends(get_db)):
    by = dict(db.execute(select(Txn.status, func.count()).where(Txn.user_id == u.id).group_by(Txn.status)).all())
    total = sum(by.values()); ok = by.get("SUCCESS", 0)
    vol = db.scalar(select(func.coalesce(func.sum(Txn.amount), 0)).where(Txn.user_id == u.id, Txn.status == "SUCCESS"))
    today = now().replace(hour=0, minute=0, second=0, microsecond=0)
    t_all = db.scalar(select(func.count()).where(Txn.user_id == u.id, Txn.created >= today))
    t_ok = db.scalar(select(func.count()).where(Txn.user_id == u.id, Txn.created >= today, Txn.status == "SUCCESS"))
    t_vol = db.scalar(select(func.coalesce(func.sum(Txn.amount), 0)).where(Txn.user_id == u.id, Txn.created >= today, Txn.status == "SUCCESS"))
    gwise = db.execute(select(Txn.gateway, func.count(), func.coalesce(func.sum(Txn.amount), 0)).where(Txn.user_id == u.id, Txn.status == "SUCCESS").group_by(Txn.gateway)).all()
    days = []
    for i in range(6, -1, -1):
        d0 = today - timedelta(days=i); d1 = d0 + timedelta(days=1)
        days.append((d0.strftime("%a"), db.scalar(select(func.coalesce(func.sum(Txn.amount), 0)).where(Txn.user_id == u.id, Txn.status == "SUCCESS", Txn.created >= d0, Txn.created < d1))))
    recent = db.scalars(select(Txn).where(Txn.user_id == u.id).order_by(Txn.id.desc()).limit(5)).all()
    pend_amt = db.scalar(select(func.coalesce(func.sum(Txn.amount), 0)).where(Txn.user_id == u.id, Txn.status.in_(["PENDING", "CREATED"])))
    bad = by.get("FAILED", 0) + by.get("EXPIRED", 0)
    s = dict(total=total, ok=ok, failed=bad, pending=by.get("PENDING", 0) + by.get("CREATED", 0), vol=vol, t_all=t_all, t_ok=t_ok, t_vol=t_vol, pend_amt=pend_amt,
             rate=round(ok * 100 / total, 1) if total else 0, avg=int(vol / ok) if ok else 0)
    return render(request, "dashboard.html", u, s=s, gwise=gwise, days=days, maxd=max([d[1] for d in days] + [1]), recent=recent, noplan=(u.role != "owner" and not __import__("billing").allowed(db, u)))

@app.get("/transactions")
def transactions(request: Request, status: str = "", gateway: str = "", search: str = "", u=Depends(current), db: Session = Depends(get_db)):
    q = select(Txn).where(Txn.user_id == u.id)
    search = search.strip()
    if status: q = q.where(Txn.status == status)
    if gateway: q = q.where(Txn.gateway == gateway)
    if search: q = q.where(Txn.pay_id.icontains(search) | Txn.merchant_order_id.icontains(search) | Txn.provider_ref.icontains(search) | Txn.customer.icontains(search))
    return render(request, "transactions.html", u, rows=db.scalars(q.order_by(Txn.id.desc()).limit(200)).all(), f=dict(status=status, gateway=gateway, search=search))
@app.get("/transactions/new")
def txn_new(request: Request, u=Depends(current), db: Session = Depends(get_db)):
    from billing import allowed as _allowed
    al = _allowed(db, u)
    gws = [g for g in ("paytm", "fampay") if g in al and cfg_for(db, u.id, g)[0]]
    return render(request, "newpayment.html", u, gws=gws)
@app.post("/transactions/new")
def txn_new_post(request: Request, amount: float = Form(), order_id: str = Form(""), gateway: str = Form(""), csrf: str = Form(""), u=Depends(current), db: Session = Depends(get_db)):
    check_csrf(request, csrf)
    try: t = create_payment(db, u, round(amount * 100), pick_gateway(db, u, gateway), order_id.strip()[:64] or None, None)
    except HTTPException as e: flash(request, str(e.detail), "err"); return go("/transactions/new")
    return go(f"/pay/{t.pay_id}")

# ---------- gateways ----------
@app.get("/gateways")
def gateways(request: Request, edit: str = "", u=Depends(current), db: Session = Depends(get_db)):
    from billing import allowed as _allowed
    from extras import plan_for
    al = _allowed(db, u)
    creds = {c.gateway: c for c in db.scalars(select(GatewayCred).where(GatewayCred.user_id == u.id)) if c.gateway in al}  # only gateways the user bought
    cfgs = {}
    for g, c in creds.items():
        try: cfgs[g] = sec.dec(c.data_enc)
        except Exception: cfgs[g] = {}
    default = next((g for g in ("fampay", "paytm") if g in creds and creds[g].enabled), "")
    edit = edit if edit in ("paytm", "fampay") and edit in creds else ""
    return render(request, "gateways.html", u, creds=creds, cfgs=cfgs, default=default, edit=edit, adapters=gw.ADAPTERS, al=[g for g in ("fampay", "paytm") if g in al], plan=plan_for(db, u.id)[0])
@app.post("/gateways/{gname}/delete")
def gateway_delete(gname: str, request: Request, csrf: str = Form(""), u=Depends(current), db: Session = Depends(get_db)):
    check_csrf(request, csrf)
    c = db.scalar(select(GatewayCred).where(GatewayCred.user_id == u.id, GatewayCred.gateway == gname))
    if c: db.delete(c); audit(db, u.id, "gateway_delete", gname); db.commit(); flash(request, f"{gname.capitalize()} setup deleted.")
    return go("/gateways")
@app.post("/gateways/paytm/save")
def paytm_save(request: Request, shop: str = Form(""), upi: str = Form(), mid: str = Form(), enable: str = Form(""), csrf: str = Form(""), u=Depends(current), db: Session = Depends(get_db)):
    check_csrf(request, csrf)
    if not _gate_ok(request, db, u, "paytm"): return go("/subscription")
    upi, mid, shop = upi.strip(), mid.strip(), shop.strip()
    if not shop or "@" not in upi or not mid: flash(request, "Your shop name, UPI ID and MID are required.", "err"); return go("/gateways")
    c = db.scalar(select(GatewayCred).where(GatewayCred.user_id == u.id, GatewayCred.gateway == "paytm"))
    data = sec.enc({"shop": shop[:60], "upi": upi, "mid": mid, "key": ""})  # only Shop name + UPI ID + MID are needed
    if c: c.data_enc, c.env, c.enabled = data, "production", bool(enable)
    else: db.add(GatewayCred(user_id=u.id, gateway="paytm", data_enc=data, env="production", enabled=bool(enable)))
    audit(db, u.id, "gateway_save", "paytm"); db.commit(); flash(request, "Paytm setup saved. Press the Rs 1 test to confirm."); return go("/gateways")
@app.post("/gateways/{gname}/testpay")
def gateway_testpay(gname: str, request: Request, csrf: str = Form(""), u=Depends(current), db: Session = Depends(get_db)):
    """Creates a Rs 1 QR with the saved credentials (even if the gateway is not enabled yet). Pay it: SUCCESS = gateway works."""
    check_csrf(request, csrf)
    if not _gate_ok(request, db, u, gname): return go("/subscription")
    ad = gw.get(gname)
    if not ad or not ad.AVAILABLE: raise HTTPException(404)
    c = db.scalar(select(GatewayCred).where(GatewayCred.user_id == u.id, GatewayCred.gateway == gname))
    if not c: flash(request, "Save setup first.", "err"); return go("/gateways")
    if not sec.rate_ok(f"testpay:{u.id}", 10, 300): flash(request, "Too many tests. Wait a few minutes.", "err"); return go("/gateways")
    pay_id = new_pay_id(db, gname); amount = 100
    if gname == "fampay":
        used = set(db.scalars(select(Txn.amount).where(Txn.user_id == u.id, Txn.gateway == "fampay", Txn.status == "PENDING", Txn.expires_at > now())))
        amount += next((a for a in range(0, 100) if amount + a not in used), 0)
    try: r = ad.create(sec.dec(c.data_enc), c.env, pay_id, amount)
    except Exception as e: flash(request, f"Test failed: {str(e)[:200]}", "err"); return go("/gateways")
    t = Txn(pay_id=pay_id, user_id=u.id, merchant_order_id="TEST-" + pay_id[3:], gateway=gname, amount=amount, status="PENDING",
            customer=json.dumps({"test": True}), qr_data=r["qr_data"], provider_ref=r["provider_ref"], expires_at=now() + timedelta(minutes=10))
    db.add(t); audit(db, u.id, "gateway_testpay", gname); db.commit(); return go(f"/pay/{t.pay_id}")
@app.post("/gateways/paytm/{action}")
def paytm_action(action: str, request: Request, csrf: str = Form(""), u=Depends(current), db: Session = Depends(get_db)):
    check_csrf(request, csrf)
    if not _gate_ok(request, db, u, "paytm"): return go("/subscription")
    c = db.scalar(select(GatewayCred).where(GatewayCred.user_id == u.id, GatewayCred.gateway == "paytm"))
    if not c: flash(request, "Save credentials first.", "err"); return go("/gateways")
    if action == "test":
        try: ok, msg = gw.paytm.test(sec.dec(c.data_enc), c.env)
        except Exception as e: ok, msg = False, str(e)[:250]
        c.last_test_at, c.last_test_ok, c.last_test_msg = now(), ok, msg; flash(request, msg, "ok" if ok else "err")
    elif action in ("enable", "disable"):
        c.enabled = action == "enable"; flash(request, f"Paytm {action}d.")
    db.commit(); return go("/gateways")

@app.post("/gateways/fampay/save")
def fampay_save(request: Request, shop: str = Form(""), upi: str = Form(), gmail: str = Form(), app_password: str = Form(""), enable: str = Form(""), csrf: str = Form(""), u=Depends(current), db: Session = Depends(get_db)):
    check_csrf(request, csrf)
    if not _gate_ok(request, db, u, "fampay"): return go("/subscription")
    c = db.scalar(select(GatewayCred).where(GatewayCred.user_id == u.id, GatewayCred.gateway == "fampay"))
    old = sec.dec(c.data_enc) if c else {}
    pw = app_password.replace(" ", "").strip() or old.get("app_password", "")
    if not shop.strip() or not pw or "@" not in upi or "@" not in gmail: flash(request, "Your shop name, UPI ID, Gmail and App Password are required.", "err"); return go("/gateways")
    data = sec.enc({"shop": shop.strip()[:60], "upi": upi.strip(), "gmail": gmail.strip(), "app_password": pw})
    if c: c.data_enc, c.enabled = data, bool(enable)
    else: db.add(GatewayCred(user_id=u.id, gateway="fampay", data_enc=data, env="production", enabled=bool(enable)))
    audit(db, u.id, "gateway_save", "fampay"); db.commit(); flash(request, "FamPay setup saved (encrypted)."); return go("/gateways")
@app.post("/gateways/fampay/{action}")
def fampay_action(action: str, request: Request, csrf: str = Form(""), u=Depends(current), db: Session = Depends(get_db)):
    check_csrf(request, csrf)
    if not _gate_ok(request, db, u, "fampay"): return go("/subscription")
    c = db.scalar(select(GatewayCred).where(GatewayCred.user_id == u.id, GatewayCred.gateway == "fampay"))
    if not c: flash(request, "Save setup first.", "err"); return go("/gateways")
    if action == "test":
        ok, msg = gw.fampay.test(sec.dec(c.data_enc), c.env)
        c.last_test_at, c.last_test_ok, c.last_test_msg = now(), ok, msg; flash(request, msg, "ok" if ok else "err")
    elif action in ("enable", "disable"): c.enabled = action == "enable"; flash(request, f"FamPay {action}d.")
    db.commit(); return go("/gateways")

# ---------- api keys ----------
@app.get("/keys")
def keys(request: Request, u=Depends(current), db: Session = Depends(get_db)):
    from extras import plan_for
    rows = db.scalars(select(ApiKey).where(ApiKey.user_id == u.id).order_by(ApiKey.id.desc())).all()
    plan, sub = plan_for(db, u.id)
    return render(request, "keys.html", u, keys=rows, active=next((k for k in rows if not k.revoked), None), plan=plan, sub=sub)
@app.post("/keys/create")
def key_create(request: Request, label: str = Form(""), csrf: str = Form(""), u=Depends(current), db: Session = Depends(get_db)):
    check_csrf(request, csrf)
    label = label.strip()[:60]
    if not label: flash(request, "Enter your shop name first.", "err"); return go("/keys")
    if db.scalar(select(func.count()).where(ApiKey.user_id == u.id, ApiKey.revoked == False)):  # noqa
        flash(request, "Revoke or delete your current active key before generating another.", "err"); return go("/keys")
    k = sec.new_api_key()
    db.add(ApiKey(user_id=u.id, label=label, prefix=k[:9], key_hash=sec.hash_key(k), key_enc=sec.enc({"k": k})))
    audit(db, u.id, "key_create"); db.commit(); flash(request, "Live API key generated."); return go("/keys")
@app.post("/keys/{kid}/reveal")  # used by Show / Copy buttons (fetch)
def key_reveal(kid: int, request: Request, csrf: str = Form(""), u=Depends(current), db: Session = Depends(get_db)):
    check_csrf(request, csrf)
    k = db.get(ApiKey, kid)
    if not k or k.user_id != u.id or k.revoked: raise HTTPException(404, "Key not found")
    if not k.key_enc: raise HTTPException(409, "This older key cannot be shown. Delete it and generate a new one.")
    try: return {"key": sec.dec(k.key_enc)["k"]}
    except Exception: raise HTTPException(500, "Could not decrypt key (SECRET_KEY changed?)")
@app.post("/keys/{kid}/revoke")
def key_revoke(kid: int, request: Request, csrf: str = Form(""), u=Depends(current), db: Session = Depends(get_db)):
    check_csrf(request, csrf)
    k = db.get(ApiKey, kid)
    if k and k.user_id == u.id: k.revoked = True; k.key_enc = ""; audit(db, u.id, "key_revoke", str(kid)); db.commit(); flash(request, "Key revoked.")
    return go("/keys")
@app.post("/keys/{kid}/delete")
def key_delete(kid: int, request: Request, csrf: str = Form(""), u=Depends(current), db: Session = Depends(get_db)):
    check_csrf(request, csrf)
    k = db.get(ApiKey, kid)
    if k and k.user_id == u.id: db.delete(k); audit(db, u.id, "key_delete", str(kid)); db.commit(); flash(request, "Key deleted permanently.")
    return go("/keys")

# ---------- payment core ----------
def cfg_for(db, user_id, gateway, enabled_only=True):
    q = select(GatewayCred).where(GatewayCred.user_id == user_id, GatewayCred.gateway == gateway)
    if enabled_only: q = q.where(GatewayCred.enabled == True)  # noqa
    c = db.scalar(q)
    return (sec.dec(c.data_enc), c.env) if c else (None, None)
def new_pay_id(db, gateway):
    for _ in range(20):
        pid = gw.paytm.new_order_id() if gateway == "paytm" else "ORD" + secrets.token_hex(8).upper()
        if not db.scalar(select(Txn.id).where(Txn.pay_id == pid)): return pid
    return "ORD" + secrets.token_hex(8).upper()
def create_payment(db, user, amount, gateway, merchant_order_id, customer):
    ad = gw.get(gateway)
    if not ad or not ad.AVAILABLE: raise HTTPException(400, f"Gateway '{gateway}' not available")
    if not (1 <= amount <= 10_000_000): raise HTTPException(400, "Invalid amount")
    if merchant_order_id:
        ex = db.scalar(select(Txn).where(Txn.user_id == user.id, Txn.merchant_order_id == merchant_order_id))
        if ex: return ex
    from extras import check_plan; check_plan(db, user, gateway)
    cfg, env = cfg_for(db, user.id, gateway)
    if not cfg: raise HTTPException(400, f"Gateway '{gateway}' not configured/enabled")
    pay_id = new_pay_id(db, gateway)
    if gateway == "fampay":  # unique-paise adjustment (+1..+99 paise) so each pending order has its own exact amount
        used = set(db.scalars(select(Txn.amount).where(Txn.user_id == user.id, Txn.gateway == "fampay", Txn.status == "PENDING", Txn.expires_at > now())))
        adj = next((a for a in range(0, 100) if amount + a not in used), None)
        if adj is None: raise HTTPException(429, "Too many pending payments, try again shortly")
        amount += adj
    try: r = ad.create(cfg, env, pay_id, amount)
    except Exception as e: raise HTTPException(502, str(e)[:200])
    t = Txn(pay_id=pay_id, user_id=user.id, merchant_order_id=merchant_order_id, gateway=gateway, amount=amount, status="PENDING",
            customer=json.dumps(customer or {}), qr_data=r["qr_data"], provider_ref=r["provider_ref"], expires_at=now() + timedelta(minutes=10))
    db.add(t); db.commit(); return t
def _refresh(db, t):
    """Only a provider-verified response (order id + amount + success) may set SUCCESS. Idempotent."""
    if t.status not in ("PENDING", "CREATED"): return t
    cfg, env = cfg_for(db, t.user_id, t.gateway, enabled_only=False)  # pending/test payments must verify even if the gateway is not enabled
    if cfg:
        try:
            used = set(db.scalars(select(Txn.msg_key).where(Txn.user_id == t.user_id, Txn.gateway == t.gateway, Txn.msg_key != "", Txn.id != t.id)))  # emails that already confirmed another order
            r = gw.get(t.gateway).status(cfg, env, t.pay_id, amount=t.amount, since=t.created, used_keys=used)
            t = db.scalar(select(Txn).where(Txn.id == t.id).with_for_update().execution_options(populate_existing=True))
            if t.status in ("PENDING", "CREATED"):
                if r["state"] == "SUCCESS":
                    key = r.get("key", "")
                    if key: dup = key in used
                    else: dup = r["txn_id"] and db.scalar(select(Txn.id).where(Txn.gateway == t.gateway, Txn.user_id == t.user_id, Txn.provider_ref == r["txn_id"], Txn.id != t.id))
                    if dup: audit(db, t.user_id, "verify_dup_utr", t.pay_id)
                    elif r["order_id"] == t.pay_id and r["amount"] == t.amount: t.status, t.provider_ref, t.msg_key, t.updated = "SUCCESS", r["txn_id"] or t.provider_ref, r.get("key", ""), now()
                    else: audit(db, t.user_id, "verify_mismatch", t.pay_id)
                elif r["state"] == "FAILED": t.status, t.updated = "FAILED", now()
        except Exception as e: audit(db, t.user_id, "verify_error", f"{t.pay_id}: {e}")
    if t.status == "PENDING" and t.expires_at < now(): t.status, t.updated = "EXPIRED", now()
    db.commit(); return t
def refresh(db, t):
    old = t.status; t = _refresh(db, t)
    if t.status != old:
        from extras import on_change; on_change(db, t)
    return t
def tx_json(t, request):
    return {"order_id": t.merchant_order_id or t.pay_id, "pay_id": t.pay_id, "status": t.status, "amount": t.amount / 100, "currency": t.currency,
            "gateway": t.gateway, "transaction_id": t.provider_ref, "payment_url": f"{base_url(request)}/pay/{t.pay_id}",
            "qr_url": f"{base_url(request)}/pay/{t.pay_id}/qr.svg", "qr_png_url": f"{base_url(request)}/pay/{t.pay_id}/qr.png",
            "upi_uri": t.qr_data, "expires_at": t.expires_at.isoformat() + "Z"}

# ---------- public API ----------
def api_user(request: Request, authorization: str = Header(""), x_api_key: str = Header(""), api_key: str = "", db: Session = Depends(get_db)):
    k = x_api_key or api_key or authorization.replace("Bearer ", "").strip()
    row = db.scalar(select(ApiKey).where(ApiKey.key_hash == sec.hash_key(k))) if k else None
    if not row or row.revoked: raise HTTPException(401, "Invalid API key")
    if not sec.rate_ok(f"api:{row.id}", 60): raise HTTPException(429, "Rate limit: 60 req/min")
    u = db.get(User, row.user_id)
    if u.status != "active": raise HTTPException(403, "Account suspended")
    row.uses += 1; row.last_used = now(); db.commit(); return u
@app.exception_handler(HTTPException)
async def http_exc(request: Request, exc: HTTPException):
    if exc.status_code == 303: return RedirectResponse(exc.headers["Location"], 303)
    if request.url.path.startswith("/api/"): return JSONResponse({"success": False, "error": exc.detail}, exc.status_code)
    return Response(str(exc.detail), exc.status_code)
@app.post("/api/v1/payment/create")
async def api_create(request: Request, u=Depends(api_user), db: Session = Depends(get_db)):
    b = await request.json()
    try: amt = round(float(b["amount"]) * 100)
    except Exception: raise HTTPException(400, "amount required")
    t = create_payment(db, u, amt, b.get("gateway", "paytm"), (b.get("order_id") or None), b.get("customer"))
    return {"success": True, **tx_json(t, request)}
def pick_gateway(db, u, gateway):
    if gateway: return gateway
    from billing import allowed as _allowed
    al = _allowed(db, u)
    for g in ("fampay", "paytm"):  # first enabled gateway of the merchant that the plan unlocks
        if g in al and cfg_for(db, u.id, g)[0]: return g
    return "paytm"
@app.get("/api/create_order")  # simple GET style: ?amount=99&api_key=...
@app.get("/api/create_order.php")
def api_create_get(request: Request, amount: float, gateway: str = "", order_id: str = "", u=Depends(api_user), db: Session = Depends(get_db)):
    gateway = pick_gateway(db, u, gateway)
    t = create_payment(db, u, round(amount * 100), gateway, order_id or None, None)
    return {"status": "success", "data": tx_json(t, request)}
def _lookup(db, u, oid):
    t = db.scalar(select(Txn).where(Txn.user_id == u.id, (Txn.pay_id == oid) | (Txn.merchant_order_id == oid)))
    if not t: raise HTTPException(404, "Order not found")
    return t
@app.get("/api/v1/payment/status/{oid}")
def api_status(oid: str, request: Request, u=Depends(api_user), db: Session = Depends(get_db)):
    return {"success": True, **tx_json(refresh(db, _lookup(db, u, oid)), request)}
@app.get("/api/verify")
@app.get("/api/verify.php")
def api_verify(order_id: str, request: Request, u=Depends(api_user), db: Session = Depends(get_db)):
    t = refresh(db, _lookup(db, u, order_id))
    if t.status != "SUCCESS": return {"status": t.status.lower(), "data": None}
    return {"status": "success", "data": {"order_id": t.pay_id, "amount": t.amount / 100, "utr": t.provider_ref, "payment_time": t.updated.isoformat() + "Z"}}
@app.get("/api/v1/transactions")
def api_txns(request: Request, u=Depends(api_user), db: Session = Depends(get_db)):
    return {"success": True, "data": [tx_json(t, request) for t in db.scalars(select(Txn).where(Txn.user_id == u.id).order_by(Txn.id.desc()).limit(100))]}
@app.get("/api/v1/account")
def api_account(u=Depends(api_user)): return {"success": True, "username": u.username, "email": u.email, "role": u.role}
@app.get("/api/v1/gateways")
def api_gws(u=Depends(api_user), db: Session = Depends(get_db)):
    return {"success": True, "gateways": [{"gateway": k, "available": a.AVAILABLE, "enabled": bool(cfg_for(db, u.id, k)[0])} for k, a in gw.ADAPTERS.items()]}

# ---------- hosted checkout ----------
def _pay(db, pay_id):
    t = db.scalar(select(Txn).where(Txn.pay_id == pay_id))
    if not t: raise HTTPException(404, "Not found")
    return t
@app.get("/checkout.php")  # ready-made checkout: /checkout.php?amount=99&api_key=KEY[&order_id=X]
def checkout_php(request: Request, amount: float, api_key: str = "", order_id: str = "", gateway: str = "", u=Depends(api_user), db: Session = Depends(get_db)):
    t = create_payment(db, u, round(amount * 100), pick_gateway(db, u, gateway), order_id or None, None)
    return go(f"/pay/{t.pay_id}")
@app.get("/pay/{pay_id}")
def pay_page(pay_id: str, request: Request, db: Session = Depends(get_db)):
    t = _pay(db, pay_id); m = db.get(User, t.user_id)
    return render(request, "checkout.html", None, t=t, merchant=(m.business_name or m.name), mphoto=(f"/profile/photo/{m.id}?v={m.photo_v}" if m.photo_b64 else ""), secs=max(0, int((t.expires_at - now()).total_seconds())), ret=("/subscription" if (t.merchant_order_id or "").startswith("SUB-") else ""), back=("/subscription" if (t.merchant_order_id or "").startswith("SUB-") else ("/gateways" if (t.merchant_order_id or "").startswith("TEST-") else "")))
@app.get("/pay/{pay_id}/qr.svg")
def pay_qr(pay_id: str, db: Session = Depends(get_db)):
    t = _pay(db, pay_id); buf = io.BytesIO(); segno.make(t.qr_data, error="m").save(buf, kind="svg", scale=8, border=2)
    return Response(buf.getvalue(), media_type="image/svg+xml")
@app.get("/pay/{pay_id}/qr.png")  # PNG for Telegram/WhatsApp bots (they cannot show SVG)
def pay_qr_png(pay_id: str, db: Session = Depends(get_db)):
    t = _pay(db, pay_id); buf = io.BytesIO(); segno.make(t.qr_data, error="m").save(buf, kind="png", scale=8, border=3)
    return Response(buf.getvalue(), media_type="image/png")
@app.get("/pay/{pay_id}/status")
def pay_status(pay_id: str, db: Session = Depends(get_db)):
    if not sec.rate_ok(f"poll:{pay_id}", 40): raise HTTPException(429, "slow down")
    t = refresh(db, _pay(db, pay_id)); return {"status": t.status}
@app.api_route("/callback/paytm", methods=["GET", "POST"])
async def paytm_cb(request: Request, db: Session = Depends(get_db)):
    f = dict(await request.form()) if request.method == "POST" else dict(request.query_params)
    t = db.scalar(select(Txn).where(Txn.pay_id == f.get("ORDERID", ""))) # status is re-verified server-to-server; callback body is never trusted
    if t and f.get("CHECKSUMHASH"):
        try:
            cfg, _ = cfg_for(db, t.user_id, "paytm")
            if not (cfg and gw.paytm.verify_signature({k: v for k, v in f.items() if k != "CHECKSUMHASH"}, cfg["key"], f["CHECKSUMHASH"])):
                audit(db, t.user_id, "callback_bad_checksum", t.pay_id); db.commit()
        except Exception: pass
    if t: refresh(db, t); return go(f"/pay/{t.pay_id}")
    return Response("ok")

# ---------- owner ----------
def _user_stats(db):
    rows = db.execute(select(Txn.user_id, Txn.status, func.count(), func.coalesce(func.sum(Txn.amount), 0)).group_by(Txn.user_id, Txn.status)).all()
    st = {}
    for uid, s, n, amt in rows:
        d = st.setdefault(uid, dict(total=0, ok=0, failed=0, pending=0, balance=0, pend_amt=0))
        d["total"] += n
        if s == "SUCCESS": d["ok"] += n; d["balance"] += amt
        elif s == "FAILED": d["failed"] += n
        elif s in ("PENDING", "CREATED"): d["pending"] += n; d["pend_amt"] += amt
    return st
@app.get("/admin")
def admin(request: Request, q: str = "", u=Depends(owner_only), db: Session = Depends(get_db)):
    c = lambda *w: db.scalar(select(func.count()).where(*w)) if w else db.scalar(select(func.count()).select_from(Txn))
    st = dict(users=db.scalar(select(func.count()).select_from(User)), active=c(User.status == "active"), sellers=c(User.role == "seller"), resellers=c(User.role == "reseller"),
              txns=db.scalar(select(func.count()).select_from(Txn)), ok=c(Txn.status == "SUCCESS"), failed=c(Txn.status == "FAILED"),
              vol=db.scalar(select(func.coalesce(func.sum(Txn.amount), 0)).where(Txn.status == "SUCCESS")),
              rev=db.scalar(select(func.coalesce(func.sum(Subscription.price_paid), 0))))
    uq = select(User).order_by(User.id.desc())
    if q: uq = uq.where(User.name.contains(q) | User.business_name.contains(q) | User.username.contains(q.lower()) | User.email.contains(q.lower()) | User.phone.contains(q))
    users = db.scalars(uq.limit(300)).all()
    names = {x.id: x.username for x in db.scalars(select(User)).all()}
    recent = db.scalars(select(Txn).order_by(Txn.id.desc()).limit(50)).all()
    from billing import plan_map
    return render(request, "admin.html", u, st=st, users=users, upl=plan_map(db, [x.id for x in users]), ustats=_user_stats(db), q=q, names=names, recent=recent, bset=get_setting("base_url"), bauto=base_url(request),
                  logs=db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(30)).all())
@app.get("/admin/users/{uid}")
def admin_user(uid: int, request: Request, status: str = "", u=Depends(owner_only), db: Session = Depends(get_db)):
    t = db.get(User, uid)
    if not t: raise HTTPException(404, "User not found")
    tq = select(Txn).where(Txn.user_id == uid)
    if status: tq = tq.where(Txn.status == status)
    gws = db.scalars(select(GatewayCred).where(GatewayCred.user_id == uid)).all()
    keys = db.scalars(select(ApiKey).where(ApiKey.user_id == uid).order_by(ApiKey.id.desc())).all()
    logins = db.scalars(select(LoginLog).where(LoginLog.user_id == uid).order_by(LoginLog.id.desc()).limit(10)).all()
    from extras import plan_for
    from models import Plan, SubOrder, Notification
    gdata = {}
    for g in gws:  # admin sees what the merchant configured, but never secrets (passwords / app passwords)
        try: d = sec.dec(g.data_enc)
        except Exception: d = {}
        gdata[g.gateway] = dict(shop=d.get("shop", ""), upi=d.get("upi", ""), extra=d.get("mid") or d.get("gmail") or "")
    cur, sub = plan_for(db, uid)
    return render(request, "admin_user.html", u, t=t, s=_user_stats(db).get(uid, dict(total=0, ok=0, failed=0, pending=0, balance=0, pend_amt=0)),
                  txns=db.scalars(tq.order_by(Txn.id.desc()).limit(200)).all(), gws=gws, gdata=gdata, keys=keys, logins=logins, f=status, plan=cur, sub=sub,
                  plans={p.id: p for p in db.scalars(select(Plan))},
                  subs=db.scalars(select(Subscription).where(Subscription.user_id == uid).order_by(Subscription.id.desc()).limit(20)).all(),
                  orders=db.scalars(select(SubOrder).where(SubOrder.user_id == uid).order_by(SubOrder.id.desc()).limit(20)).all(),
                  notes=db.scalars(select(Notification).where(Notification.user_id == uid).order_by(Notification.id.desc()).limit(15)).all(),
                  acts=db.scalars(select(AuditLog).where(AuditLog.user_id == uid).order_by(AuditLog.id.desc()).limit(30)).all(),
                  parent=db.get(User, t.parent_id) if t.parent_id else None)
@app.post("/admin/users/{uid}/toggle")
def admin_toggle(uid: int, request: Request, csrf: str = Form(""), u=Depends(owner_only), db: Session = Depends(get_db)):
    check_csrf(request, csrf); t = db.get(User, uid)
    if t and t.role != "owner": t.status = "suspended" if t.status == "active" else "active"; audit(db, u.id, "user_toggle", f"{uid}->{t.status}"); db.commit()
    return go("/admin")

STARTUP_ERROR = None
@app.on_event("startup")
def startup():
    global STARTUP_ERROR
    try: _startup_inner()
    except Exception as e:
        import traceback; print(traceback.format_exc(), flush=True)
        STARTUP_ERROR = f"{type(e).__name__}: {str(e)[:400]}"
@app.middleware("http")
async def show_startup_error(request: Request, call_next):
    if STARTUP_ERROR: return Response("STARTUP ERROR - fix the setting/DB, then redeploy:\n\n" + STARTUP_ERROR, status_code=500, media_type="text/plain")
    return await call_next(request)
@app.get("/healthz")
def healthz(): return {"ok": True}
@app.head("/healthz")
def healthz_head(): return Response(status_code=200)

# ---------- keep-alive (Render free never sleeps): pings its own public URL every 5 min ----------
def _keep_alive():
    import time, requests
    url = (os.getenv("KEEP_ALIVE_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
    if not url: return
    time.sleep(45)
    while True:
        try: requests.get(url + "/healthz", timeout=20)
        except Exception: pass
        time.sleep(int(os.getenv("KEEP_ALIVE_INTERVAL", "300")))
import threading as _th
_th.Thread(target=_keep_alive, daemon=True, name="keep-alive").start()

from extras import router  # noqa: E402
app.include_router(router)
from profile_pages import router as _prouter  # noqa: E402
app.include_router(_prouter)
from billing import router as _brouter  # noqa: E402
app.include_router(_brouter)
from backup import router as _bkrouter  # noqa: E402
app.include_router(_bkrouter)
from music import router as _mrouter  # noqa: E402
app.include_router(_mrouter)
