from fastapi.testclient import TestClient

from recon.web import app


def test_pages_render_on_an_empty_database(conn):
    c = TestClient(app)
    for path in ("/", "/findings", "/upload", "/rules"):
        assert c.get(path).status_code == 200, path


def test_upload_rejects_garbage_without_crashing(conn):
    c = TestClient(app)
    r = c.post("/upload", files=[("files", ("junk.csv", b"a,b\n1,2\n", "text/csv"))])
    assert r.status_code == 200 and "rejected" in r.text
