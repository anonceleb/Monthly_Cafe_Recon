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


# ---- one-file-at-a-time upload flow: checklist of loaded vs pending -------------------------------------------------
from pathlib import Path

import pytest

APRIL = Path("/Users/ashwin/Streak/L'Amour/Kredo /April 2026")
NEEDS_APRIL = pytest.mark.skipif(not APRIL.exists(), reason="April 2026 files not present")


def send(c, path):
    return c.post("/upload/file", files=[("file", (path.name, path.read_bytes(), "application/octet-stream"))])


def test_garbage_file_is_reported_and_everything_is_still_pending(conn):
    j = TestClient(app).post("/upload/file", files=[("file", ("junk.csv", b"a,b\n1,2\n", "text/csv"))]).json()
    assert j["result"]["status"] == "rejected" and j["loaded_required"] == 0 and j["required"] == 6
    assert len(j["pending"]) == 6 and "ICICI bank statement" in j["pending"]
    assert "0 of 6 required files loaded" in j["checklist_html"]


@NEEDS_APRIL
def test_checklist_moves_from_pending_to_loaded_as_files_arrive(conn):
    c = TestClient(app)
    j = send(c, APRIL / "Bank statements/2026-03-16 to 2026-05-15 Cafe ICICI Statement.xlsx").json()
    assert j["result"]["status"] == "loaded" and j["result"]["label"] == "ICICI bank statement"
    assert j["loaded_required"] == 1 and "ICICI bank statement" not in j["pending"] and "Zomato dining payout" in j["pending"]
    assert "16 Mar 2026" in j["checklist_html"]
    assert send(c, APRIL / "Bank statements/2026-03-16 to 2026-05-15 Cafe ICICI Statement.xlsx").json()["result"]["status"] == "duplicate"   # same file again: no double count
    assert send(c, APRIL / "Petpooja/Supplier_Report_2026_09_14_06_15_49.xlsx").json()["loaded_required"] == 1                                 # ignored file changes nothing


@NEEDS_APRIL
def test_finish_runs_rules_once_and_reports_what_is_still_missing(conn):
    c = TestClient(app)
    send(c, APRIL / "Petpooja/Order_Summary_Item_Report_152749_2026-04-01_2026-04-30.csv")
    j = c.post("/upload/finish").json()
    assert j["run_id"] and "Zomato delivery settlement" in j["pending"] and j["gap"] >= 0
    assert conn.execute("select count(*) n from run").fetchone()["n"] == 1


@NEEDS_APRIL
def test_overview_warns_when_files_arrive_after_the_last_run(conn):
    c = TestClient(app)
    send(c, APRIL / "Petpooja/Order_Summary_Item_Report_152749_2026-04-01_2026-04-30.csv")
    c.post("/upload/finish")
    assert "New files were loaded after the last run" not in c.get("/").text
    send(c, APRIL / "Petpooja/Sales_Report_Online_Platforms_2026_09_18_15_51_09.xlsx")
    page = c.get("/").text
    assert "New files were loaded after the last run" in page and "required files still missing" in page
    c.post("/upload/finish")
    assert "New files were loaded after the last run" not in c.get("/").text
