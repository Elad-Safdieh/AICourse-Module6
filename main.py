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

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

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


# ---------- response models (these drive the OpenAPI schema) ----------

class Customer(BaseModel):
    id: str = Field(examples=["cus-7f3a9c"], description="Unique customer ID - routing is by ID, never by name")
    name: str = Field(examples=["דנה"])
    document_count: int = Field(examples=[0])


class Source(str, Enum):
    office = "office"
    field = "field"
    unknown = "unknown"


class Document(BaseModel):
    id: str = Field(examples=["doc-1a2b3c4d5e6f"])
    customer_id: str
    file_name: str = Field(examples=["quote-a.pdf"])
    size_bytes: int
    source: Source = Field(description="office = dashboard, field = sales form (n8n)")
    uploaded_at: str = Field(description="ISO-8601 UTC")
    file_ref: str = Field(description="Storage reference (gs://bucket/object in the cloud)")
    file_url: str = Field(description="Path on this service that opens the source PDF")


class DocumentList(BaseModel):
    customer: Customer
    documents: list[Document]


class Error(BaseModel):
    detail: str


def _with_url(d: dict) -> dict:
    return {**d, "file_url": f"/api/customers/{d['customer_id']}/documents/{d['id']}/file"}


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
def list_customer_documents(customer_id: str):
    """Returns the documents stored in the chosen customer's folder (oldest first)."""
    c = _require_customer(customer_id)
    docs = [_with_url(d) for d in store.list_documents(customer_id)]
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
    return _with_url(store.add_document(customer_id, name, content, source.value))


@app.get("/api/customers/{customer_id}/documents/{document_id}/file", operation_id="getDocumentFile",
         summary="GET - open the source PDF", tags=["documents"], response_class=Response,
         responses={200: {"content": {"application/pdf": {}}, "description": "The PDF"},
                    404: {"model": Error, "description": "Customer or document not found"}})
def get_document_file(customer_id: str, document_id: str):
    _require_customer(customer_id)
    d = store.get_document(customer_id, document_id)
    if d is None:
        raise HTTPException(404, "Document not found")
    disposition = f"inline; filename*=UTF-8''{quote(d['file_name'])}"
    return Response(store.read_file(d), media_type="application/pdf",
                    headers={"Content-Disposition": disposition})


# ---------- pages ----------

@app.get("/dashboard", include_in_schema=False)
def dashboard():
    return FileResponse(STATIC / "dashboard.html")


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/dashboard")
