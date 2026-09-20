import pytest
from fastapi.testclient import TestClient

from recon import auth
from recon.web import app

PW = "correct-horse-battery"


@pytest.fixture()
def client(conn, monkeypatch):
    monkeypatch.setenv("RECON_PASSWORD", PW)
    monkeypatch.delenv("RECON_REQUIRE_AUTH", raising=False)
    return TestClient(app, follow_redirects=False)


def test_pages_redirect_to_login_when_signed_out(client):
    for path in ("/", "/findings", "/upload", "/rules", "/export.xlsx"):
        r = client.get(path)
        assert r.status_code == 303 and r.headers["location"].startswith("/login"), path
    assert client.post("/upload", files=[("files", ("x.csv", b"a,b\n1,2\n", "text/csv"))]).status_code == 303   # no upload without login
    assert client.post("/run").status_code == 303


def test_health_check_and_login_page_are_open(client):
    assert client.get("/healthz").json() == {"ok": True}
    assert client.get("/login").status_code == 200


def test_wrong_password_rejected_right_password_signs_in_and_out(client):
    assert client.post("/login", data={"password": "nope", "next": "/"}).status_code == 401
    assert client.get("/").status_code == 303
    r = client.post("/login", data={"password": PW, "next": "/findings"})
    assert r.status_code == 303 and r.headers["location"] == "/findings"
    assert client.get("/findings").status_code == 200
    client.post("/logout")
    assert client.get("/findings").status_code == 303


def test_login_never_redirects_off_site(client):
    for evil in ("https://evil.example", "//evil.example", "/\\evil.example"):
        r = client.post("/login", data={"password": PW, "next": evil})
        assert r.headers["location"] == "/", evil
        client.post("/logout")


def test_repeated_failures_lock_out_even_the_right_password(client, monkeypatch):
    async def instant(_):  # don't actually sleep in the test
        return None
    monkeypatch.setattr(auth.asyncio, "sleep", instant)
    for _ in range(auth.MAX_FAILS):
        assert client.post("/login", data={"password": "bad"}).status_code == 401
    assert client.post("/login", data={"password": PW}).status_code == 429


def test_no_password_configured_means_open_for_local_use(conn, monkeypatch):
    monkeypatch.delenv("RECON_PASSWORD", raising=False)
    assert TestClient(app).get("/").status_code == 200


def test_cloud_mode_refuses_to_start_without_credentials(monkeypatch):
    monkeypatch.setenv("RECON_REQUIRE_AUTH", "1")
    monkeypatch.setenv("RECON_PASSWORD", "")
    with pytest.raises(RuntimeError, match="RECON_PASSWORD"):
        auth.check_startup()
    monkeypatch.setenv("RECON_PASSWORD", "short")
    with pytest.raises(RuntimeError, match="RECON_PASSWORD"):
        auth.check_startup()
    monkeypatch.setenv("RECON_PASSWORD", PW)
    with pytest.raises(RuntimeError, match="RECON_SECRET"):
        auth.check_startup()
    monkeypatch.setenv("RECON_SECRET", "x" * 32)
    auth.check_startup()


def test_new_upload_endpoints_require_login(client):
    assert client.post("/upload/file", files=[("file", ("x.csv", b"a\n1\n", "text/csv"))]).status_code == 303
    assert client.post("/upload/finish").status_code == 303
