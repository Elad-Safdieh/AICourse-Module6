"""Exercise 3: remote MCP server for the CRM, served by the same Cloud Run service at /mcp (Streamable HTTP).

Auth (OAuth for MCP, via FastMCP's maintained GoogleProvider = OAuth proxy on top of the existing Google client):
  - claude.ai registers itself (DCR), the user signs in with Google, approves.
  - Claude receives a token issued BY THIS SERVER with audience = this MCP URL. It is verified on every request;
    the upstream Google token is checked against our Google client ID. Claude never gets the Drive token:
    the Drive token stays in Firestore and is only used by the dashboard upload.
  - Every tool call also checks that the signed-in Google account may access the CRM (ALLOWED_EMAILS),
    BEFORE any folder is read or written.

Tools (AX decisions):
  - find_latest_customer_quote  (read-only): search by customer name, returns every matching folder with
    distinguishing details so Claude must ask when two customers share a name.
  - add_customer_note (write): requires the exact customer_id (from the find tool), never a name, so a note can't
    land in the wrong "Dana". Returns the saved note exactly as stored + a link to the folder's notes.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token
from mcp_types import ToolAnnotations

BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
ALLOWED_EMAILS = {e.strip().lower() for e in os.environ.get("ALLOWED_EMAILS", "").split(",") if e.strip()}
MAX_NOTE_CHARS = 1000

_store = None


def _caller_email() -> str:
    """Identity of the person behind the token. No token -> rejected before touching any folder."""
    tok = get_access_token()
    email = ((tok.claims or {}).get("email") if tok else None) or ""
    if not email:
        raise ToolError("Not authenticated - reconnect the CRM connector")
    if email.lower() not in ALLOWED_EMAILS:
        raise ToolError(f"The Google account {email} has no access to the CRM customer folders")
    return email


def _require_customer(customer_id: str) -> dict:
    c = _store.get_customer(customer_id)
    if c is None:
        raise ToolError(f"No customer folder with id '{customer_id}'. Use find_latest_customer_quote to get the exact id.")
    return c


def _folder_summary(c: dict) -> dict:
    docs = _store.list_documents(c["id"])
    latest = docs[-1] if docs else None
    out = {
        "customer_id": c["id"],
        "customer_name": c["name"],
        "document_count": len(docs),
        "notes_count": len(_store.list_notes(c["id"])),
        "folder_link": f"{BASE_URL}/dashboard?customer={c['id']}",
        "latest_quote": None,
    }
    if latest:
        out["latest_quote"] = {
            "document_id": latest["id"],
            "file_name": latest["file_name"],
            "uploaded_at": latest["uploaded_at"],
            "uploaded_from": {"office": "office dashboard", "field": "sales form (n8n)"}.get(latest["source"], latest["source"]),
            "open_link": f"{BASE_URL}/api/customers/{c['id']}/documents/{latest['id']}/file",
            "open_link_note": "Opens the PDF in the CRM after Google sign-in.",
        }
    return out


def build(store, client_id: str, client_secret: str, signing_secret: str, project: str | None) -> FastMCP:
    global _store
    _store = store

    from fastmcp.server.auth.providers.google import GoogleProvider

    client_storage = None
    if project:
        # OAuth client registrations + token mappings survive Cloud Run restarts: Firestore, encrypted.
        from key_value.aio.stores.firestore import FirestoreStore
        from key_value.aio.wrappers.encryption import FernetEncryptionWrapper
        from key_value.aio.wrappers.prefix_collections import PrefixCollectionsWrapper
        client_storage = FernetEncryptionWrapper(
            PrefixCollectionsWrapper(FirestoreStore(project=project), prefix="ofek_mcp_oauth"),
            source_material=signing_secret, salt="ofek-mcp-oauth-storage")

    auth = GoogleProvider(
        client_id=client_id,
        client_secret=client_secret,
        base_url=BASE_URL,
        required_scopes=["openid", "https://www.googleapis.com/auth/userinfo.email"],
        jwt_signing_key=signing_secret + ":mcp-jwt",
        client_storage=client_storage,
    )

    mcp = FastMCP(
        name="Ofek CRM - customer folders",
        instructions=(
            "Tools for the Ofek CRM customer folders (quotes and notes). "
            "Always find the customer first with find_latest_customer_quote. If more than one folder matches, "
            "show the user the distinguishing details and ask which one before writing anything. "
            "Before add_customer_note, show the user the exact note text and the folder (name + id) and wait for approval."
        ),
        auth=auth,
    )

    @mcp.tool(
        annotations=ToolAnnotations(title="Find a customer's latest quote", readOnlyHint=True, openWorldHint=False),
    )
    def find_latest_customer_quote(customer_name: str) -> dict:
        """Find a customer's folder in the Ofek CRM and its most recent quote (latest uploaded PDF).

        Use this whenever the user asks about a customer's quote / folder / documents, and ALWAYS before adding a
        note, to get the exact customer_id.

        customer_name: the customer's name as written in the CRM. Names are stored in Hebrew (e.g. "דנה", "עמית");
        if the user writes the name in English, pass the Hebrew spelling. Partial names match.

        Returns every matching folder. If `matches` has more than one entry, two customers share the name:
        show the user the customer_id, document count and latest quote of each and ask which one - do not guess.
        If nothing matches, `all_customers` lists the folders that exist.
        """
        _caller_email()  # permission check before reading any folder
        q = customer_name.strip().lower()
        customers = _store.list_customers()
        matches = [c for c in customers if q and q in c["name"].lower()]
        if not matches:
            return {"matches": [], "all_customers": [{"customer_id": c["id"], "customer_name": c["name"]} for c in customers],
                    "message": f"No customer named '{customer_name}'."}
        result = {"matches": [_folder_summary(c) for c in matches]}
        if len(matches) > 1:
            result["ambiguous"] = True
            result["message"] = "More than one customer matches this name. Ask the user which folder they mean."
        return result

    @mcp.tool(
        annotations=ToolAnnotations(title="Add a note to a customer folder", readOnlyHint=False,
                                    destructiveHint=False, idempotentHint=False, openWorldHint=False),
    )
    def add_customer_note(customer_id: str, note: str) -> dict:
        """Save a short text note (e.g. "Dana asked to be called back on Thursday") in ONE customer's CRM folder.

        customer_id: the exact id returned by find_latest_customer_quote (e.g. "cus-7f3a9c"). A name is NOT accepted,
        so the note can never land in the wrong folder when two customers share a name.
        note: the note text, exactly as the user approved it (max 1000 characters).

        Before calling: show the user the note text and the target folder (name + customer_id) and get approval.
        Returns the note exactly as saved, the folder it was saved in, and a link to that folder's notes.
        Saving the same text to the same folder again within 10 minutes is not duplicated; the existing note is returned.
        """
        email = _caller_email()  # permission check before reading or writing any folder
        text = note.strip()
        if not text:
            raise ToolError("The note is empty")
        if len(text) > MAX_NOTE_CHARS:
            raise ToolError(f"The note is longer than {MAX_NOTE_CHARS} characters")
        c = _require_customer(customer_id.strip())
        recent_cutoff = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(timespec="seconds")
        for n in reversed(_store.list_notes(c["id"])):
            if n["text"] == text and n["created_at"] >= recent_cutoff:
                saved, duplicate = n, True
                break
        else:
            saved, duplicate = _store.add_note(c["id"], text, created_by=email, via="mcp"), False
        return {
            "saved": True,
            "duplicate_prevented": duplicate,
            "note": {"note_id": saved["id"], "text": saved["text"], "created_at": saved["created_at"],
                     "created_by": saved["created_by"]},
            "folder": {"customer_id": c["id"], "customer_name": c["name"]},
            "notes_in_folder": len(_store.list_notes(c["id"])),
            "notes_link": f"{BASE_URL}/api/customers/{c['id']}/notes",
            "folder_link": f"{BASE_URL}/dashboard?customer={c['id']}",
        }

    return mcp
