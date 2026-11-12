"""Paytm adapter - documented Paytm endpoints; checksum (AES-CBC + SHA256 salt) implemented locally.
Verify field names against https://business.paytm.com/docs in your staging account before going live."""
import json, base64, hashlib, random, string, requests
from urllib.parse import quote
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding
_IV = b"@@@@&&&&####$$$$"
def _enc(text, key):
    pd = padding.PKCS7(128).padder(); d = pd.update(text.encode()) + pd.finalize()
    e = Cipher(algorithms.AES(key.encode()), modes.CBC(_IV)).encryptor(); return base64.b64encode(e.update(d) + e.finalize()).decode()
def _dec(b64, key):
    dd = Cipher(algorithms.AES(key.encode()), modes.CBC(_IV)).decryptor(); raw = dd.update(base64.b64decode(b64)) + dd.finalize()
    up = padding.PKCS7(128).unpadder(); return (up.update(raw) + up.finalize()).decode()
def _flat(params):
    if isinstance(params, dict): return "|".join("" if (params[k] is None or str(params[k]).lower() == "null") else str(params[k]) for k in sorted(params))
    return params
def generate_signature(params, key):
    salt = "".join(random.choice(string.ascii_letters + string.digits) for _ in range(4))
    return _enc(hashlib.sha256((_flat(params) + "|" + salt).encode()).hexdigest() + salt, key)
def verify_signature(params, key, checksum):
    try:
        dec = _dec(checksum, key); salt = dec[-4:]
        return hashlib.sha256((_flat(params) + "|" + salt).encode()).hexdigest() + salt == dec
    except Exception: return False
LABEL = "Paytm"; AVAILABLE = True
FIELDS = [("shop", "Shop Name", False), ("upi", "UPI ID", False), ("mid", "Merchant ID (MID)", False), ("key", "Merchant Key (optional)", True)]
HOSTS = {"staging": "https://securegw-stage.paytm.in", "production": "https://securegw.paytm.in"}
def _post(cfg, env, path, body, client_id=None):
    sig = generate_signature(json.dumps(body), cfg["key"])
    head = {"signature": sig}
    if client_id: head.update({"clientId": client_id, "version": "v1"})
    r = requests.post(HOSTS[env] + path, json={"head": head, "body": body}, timeout=20)
    r.raise_for_status(); return r.json()
def _has_key(cfg): return bool((cfg.get("key") or "").strip())

def new_order_id():
    """PTM + UTC timestamp + 4 digits (same shape the working Payment Gateway site uses)."""
    import datetime as _d
    return "PTM" + _d.datetime.utcnow().strftime("%Y%m%d%H%M%S") + "".join(random.choice(string.digits) for _ in range(4))

def create(cfg, env, pay_id, amount_paise):
    """Local UPI QR: no Paytm API call needed. The order id goes into tr/tn so Paytm can report the payment by order id."""
    shop = cfg.get("shop") or "Shop"
    uri = (f"upi://pay?pa={quote(cfg['upi'])}&pn={quote(shop)}&am={amount_paise/100:.2f}&cu=INR"
           f"&tr={pay_id}&tn={quote('Order ' + pay_id)}")
    return {"qr_data": uri, "provider_ref": ""}

def _amt_paise(v):
    try: return round(float(v) * 100)
    except (TypeError, ValueError): return 0

def _status_keyless(cfg, env, pay_id):
    """Merchant status lookup by MID + ORDERID only (no Merchant Key). Returns Paytm's raw JSON."""
    q = json.dumps({"MID": cfg["mid"], "ORDERID": pay_id}, separators=(",", ":"))
    r = requests.get(HOSTS["production"] + "/merchant-status/getTxnStatus", params={"JsonData": q}, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
    r.raise_for_status(); return r.json()

# Paytm replies TXN_FAILURE + these codes when the order simply does not exist (yet): the customer has not paid.
# That is NOT a failed payment, so it must stay PENDING until Paytm reports a real transaction.
_NOT_FOUND = {"334", "335", "400", "401", "402", "810", "501", "1"}
def _state(st, code, has_txn):
    if st == "TXN_SUCCESS": return "SUCCESS"
    if st == "TXN_FAILURE" and has_txn and str(code) not in _NOT_FOUND: return "FAILED"
    return "PENDING"

def status(cfg, env, pay_id, **kw):
    env = "production"
    if _has_key(cfg):  # optional signed API (only if an old Merchant Key is still saved)
        res = _post(cfg, env, "/v3/order/status", {"mid": cfg["mid"], "orderId": pay_id})
        b = res.get("body", {}); i = b.get("resultInfo", {})
        state = _state(i.get("resultStatus", ""), i.get("resultCode", ""), bool(b.get("txnId")))
        return {"state": state, "order_id": b.get("orderId"), "amount": _amt_paise(b.get("txnAmount")), "txn_id": b.get("txnId", "")}
    b = _status_keyless(cfg, env, pay_id)
    txn = str(b.get("BANKTXNID") or b.get("TXNID") or "")
    state = _state(str(b.get("STATUS", "")), b.get("RESPCODE", ""), bool(txn))
    return {"state": state, "order_id": b.get("ORDERID"), "amount": _amt_paise(b.get("TXNAMOUNT")), "txn_id": txn}

def test(cfg, env):
    """Setup check only (does not prove payments work - use the Rs 1 test payment for that)."""
    if not cfg.get("mid") or "@" not in (cfg.get("upi") or ""): return False, "MID and a valid UPI ID are required"
    try:
        if _has_key(cfg):
            res = _post(cfg, env, "/v3/order/status", {"mid": cfg["mid"], "orderId": "TESTCONN0001"})
            code = str(res.get("body", {}).get("resultInfo", {}).get("resultCode", ""))
            if code in ("330", "501", "227"): return False, f"Credentials rejected (code {code})"
            return True, f"Reached Paytm {env} (code {code})"
        b = _status_keyless(cfg, env, "TESTCONN0001")
        return True, f"Reached Paytm (reply: {str(b.get('RESPCODE', ''))} {str(b.get('RESPMSG', ''))[:80]}). Now run the Rs 1 test payment."
    except Exception as e: return False, f"Cannot reach Paytm: {str(e)[:150]}"

def _rstate(res, default):
    i = res.get("body", {}).get("resultInfo", {})
    return {"TXN_SUCCESS": "SUCCESS", "PENDING": "PENDING", "TXN_FAILURE": "FAILED"}.get(i.get("resultStatus"), default), i.get("resultMsg", "")
def refund(cfg, env, pay_id, txn_id, refund_id, amount_paise):
    body = {"mid": cfg["mid"], "txnType": "REFUND", "orderId": pay_id, "txnId": txn_id, "refId": refund_id, "refundAmount": f"{amount_paise/100:.2f}"}
    st, msg = _rstate(_post(cfg, env, "/refund/apply", body), "FAILED"); return {"state": st, "msg": msg}
def refund_status(cfg, env, pay_id, refund_id):
    st, msg = _rstate(_post(cfg, env, "/v2/refund/status", {"mid": cfg["mid"], "orderId": pay_id, "refId": refund_id}), "PENDING"); return {"state": st, "msg": msg}
