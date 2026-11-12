"""Owner Panel -> Backup: download every table (all users A-Z, gateways, keys, payments, plans...) as one file, upload it later to restore.
Restore reads only the columns that exist in the current code, so a backup taken before you added new columns/features still restores (new columns get their defaults)."""
import json, gzip, zlib, os, time, threading
import requests
from datetime import datetime
from fastapi import APIRouter, Request, Depends, Form, File, UploadFile
from fastapi.responses import Response
from sqlalchemy import select, func, delete, insert, text, DateTime
from sqlalchemy.exc import IntegrityError
from db import Base, engine, get_db, SessionLocal
import models
from models import now
from core import render, flash, go, check_csrf, audit, owner_only, get_setting, set_setting, ist

router = APIRouter()
MAX_UPLOAD = 100 * 1024 * 1024
def _tables(): return [t for t in Base.metadata.sorted_tables if t.name != "music_tracks"]  # audio files are large: not part of the JSON backup

def export_all(db):
    out = {}
    for t in _tables():
        out[t.name] = [{k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in r.items()} for r in db.execute(select(t)).mappings()]
    return {"format": "payment-gateway-backup", "version": 1, "created": now().isoformat() + "Z", "tables": out}

def _read(raw):
    if len(raw) > MAX_UPLOAD: raise ValueError("File is too large (max 100 MB).")
    if raw[:2] == b"\x1f\x8b":  # gzip, with a decompression-bomb limit
        d = zlib.decompressobj(31); raw = d.decompress(raw, 500 * 1024 * 1024)
        if d.unconsumed_tail: raise ValueError("Backup is too large once unpacked.")
    try: data = json.loads(raw.decode("utf-8-sig"))
    except Exception: raise ValueError("This is not a valid Payment Gateway backup file.")
    if not isinstance(data, dict) or data.get("format") not in ("payment-gateway-backup", "zyvo-backup") or not isinstance(data.get("tables"), dict): raise ValueError("This is not a Payment Gateway backup file.")
    return data

def _coerce(t, row):
    out = {}
    for c in t.columns:  # only columns that exist now; missing ones fall back to their defaults
        if c.name not in row: continue
        v = row[c.name]
        if isinstance(v, str) and isinstance(c.type, DateTime):
            try: v = datetime.fromisoformat(v.replace("Z", ""))
            except ValueError: v = None
        out[c.name] = v
    return out
def _rows(t, data):
    rows = [_coerce(t, r) for r in data["tables"].get(t.name, []) if isinstance(r, dict)]
    if t.name == "users": rows.sort(key=lambda r: (r.get("parent_id") is not None, r.get("id") or 0))  # parents first
    return rows
def _fix_sequences(cn):
    if engine.dialect.name != "postgresql": return
    for t in _tables():
        if "id" in t.c and t.c.id.primary_key:
            cn.execute(text(f"SELECT setval(pg_get_serial_sequence('{t.name}','id'), COALESCE((SELECT MAX(id) FROM {t.name}), 0) + 1, false)"))

def restore_replace(data):
    """Wipes everything and loads the backup in ONE transaction: if anything fails nothing changes."""
    n = 0
    with engine.begin() as cn:
        for t in reversed(_tables()): cn.execute(delete(t))
        for t in _tables():
            rows = _rows(t, data)
            for i in range(0, len(rows), 500): cn.execute(insert(t), rows[i:i + 500])
            n += len(rows)
        _fix_sequences(cn)
    return n, 0
def restore_merge(data):
    """Keeps current data; adds only records that are missing (same id = skipped)."""
    added = skipped = 0
    with engine.begin() as cn:
        for t in _tables():
            pk = list(t.primary_key.columns)
            have = {tuple(r) for r in cn.execute(select(*pk))}
            for row in _rows(t, data):
                if tuple(row.get(c.name) for c in pk) in have: skipped += 1; continue
                try:
                    with cn.begin_nested(): cn.execute(insert(t), [row])
                    added += 1
                except IntegrityError: skipped += 1  # e.g. same email/username already used by another account
        _fix_sequences(cn)
    return added, skipped

@router.get("/admin/backup")
def backup_page(request: Request, u=Depends(owner_only), db=Depends(get_db)):
    counts = [(t.name, db.scalar(select(func.count()).select_from(t))) for t in _tables()]
    tok, chat = _tg_cfg(); last = get_setting("backup_last_ok", "0")
    try: last_txt = f"{ist(datetime.utcfromtimestamp(float(last))):%d %b %Y, %I:%M %p}" if float(last) > 0 else ""
    except Exception: last_txt = ""
    return render(request, "admin_backup.html", u, counts=counts, total=sum(c for _, c in counts),
                  tg_tok=bool(tok), tg_tok_hint=("…" + tok[-4:]) if tok else "", tg_chat=chat, auto_on=get_setting("backup_auto", "1") == "1",
                  hours=_hours(), last_txt=last_txt, status=get_setting("backup_status", ""))
@router.get("/admin/backup/download")
def backup_download(request: Request, u=Depends(owner_only), db=Depends(get_db)):
    data = export_all(db); n = sum(len(v) for v in data["tables"].values())
    audit(db, u.id, "backup_download", f"{n} records"); db.commit()
    body = gzip.compress(json.dumps(data, separators=(",", ":"), default=str).encode("utf-8"), 6)
    return Response(body, media_type="application/gzip", headers={"Content-Disposition": f'attachment; filename="payment-gateway-backup-{now():%Y%m%d-%H%M}.json.gz"', "Cache-Control": "no-store"})
@router.post("/admin/backup/restore")
async def backup_restore(request: Request, file: UploadFile = File(...), mode: str = Form("merge"), confirm: str = Form(""), csrf: str = Form(""), u=Depends(owner_only), db=Depends(get_db)):
    check_csrf(request, csrf)
    try:
        data = _read(await file.read(MAX_UPLOAD + 1))
        if mode == "replace":
            if confirm.strip().upper() != "RESTORE": raise ValueError('Type RESTORE to confirm replacing all current data.')
            if not data["tables"].get("users"): raise ValueError("The backup has no users, so it was not applied.")
    except ValueError as e: flash(request, str(e), "err"); return go("/admin/backup")
    uid = u.id
    try: a, b = restore_replace(data) if mode == "replace" else restore_merge(data)
    except Exception as e: flash(request, f"Restore failed, nothing was changed: {str(e)[:220]}", "err"); return go("/admin/backup")
    db.rollback()
    try: audit(db, None, "backup_restore", f"{mode}: {a} records" + (f", {b} skipped" if b else "") + f" by user {uid}"); db.commit()
    except Exception: db.rollback()
    request.session.clear()  # accounts may have changed: sign in again
    flash(request, (f"Restore complete: {a} records loaded." if mode == "replace" else f"Restore complete: {a} records added, {b} already existed or conflicted.") + " Please sign in again.")
    return go("/login")


# ---------- automatic backup: every N hours (default 12) the full backup is sent to the owner's Telegram ----------
# Why Telegram: the copy lives OUTSIDE Render, so even if the service/database is wiped you still have the file.
def _tg_cfg():
    tok = (get_setting("backup_tg_token") or os.getenv("TELEGRAM_BOT_TOKEN", "")).strip()
    chat = (get_setting("backup_tg_chat") or os.getenv("BACKUP_CHAT_ID", "")).strip()
    return tok, chat
def _hours():
    try: return min(168, max(1, int(get_setting("backup_hours", "12") or 12)))
    except Exception: return 12

def make_backup_bytes():
    with SessionLocal() as db: data = export_all(db)
    n = sum(len(v) for v in data["tables"].values())
    return gzip.compress(json.dumps(data, separators=(",", ":"), default=str).encode("utf-8"), 6), n

def send_backup(reason="auto"):
    """Returns (ok, message). The bot token is never put into any message."""
    tok, chat = _tg_cfg()
    if not tok or not chat: return False, "Telegram bot token / chat ID is not set."
    try: body, n = make_backup_bytes()
    except Exception as e: return False, f"Could not build the backup: {type(e).__name__}"
    if len(body) > 49 * 1024 * 1024: return False, "Backup is over Telegram's 50 MB file limit."
    fn = f"payment-gateway-backup-{now():%Y%m%d-%H%M}.json.gz"
    cap = f"💾 Payment Gateway backup ({reason})\n{n} records · {max(1, len(body) // 1024)} KB\n{now():%d %b %Y %H:%M} UTC"
    try:
        r = requests.post(f"https://api.telegram.org/bot{tok}/sendDocument", data={"chat_id": chat, "caption": cap},
                          files={"document": (fn, body, "application/gzip")}, timeout=90)
        j = r.json()
    except Exception as e: return False, f"Could not reach Telegram ({type(e).__name__})."
    if not j.get("ok"): return False, "Telegram refused: " + str(j.get("description", "unknown error"))[:160]
    return True, f"Sent {n} records ({max(1, len(body) // 1024)} KB)."

def _record(ok, msg):
    with SessionLocal() as db:
        t = time.time()
        if ok: set_setting(db, "backup_last_ok", str(t))
        set_setting(db, "backup_status", f"{'OK' if ok else 'FAILED'} · {now():%d %b %H:%M} UTC · {msg}")

def _scheduler():
    time.sleep(60)  # let the app finish starting
    while True:
        try:
            if get_setting("backup_auto", "1") == "1":
                tok, chat = _tg_cfg()
                if tok and chat:
                    last_ok = float(get_setting("backup_last_ok", "0") or 0); last_try = float(get_setting("backup_last_try", "0") or 0)
                    t = time.time()
                    if t - last_ok >= _hours() * 3600 and t - last_try >= 600:  # failed attempts are retried every 10 min
                        with SessionLocal() as db:
                            set_setting(db, "backup_last_try", str(t))
                            has_users = db.scalar(select(func.count()).select_from(models.User)) > 0
                        if has_users:  # never send an empty backup after a database wipe
                            ok, msg = send_backup("auto"); _record(ok, msg); print("auto-backup:", ok, msg, flush=True)
        except Exception as e: print("backup scheduler error:", type(e).__name__, flush=True)
        time.sleep(60)
_sched_started = False
def _start_scheduler():
    global _sched_started
    if not _sched_started: _sched_started = True; threading.Thread(target=_scheduler, daemon=True, name="auto-backup").start()
_start_scheduler()

@router.post("/admin/backup/auto")
def backup_auto_save(request: Request, tg_token: str = Form(""), tg_chat: str = Form(""), hours: int = Form(12), auto: str = Form(""),
                     csrf: str = Form(""), u=Depends(owner_only), db=Depends(get_db)):
    check_csrf(request, csrf)
    tg_token, tg_chat = tg_token.strip(), tg_chat.strip()
    if tg_token and (":" not in tg_token or " " in tg_token): flash(request, "That does not look like a Telegram bot token (from @BotFather).", "err"); return go("/admin/backup")
    if tg_chat and not (tg_chat.lstrip("-").isdigit() or tg_chat.startswith("@")): flash(request, "Chat ID must be a number (or @channelname).", "err"); return go("/admin/backup")
    if tg_token: set_setting(db, "backup_tg_token", tg_token)  # empty = keep the saved token
    set_setting(db, "backup_tg_chat", tg_chat)
    set_setting(db, "backup_hours", str(min(168, max(1, hours))))
    set_setting(db, "backup_auto", "1" if auto else "0")
    audit(db, u.id, "backup_auto_settings", f"auto={'on' if auto else 'off'}, every {min(168, max(1, hours))}h"); db.commit()
    flash(request, "Auto-backup settings saved." + ("" if auto else " Auto-backup is OFF.")); return go("/admin/backup")

@router.post("/admin/backup/send-now")
def backup_send_now(request: Request, csrf: str = Form(""), u=Depends(owner_only), db=Depends(get_db)):
    check_csrf(request, csrf)
    ok, msg = send_backup("manual"); _record(ok, msg)
    audit(db, u.id, "backup_send_now", ("ok: " if ok else "failed: ") + msg); db.commit()
    flash(request, ("Backup sent to your Telegram. " if ok else "Could not send: ") + msg, "ok" if ok else "err"); return go("/admin/backup")
