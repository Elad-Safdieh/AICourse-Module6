"""Shared document service - one API, two interfaces (office dashboard + n8n sales form).

Routes
  /dashboard                                         office dashboard (static page that calls the API below)
  /docs, /openapi.json                               Swagger UI and the OpenAPI spec (generated from this code)
  GET  /api/customers                                listCustomers
  GET  /api/customers/{customer_id}/documents        listCustomerDocuments
  POST /api/documents                                uploadDocument   (multipart: customer_id + PDF file)
  GET  /api/customers/{customer_id}/documents/{document_id}/file   getDocumentFile (opens the source PDF)

All validation (customer exists, file is a real PDF, size) happens HERE, in the service,
so every interface gets the same rules - not only the dashboard.
"""
from __future__ import annotations

import os
from enum import Enum
from pathlib import Path
from urllib.parse import quote

import secrets

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

import google_drive
from store import make_store

MAX_BYTES = int(os.environ.get("MAX_UPLOAD_MB", "10")) * 1024 * 1024
STATIC = Path(__file__).parent / "static"

app = FastAPI(
    title="Ofek Shared Documents API",
    version="1.0.0",
    description=(
        "שירות משותף של אופק שירותי מחשוב: מקבל קבצי PDF ומשייך כל קובץ לתיקיית הלקוח הנכונה. "
        "דשבורד המשרד וטופס המכירות ב-n8n קוראים לאותן פעולות בדיוק.\n\n"
        "Shared service: receives PDF files and assigns each one to the right customer folder. "
        "The office dashboard and the n8n sales form call the same operations."
    ),
)
store = make_store()

# Session cookie (signed) holds only who is signed in. Drive tokens stay server-side in Firestore.
app.add_middleware(SessionMiddleware, secret_key=os.environ.get("SESSION_SECRET") or secrets.token_hex(32),
                   same_site="lax", https_only=os.environ.get("STORE_BACKEND") == "gcp", max_age=8 * 3600)
google_drive.init(store)
app.include_router(google_drive.router)
REQUIRE_LOGIN_FOR_FILES = os.environ.get("REQUIRE_LOGIN_FOR_FILES", "1") == "1"

# ---------- Exercise 3: MCP server at /mcp (only when Google OAuth is configured - never unauthenticated) ----------
_mcp_app = None
if os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_CLIENT_SECRET") and os.environ.get("SESSION_SECRET"):
    import mcp_server
    _mcp = mcp_server.build(store, os.environ["GOOGLE_CLIENT_ID"], os.environ["GOOGLE_CLIENT_SECRET"],
                            os.environ["SESSION_SECRET"],
                            os.environ.get("GOOGLE_CLOUD_PROJECT") if os.environ.get("STORE_BACKEND") == "gcp" else None)
    _mcp_app = _mcp.http_app(path="/mcp")
    app.router.lifespan_context = _mcp_app.lifespan


# ---------- response models (these drive the OpenAPI schema) ----------

class Customer(BaseModel):
    id: str = Field(examples=["cus-7f3a9c"], description="Unique customer ID - routing is by ID, never by name")
    name: str = Field(examples=["דנה"])
    document_count: int = Field(examples=[0])


class Source(str, Enum):
    office = "office"
    field = "field"
    unknown = "unknown"


class DriveStatus(BaseModel):
    status: str = Field(description="saved | failed | not_saved (only for the signed-in user's own Drive)")
    web_view_link: str | None = None
    error: str | None = None


class Document(BaseModel):
    id: str = Field(examples=["doc-1a2b3c4d5e6f"])
    customer_id: str
    file_name: str = Field(examples=["quote-a.pdf"])
    size_bytes: int
    source: Source = Field(description="office = dashboard, field = sales form (n8n)")
    uploaded_at: str = Field(description="ISO-8601 UTC")
    file_ref: str = Field(description="Storage reference (gs://bucket/object in the cloud)")
    file_url: str = Field(description="Path on this service that opens the source PDF")
    drive: DriveStatus | None = Field(None, description="Copy in the signed-in user's Drive (dashboard only)")


class DocumentList(BaseModel):
    customer: Customer
    documents: list[Document]


class Error(BaseModel):
    detail: str


def _with_url(d: dict, user: dict | None = None) -> dict:
    out = {k: v for k, v in d.items() if k != "drive_copies"}  # never leak other users' Drive links
    out["file_url"] = f"/api/customers/{d['customer_id']}/documents/{d['id']}/file"
    if user:
        copy = (d.get("drive_copies") or {}).get(user["sub"])
        out["drive"] = {"status": "saved", "web_view_link": copy["web_view_link"]} if copy else {"status": "not_saved"}
    return out


def _save_to_drive(user: dict, d: dict, content: bytes) -> dict:
    try:
        info = google_drive.save_pdf(user, d["file_name"], content)
    except google_drive.DriveError as e:
        return {"status": "failed", "error": str(e)}
    store.set_drive_copy(d["customer_id"], d["id"], user["sub"], info)
    return {"status": "saved", "web_view_link": info["web_view_link"]}


def _require_customer(customer_id: str) -> dict:
    c = store.get_customer(customer_id)
    if c is None:
        raise HTTPException(404, f"Customer '{customer_id}' does not exist")
    return c


# ---------- API ----------

@app.get("/api/customers", response_model=list[Customer], operation_id="listCustomers",
         summary="GET - list customers", tags=["customers"])
def list_customers():
    """Returns every customer folder with its ID and number of documents."""
    return store.list_customers()


@app.get("/api/customers/{customer_id}/documents", response_model=DocumentList,
         operation_id="listCustomerDocuments", summary="GET - list documents of one customer",
         tags=["documents"], responses={404: {"model": Error, "description": "Customer does not exist"}})
def list_customer_documents(customer_id: str, request: Request):
    """Returns the documents stored in the chosen customer's folder (oldest first)."""
    c = _require_customer(customer_id)
    user = google_drive.current_user(request)
    docs = [_with_url(d, user) for d in store.list_documents(customer_id)]
    return {"customer": {**c, "document_count": len(docs)}, "documents": docs}


@app.post("/api/documents", response_model=Document, status_code=201, operation_id="uploadDocument",
          summary="POST - upload a PDF to a customer folder", tags=["documents"],
          responses={
              400: {"model": Error, "description": "Empty file"},
              404: {"model": Error, "description": "Customer does not exist - nothing is stored"},
              413: {"model": Error, "description": "File too large"},
              415: {"model": Error, "description": "Not a PDF - nothing is stored"},
          })
async def upload_document(
    request: Request,
    customer_id: str = Form(..., description="ID of an existing customer, e.g. cus-7f3a9c"),
    file: UploadFile = File(..., description="PDF file only"),
    source: Source = Form(Source.unknown, description="Who uploaded: office (dashboard) / field (sales form)"),
):
    """Stores the PDF in Cloud Storage and records the assignment (file name, customer, file reference) in Firestore.
    Rejects unknown customers and non-PDF files before anything is stored."""
    _require_customer(customer_id)
    content = await file.read(MAX_BYTES + 1)
    if len(content) == 0:
        raise HTTPException(400, "File is empty")
    if len(content) > MAX_BYTES:
        raise HTTPException(413, f"File is larger than {MAX_BYTES // (1024 * 1024)} MB")
    # Check the actual bytes, not just the extension or the declared content type:
    # a Word file renamed to .pdf is still rejected.
    if not content.startswith(b"%PDF-"):
        raise HTTPException(415, "Only PDF files are accepted")
    name = os.path.basename(file.filename or "document.pdf")
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    d = store.add_document(customer_id, name, content, source.value)
    out = _with_url(d)
    # Exercise 2: if the caller is signed in and connected Drive, also save a copy to THEIR Drive.
    # The CRM upload above is already saved; a Drive failure is reported, not fatal.
    user = google_drive.current_user(request)
    if user and store.get_drive_token(user["sub"]):
        out["drive"] = _save_to_drive(user, d, content)
    elif user:
        out["drive"] = {"status": "not_saved", "error": "Drive is not connected"}
    return out


@app.post("/api/customers/{customer_id}/documents/{document_id}/drive", response_model=DriveStatus,
          operation_id="saveDocumentToDrive", summary="POST - (re)save a document to the signed-in user's Drive",
          tags=["documents"], responses={401: {"model": Error}, 404: {"model": Error}})
def save_document_to_drive(customer_id: str, document_id: str, request: Request):
    """Retry button: copies an existing CRM document to the signed-in user's Drive. Needs Google sign-in + Drive."""
    user = google_drive.current_user(request)
    if not user:
        raise HTTPException(401, "Sign in with Google first")
    _require_customer(customer_id)
    d = store.get_document(customer_id, document_id)
    if d is None:
        raise HTTPException(404, "Document not found")
    return _save_to_drive(user, d, store.read_file(d))


@app.get("/api/customers/{customer_id}/documents/{document_id}/file", operation_id="getDocumentFile",
         summary="GET - open the source PDF", tags=["documents"], response_class=Response,
         responses={200: {"content": {"application/pdf": {}}, "description": "The PDF"},
                    401: {"model": Error, "description": "Not signed in"},
                    404: {"model": Error, "description": "Customer or document not found"}})
def get_document_file(customer_id: str, document_id: str, request: Request):
    """Opens the PDF inside the CRM. The service checks access first: a Google-signed-in user is required."""
    if REQUIRE_LOGIN_FOR_FILES and not google_drive.current_user(request):
        if "text/html" in request.headers.get("accept", ""):
            # A person clicked a link (e.g. from Claude): sign in on the dashboard, then come back here.
            return RedirectResponse(f"/dashboard?open={quote(request.url.path)}")
        raise HTTPException(401, "Sign in with Google to open documents")
    _require_customer(customer_id)
    d = store.get_document(customer_id, document_id)
    if d is None:
        raise HTTPException(404, "Document not found")
    disposition = f"inline; filename*=UTF-8''{quote(d['file_name'])}"
    return Response(store.read_file(d), media_type="application/pdf",
                    headers={"Content-Disposition": disposition})


# ---------- pages ----------

class Note(BaseModel):
    id: str
    customer_id: str
    text: str
    created_by: str
    via: str = Field(description="mcp = written by Claude through the MCP connector")
    created_at: str


class NoteList(BaseModel):
    customer: Customer
    notes: list[Note]


@app.get("/api/customers/{customer_id}/notes", response_model=NoteList, operation_id="listCustomerNotes",
         summary="GET - list notes in one customer's folder", tags=["notes"],
         responses={404: {"model": Error, "description": "Customer does not exist"}})
def list_customer_notes(customer_id: str):
    """Notes saved in the customer's folder (oldest first). Works also when the list is empty."""
    c = _require_customer(customer_id)
    notes = store.list_notes(customer_id)
    docs = store.list_documents(customer_id)
    return {"customer": {**c, "document_count": len(docs)}, "notes": notes}


@app.get("/dashboard", include_in_schema=False)
def dashboard():
    return FileResponse(STATIC / "dashboard.html")


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/dashboard")


# Mounted LAST so every route above wins; the MCP app adds /mcp plus its OAuth routes
# (/.well-known/*, /register, /authorize, /token, /auth/callback, /consent).
if _mcp_app is not None:
    app.mount("/", _mcp_app)
