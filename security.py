import os, hashlib, hmac, base64, secrets, json, time
from cryptography.fernet import Fernet
SECRET = os.getenv("SECRET_KEY", "dev-only-change-me")
_f = Fernet(base64.urlsafe_b64encode(hashlib.sha256(SECRET.encode()).digest()))
def hash_pw(p: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(p.encode(), salt=salt, n=2**14, r=8, p=1)
    return salt.hex() + "$" + h.hex()
def check_pw(p: str, stored: str) -> bool:
    try: s, h = stored.split("$")
    except ValueError: return False
    return hmac.compare_digest(hashlib.scrypt(p.encode(), salt=bytes.fromhex(s), n=2**14, r=8, p=1).hex(), h)
def enc(d: dict) -> str: return _f.encrypt(json.dumps(d).encode()).decode()
def dec(s: str) -> dict: return json.loads(_f.decrypt(s.encode()))
def new_api_key() -> str: return "ZP_" + secrets.token_hex(24).upper()
def hash_key(k: str) -> str: return hashlib.sha256(k.encode()).hexdigest()
_hits: dict = {}
def rate_ok(bucket: str, limit: int, window: int = 60) -> bool:
    t = time.time(); arr = [x for x in _hits.get(bucket, []) if t - x < window]
    if len(arr) >= limit: _hits[bucket] = arr; return False
    arr.append(t); _hits[bucket] = arr; return True
