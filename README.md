# Ofek shared document API (module 6, exercise 1)

One Cloud Run service, two interfaces: the office dashboard and an n8n sales form both call the same `/api/*` operations.

| | URL |
|---|---|
| Dashboard | https://shared-docs-api-799772464889.me-west1.run.app/dashboard |
| Swagger UI | https://shared-docs-api-799772464889.me-west1.run.app/docs |
| OpenAPI | https://shared-docs-api-799772464889.me-west1.run.app/openapi.json |
| n8n form (workflow `qiZci0PK5QsIaHk1`) | https://eladgk8.app.n8n.cloud/form/ofek-shared-api-test |

## API
- `GET /api/customers` – listCustomers
- `GET /api/customers/{customer_id}/documents` – listCustomerDocuments
- `POST /api/documents` – uploadDocument (multipart: `customer_id`, `file` = PDF, optional `source` = office|field)
- `GET /api/customers/{customer_id}/documents/{document_id}/file` – getDocumentFile (opens the source PDF)

Validation lives in the service, so it applies to every interface: unknown customer -> 404, non-PDF bytes (even renamed to .pdf) -> 415, empty -> 400, >10 MB -> 413. Nothing is stored on rejection.

Customers: Dana `cus-7f3a9c`, Amit `cus-b2e41d` (routing is by ID, never by name).

## Storage
- Files: `gs://ai-gcp-course-test-shared-docs/customers/{customer_id}/{doc_id}.pdf` (private bucket)
- Records: Firestore `(default)` DB, `ofek_customers/{customer_id}/documents/{doc_id}` (file name, size, source, uploaded_at, file_ref)
- Runs as service account `shared-docs-api@ai-gcp-course-test.iam.gserviceaccount.com` (objectAdmin on the bucket, datastore.user)

## Run locally (temporary disk storage)
```
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt
.venv/Scripts/python -m uvicorn main:app --port 8091
```
Tests: `.venv/Scripts/pip install pytest httpx && .venv/Scripts/python -m pytest -q tests`

## Deploy
```
gcloud run deploy shared-docs-api --source . --project ai-gcp-course-test --region me-west1 \
  --service-account shared-docs-api@ai-gcp-course-test.iam.gserviceaccount.com --allow-unauthenticated \
  --set-env-vars STORE_BACKEND=gcp,GOOGLE_CLOUD_PROJECT=ai-gcp-course-test,GCS_BUCKET=ai-gcp-course-test-shared-docs,FIRESTORE_COLLECTION=ofek_customers
```

## Real test results (2026-11-23, against the cloud)
- listCustomers: Dana + Amit, both 0 docs initially.
- Upload to `cus-7f3a9d` (Dana's ID, one char changed) -> 404, nothing stored. Word bytes as .pdf -> 415.
- Quote A uploaded to Dana with source=office -> 201 (sent directly to the API, same call the dashboard makes).
- n8n: listCustomers and listCustomerDocuments run from the workflow -> 200; Quote B submitted through the live n8n form -> 201, source=field, form page shows "Status: 201".
- Dana: 2 docs in the API and on the dashboard; both downloads are byte-identical to the originals (SHA-1 match). Amit: empty.

## Not tested
- Uploading through the dashboard UI itself (file picker) and opening the files by clicking in a browser.
- The n8n form *test URL* flow (Execute Workflow in the editor); tested via the published production URL instead.
- Bonus (PDF thumbnails) not built.
