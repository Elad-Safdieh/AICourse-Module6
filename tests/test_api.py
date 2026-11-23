import os, importlib
os.environ["STORE_BACKEND"] = "local"
os.environ["REQUIRE_LOGIN_FOR_FILES"] = "0"
def client(tmp_path):
    os.environ["LOCAL_DATA_DIR"] = str(tmp_path)
    import main; importlib.reload(main)
    from fastapi.testclient import TestClient
    return TestClient(main.app)
PDF = b"%PDF-1.4\n%test\n"
def test_flow(tmp_path):
    c = client(tmp_path)
    cs = c.get("/api/customers").json()
    assert {x["name"] for x in cs} == {"דנה", "עמית"} and all(x["document_count"] == 0 for x in cs)
    dana = "cus-7f3a9c"
    r = c.post("/api/documents", data={"customer_id": dana, "source": "field"}, files={"file": ("quote-b.pdf", PDF, "application/pdf")})
    assert r.status_code == 201, r.text
    docs = c.get(f"/api/customers/{dana}/documents").json()["documents"]
    assert len(docs) == 1 and c.get(docs[0]["file_url"]).content == PDF
    assert c.get("/api/customers/cus-b2e41d/documents").json()["documents"] == []
def test_rejects(tmp_path):
    c = client(tmp_path)
    assert c.post("/api/documents", data={"customer_id": "cus-7f3a9d"}, files={"file": ("a.pdf", PDF, "application/pdf")}).status_code == 404
    assert c.post("/api/documents", data={"customer_id": "cus-7f3a9c"}, files={"file": ("a.pdf", b"PK\x03\x04word", "application/pdf")}).status_code == 415
    assert c.get("/api/customers/cus-7f3a9c/documents").json()["documents"] == []
def test_docs(tmp_path):
    c = client(tmp_path)
    spec = c.get("/openapi.json").json()
    assert {"listCustomers","listCustomerDocuments","uploadDocument"} <= {op["operationId"] for p in spec["paths"].values() for op in p.values()}
    assert c.get("/docs").status_code == 200 and c.get("/dashboard").status_code == 200
