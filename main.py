"""Object Souls — Voice stories for physical objects.

FastAPI backend: REST API for stories + static file server for frontend.
"""

import os
import uuid
import json
import sqlite3
import logging
from datetime import datetime, timezone
from pathlib import Path

import qrcode
import qrcode.image.svg
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

# --- Setup ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("object-souls")

app = FastAPI(title="Object Souls")

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
RECORDINGS_DIR = BASE_DIR / "recordings"
STATIC_DIR = BASE_DIR / "static"

DATA_DIR.mkdir(exist_ok=True)
RECORDINGS_DIR.mkdir(exist_ok=True)
STATIC_DIR.mkdir(exist_ok=True)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/recordings", StaticFiles(directory=str(RECORDINGS_DIR)), name="recordings")


# --- Database ---
def get_db():
    conn = sqlite3.connect(str(DATA_DIR / "objects.db"))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS items (
            id TEXT PRIMARY KEY,
            qr_code_id TEXT UNIQUE NOT NULL,
            title TEXT,
            daangn_item_id TEXT,
            created_at TEXT NOT NULL,
            story_count INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS stories (
            id TEXT PRIMARY KEY,
            qr_code_id TEXT NOT NULL,
            voice_filename TEXT,
            created_at TEXT NOT NULL,
            email TEXT,
            FOREIGN KEY (qr_code_id) REFERENCES items(qr_code_id)
        );
        CREATE INDEX IF NOT EXISTS idx_stories_qr ON stories(qr_code_id);
        CREATE INDEX IF NOT EXISTS idx_items_qr ON items(qr_code_id);
    """)
    conn.commit()
    conn.close()


init_db()


# --- QR Helper ---
def generate_qr_svg(data_url: str) -> str:
    qr = qrcode.QRCode(border=2, box_size=10)
    qr.add_data(data_url)
    qr.make(fit=True)
    img = qr.make_image(image_factory=qrcode.image.svg.SvgPathImage)
    return img.to_string().decode("utf-8")


def render_html(template_name: str, **kwargs) -> str:
    """Simple HTML template rendering (no Jinja2 dependency)."""
    template_dir = BASE_DIR / "templates"
    path = template_dir / template_name
    if not path.exists():
        raise HTTPException(404, f"Template {template_name} not found")

    html = path.read_text(encoding="utf-8")

    for key, val in kwargs.items():
        placeholder = "{{ " + key + " }}"
        html = html.replace(placeholder, str(val) if val is not None else "")

    return html


# --- Routes ---

@app.get("/", response_class=HTMLResponse)
async def index():
    """Seller page: record a voice story and generate a QR code."""
    html = render_html("index.html")
    return HTMLResponse(html)


@app.get("/s/{qr_code_id}", response_class=HTMLResponse)
async def view_story(qr_code_id: str):
    """Landing page: hear stories and add your own."""
    conn = get_db()
    item = conn.execute(
        "SELECT * FROM items WHERE qr_code_id = ?", (qr_code_id,)
    ).fetchone()
    stories = conn.execute(
        "SELECT * FROM stories WHERE qr_code_id = ? ORDER BY created_at ASC",
        (qr_code_id,),
    ).fetchall()
    conn.close()

    items_json = json.dumps(dict(item) if item else None)
    stories_json = json.dumps([dict(s) for s in stories])

    html = render_html(
        "story.html",
        qr_code_id=qr_code_id,
        items_json=items_json,
        stories_json=stories_json,
    )
    return HTMLResponse(html)


@app.post("/api/items")
async def create_item(
    title: str = Form(""),
    daangn_item_id: str = Form(""),
    voice_file: UploadFile = File(...),
):
    """Create a new item with a voice story and return QR code."""
    qr_code_id = str(uuid.uuid4())
    item_id = str(uuid.uuid4())
    story_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    ext = (voice_file.filename or "recording.webm").split(".")[-1]
    filename = f"{story_id}.{ext}"
    filepath = RECORDINGS_DIR / filename

    content = await voice_file.read()
    if not content:
        raise HTTPException(400, "Empty voice file")
    filepath.write_bytes(content)

    conn = get_db()
    conn.execute(
        "INSERT INTO items (id, qr_code_id, title, daangn_item_id, created_at, story_count) "
        "VALUES (?, ?, ?, ?, ?, 1)",
        (item_id, qr_code_id, title, daangn_item_id, now),
    )
    conn.execute(
        "INSERT INTO stories (id, qr_code_id, voice_filename, created_at) VALUES (?, ?, ?, ?)",
        (story_id, qr_code_id, filename, now),
    )
    conn.commit()
    conn.close()

    story_url = f"/s/{qr_code_id}"
    qr_svg = generate_qr_svg(story_url)

    return JSONResponse({
        "item_id": item_id,
        "qr_code_id": qr_code_id,
        "story_url": story_url,
        "qr_svg": qr_svg,
        "story_count": 1,
    })


@app.post("/api/stories")
async def add_story(
    qr_code_id: str = Form(...),
    voice_file: UploadFile = File(...),
    email: str = Form(""),
):
    """Add a new story to an existing item."""
    conn = get_db()
    item = conn.execute(
        "SELECT * FROM items WHERE qr_code_id = ?", (qr_code_id,)
    ).fetchone()
    if not item:
        conn.close()
        raise HTTPException(404, "Item not found")

    story_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    ext = (voice_file.filename or "story.webm").split(".")[-1]
    filename = f"{story_id}.{ext}"
    filepath = RECORDINGS_DIR / filename
    content = await voice_file.read()
    filepath.write_bytes(content)

    conn.execute(
        "INSERT INTO stories (id, qr_code_id, voice_filename, created_at, email) VALUES (?, ?, ?, ?, ?)",
        (story_id, qr_code_id, filename, now, email),
    )
    conn.execute(
        "UPDATE items SET story_count = story_count + 1 WHERE qr_code_id = ?",
        (qr_code_id,),
    )
    conn.commit()
    conn.close()

    return JSONResponse({
        "story_id": story_id,
        "story_count": item["story_count"] + 1,
    })


@app.post("/api/qr/reconnect")
async def reconnect_qr(
    old_qr_code_id: str = Form(""),
    daangn_item_id: str = Form(""),
):
    """Try to reconnect a lost QR to an existing item."""
    conn = get_db()

    if old_qr_code_id:
        item = conn.execute(
            "SELECT * FROM items WHERE qr_code_id = ?", (old_qr_code_id,)
        ).fetchone()
        if item:
            conn.close()
            return JSONResponse({
                "found": True,
                "qr_code_id": old_qr_code_id,
                "story_count": item["story_count"],
                "story_url": f"/s/{old_qr_code_id}",
            })

    if daangn_item_id:
        item = conn.execute(
            "SELECT * FROM items WHERE daangn_item_id = ?", (daangn_item_id,)
        ).fetchone()
        if item:
            conn.close()
            return JSONResponse({
                "found": True,
                "qr_code_id": item["qr_code_id"],
                "story_count": item["story_count"],
                "story_url": f"/s/{item['qr_code_id']}",
            })

    conn.close()
    return JSONResponse({"found": False})


# --- Run ---
if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
