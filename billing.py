"""Part 6: gateway-wise plans, 12h trial, buying a plan through the OWNER's own gateway (auto-verified),
unused-day credit, auto unlock/expire, notifications page + admin broadcast, admin plan/price control."""
import json, math
from datetime import timedelta
from fastapi import APIRouter, Request, Depends, HTTPException, Form
from sqlalchemy import select, func
from db import SessionLocal, get_db
from models import User, GatewayCred, Txn, Plan, Subscription, SubOrder, Broadcast, Notification, AuditLog, LoginLog, ApiKey, now
import security as sec, providers as gw
from core import ist, render, flash, go, check_csrf, audit, current, owner_only, cfg_for, new_pay_id, get_setting, set_setting
from extras import plan_for, notify

router = APIRouter()
TENURES = (30, 90, 180, 365)
LABEL = {"paytm": "Paytm", "fampay": "FamPay"}
REMOVED = ("bank", "bharatpe")  # discontinued plans: deleted when unused, hidden everywhere otherwise
HIDE = ("", "free") + REMOVED
FEATURES = {
    "trial": ["{h} hours of full access", "FamPay & Paytm", "All payment tools included", "One-time free trial"],
    "fampay": ["FamPay configuration only", "Gmail App Password security", "Live payments & payment links", "API keys and webhooks"],
    "paytm": ["Paytm configuration only", "MID + UPI routing", "Live payments & payment links", "API keys and webhooks"],
}
ALLG = "fampay,paytm"

# ---------- plan catalogue ----------
def seed_plans(db):
    """Creates the gateway plans once (prices are editable in Owner Panel -> Plans). Legacy plans are hidden, existing subscribers keep them until expiry."""
    seeds = [("trial", "12-Hour Free Trial", 100, 12, ALLG, {}, True, 0),
             ("fampay", "FamPay Only", 0, 0, "fampay", {30: 9900, 90: 29700, 180: 59400, 365: 99900}, True, 2),
             ("paytm", "Paytm Only", 0, 0, "paytm", {30: 12900, 90: 38700, 180: 77400, 365: 149900}, True, 3)]
    for code, name, price, hours, g, prices, active, sort in seeds:
        if db.scalar(select(Plan).where(Plan.code == code)): continue
        p = db.scalar(select(Plan).where(Plan.name == name))
        if p: p.code = code; continue
        db.add(Plan(name=name, code=code, price=price, duration_days=0 if code == "trial" else 30, req_limit=10_000_000, txn_limit=1_000_000, max_keys=3, gateways=g,
                    support="Email", prices=json.dumps({str(k): v for k, v in prices.items()}), hours=hours, active=active, sort=sort))
    for p in db.scalars(select(Plan).where(Plan.code.in_(REMOVED))).all():  # BharatPe / Bank are gone
        used = db.scalar(select(Subscription.id).where(Subscription.plan_id == p.id).limit(1)) or db.scalar(select(SubOrder.id).where(SubOrder.plan_id == p.id).limit(1))
        if used: p.active = False
        else: db.delete(p)
    for p in db.scalars(select(Plan)).all():  # tidy leftovers: spelling + removed gateways inside plan gateway lists
        if p.name == "FamePay Only" and not db.scalar(select(Plan.id).where(Plan.name == "FamPay Only")): p.name = "FamPay Only"
        g = ",".join(x for x in (p.gateways or "").split(",") if x.strip() and x.strip() not in REMOVED)
        if g != (p.gateways or "") and p.code not in REMOVED: p.gateways = g
    free = db.scalar(select(Plan).where(Plan.name == "Free"))
    if free and not free.code: free.code, free.gateways = "free", ""  # no plan = no gateway
    for p in db.scalars(select(Plan).where(Plan.code == "")): p.active = False
    db.commit()

def _safe_dec(c):
    try: return sec.dec(c.data_enc)
    except Exception: return {}
def prices_of(p):
    try: return {int(k): int(v) for k, v in json.loads(p.prices or "{}").items() if int(v) > 0}
    except Exception: return {}
def base_price(p): return prices_of(p).get(30) or p.price or 0
def allowed(db, u):
    """Gateways this user may configure/use right now (owner: all)."""
    if u.role == "owner": return set(gw.ADAPTERS)
    p, _ = plan_for(db, u.id)
    return {g.strip() for g in (p.gateways or "").split(",") if g.strip()} & set(gw.ADAPTERS)

def credit_for(sub, cur):
    """Unused-day credit: list value x remaining days / total days (e.g. 129 x 23/30 = 98.90)."""
    if not sub or not sub.end or cur.code == "trial": return 0
    total = sub.days or max(1, round((sub.end - sub.start).total_seconds() / 86400))
    rem = min(total, math.ceil(max(0, (sub.end - now()).total_seconds()) / 86400))
    return int((sub.value or sub.price_paid) * rem / total)
def quote(db, u, p, days):
    cur, sub = plan_for(db, u.id); renew = bool(sub and cur.id == p.id)
    if p.code == "trial": lp, credit = p.price, 0
    else:
        lp = prices_of(p).get(days)
        if not lp: return None
        credit = 0 if renew else credit_for(sub, cur)
    pay = max(0, lp - credit)
    if 0 < pay < 100: pay = 100  # gateways cannot collect below Rs 1
    return dict(list=lp, credit=credit, payable=pay, renew=renew)

# ---------- granting a plan ----------
def grant(db, uid, p, days, list_price, cash, source):
    cur = db.scalar(select(Subscription).where(Subscription.user_id == uid, Subscription.status == "active").order_by(Subscription.id.desc()))
    t0 = now(); start, value, total = t0, list_price, days
    if p.code == "trial": end, total = t0 + timedelta(hours=p.hours or 12), 0
    elif cur and cur.plan_id == p.id and cur.end and cur.end > t0:  # renewal: extend the running period
        start, end = cur.start, cur.end + timedelta(days=days)
        total = (cur.days or max(1, round((cur.end - cur.start).total_seconds() / 86400))) + days; value = (cur.value or cur.price_paid) + list_price
    else: end = t0 + timedelta(days=days)
    if cur: cur.status = "replaced"
    s = Subscription(user_id=uid, plan_id=p.id, start=start, end=end, price_paid=cash, value=value, days=total, source=source)
    db.add(s); db.flush(); return s
def activate(db, o, source="payment"):
    """Idempotent: only a PENDING order can be activated, so a payment can never unlock twice."""
    if o.status != "PENDING": return None
    p = db.get(Plan, o.plan_id); u = db.get(User, o.user_id)
    s = grant(db, o.user_id, p, o.days, o.list_price, o.amount, "trial" if p.code == "trial" else source)
    o.status, o.paid_at, o.sub_id = "PAID", now(), s.id
    if p.code == "trial": u.trial_used = True
    audit(db, o.user_id, "plan_activated", f"{p.name} order {o.id} paid {o.amount}"); db.commit()
    notify(db, u.id, "subscription", f"{p.name} is active until {ist(s.end):%d %b %Y %H:%M} IST. Payment successful.")
    owner = db.scalar(select(User).where(User.role == "owner").order_by(User.id))
    if owner and owner.id != u.id and o.amount: notify(db, owner.id, "plan_sold", f"{u.business_name or u.name} bought {p.name} for ₹{o.amount/100:.2f}")
    return s
def on_sub_txn(db, t):
    """Called from extras.on_change when the owner-side payment of a plan order changes status."""
    try: oid = int((t.merchant_order_id or "")[4:])
    except ValueError: return
    o = db.scalar(select(SubOrder).where(SubOrder.id == oid).with_for_update())
    if not o: return
    if t.status == "SUCCESS": activate(db, o)
    elif t.status in ("FAILED", "EXPIRED") and o.status == "PENDING": o.status = t.status; db.commit()

# ---------- collecting the money on the owner's own gateway ----------
def collect_gateway(db, owner):
    pref = get_setting("collect_gateway", "auto")
    for g in ([pref] if pref in gw.ADAPTERS else []) + [x for x in ("fampay", "paytm") if x != pref]:
        if cfg_for(db, owner.id, g)[0]: return g
    return None
def make_txn(db, owner, g, amount, moid, meta):
    cfg, env = cfg_for(db, owner.id, g); pay_id = new_pay_id(db, g)
    if g == "fampay":  # unique paise so every pending order matches exactly one bank credit
        used = set(db.scalars(select(Txn.amount).where(Txn.user_id == owner.id, Txn.gateway == "fampay", Txn.status == "PENDING", Txn.expires_at > now())))
        adj = next((a for a in range(0, 100) if amount + a not in used), None)
        if adj is None: raise HTTPException(429, "Too many pending payments, try again shortly")
        amount += adj
    r = gw.get(g).create(cfg, env, pay_id, amount)
    t = Txn(pay_id=pay_id, user_id=owner.id, merchant_order_id=moid, gateway=g, amount=amount, status="PENDING", customer=json.dumps(meta),
            qr_data=r["qr_data"], provider_ref=r["provider_ref"], expires_at=now() + timedelta(minutes=10))
    db.add(t); db.commit(); return t

# ---------- merchant: subscriptions page ----------
def _left(sub):
    if not sub or not sub.end: return ""
    secs = max(0, (sub.end - now()).total_seconds())
    return f"{math.ceil(secs / 86400)} day(s) remaining" if secs >= 86400 else f"{int(secs // 3600)}h {int(secs % 3600 // 60)}m remaining"
@router.get("/subscription")
def my_sub(request: Request, u=Depends(current), db=Depends(get_db)):
    cur, sub = plan_for(db, u.id); credit = credit_for(sub, cur); plans = {x.id: x for x in db.scalars(select(Plan))}
    cards = []
    for p in db.scalars(select(Plan).where(Plan.active == True, Plan.code.notin_(HIDE)).order_by(Plan.sort, Plan.id)).all():  # noqa
        if p.code == "trial":
            cards.append(dict(p=p, trial=True, ok=(not u.trial_used and not sub), why="Free trial already used" if u.trial_used else ("Available only before your first plan" if sub else ""), feats=[f.format(h=p.hours or 12) for f in FEATURES["trial"]]))
            continue
        ten = sorted(prices_of(p).items())
        if not ten: continue
        renew = bool(sub and cur.id == p.id)
        mode = "Renew plan" if renew else ("Change plan" if sub else "Buy plan")
        note = "" if not sub else ("Renewal extends your current end date." if renew else ("Upgrade" if base_price(p) > base_price(cur) else "Downgrade") + " price is adjusted with your unused-day credit at checkout.")
        cards.append(dict(p=p, trial=False, ten=ten, renew=renew, mode=mode, note=note, feats=FEATURES.get(p.code, []), credit=0 if renew else credit))
    po = db.scalar(select(SubOrder).where(SubOrder.user_id == u.id, SubOrder.status == "PENDING").order_by(SubOrder.id.desc())); pend = None
    if po and po.pay_id:
        t = db.scalar(select(Txn).where(Txn.pay_id == po.pay_id))
        if t and t.status == "PENDING" and t.expires_at > now(): pend = t
    last = None if sub else db.scalar(select(Subscription).where(Subscription.user_id == u.id, Subscription.status == "expired").order_by(Subscription.id.desc()))
    hist = db.scalars(select(Subscription).where(Subscription.user_id == u.id).order_by(Subscription.id.desc()).limit(30)).all()
    desc = (", ".join(LABEL.get(g, g) for g in cur.gateways.split(",") if g and g not in REMOVED) + " unlocked.") if sub else ""
    return render(request, "subscription.html", u, plan=cur, sub=sub, credit=credit, cards=cards, pend=pend, last=last, hist=hist, plans=plans, left=_left(sub), desc=desc, owner=(u.role == "owner"))
@router.post("/subscription/buy")
def buy(request: Request, plan_id: int = Form(), days: int = Form(0), csrf: str = Form(""), u=Depends(current), db=Depends(get_db)):
    check_csrf(request, csrf)
    if not sec.rate_ok(f"buy:{u.id}", 10, 300): flash(request, "Too many attempts. Wait a few minutes.", "err"); return go("/subscription")
    p = db.get(Plan, plan_id)
    if not p or not p.active or p.code in HIDE: flash(request, "This plan is not available.", "err"); return go("/subscription")
    cur, sub = plan_for(db, u.id)
    if p.code == "trial":
        if u.trial_used or sub: flash(request, "The free trial is one-time and only for accounts without a plan.", "err"); return go("/subscription")
        days = 0
    q = quote(db, u, p, days)
    if not q: flash(request, "Choose a valid tenure.", "err"); return go("/subscription")
    owner = db.scalar(select(User).where(User.role == "owner").order_by(User.id))
    g = collect_gateway(db, owner) if q["payable"] else None
    if q["payable"] and not g: flash(request, "Online payment is not available right now. Please contact support.", "err"); return go("/subscription")
    for o in db.scalars(select(SubOrder).where(SubOrder.user_id == u.id, SubOrder.status == "PENDING")): o.status = "CANCELLED"
    o = SubOrder(user_id=u.id, plan_id=p.id, days=days, list_price=q["list"], credit=q["credit"], amount=q["payable"]); db.add(o); db.flush()
    if not q["payable"]:  # credit covers the whole price
        activate(db, o, "credit"); flash(request, f"{p.name} activated using your unused-day credit."); return go("/subscription")
    try: t = make_txn(db, owner, g, q["payable"], f"SUB-{o.id}", {"sub_order": o.id, "user": u.id, "plan": p.name})
    except Exception as e:
        db.rollback(); o = db.get(SubOrder, o.id)
        if o: o.status = "CANCELLED"; db.commit()
        flash(request, f"Could not start the payment: {str(getattr(e, 'detail', e))[:160]}", "err"); return go("/subscription")
    o.pay_id = t.pay_id; audit(db, u.id, "plan_order", f"{p.name} {days}d {q['payable']}"); db.commit()
    return go(f"/pay/{t.pay_id}")

# ---------- notifications (bell) ----------
@router.get("/notifications")
def notifications(request: Request, u=Depends(current), db=Depends(get_db)):
    seen = u.notif_seen
    rows = db.scalars(select(Notification).where(Notification.user_id == u.id).order_by(Notification.id.desc()).limit(100)).all()
    u.notif_seen = now(); db.commit()  # opening the page clears the bell badge
    return render(request, "notifications.html", u, rows=rows, seen=seen)

# ---------- owner: plans, prices, trial, collection gateway ----------
def _paise(v):
    try: return max(0, round(float(v or 0) * 100))
    except Exception: return 0
def plan_map(db, ids):
    pm = {p.id: p for p in db.scalars(select(Plan))}; out = {}
    for s in db.scalars(select(Subscription).where(Subscription.user_id.in_(ids or [0]), Subscription.status == "active").order_by(Subscription.id)):
        if s.end is None or s.end > now(): out[s.user_id] = (pm[s.plan_id].name, s.end)
    return out
@router.get("/admin/subscriptions")
def admin_subs(request: Request, u=Depends(owner_only), db=Depends(get_db)):
    plans = db.scalars(select(Plan).where(Plan.code.notin_(HIDE)).order_by(Plan.sort, Plan.id)).all()
    pm = {p.id: p for p in db.scalars(select(Plan))}; names = {x.id: (x.business_name or x.name) for x in db.scalars(select(User))}
    ocreds = {c.gateway: c for c in db.scalars(select(GatewayCred).where(GatewayCred.user_id == u.id))}
    return render(request, "admin_subs.html", u, plans=plans, pm=pm, pr={p.id: prices_of(p) for p in plans}, tenures=TENURES, names=names, ocreds=ocreds, label=LABEL, ocfg={g: _safe_dec(c) for g, c in ocreds.items()},
                  users=db.scalars(select(User).where(User.role != "owner").order_by(User.id.desc()).limit(300)).all(),
                  subs=db.scalars(select(Subscription).order_by(Subscription.id.desc()).limit(50)).all(),
                  orders=db.scalars(select(SubOrder).order_by(SubOrder.id.desc()).limit(50)).all(),
                  rev=db.scalar(select(func.coalesce(func.sum(Subscription.price_paid), 0))), collect=get_setting("collect_gateway", "auto"), cg=collect_gateway(db, u))
@router.post("/admin/collect")
def admin_collect(request: Request, gateway: str = Form("auto"), csrf: str = Form(""), u=Depends(owner_only), db=Depends(get_db)):
    check_csrf(request, csrf)
    if gateway not in ("auto", "fampay", "paytm"): gateway = "auto"
    set_setting(db, "collect_gateway", gateway); audit(db, u.id, "collect_gateway", gateway); db.commit()
    flash(request, "Plan payments will be collected on " + ("your first enabled gateway." if gateway == "auto" else LABEL[gateway] + ".")); return go("/admin/subscriptions")
# ---------- owner: QR collection setup for plan payments (shop name + UPI ID + Gmail + app password / MID) ----------
@router.post("/admin/collect/{g}/save")
def collect_save(g: str, request: Request, shop: str = Form(""), upi: str = Form(""), gmail: str = Form(""), app_password: str = Form(""), mid: str = Form(""),
                 csrf: str = Form(""), u=Depends(owner_only), db=Depends(get_db)):
    check_csrf(request, csrf)
    if g not in LABEL: raise HTTPException(404)
    shop, upi, gmail, mid = shop.strip()[:60], upi.strip(), gmail.strip(), mid.strip()
    c = db.scalar(select(GatewayCred).where(GatewayCred.user_id == u.id, GatewayCred.gateway == g))
    old = sec.dec(c.data_enc) if c else {}
    if g == "fampay":
        pw = app_password.replace(" ", "").strip() or old.get("app_password", "")
        if not shop or "@" not in upi or "@" not in gmail or not pw: flash(request, "Shop name, UPI ID, Gmail and the 16-digit app password are required.", "err"); return go("/admin/subscriptions")
        data = sec.enc({"shop": shop, "upi": upi, "gmail": gmail, "app_password": pw})
    else:
        if not shop or "@" not in upi or not mid: flash(request, "Shop name, UPI ID and MID are required.", "err"); return go("/admin/subscriptions")
        data = sec.enc({"shop": shop, "upi": upi, "mid": mid, "key": old.get("key", "")})
    if c: c.data_enc, c.enabled = data, True
    else: db.add(GatewayCred(user_id=u.id, gateway=g, data_enc=data, env="production", enabled=True))
    set_setting(db, "collect_gateway", g); audit(db, u.id, "collect_save", g); db.commit()
    flash(request, f"{LABEL[g]} saved. Plan payments now go to this account. Press the ₹1 test to confirm it works."); return go("/admin/subscriptions")
@router.post("/admin/collect/{g}/{action}")
def collect_action(g: str, action: str, request: Request, csrf: str = Form(""), u=Depends(owner_only), db=Depends(get_db)):
    check_csrf(request, csrf)
    c = db.scalar(select(GatewayCred).where(GatewayCred.user_id == u.id, GatewayCred.gateway == g)) if g in LABEL else None
    if not c: flash(request, "Save the setup first.", "err"); return go("/admin/subscriptions")
    if action == "test":
        try: ok, msg = gw.get(g).test(sec.dec(c.data_enc), c.env)
        except Exception as e: ok, msg = False, str(e)[:200]
        c.last_test_at, c.last_test_ok, c.last_test_msg = now(), ok, msg; flash(request, msg, "ok" if ok else "err")
    elif action in ("enable", "disable"): c.enabled = action == "enable"; flash(request, f"{LABEL[g]} {action}d.")
    elif action == "delete": db.delete(c); flash(request, f"{LABEL[g]} setup deleted.")
    db.commit(); return go("/admin/subscriptions")
@router.post("/admin/plans/{pid}")
def plan_edit(pid: int, request: Request, name: str = Form(""), active: str = Form(""), gateways: str = Form(""), p30: str = Form(""), p90: str = Form(""), p180: str = Form(""), p365: str = Form(""),
              price: str = Form(""), hours: int = Form(12), txn_limit: int = Form(1000000), max_keys_: int = Form(3, alias="max_keys"), support: str = Form(""),
              csrf: str = Form(""), u=Depends(owner_only), db=Depends(get_db)):
    check_csrf(request, csrf); p = db.get(Plan, pid)
    if not p or p.code in HIDE: raise HTTPException(404)
    name = name.strip()[:40] or p.name
    if db.scalar(select(Plan.id).where(Plan.name == name, Plan.id != pid)): flash(request, "Another plan already uses that name.", "err"); return go("/admin/subscriptions")
    p.name, p.active = name, bool(active)
    p.gateways = ",".join(x.strip() for x in gateways.lower().split(",") if x.strip())[:100]
    if p.code == "trial": p.price, p.hours = _paise(price), max(1, hours)
    else: p.prices = json.dumps({str(d): _paise(v) for d, v in zip(TENURES, (p30, p90, p180, p365)) if _paise(v) > 0})
    p.txn_limit, p.max_keys, p.support = max(0, txn_limit), max(1, max_keys_), support.strip()[:40]
    audit(db, u.id, "plan_edit", p.name); db.commit(); flash(request, f"{p.name} saved."); return go("/admin/subscriptions")
@router.post("/admin/assign")
def assign(request: Request, user_id: int = Form(), plan_id: int = Form(), days: int = Form(30), price_paid: str = Form(""), csrf: str = Form(""), u=Depends(owner_only), db=Depends(get_db)):
    check_csrf(request, csrf); tu, p = db.get(User, user_id), db.get(Plan, plan_id)
    if not tu or not p or p.code == "free": raise HTTPException(404)
    if p.code == "trial": days, lp = 0, p.price
    else:
        lp = prices_of(p).get(days) or (p.price if not p.code else 0)
        if not lp: flash(request, "That plan has no price for the chosen tenure.", "err"); return go("/admin/subscriptions")
    cash = _paise(price_paid) if price_paid.strip() else lp
    s = grant(db, tu.id, p, days or p.duration_days or 30, lp, cash, "admin")
    if p.code == "trial": tu.trial_used = True
    audit(db, u.id, "assign_plan", f"user {tu.id} -> {p.name}"); db.commit()
    notify(db, tu.id, "subscription", f"Your plan is now {p.name}" + (f" (valid until {ist(s.end):%d %b %Y %H:%M} IST)" if s.end else " (lifetime)"))
    flash(request, f"Assigned {p.name} to {tu.username}."); return go("/admin/subscriptions")

# ---------- owner: per-user controls ----------
@router.post("/admin/users/{uid}/trial_reset")
def trial_reset(uid: int, request: Request, csrf: str = Form(""), u=Depends(owner_only), db=Depends(get_db)):
    check_csrf(request, csrf); t = db.get(User, uid)
    if t: t.trial_used = False; audit(db, u.id, "trial_reset", str(uid)); db.commit(); flash(request, "Free trial can be used again by this user.")
    return go(f"/admin/users/{uid}")
@router.post("/admin/users/{uid}/unlock")
def unlock_user(uid: int, request: Request, plan_id: int = Form(), days: int = Form(30), custom_days: str = Form(""), csrf: str = Form(""), u=Depends(owner_only), db=Depends(get_db)):
    """Owner quick-unlock from the user page: gives FamPay / Paytm access for N days (default 30 = 1 month) at no charge. Same plan again = extends."""
    check_csrf(request, csrf); tu, p = db.get(User, uid), db.get(Plan, plan_id)
    if not tu or not p or p.code not in LABEL: raise HTTPException(404)
    try: d = int(custom_days) if custom_days.strip() else days
    except ValueError: d = days
    d = min(max(1, d), 3650)
    s = grant(db, tu.id, p, d, 0, 0, "admin")
    audit(db, u.id, "unlock_plan", f"user {tu.id} -> {p.name} {d}d"); db.commit()
    notify(db, tu.id, "subscription", f"{LABEL[p.code]} unlocked by admin until {ist(s.end):%d %b %Y %H:%M} IST.")
    flash(request, f"{LABEL[p.code]} unlocked for {tu.username} for {d} day(s)."); return go(f"/admin/users/{uid}")
@router.post("/admin/users/{uid}/plan_cancel")
def plan_cancel(uid: int, request: Request, csrf: str = Form(""), u=Depends(owner_only), db=Depends(get_db)):
    check_csrf(request, csrf); n = 0
    for s in db.scalars(select(Subscription).where(Subscription.user_id == uid, Subscription.status == "active")): s.status, s.end, n = "expired", now(), n + 1
    if n: audit(db, u.id, "plan_cancel", str(uid)); db.commit(); notify(db, uid, "subscription_expired", "Your plan was ended by the admin. Gateways are locked."); flash(request, "Plan ended.")
    return go(f"/admin/users/{uid}")
@router.post("/admin/users/{uid}/message")
def msg_user(uid: int, request: Request, message: str = Form(""), csrf: str = Form(""), u=Depends(owner_only), db=Depends(get_db)):
    check_csrf(request, csrf); m = message.strip()[:300]
    if m and db.get(User, uid): notify(db, uid, "admin_message", m); audit(db, u.id, "admin_message", str(uid)); db.commit(); flash(request, "Message sent to the user's bell.")
    return go(f"/admin/users/{uid}")

# ---------- owner: broadcast ----------
AUDIENCES = [("all", "Everyone"), ("paid", "Paid plan users"), ("trial", "Trial users"), ("free", "No active plan / expired"), ("fampay", "FamPay plan users"), ("paytm", "Paytm plan users")]
def audience_ids(db, aud):
    users = db.scalars(select(User).where(User.role != "owner", User.status == "active")).all()
    if aud == "all": return [x.id for x in users]
    pm = {p.id: p for p in db.scalars(select(Plan))}; act = {}
    for s in db.scalars(select(Subscription).where(Subscription.status == "active").order_by(Subscription.id)):
        if s.end is None or s.end > now(): act[s.user_id] = pm[s.plan_id]
    def ok(uid):
        p = act.get(uid)
        if aud == "free": return p is None
        if p is None: return False
        if aud == "paid": return p.code != "trial"
        if aud == "trial": return p.code == "trial"
        return aud in [g.strip() for g in (p.gateways or "").split(",")]
    return [x.id for x in users if ok(x.id)]
@router.get("/admin/broadcast")
def broadcast_page(request: Request, u=Depends(owner_only), db=Depends(get_db)):
    return render(request, "admin_broadcast.html", u, aud=AUDIENCES, counts={k: len(audience_ids(db, k)) for k, _ in AUDIENCES},
                  rows=db.scalars(select(Broadcast).order_by(Broadcast.id.desc()).limit(30)).all())
@router.post("/admin/broadcast")
def broadcast_send(request: Request, message: str = Form(""), audience: str = Form("all"), csrf: str = Form(""), u=Depends(owner_only), db=Depends(get_db)):
    check_csrf(request, csrf); m = message.strip()[:300]
    if not m: flash(request, "Write a message first.", "err"); return go("/admin/broadcast")
    if audience not in dict(AUDIENCES): audience = "all"
    ids = audience_ids(db, audience)
    db.add_all([Notification(user_id=i, kind="broadcast", message=m) for i in ids]); db.add(Broadcast(message=m, audience=audience, count=len(ids)))
    audit(db, u.id, "broadcast", f"{audience} x{len(ids)}"); db.commit()
    flash(request, f"Broadcast delivered to {len(ids)} user(s)."); return go("/admin/broadcast")
