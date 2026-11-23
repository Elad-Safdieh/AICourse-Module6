"""Exercise 2: Sign in with Google + save a copy of uploaded quotes to the user's own Drive.

Flow
 1. Sign in: the official Google button (Google Identity Services) gives the page an ID token.
    POST /auth/google verifies it on the server and puts {sub, email, name} in the session cookie.
    Sign-in asks for NO Drive permission.
 2. Connect Drive: only when the user clicks "חיבור ל-Drive" -> GET /auth/drive/start redirects to Google
    with scope drive.file (+ openid email to confirm it is the same account). Google returns a one-time code
    to /auth/drive/callback, the server exchanges it for tokens and keeps them in Firestore (server-side only).
 3. Save: after a dashboard upload, the server uploads the PDF to the folder "הצעות מחיר מה-CRM" in that user's Drive.

Config (env): GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET (from Secret Manager), PUBLIC_BASE_URL.
"""
from __future__ import annotations

import json
import os
import secrets
import time
from urllib.parse import urlencode

import requests
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.file"
FOLDER_NAME = "הצעות מחיר מה-CRM"
CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
REDIRECT_PATH = "/auth/drive/callback"

router = APIRouter(include_in_schema=False)
_store = None  # set by main.py


def init(store) -> None:
    global _store
    _store = store


def current_user(request: Request) -> dict | None:
    return request.session.get("user")


def redirect_uri(request: Request) -> str:
    base = BASE_URL or str(request.base_url).rstrip("/")
    return base + REDIRECT_PATH


def _verify_id_token(token: str) -> dict:
    from google.auth.transport import requests as greq
    from google.oauth2 import id_token
    return id_token.verify_oauth2_token(token, greq.Request(), CLIENT_ID)


# ---------- status / sign-in ----------

@router.get("/auth/me")
def me(request: Request):
    user = current_user(request)
    tok = _store.get_drive_token(user["sub"]) if user else None
    return {
        "configured": bool(CLIENT_ID),
        "drive_configured": bool(CLIENT_ID and CLIENT_SECRET),
        "client_id": CLIENT_ID,
        "user": user,
        "drive_connected": bool(tok),
        "drive_folder": FOLDER_NAME,
    }


class Credential(BaseModel):
    credential: str


@router.post("/auth/google")
def sign_in(body: Credential, request: Request):
    if not CLIENT_ID:
        raise HTTPException(503, "Google sign-in is not configured yet (GOOGLE_CLIENT_ID)")
    try:
        info = _verify_id_token(body.credential)
    except Exception:
        raise HTTPException(401, "Google sign-in could not be verified")
    request.session["user"] = {"sub": info["sub"], "email": info.get("email", ""), "name": info.get("name", "")}
    return {"user": request.session["user"]}


@router.post("/auth/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}


# ---------- Drive authorization (only on "Connect Drive") ----------

@router.get("/auth/drive/start")
def drive_start(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/dashboard?drive=signin_first")
    if not (CLIENT_ID and CLIENT_SECRET):
        return RedirectResponse("/dashboard?drive=not_configured")
    state = secrets.token_urlsafe(24)
    request.session["drive_state"] = state
    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": redirect_uri(request),
        "response_type": "code",
        "scope": f"openid email {DRIVE_SCOPE}",
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "false",
        "login_hint": user["email"],
        "state": state,
    }
    return RedirectResponse("https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params))


@router.get(REDIRECT_PATH)
def drive_callback(request: Request, state: str = "", code: str = "", error: str = ""):
    user = current_user(request)
    expected = request.session.pop("drive_state", None)
    if not user or not expected or state != expected:
        return RedirectResponse("/dashboard?drive=bad_state")
    if error or not code:  # user pressed Cancel on Google's screen
        return RedirectResponse("/dashboard?drive=cancelled")
    r = requests.post("https://oauth2.googleapis.com/token", data={
        "code": code, "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
        "redirect_uri": redirect_uri(request), "grant_type": "authorization_code"}, timeout=15)
    if r.status_code != 200:
        return RedirectResponse("/dashboard?drive=exchange_failed")
    tok = r.json()
    granted = tok.get("scope", "").split()
    if DRIVE_SCOPE not in granted:
        return RedirectResponse("/dashboard?drive=scope_missing")
    try:
        info = _verify_id_token(tok["id_token"])
    except Exception:
        return RedirectResponse("/dashboard?drive=exchange_failed")
    if info["sub"] != user["sub"]:
        # The Drive approval came from a different Google account than the one signed in.
        return RedirectResponse("/dashboard?drive=wrong_account")
    _store.set_drive_token(user["sub"], {
        "access_token": tok["access_token"],
        "refresh_token": tok.get("refresh_token", ""),
        "expires_at": time.time() + int(tok.get("expires_in", 3600)) - 60,
        "email": user["email"],
    })
    return RedirectResponse("/dashboard?drive=connected")


@router.post("/auth/drive/disconnect")
def drive_disconnect(request: Request):
    user = current_user(request)
    if user:
        _store.set_drive_token(user["sub"], None)
    return {"ok": True}


# ---------- Drive API calls ----------

class DriveError(Exception):
    pass


def _access_token(user: dict) -> str:
    tok = _store.get_drive_token(user["sub"])
    if not tok:
        raise DriveError("Drive is not connected")
    if time.time() < tok["expires_at"]:
        return tok["access_token"]
    if not tok.get("refresh_token"):
        raise DriveError("Drive session expired - connect Drive again")
    r = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
        "refresh_token": tok["refresh_token"], "grant_type": "refresh_token"}, timeout=15)
    if r.status_code != 200:
        _store.set_drive_token(user["sub"], None)  # revoked: force a clean reconnect
        raise DriveError("Drive access was revoked - connect Drive again")
    new = r.json()
    tok.update(access_token=new["access_token"], expires_at=time.time() + int(new.get("expires_in", 3600)) - 60)
    _store.set_drive_token(user["sub"], tok)
    return tok["access_token"]


def _folder_id(headers: dict) -> str:
    # With drive.file the app only sees folders it created itself, so this finds "our" folder.
    q = f"name = '{FOLDER_NAME}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    r = requests.get("https://www.googleapis.com/drive/v3/files", headers=headers,
                     params={"q": q, "fields": "files(id)", "spaces": "drive"}, timeout=15)
    if r.status_code != 200:
        raise DriveError(f"Drive folder lookup failed ({r.status_code})")
    files = r.json().get("files", [])
    if files:
        return files[0]["id"]
    r = requests.post("https://www.googleapis.com/drive/v3/files", headers=headers, params={"fields": "id"},
                      json={"name": FOLDER_NAME, "mimeType": "application/vnd.google-apps.folder"}, timeout=15)
    if r.status_code != 200:
        raise DriveError(f"Could not create Drive folder ({r.status_code})")
    return r.json()["id"]


def save_pdf(user: dict, file_name: str, content: bytes) -> dict:
    """Upload a PDF to the user's Drive folder. Returns {file_id, web_view_link, saved_at}. Raises DriveError."""
    try:
        headers = {"Authorization": f"Bearer {_access_token(user)}"}
        folder = _folder_id(headers)
        boundary = "ofek" + secrets.token_hex(8)
        meta = json.dumps({"name": file_name, "parents": [folder], "mimeType": "application/pdf"})
        body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{meta}\r\n"
                f"--{boundary}\r\nContent-Type: application/pdf\r\n\r\n").encode() + content + f"\r\n--{boundary}--".encode()
        r = requests.post("https://www.googleapis.com/upload/drive/v3/files",
                          params={"uploadType": "multipart", "fields": "id,webViewLink"},
                          headers={**headers, "Content-Type": f"multipart/related; boundary={boundary}"},
                          data=body, timeout=60)
        if r.status_code == 401:
            raise DriveError("Drive rejected the token - connect Drive again")
        if r.status_code != 200:
            raise DriveError(f"Drive upload failed ({r.status_code})")
        j = r.json()
        return {"file_id": j["id"], "web_view_link": j["webViewLink"], "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    except requests.RequestException as e:
        raise DriveError(f"Could not reach Google Drive ({type(e).__name__})")
