"""Storage layer: customer folders + documents.

Two backends with the same interface:
- GcpStore:   files in Cloud Storage, assignment records in Firestore (production / Cloud Run).
- LocalStore: files + a JSON index on local disk (temporary storage, for local runs and tests).

Chosen by STORE_BACKEND env var ("gcp" or "local").
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

# The two customer folders the exercise asks for. IDs are deliberately opaque and distinct,
# so two customers with the same name can never be confused - the API routes by ID only.
SEED_CUSTOMERS = [
    {"id": "cus-7f3a9c", "name": "דנה"},
    {"id": "cus-b2e41d", "name": "עמית"},
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_doc_id() -> str:
    return "doc-" + uuid.uuid4().hex[:12]


class LocalStore:
    """Temporary local storage. Not for production."""

    def __init__(self, root: str):
        self.root = Path(root)
        self.files = self.root / "files"
        self.index = self.root / "index.json"
        self._lock = threading.Lock()
        self.files.mkdir(parents=True, exist_ok=True)
        if not self.index.exists():
            self._save({"customers": {c["id"]: {"name": c["name"], "documents": []} for c in SEED_CUSTOMERS}})

    def _load(self) -> dict:
        return json.loads(self.index.read_text(encoding="utf-8"))

    def _save(self, data: dict) -> None:
        self.index.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def list_customers(self) -> list[dict]:
        data = self._load()
        return [{"id": cid, "name": c["name"], "document_count": len(c["documents"])}
                for cid, c in data["customers"].items()]

    def get_customer(self, customer_id: str) -> dict | None:
        c = self._load()["customers"].get(customer_id)
        return {"id": customer_id, "name": c["name"]} if c else None

    def list_documents(self, customer_id: str) -> list[dict]:
        docs = self._load()["customers"][customer_id]["documents"]
        return sorted(docs, key=lambda d: d["uploaded_at"])

    def add_document(self, customer_id: str, file_name: str, content: bytes, source: str) -> dict:
        doc_id = _new_doc_id()
        ref = f"customers/{customer_id}/{doc_id}.pdf"
        path = self.files / ref
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        record = {"id": doc_id, "customer_id": customer_id, "file_name": file_name,
                  "size_bytes": len(content), "source": source, "uploaded_at": _now(),
                  "file_ref": f"local://{ref}"}
        with self._lock:
            data = self._load()
            data["customers"][customer_id]["documents"].append(record)
            self._save(data)
        return record

    def get_document(self, customer_id: str, doc_id: str) -> dict | None:
        for d in self._load()["customers"].get(customer_id, {}).get("documents", []):
            if d["id"] == doc_id:
                return d
        return None

    def read_file(self, record: dict) -> bytes:
        return (self.files / record["file_ref"].removeprefix("local://")).read_bytes()

    # --- Drive (exercise 2) ---
    def set_drive_copy(self, customer_id: str, doc_id: str, user_sub: str, info: dict) -> None:
        with self._lock:
            data = self._load()
            for d in data["customers"][customer_id]["documents"]:
                if d["id"] == doc_id:
                    d.setdefault("drive_copies", {})[user_sub] = info
            self._save(data)

    def get_drive_token(self, user_sub: str) -> dict | None:
        return self._load().get("drive_tokens", {}).get(user_sub)

    def set_drive_token(self, user_sub: str, token: dict | None) -> None:
        with self._lock:
            data = self._load()
            tokens = data.setdefault("drive_tokens", {})
            if token is None:
                tokens.pop(user_sub, None)
            else:
                tokens[user_sub] = token
            self._save(data)


class GcpStore:
    """Cloud Storage for the PDF bytes, Firestore for the customer-assignment records.

    Firestore layout:
      customers/{customer_id}                       -> {name}
      customers/{customer_id}/documents/{doc_id}    -> {file_name, size_bytes, source, uploaded_at, file_ref}
    """

    def __init__(self, project: str, bucket: str):
        from google.cloud import firestore, storage
        self.db = firestore.Client(project=project)
        self.bucket = storage.Client(project=project).bucket(bucket)
        self.bucket_name = bucket
        # Own collection name: the (default) Firestore DB is shared with other course projects.
        self.col = os.environ.get("FIRESTORE_COLLECTION", "ofek_customers")

    def seed(self) -> None:
        """Create the two customer folders if missing (empty document lists). Idempotent."""
        for c in SEED_CUSTOMERS:
            ref = self.db.collection(self.col).document(c["id"])
            if not ref.get().exists:
                ref.set({"name": c["name"], "created_at": _now()})

    def list_customers(self) -> list[dict]:
        out = []
        for snap in self.db.collection(self.col).stream():
            count = self.db.collection(self.col).document(snap.id).collection("documents").count().get()
            out.append({"id": snap.id, "name": snap.get("name"), "document_count": int(count[0][0].value)})
        return out

    def get_customer(self, customer_id: str) -> dict | None:
        snap = self.db.collection(self.col).document(customer_id).get()
        return {"id": snap.id, "name": snap.get("name")} if snap.exists else None

    def list_documents(self, customer_id: str) -> list[dict]:
        col = self.db.collection(self.col).document(customer_id).collection("documents")
        docs = [{"id": s.id, "customer_id": customer_id, **s.to_dict()} for s in col.stream()]
        return sorted(docs, key=lambda d: d["uploaded_at"])

    def add_document(self, customer_id: str, file_name: str, content: bytes, source: str) -> dict:
        doc_id = _new_doc_id()
        object_name = f"customers/{customer_id}/{doc_id}.pdf"
        # 1) file first - a Firestore record must never point at a file that does not exist
        self.bucket.blob(object_name).upload_from_string(content, content_type="application/pdf")
        record = {"file_name": file_name, "size_bytes": len(content), "source": source,
                  "uploaded_at": _now(), "file_ref": f"gs://{self.bucket_name}/{object_name}"}
        # 2) then the assignment record
        self.db.collection(self.col).document(customer_id).collection("documents").document(doc_id).set(record)
        return {"id": doc_id, "customer_id": customer_id, **record}

    def get_document(self, customer_id: str, doc_id: str) -> dict | None:
        snap = (self.db.collection(self.col).document(customer_id)
                .collection("documents").document(doc_id).get())
        return {"id": snap.id, "customer_id": customer_id, **snap.to_dict()} if snap.exists else None

    def read_file(self, record: dict) -> bytes:
        object_name = record["file_ref"].removeprefix(f"gs://{self.bucket_name}/")
        return self.bucket.blob(object_name).download_as_bytes()

    # --- Drive (exercise 2) ---
    # Per-user Drive copy is recorded on the document: drive_copies.{google_sub} = {file_id, web_view_link, ...}
    def set_drive_copy(self, customer_id: str, doc_id: str, user_sub: str, info: dict) -> None:
        (self.db.collection(self.col).document(customer_id).collection("documents").document(doc_id)
         .set({"drive_copies": {user_sub: info}}, merge=True))

    # Drive OAuth tokens live server-side in Firestore (never in the browser cookie).
    def get_drive_token(self, user_sub: str) -> dict | None:
        snap = self.db.collection(self.col + "_drive_tokens").document(user_sub).get()
        return snap.to_dict() if snap.exists else None

    def set_drive_token(self, user_sub: str, token: dict | None) -> None:
        ref = self.db.collection(self.col + "_drive_tokens").document(user_sub)
        ref.delete() if token is None else ref.set(token)


def make_store():
    backend = os.environ.get("STORE_BACKEND", "local")
    if backend == "gcp":
        project = os.environ["GOOGLE_CLOUD_PROJECT"]
        bucket = os.environ["GCS_BUCKET"]
        s = GcpStore(project, bucket)
        s.seed()  # create the two customer folders (empty) if they do not exist yet
        return s
    return LocalStore(os.environ.get("LOCAL_DATA_DIR", ".local-data"))


# ---------- Notes (exercise 3) ----------
# Notes live in the customer's folder next to its documents:
#   local: index.json customers.{id}.notes[]     gcp: {FIRESTORE_COLLECTION}/{customer_id}/notes/{note_id}

def _new_note_id() -> str:
    return "note-" + uuid.uuid4().hex[:12]


def _local_add_note(self, customer_id: str, text: str, created_by: str, via: str) -> dict:
    note = {"id": _new_note_id(), "customer_id": customer_id, "text": text,
            "created_by": created_by, "via": via, "created_at": _now()}
    with self._lock:
        data = self._load()
        data["customers"][customer_id].setdefault("notes", []).append(note)
        self._save(data)
    return note


def _local_list_notes(self, customer_id: str) -> list[dict]:
    return sorted(self._load()["customers"][customer_id].get("notes", []), key=lambda n: n["created_at"])


def _gcp_add_note(self, customer_id: str, text: str, created_by: str, via: str) -> dict:
    note_id = _new_note_id()
    rec = {"text": text, "created_by": created_by, "via": via, "created_at": _now()}
    self.db.collection(self.col).document(customer_id).collection("notes").document(note_id).set(rec)
    return {"id": note_id, "customer_id": customer_id, **rec}


def _gcp_list_notes(self, customer_id: str) -> list[dict]:
    col = self.db.collection(self.col).document(customer_id).collection("notes")
    return sorted(({"id": s.id, "customer_id": customer_id, **s.to_dict()} for s in col.stream()),
                  key=lambda n: n["created_at"])


LocalStore.add_note, LocalStore.list_notes = _local_add_note, _local_list_notes
GcpStore.add_note, GcpStore.list_notes = _gcp_add_note, _gcp_list_notes
