import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urlparse

import plotly.graph_objects as go

from bs4 import BeautifulSoup

import httpx
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

DB_PATH = Path(__file__).resolve().parent / "links.db"
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))

app = FastAPI(title="Link Checker")


class LinkHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.title: Optional[str] = None
        self.heading: Optional[str] = None
        self._in_title = False
        self._in_heading = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        if tag == "title":
            self._in_title = True
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._in_heading = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._in_heading = False

    def handle_data(self, data: str) -> None:
        if self._in_title and self.title is None:
            self.title = data.strip()
        elif self._in_heading and self.heading is None:
            self.heading = data.strip()


def extract_page_fields(html: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    parser = LinkHTMLParser()
    parser.feed(html)

    soup = BeautifulSoup(html, "html.parser")
    applicant_span = soup.find(
        "span",
        class_="num-applicants__caption topcard__flavor--metadata topcard__flavor--bullet",
    )
    # If the span is not found, try to find the figcaption element
    if applicant_span is None:
        applicant_span = soup.find('figcaption', class_='num-applicants__caption')

    n_applicants = applicant_span.get_text(strip=True) if applicant_span else None

    return parser.title, parser.heading, n_applicants


def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL UNIQUE,
            title TEXT,
            heading TEXT,
            next_poll_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS link_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            link_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            last_status_code INTEGER,
            last_checked TEXT,
            n_applicants TEXT,
            FOREIGN KEY(link_id) REFERENCES links(id) ON DELETE CASCADE
        )
        """
    )
    conn.commit()
    conn.close()


init_db()


def determine_health_status(status_code: int) -> str:
    return "healthy" if status_code == 200 else "unhealthy"


def extract_placeholder_fields(html: str) -> Tuple[Optional[str], Optional[str]]:
    title, heading, _ = extract_page_fields(html)
    return title, heading


def normalize_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme and parsed.netloc:
        return url.strip()
    return f"https://{url.strip()}"


def extract_numeric_applicant_count(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    match = re.search(r"(\d+)", value)
    if not match:
        return None
    return int(match.group(1))


def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    conn = get_db_connection()
    rows = conn.execute(
        """
        SELECT l.id, l.url, l.title, l.heading,
               s.status, s.last_status_code, s.last_checked, s.n_applicants
        FROM links l
        LEFT JOIN link_snapshots s ON s.link_id = l.id
        WHERE s.id = (SELECT MAX(id) FROM link_snapshots WHERE link_id = l.id)
        OR s.id IS NULL
        ORDER BY l.id DESC
        """
    ).fetchall()
    conn.close()
    return TEMPLATES.TemplateResponse(
        request,
        "index.html",
        {"links": [dict(row) for row in rows]},
    )


@app.post("/links")
async def create_link(url: str = Form(...)):
    cleaned_url = normalize_url(url)
    try:
        response = httpx.get(cleaned_url, timeout=10.0)
    except httpx.HTTPError:
        status_code = 0
        status = "unhealthy"
        title = None
        heading = None
        n_applicants = None
    else:
        status_code = response.status_code
        status = determine_health_status(status_code)
        title = None
        heading = None
        n_applicants = None
        if response.is_success:
            title, heading, n_applicants = extract_page_fields(response.text)

    conn = get_db_connection()
    next_poll_value = (datetime.now() + timedelta(hours=12)).isoformat() if status == "healthy" else None
    cursor = conn.execute(
        "INSERT INTO links (url, title, heading, next_poll_at) VALUES (?, ?, ?, ?) ON CONFLICT(url) DO UPDATE SET title = excluded.title, heading = excluded.heading RETURNING id",
        (
            cleaned_url,
            title,
            heading,
            next_poll_value,
        ),
    )
    link_row = cursor.fetchone()
    link_id = link_row[0] if link_row else conn.execute("SELECT id FROM links WHERE url = ?", (cleaned_url,)).fetchone()[0]
    conn.execute(
        "INSERT INTO link_snapshots (link_id, status, last_status_code, last_checked, n_applicants) VALUES (?, ?, ?, ?, ?)",
        (
            link_id,
            status,
            status_code,
            datetime.now().isoformat(),
            n_applicants,
        ),
    )
    conn.commit()
    conn.close()
    return RedirectResponse(url="/", status_code=303)


@app.post("/links/{link_id}/delete")
async def delete_link(link_id: int):
    conn = get_db_connection()
    conn.execute("DELETE FROM links WHERE id = ?", (link_id,))
    conn.commit()
    conn.close()
    return RedirectResponse(url="/", status_code=303)


@app.get("/links/{link_id}/chart")
async def chart_link(link_id: int):
    conn = get_db_connection()
    rows = conn.execute(
        "SELECT last_checked, n_applicants FROM link_snapshots WHERE link_id = ? ORDER BY id ASC",
        (link_id,),
    ).fetchall()
    conn.close()

    timestamps = []
    values = []
    for row in rows:
        numeric_value = extract_numeric_applicant_count(row["n_applicants"])
        if numeric_value is None:
            continue
        timestamps.append(row["last_checked"])
        values.append(numeric_value)

    fig = go.Figure(data=go.Scatter(x=timestamps, y=values, mode="lines+markers", name="Applicants"))
    fig.update_layout(
        title="Applicant count history",
        xaxis_title="Checked at",
        yaxis_title="Applicants",
        template="plotly_white",
    )
    return HTMLResponse(fig.to_html(include_plotlyjs="cdn", full_html=True, default_width="100%", default_height="100%"))


def poll_links() -> None:
    while True:
        conn = get_db_connection()
        rows = conn.execute(
            """
            SELECT l.id, l.url
            FROM links l
            JOIN link_snapshots s ON s.link_id = l.id
            WHERE s.id = (SELECT MAX(id) FROM link_snapshots WHERE link_id = l.id)
              AND s.status = 'healthy'
              AND (l.next_poll_at IS NULL OR l.next_poll_at <= ?)
            """,
            (datetime.utcnow().isoformat(),),
        ).fetchall()
        conn.close()

        for row in rows:
            try:
                response = httpx.get(row["url"], timeout=10.0)
            except httpx.HTTPError:
                status_code = 0
                status = "unhealthy"
                title = None
                heading = None
                n_applicants = None
            else:
                status_code = response.status_code
                status = determine_health_status(status_code)
                title = None
                heading = None
                n_applicants = None
                if response.is_success:
                    title, heading, n_applicants = extract_page_fields(response.text)

            conn = get_db_connection()
            next_poll_value = (datetime.utcnow() + timedelta(hours=12)).isoformat() if status == "healthy" else None
            if status == "healthy":
                conn.execute(
                    "UPDATE links SET title = ?, heading = ?, next_poll_at = ? WHERE id = ?",
                    (
                        title,
                        heading,
                        next_poll_value,
                        row["id"],
                    ),
                )
            else:
                conn.execute(
                    "UPDATE links SET next_poll_at = ? WHERE id = ?",
                    (next_poll_value, row["id"]),
                )
            conn.execute(
                "INSERT INTO link_snapshots (link_id, status, last_status_code, last_checked, n_applicants) VALUES (?, ?, ?, ?, ?)",
                (
                    row["id"],
                    status,
                    status_code,
                    datetime.utcnow().isoformat(),
                    n_applicants,
                ),
            )
            conn.commit()
            conn.close()

        time.sleep(43200) # 


threading.Thread(target=poll_links, daemon=True).start()
