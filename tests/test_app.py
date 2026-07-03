import os
import sqlite3
import sys

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import main
from app.main import app, determine_health_status, extract_page_fields, extract_placeholder_fields


def test_determine_health_status_marks_200_as_healthy():
    assert determine_health_status(200) == "healthy"


def test_determine_health_status_marks_non_200_as_unhealthy():
    assert determine_health_status(404) == "unhealthy"


def test_extract_placeholder_fields_reads_title_and_heading():
    html = "<html><head><title>Example</title></head><body><h1>Welcome</h1></body></html>"
    assert extract_placeholder_fields(html) == ("Example", "Welcome")


def test_extract_page_fields_reads_applicant_count():
    html = "<html><head><title>Example</title></head><body><h1>Welcome</h1><span class='num-applicants__caption topcard__flavor--metadata topcard__flavor--bullet'>12 applicants</span></body></html>"
    assert extract_page_fields(html) == ("Example", "Welcome", "12 applicants")


def test_init_db_creates_link_and_snapshot_tables():
    main.init_db()
    conn = sqlite3.connect(main.DB_PATH)
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('links', 'link_snapshots')")
    tables = {row[0] for row in cursor.fetchall()}
    conn.close()
    assert {"links", "link_snapshots"}.issubset(tables)


def test_index_route_renders_successfully():
    client = TestClient(app)
    response = client.get("/")
    assert response.status_code == 200
