"""
PharmaFind — local medication availability & price comparison platform.

Architecture:
- Flask serves the public search page and pharmacy dashboards.
- SQLite stores pharmacies, the shared medication catalog, and each
  pharmacy's inventory (price + stock status) for that catalog.
- A free-text search query is interpreted by the Anthropic API, which
  maps loose/natural language ("something for allergies", a misspelled
  brand name, a generic name) onto real entries in the medication
  catalog. Results are then pulled straight from the database and
  sorted by price — the AI never invents prices or stock, it only
  identifies which catalog entries match the query.
"""

import os
import sqlite3
import json
from datetime import datetime
from functools import wraps

import requests
from flask import Flask, g, render_template, request, jsonify, session, redirect, url_for, flash
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv

load_dotenv()

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(APP_DIR, "pharmafind.db")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = "claude-sonnet-4-6"

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS pharmacies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            address TEXT,
            phone TEXT
        );

        CREATE TABLE IF NOT EXISTS medications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            generic_name TEXT,
            UNIQUE(name)
        );

        CREATE TABLE IF NOT EXISTS inventory (
            pharmacy_id INTEGER NOT NULL REFERENCES pharmacies(id) ON DELETE CASCADE,
            medication_id INTEGER NOT NULL REFERENCES medications(id) ON DELETE CASCADE,
            price REAL NOT NULL DEFAULT 0,
            in_stock INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT,
            PRIMARY KEY (pharmacy_id, medication_id)
        );
        """
    )
    db.commit()
    db.close()


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("pharmacy_id"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


# ---------------------------------------------------------------------------
# AI-assisted search
# ---------------------------------------------------------------------------

def ai_match_medications(query, catalog):
    """
    Ask the Anthropic API to map a free-text query onto entries in the
    medication catalog. `catalog` is a list of {id, name, generic_name}.
    Returns a list of matched medication ids (possibly empty).

    Falls back to simple substring matching if no API key is configured
    or the API call fails, so the app still works without a key.
    """
    if not ANTHROPIC_API_KEY:
        return _fallback_match(query, catalog)

    catalog_lines = "\n".join(
        f"{item['id']}: {item['name']}" + (f" (generic: {item['generic_name']})" if item["generic_name"] else "")
        for item in catalog
    )

    system_prompt = (
        "You match a customer's medication search to entries in a pharmacy catalog. "
        "You are given a numbered catalog and a free-text query, which may be a brand "
        "name, generic name, misspelling, or a description of a symptom or condition. "
        "Return ONLY a JSON array of the matching catalog ids, e.g. [3, 7]. "
        "If nothing plausibly matches, return []. Do not include any other text. "
        "Never suggest dosing, safety, or medical advice — only identity matching."
    )
    user_prompt = f"Catalog:\n{catalog_lines}\n\nCustomer query: {query}\n\nMatching ids (JSON array only):"

    try:
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": ANTHROPIC_MODEL,
                "max_tokens": 200,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_prompt}],
            },
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
        text = "".join(
            block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
        ).strip()
        text = text.strip("`").strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()
        ids = json.loads(text)
        return [int(i) for i in ids if isinstance(i, (int, float))]
    except Exception:
        return _fallback_match(query, catalog)


def _fallback_match(query, catalog):
    q = query.strip().lower()
    if not q:
        return []
    matches = []
    for item in catalog:
        name = (item["name"] or "").lower()
        generic = (item["generic_name"] or "").lower()
        if q in name or q in generic or name in q or (generic and generic in q):
            matches.append(item["id"])
    return matches


# ---------------------------------------------------------------------------
# Public routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/search")
def api_search():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"query": query, "results": []})

    db = get_db()
    catalog = [dict(row) for row in db.execute("SELECT id, name, generic_name FROM medications")]

    matched_ids = ai_match_medications(query, catalog)
    if not matched_ids:
        return jsonify({"query": query, "results": [], "medication": None})

    placeholders = ",".join("?" for _ in matched_ids)
    rows = db.execute(
        f"""
        SELECT m.id AS medication_id, m.name AS medication_name, m.generic_name,
               p.id AS pharmacy_id, p.name AS pharmacy_name, p.address, p.phone,
               i.price, i.in_stock
        FROM inventory i
        JOIN medications m ON m.id = i.medication_id
        JOIN pharmacies p ON p.id = i.pharmacy_id
        WHERE m.id IN ({placeholders}) AND i.in_stock = 1
        ORDER BY m.name, i.price ASC
        """,
        matched_ids,
    ).fetchall()

    results = [dict(row) for row in rows]
    return jsonify({"query": query, "results": results})


# ---------------------------------------------------------------------------
# Pharmacy auth + dashboard
# ---------------------------------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        db = get_db()
        pharmacy = db.execute("SELECT * FROM pharmacies WHERE username = ?", (username,)).fetchone()
        if pharmacy and check_password_hash(pharmacy["password_hash"], password):
            session["pharmacy_id"] = pharmacy["id"]
            session["pharmacy_name"] = pharmacy["name"]
            return redirect(url_for("dashboard"))
        flash("Incorrect username or password.")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.route("/dashboard")
@login_required
def dashboard():
    db = get_db()
    pharmacy_id = session["pharmacy_id"]

    inventory = db.execute(
        """
        SELECT m.id AS medication_id, m.name, m.generic_name,
               i.price, i.in_stock
        FROM medications m
        LEFT JOIN inventory i ON i.medication_id = m.id AND i.pharmacy_id = ?
        ORDER BY m.name
        """,
        (pharmacy_id,),
    ).fetchall()

    return render_template("dashboard.html", inventory=inventory, pharmacy_name=session["pharmacy_name"])


@app.route("/dashboard/update", methods=["POST"])
@login_required
def dashboard_update():
    pharmacy_id = session["pharmacy_id"]
    medication_id = request.form.get("medication_id", type=int)
    price = request.form.get("price", type=float)
    in_stock = 1 if request.form.get("in_stock") == "on" else 0
    now = datetime.utcnow().isoformat()

    db = get_db()
    db.execute(
        """
        INSERT INTO inventory (pharmacy_id, medication_id, price, in_stock, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(pharmacy_id, medication_id)
        DO UPDATE SET price = excluded.price, in_stock = excluded.in_stock, updated_at = excluded.updated_at
        """,
        (pharmacy_id, medication_id, price or 0, in_stock, now),
    )
    db.commit()
    flash("Inventory updated.")
    return redirect(url_for("dashboard"))


@app.route("/dashboard/add-medication", methods=["POST"])
@login_required
def add_medication():
    name = request.form.get("name", "").strip()
    generic_name = request.form.get("generic_name", "").strip() or None
    price = request.form.get("price", type=float) or 0
    in_stock = 1 if request.form.get("in_stock") == "on" else 0

    if not name:
        flash("Medication name is required.")
        return redirect(url_for("dashboard"))

    db = get_db()
    existing = db.execute("SELECT id FROM medications WHERE name = ?", (name,)).fetchone()
    if existing:
        medication_id = existing["id"]
    else:
        cur = db.execute("INSERT INTO medications (name, generic_name) VALUES (?, ?)", (name, generic_name))
        medication_id = cur.lastrowid

    now = datetime.utcnow().isoformat()
    db.execute(
        """
        INSERT INTO inventory (pharmacy_id, medication_id, price, in_stock, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(pharmacy_id, medication_id)
        DO UPDATE SET price = excluded.price, in_stock = excluded.in_stock, updated_at = excluded.updated_at
        """,
        (session["pharmacy_id"], medication_id, price, in_stock, now),
    )
    db.commit()
    flash(f"Added {name}.")
    return redirect(url_for("dashboard"))


if __name__ == "__main__":
    if not os.path.exists(DB_PATH):
        init_db()
    app.run(debug=True, port=5000)
