"""Background music: the owner uploads tracks (title + audio file), every signed-in user hears them in the top player bar."""
import os
from fastapi import APIRouter, Request, Depends, HTTPException, Form, File, UploadFile
from fastapi.responses import Response, JSONResponse
from sqlalchemy import select, func
from db import get_db
from models import MusicTrack, now
from core import render, flash, go, check_csrf, audit, current, owner_only

router = APIRouter()
MAX_TRACKS = 100
MAX_BYTES = 25 * 1024 * 1024
MIME = {"mp3": "audio/mpeg", "m4a": "audio/mp4", "aac": "audio/aac", "ogg": "audio/ogg", "oga": "audio/ogg", "opus": "audio/ogg", "wav": "audio/wav", "weba": "audio/webm", "webm": "audio/webm", "flac": "audio/flac"}

def _ordered(db, only_active=False):
    q = select(MusicTrack.id, MusicTrack.title, MusicTrack.mime, MusicTrack.size, MusicTrack.sort, MusicTrack.active, MusicTrack.created)
    if only_active: q = q.where(MusicTrack.active == True)  # noqa
    return db.execute(q.order_by(MusicTrack.sort, MusicTrack.id)).all()

# ---------- for every signed-in user ----------
@router.get("/music/list")
def music_list(u=Depends(current), db=Depends(get_db)):
    return JSONResponse([{"id": r.id, "title": r.title, "url": f"/music/{r.id}/file"} for r in _ordered(db, True)], headers={"Cache-Control": "no-store"})

@router.get("/music/{tid}/file")
def music_file(tid: int, request: Request, u=Depends(current), db=Depends(get_db)):
    t = db.get(MusicTrack, tid)
    if not t or not t.active and u.role != "owner": raise HTTPException(404)
    data, size = t.data, len(t.data)
    h = {"Accept-Ranges": "bytes", "Cache-Control": "private, max-age=86400"}
    rng = request.headers.get("range", "")
    if rng.startswith("bytes="):  # seeking / resuming from the saved position needs byte ranges
        try:
            a, _, z = rng[6:].split(",")[0].partition("-")
            start = int(a) if a else max(0, size - int(z)); end = int(z) if (a and z) else size - 1
            end = min(end, size - 1)
            if start > end or start >= size: raise ValueError
        except ValueError: return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
        return Response(data[start:end + 1], status_code=206, media_type=t.mime, headers={**h, "Content-Range": f"bytes {start}-{end}/{size}"})
    return Response(data, media_type=t.mime, headers=h)

# ---------- owner: manage ----------
@router.get("/admin/music")
def music_admin(request: Request, u=Depends(owner_only), db=Depends(get_db)):
    return render(request, "admin_music.html", u, rows=_ordered(db), max_tracks=MAX_TRACKS, max_mb=MAX_BYTES // 1048576)

@router.post("/admin/music/upload")
async def music_upload(request: Request, title: str = Form(""), file: UploadFile = File(...), csrf: str = Form(""), u=Depends(owner_only), db=Depends(get_db)):
    check_csrf(request, csrf)
    if db.scalar(select(func.count()).select_from(MusicTrack)) >= MAX_TRACKS: flash(request, f"Limit reached: {MAX_TRACKS} tracks. Delete one first.", "err"); return go("/admin/music")
    ext = (file.filename or "").rsplit(".", 1)[-1].lower()
    if ext not in MIME and not (file.content_type or "").startswith("audio/"): flash(request, "Please choose an audio file (mp3, m4a, aac, ogg, wav, flac).", "err"); return go("/admin/music")
    data = await file.read(MAX_BYTES + 1)
    if not data: flash(request, "The file is empty.", "err"); return go("/admin/music")
    if len(data) > MAX_BYTES: flash(request, f"File is too large (max {MAX_BYTES // 1048576} MB).", "err"); return go("/admin/music")
    name = title.strip()[:120] or os.path.splitext(file.filename or "Track")[0][:120] or "Track"
    mime = MIME.get(ext) or file.content_type or "audio/mpeg"
    nxt = (db.scalar(select(func.max(MusicTrack.sort))) or 0) + 1
    db.add(MusicTrack(title=name, mime=mime, size=len(data), data=data, sort=nxt, active=True)); audit(db, u.id, "music_add", name); db.commit()
    flash(request, f"“{name}” added."); return go("/admin/music")

@router.post("/admin/music/{tid}/rename")
def music_rename(tid: int, request: Request, title: str = Form(""), csrf: str = Form(""), u=Depends(owner_only), db=Depends(get_db)):
    check_csrf(request, csrf); t = db.get(MusicTrack, tid)
    if not t: raise HTTPException(404)
    if title.strip(): t.title = title.strip()[:120]; db.commit(); flash(request, "Name saved.")
    return go("/admin/music")

@router.post("/admin/music/{tid}/toggle")
def music_toggle(tid: int, request: Request, csrf: str = Form(""), u=Depends(owner_only), db=Depends(get_db)):
    check_csrf(request, csrf); t = db.get(MusicTrack, tid)
    if not t: raise HTTPException(404)
    t.active = not t.active; db.commit(); flash(request, f"“{t.title}” is now {'ON' if t.active else 'OFF'} for users."); return go("/admin/music")

@router.post("/admin/music/{tid}/delete")
def music_delete(tid: int, request: Request, csrf: str = Form(""), u=Depends(owner_only), db=Depends(get_db)):
    check_csrf(request, csrf); t = db.get(MusicTrack, tid)
    if t: audit(db, u.id, "music_delete", t.title); db.delete(t); db.commit(); flash(request, "Track deleted.")
    return go("/admin/music")

@router.post("/admin/music/{tid}/move")
def music_move(tid: int, request: Request, d: str = Form("up"), csrf: str = Form(""), u=Depends(owner_only), db=Depends(get_db)):
    check_csrf(request, csrf)
    ids = [r.id for r in _ordered(db)]
    if tid in ids:
        i = ids.index(tid); j = i - 1 if d == "up" else i + 1
        if 0 <= j < len(ids): ids[i], ids[j] = ids[j], ids[i]
        for n, x in enumerate(ids, 1): db.get(MusicTrack, x).sort = n
        db.commit()
    return go("/admin/music")
