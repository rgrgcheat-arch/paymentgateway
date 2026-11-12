"""My profile: photo, personal + merchant details, password change."""
import base64
from fastapi import APIRouter, Request, Depends, Form, UploadFile, File, HTTPException
from fastapi.responses import Response
from sqlalchemy import select
import security as sec
from db import get_db
from models import User, GatewayCred
from core import render, flash, go, check_csrf, audit, current

router = APIRouter()
TYPES = ["Individual / Freelancer", "Sole proprietorship", "Partnership", "Private limited company", "LLP", "NGO / Trust", "Gaming / Digital services", "Other"]
MAX_PHOTO = 2 * 1024 * 1024

def _sniff(b: bytes):
    if b[:3] == b"\xff\xd8\xff": return "image/jpeg"
    if b[:8] == b"\x89PNG\r\n\x1a\n": return "image/png"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP": return "image/webp"
    return None

@router.get("/profile")
def profile(request: Request, u=Depends(current), db=Depends(get_db)):
    fam = db.scalar(select(GatewayCred).where(GatewayCred.user_id == u.id, GatewayCred.gateway == "fampay"))
    gmail = ""
    if fam:
        try: gmail = sec.dec(fam.data_enc).get("gmail", "")
        except Exception: gmail = ""
    return render(request, "profile.html", u, types=TYPES, gmail=gmail)

@router.post("/profile")
async def profile_save(request: Request, name: str = Form(""), phone: str = Form(""), business_name: str = Form(""), business_type: str = Form(""),
                       gstin: str = Form(""), address: str = Form(""), remove_photo: str = Form(""), csrf: str = Form(""),
                       photo: UploadFile = File(None), u=Depends(current), db=Depends(get_db)):
    check_csrf(request, csrf)
    name, phone = name.strip(), phone.strip()
    if len(name) < 2: flash(request, "Please enter your full name.", "err"); return go("/profile")
    digits = "".join(ch for ch in phone if ch.isdigit())
    if not (8 <= len(digits) <= 15): flash(request, "Please enter a valid phone number.", "err"); return go("/profile")
    gstin = gstin.strip().upper()
    if gstin and len(gstin) != 15: flash(request, "GSTIN must be 15 characters (or leave it empty).", "err"); return go("/profile")
    if photo is not None and photo.filename:
        raw = await photo.read(MAX_PHOTO + 1)
        if len(raw) > MAX_PHOTO: flash(request, "Photo is too large (maximum 2 MB).", "err"); return go("/profile")
        mime = _sniff(raw)
        if not mime: flash(request, "Photo must be a JPG, PNG or WebP image.", "err"); return go("/profile")
        u.photo_b64, u.photo_mime, u.photo_v = base64.b64encode(raw).decode(), mime, (u.photo_v or 0) + 1
    elif remove_photo:
        u.photo_b64, u.photo_mime, u.photo_v = "", "", (u.photo_v or 0) + 1
    u.name, u.phone = name[:120], phone[:20]
    u.business_name, u.business_type = business_name.strip()[:120], (business_type if business_type in TYPES else "")
    u.gstin, u.address = gstin, address.strip()[:500]
    audit(db, u.id, "profile_update"); db.commit(); flash(request, "Profile saved."); return go("/profile")

@router.post("/profile/password")
def password_change(request: Request, current_password: str = Form(""), new_password: str = Form(""), csrf: str = Form(""), u=Depends(current), db=Depends(get_db)):
    check_csrf(request, csrf)
    if not sec.rate_ok(f"pwchg:{u.id}", 5, 600): flash(request, "Too many attempts. Try again in a few minutes.", "err"); return go("/profile")
    if not sec.check_pw(current_password, u.pw_hash): flash(request, "Current password is wrong.", "err"); return go("/profile")
    if len(new_password) < 8: flash(request, "New password must be at least 8 characters.", "err"); return go("/profile")
    u.pw_hash = sec.hash_pw(new_password); audit(db, u.id, "password_change"); db.commit()
    flash(request, "Password updated."); return go("/profile")

@router.get("/profile/photo/{uid}")
def photo(uid: int, db=Depends(get_db)):
    u = db.get(User, uid)
    if not u or not u.photo_b64: raise HTTPException(404)
    return Response(base64.b64decode(u.photo_b64), media_type=u.photo_mime or "image/jpeg", headers={"Cache-Control": "public, max-age=86400"})
