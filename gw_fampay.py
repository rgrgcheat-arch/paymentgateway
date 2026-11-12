"""FamPay adapter (auto verification via the merchant's OWN Gmail, IMAP + App Password).
Flow: customer pays a UNIQUE amount (unique paise) -> FamPay/bank credit email lands in merchant Gmail
-> we read it and match amount + time. Safety: only mails with DKIM=pass and sender matching the filter count;
UTR is de-duplicated in core so one email can never confirm two orders. Tune SENDER/regex to a real mail sample."""
import imaplib, email, re, time, threading, hashlib
from email.utils import parsedate_to_datetime
from email.header import decode_header, make_header
from datetime import datetime, timezone, timedelta
from urllib.parse import quote

LABEL = "FamPay / FamApp"; AVAILABLE = True
FIELDS = [("shop", "Shop Name", False), ("upi", "UPI ID", False), ("gmail", "Gmail Address", False), ("app_password", "Gmail App Password (16 digits)", True)]
SENDER_DEFAULT = "fam"          # substring that must appear in the From header (change if the bank mail comes from another domain)
_CREDIT = re.compile(r"(received|credited|paid you|sent you|payment from)", re.I)
_DEBIT = re.compile(r"(debited|you paid|you sent|paid to|sent to)", re.I)
_AMT = re.compile(r"(?:₹|Rs\.?|INR)\s*([\d,]+(?:\.\d{1,2})?)", re.I)
_UTR = re.compile(r"\b(\d{12})\b")
_cache: dict = {}; _lock = threading.Lock()

def _text(msg):
    parts = []
    for p in (msg.walk() if msg.is_multipart() else [msg]):
        if p.get_content_type() in ("text/plain", "text/html"):
            try: s = p.get_payload(decode=True).decode(p.get_content_charset() or "utf-8", "ignore")
            except Exception: continue
            parts.append(re.sub(r"<[^>]+>", " ", s) if p.get_content_type() == "text/html" else s)
    return re.sub(r"\s+", " ", " ".join(parts))

def _parse(raw, sender_filter):
    msg = email.message_from_bytes(raw)
    frm = str(make_header(decode_header(msg.get("From", "")))).lower()
    subj = str(make_header(decode_header(msg.get("Subject", ""))))
    if sender_filter.lower() not in frm: return None
    body = subj + " " + _text(msg)
    if not _CREDIT.search(body) or _DEBIT.search(subj): return None
    m = _AMT.search(body)
    if not m: return None
    try: paise = round(float(m.group(1).replace(",", "")) * 100)
    except ValueError: return None
    try: ts = parsedate_to_datetime(msg.get("Date")); ts = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    except Exception: return None
    auth = " ".join(msg.get_all("Authentication-Results") or []).lower()
    u = _UTR.search(body)
    mid = (msg.get("Message-ID") or raw[:200].decode("latin1"))
    return {"paise": paise, "ts": ts.astimezone(timezone.utc), "dkim": "dkim=pass" in auth, "key": hashlib.sha1(mid.encode()).hexdigest()[:24],
            "utr": u.group(1) if u else "MSG" + hashlib.sha1(mid.encode()).hexdigest()[:12].upper()}

def _login(cfg):
    M = imaplib.IMAP4_SSL("imap.gmail.com", timeout=20)
    M.login(cfg["gmail"], cfg["app_password"].replace(" ", "")); return M

def _scan(cfg):
    key = cfg["gmail"].lower()
    with _lock:
        c = _cache.get(key)
        if c and time.time() - c[0] < 6: return c[1]
    M = _login(cfg); out = []
    try:
        M.select("INBOX", readonly=True)
        since = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%d-%b-%Y")
        typ, data = M.search(None, "SINCE", since)
        for i in (data[0].split()[-80:] if typ == "OK" and data and data[0] else []):
            t, d = M.fetch(i, "(BODY.PEEK[])")
            if t == "OK" and d and isinstance(d[0], tuple):
                r = _parse(d[0][1], cfg.get("sender") or SENDER_DEFAULT)
                if r: out.append(r)
    finally:
        try: M.logout()
        except Exception: pass
    with _lock: _cache[key] = (time.time(), out)
    return out

def create(cfg, env, pay_id, amount_paise):
    uri = f"upi://pay?pa={quote(cfg['upi'])}&pn={quote(cfg['shop'])}&am={amount_paise/100:.2f}&cu=INR&tn={quote('Order ' + pay_id[-8:])}"
    return {"qr_data": uri, "provider_ref": ""}

def status(cfg, env, pay_id, amount=None, since=None, used_keys=(), **kw):
    if not amount or not since: return {"state": "PENDING", "order_id": pay_id, "amount": 0, "txn_id": ""}
    since = (since if since.tzinfo else since.replace(tzinfo=timezone.utc)) - timedelta(seconds=30)
    for r in sorted(_scan(cfg), key=lambda x: x["ts"]):
        if r["key"] in used_keys: continue  # this email already confirmed another order - look at the next one
        if r["dkim"] and r["paise"] == amount and r["ts"] >= since:
            return {"state": "SUCCESS", "order_id": pay_id, "amount": amount, "txn_id": r["utr"], "key": r["key"]}
    return {"state": "PENDING", "order_id": pay_id, "amount": 0, "txn_id": ""}

def test(cfg, env):
    try: M = _login(cfg); M.select("INBOX", readonly=True); M.logout()
    except imaplib.IMAP4.error: return False, "Gmail login failed - check Gmail + 16-digit App Password (2-step verification must be ON, IMAP enabled)"
    except Exception as e: return False, f"Cannot reach Gmail: {str(e)[:120]}"
    return True, "Gmail connected (IMAP OK)"

def refund(*a, **k): raise RuntimeError("Refunds are manual for FamPay")
def refund_status(*a, **k): return {"state": "PENDING", "msg": "manual"}
