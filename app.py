"""
PharmaFind backend — Flask + SQLite.

Implements the real logic the demo frontend simulates in-browser:
  - pharmacy registration / login / logout (hashed passwords, server sessions)
  - authenticated inventory management (create/update/delete stock rows)
  - natural-language medicine search across every pharmacy, cheapest first

Run locally:
    pip install -r requirements.txt
    python app.py
    # -> http://localhost:5000

Run in production (behind a reverse proxy that terminates HTTPS):
    gunicorn -w 4 -b 0.0.0.0:5000 app:app

See README.md for the full path-to-production checklist (env vars,
HTTPS/headers, moving off SQLite, rate limiting, CAPTCHA, etc).
"""

import os
import re
import sqlite3
from datetime import datetime, timezone
from difflib import SequenceMatcher

from flask import Flask, g, jsonify, request, session
from werkzeug.security import check_password_hash, generate_password_hash

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("PHARMAFIND_DB_PATH", os.path.join(BASE_DIR, "pharmafind.db"))

# Comma-separated list of origins allowed to call this API with credentials.
# Set this to your real frontend's origin(s) in production, e.g.
#   ALLOWED_ORIGINS="https://www.pharmafind.example"
ALLOWED_ORIGINS = [
    o.strip()
    for o in os.environ.get("ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",")
    if o.strip()
]

# Set FORCE_HTTPS=1 once this app sits behind a proxy that sets
# X-Forwarded-Proto (nearly every host/PaaS/reverse proxy does this).
FORCE_HTTPS = os.environ.get("FORCE_HTTPS") == "1"

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-secret-change-me")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=FORCE_HTTPS,
)

if app.secret_key == "dev-only-secret-change-me" and not app.debug:
    # Loud on purpose: a default secret key means anyone can forge sessions.
    print("WARNING: set a real SECRET_KEY environment variable before deploying.")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
HONEYPOT_FIELD = "hp_token"  # must match the hidden field name in the frontend form

# --------------------------------------------------------------------------
# Database helpers
# --------------------------------------------------------------------------


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
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
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            area TEXT,
            phone TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS medications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            generic_name TEXT NOT NULL,
            brand_names TEXT DEFAULT '',
            category TEXT DEFAULT '',
            description TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS inventory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pharmacy_id INTEGER NOT NULL REFERENCES pharmacies(id) ON DELETE CASCADE,
            medication_id INTEGER NOT NULL REFERENCES medications(id) ON DELETE CASCADE,
            price REAL NOT NULL,
            stock_qty INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            UNIQUE(pharmacy_id, medication_id)
        );
        """
    )
    db.commit()
    db.close()


# Reference catalog of medicines. This is not user data — it seeds once so
# /api/search and /api/medications have something real to match against.
DEMO_MEDICATIONS = [
    ("Paracetamol", "Panadol,Tylenol,Calpol", "pain,fever", "Pain reliever and fever reducer"),
    ("Ibuprofen", "Advil,Nurofen,Brufen", "pain,inflammation,fever", "Anti-inflammatory pain reliever"),
    ("Aspirin", "Disprin", "pain,fever", "Pain reliever, also used for heart health at low doses"),
    ("Amoxicillin", "Amoxil", "antibiotic", "Penicillin-type antibiotic"),
    ("Doxycycline", "Vibramycin", "antibiotic", "Broad-spectrum antibiotic"),
    ("Ciprofloxacin", "Cipro", "antibiotic", "Antibiotic for bacterial infections"),
    ("Loratadine", "Claritin", "allergy", "Non-drowsy antihistamine"),
    ("Cetirizine", "Zyrtec", "allergy", "Antihistamine for allergies"),
    ("Omeprazole", "Losec,Prilosec", "digestive", "Reduces stomach acid"),
    ("Antacid Tablets", "Gaviscon,Rennie", "digestive", "Relieves heartburn and indigestion"),
    ("Oral Rehydration Salts", "ORS", "digestive", "Rehydration for diarrhea or vomiting"),
    ("Metformin", "Glucophage", "diabetes", "Manages type 2 diabetes"),
    ("Insulin (Regular)", "Actrapid", "diabetes", "Fast-acting insulin"),
    ("Amlodipine", "Norvasc", "hypertension", "Blood pressure medication"),
    ("Atorvastatin", "Lipitor", "cholesterol", "Lowers cholesterol"),
    ("Salbutamol Inhaler", "Ventolin", "respiratory,asthma", "Reliever inhaler for asthma"),
    ("Artemether/Lumefantrine", "Coartem", "antimalarial", "Malaria treatment"),
    ("Diclofenac Gel", "Voltaren", "pain,inflammation,skin", "Topical anti-inflammatory gel"),
    ("Hydrocortisone Cream", "Cortizone", "skin", "Relieves itching and skin irritation"),
    ("Cough Syrup", "Benylin", "cough", "Relieves cough symptoms"),
    ("Throat Lozenges", "Strepsils", "cough", "Soothes a sore throat"),
    ("Multivitamin Tablets", "Centrum", "supplement", "Daily vitamin and mineral supplement"),
    ("Vitamin C", "Redoxon", "supplement", "Immune support supplement"),
    ("Folic Acid", "", "prenatal,supplement", "Supports a healthy pregnancy"),
]


# Lets "headache" find paracetamol/ibuprofen even though neither medicine
# name contains the word "headache". Keys are matched as substrings of the
# query; values are the medication categories they should pull in.
SYMPTOM_MAP = {
    "headache": ["pain"], "migraine": ["pain"], "ache": ["pain"], "pain": ["pain"],
    "fever": ["fever"], "temperature": ["fever"],
    "allergy": ["allergy"], "allergies": ["allergy"], "sneezing": ["allergy"],
    "itchy": ["allergy", "skin"], "itching": ["allergy", "skin"], "rash": ["skin"],
    "cough": ["cough"], "sore throat": ["cough"], "cold": ["cough", "fever"],
    "flu": ["fever", "pain", "cough"], "runny nose": ["allergy", "cough"],
    "stomach": ["digestive"], "diarrhea": ["digestive"], "diarrhoea": ["digestive"],
    "indigestion": ["digestive"], "heartburn": ["digestive"], "nausea": ["digestive"],
    "vomiting": ["digestive"], "asthma": ["respiratory"], "breathing": ["respiratory"],
    "wheezing": ["respiratory"], "diabetes": ["diabetes"], "blood sugar": ["diabetes"],
    "blood pressure": ["hypertension"], "hypertension": ["hypertension"],
    "cholesterol": ["cholesterol"], "malaria": ["antimalarial"], "infection": ["antibiotic"],
    "pregnant": ["prenatal"], "pregnancy": ["prenatal"], "vitamin": ["supplement"],
    "immune": ["supplement"],
}

ALL_CATEGORY_KEYS = sorted({c for cats in SYMPTOM_MAP.values() for c in cats})


def ai_suggest_categories(query):
    """Optional: ask an LLM which categories a free-text query implies, for
    descriptions SYMPTOM_MAP doesn't cover. No-ops (returns None) unless the
    `anthropic` package is installed AND ANTHROPIC_API_KEY is set, so the
    core search endpoint never depends on this. See README to enable it."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        import anthropic  # imported lazily so this stays fully optional
    except ImportError:
        return None

    prompt = (
        "A pharmacy customer typed this search, which matched no medicine name "
        f'directly:\n"{query}"\n\n'
        f"Which of these categories are most likely relevant: {', '.join(ALL_CATEGORY_KEYS)}?\n"
        'Reply with ONLY a JSON array of up to 3 category keys from that exact list, '
        'most relevant first, e.g. ["allergy","respiratory"]. If nothing plausibly '
        "matches, reply with []."
    )
    try:
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=50,
            messages=[{"role": "user", "content": prompt}],
        )
        import json

        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        categories = json.loads(text.strip())
        if isinstance(categories, list):
            return [c for c in categories if c in ALL_CATEGORY_KEYS]
    except Exception:
        return None  # never let a flaky AI call break search
    return None


def seed_medications():
    db = sqlite3.connect(DB_PATH)
    count = db.execute("SELECT COUNT(*) FROM medications").fetchone()[0]
    if count == 0:
        db.executemany(
            "INSERT INTO medications (generic_name, brand_names, category, description) VALUES (?, ?, ?, ?)",
            DEMO_MEDICATIONS,
        )
        db.commit()
    db.close()


init_db()
seed_medications()

# --------------------------------------------------------------------------
# CORS + security headers (no third-party deps, so this always installs)
# --------------------------------------------------------------------------


@app.before_request
def handle_preflight_and_https():
    if request.method == "OPTIONS":
        return ("", 204)
    if FORCE_HTTPS:
        proto = request.headers.get("X-Forwarded-Proto", request.scheme)
        if proto != "https":
            https_url = request.url.replace("http://", "https://", 1)
            return jsonify({"error": "HTTPS required", "location": https_url}), 400


@app.after_request
def add_headers(resp):
    origin = request.headers.get("Origin")
    if origin in ALLOWED_ORIGINS:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Access-Control-Allow-Credentials"] = "true"
        resp.headers["Vary"] = "Origin"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"

    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    resp.headers["Permissions-Policy"] = "geolocation=(), camera=(), microphone=()"
    if FORCE_HTTPS:
        resp.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    return resp


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------


def current_pharmacy_id():
    return session.get("pharmacy_id")


def require_login():
    pid = current_pharmacy_id()
    if not pid:
        return None, (jsonify({"error": "Not authenticated."}), 401)
    return pid, None


@app.route("/api/pharmacies/register", methods=["POST"])
def register():
    data = request.get_json(silent=True) or {}

    # Honeypot: bots fill every field, including this hidden one. Humans
    # never see it (it's visually hidden in the frontend). Pretend success
    # so we don't tip off the bot that it was caught.
    if data.get(HONEYPOT_FIELD):
        return jsonify({"success": True}), 201

    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    area = (data.get("area") or "").strip()
    phone = (data.get("phone") or "").strip()

    if len(name) < 2:
        return jsonify({"error": "Pharmacy name is required."}), 400
    if not EMAIL_RE.match(email):
        return jsonify({"error": "A valid email is required."}), 400
    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters."}), 400

    db = get_db()
    try:
        db.execute(
            """INSERT INTO pharmacies (name, email, password_hash, area, phone, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (name, email, generate_password_hash(password), area, phone, datetime.now(timezone.utc).isoformat()),
        )
        db.commit()
    except sqlite3.IntegrityError:
        return jsonify({"error": "An account with that email already exists."}), 409

    return jsonify({"success": True}), 201


@app.route("/api/pharmacies/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    db = get_db()
    row = db.execute("SELECT * FROM pharmacies WHERE email = ?", (email,)).fetchone()
    if not row or not check_password_hash(row["password_hash"], password):
        return jsonify({"error": "Invalid email or password."}), 401

    session.clear()
    session["pharmacy_id"] = row["id"]
    return jsonify({"success": True, "pharmacy": {"id": row["id"], "name": row["name"]}})


@app.route("/api/pharmacies/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"success": True})


@app.route("/api/pharmacies/me", methods=["GET"])
def me():
    pid = current_pharmacy_id()
    if not pid:
        return jsonify({"authenticated": False})
    db = get_db()
    row = db.execute("SELECT id, name, email, area, phone FROM pharmacies WHERE id = ?", (pid,)).fetchone()
    if not row:
        session.clear()
        return jsonify({"authenticated": False})
    return jsonify({"authenticated": True, "pharmacy": dict(row)})


# --------------------------------------------------------------------------
# Inventory (auth required)
# --------------------------------------------------------------------------


@app.route("/api/inventory", methods=["GET"])
def get_inventory():
    pid, err = require_login()
    if err:
        return err
    db = get_db()
    rows = db.execute(
        """SELECT i.id, i.price, i.stock_qty, i.updated_at,
                  m.id AS medication_id, m.generic_name, m.brand_names
           FROM inventory i JOIN medications m ON m.id = i.medication_id
           WHERE i.pharmacy_id = ?
           ORDER BY m.generic_name ASC""",
        (pid,),
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/inventory", methods=["POST"])
def upsert_inventory():
    pid, err = require_login()
    if err:
        return err
    data = request.get_json(silent=True) or {}

    try:
        medication_id = int(data.get("medication_id"))
        price = float(data.get("price"))
        stock_qty = int(data.get("stock_qty"))
    except (TypeError, ValueError):
        return jsonify({"error": "medication_id, price, and stock_qty must be valid numbers."}), 400

    if price < 0 or stock_qty < 0:
        return jsonify({"error": "Price and stock cannot be negative."}), 400

    db = get_db()
    med = db.execute("SELECT id FROM medications WHERE id = ?", (medication_id,)).fetchone()
    if not med:
        return jsonify({"error": "Unknown medication_id."}), 400

    db.execute(
        """INSERT INTO inventory (pharmacy_id, medication_id, price, stock_qty, updated_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(pharmacy_id, medication_id)
           DO UPDATE SET price = excluded.price, stock_qty = excluded.stock_qty, updated_at = excluded.updated_at""",
        (pid, medication_id, price, stock_qty, datetime.now(timezone.utc).isoformat()),
    )
    db.commit()
    return jsonify({"success": True})


@app.route("/api/inventory/<int:item_id>", methods=["DELETE"])
def delete_inventory(item_id):
    pid, err = require_login()
    if err:
        return err
    db = get_db()
    db.execute("DELETE FROM inventory WHERE id = ? AND pharmacy_id = ?", (item_id, pid))
    db.commit()
    return jsonify({"success": True})


# --------------------------------------------------------------------------
# Public catalog + search
# --------------------------------------------------------------------------


@app.route("/api/medications", methods=["GET"])
def list_medications():
    db = get_db()
    rows = db.execute("SELECT * FROM medications ORDER BY generic_name ASC").fetchall()
    return jsonify([dict(r) for r in rows])


def _text_similarity(a, b):
    return SequenceMatcher(None, a, b).ratio()


@app.route("/api/search", methods=["GET"])
def search():
    q = (request.args.get("q") or "").strip().lower()
    if not q:
        return jsonify({"error": "Provide a search query with ?q="}), 400

    db = get_db()
    meds = db.execute("SELECT * FROM medications").fetchall()

    def categories_of(m):
        return [c.strip().lower() for c in (m["category"] or "").split(",") if c.strip()]

    matched_ids = []
    for m in meds:
        names = [m["generic_name"].lower()] + [
            b.strip().lower() for b in (m["brand_names"] or "").split(",") if b.strip()
        ]
        cats = categories_of(m)

        is_match = any(q in n or n in q for n in names) or any(q == c or q in c for c in cats)
        if not is_match:
            is_match = any(_text_similarity(q, n) > 0.75 for n in names)
        if not is_match:
            # Symptom words ("headache") map to categories ("pain") even
            # though the word never appears in a medicine's own name.
            for keyword, symptom_cats in SYMPTOM_MAP.items():
                if keyword in q and any(c in symptom_cats for c in cats):
                    is_match = True
                    break

        if is_match:
            matched_ids.append(m["id"])

    ai_assisted = False
    if not matched_ids:
        # Nothing matched a name, brand, category, or known symptom word.
        # Try the optional LLM fallback for free-text descriptions
        # (no-ops instantly if it isn't configured — see ai_suggest_categories).
        suggested_cats = ai_suggest_categories(q)
        if suggested_cats:
            ai_assisted = True
            for m in meds:
                if any(c in suggested_cats for c in categories_of(m)):
                    matched_ids.append(m["id"])

    if not matched_ids:
        return jsonify({"query": q, "results": [], "ai_assisted": False})

    placeholders = ",".join("?" * len(matched_ids))
    rows = db.execute(
        f"""SELECT p.name AS pharmacy_name, p.area, p.phone,
                   i.price, i.stock_qty,
                   m.generic_name, m.brand_names
            FROM inventory i
            JOIN pharmacies p ON p.id = i.pharmacy_id
            JOIN medications m ON m.id = i.medication_id
            WHERE i.medication_id IN ({placeholders})
            ORDER BY i.price ASC""",
        matched_ids,
    ).fetchall()

    return jsonify({"query": q, "results": [dict(r) for r in rows], "ai_assisted": ai_assisted})


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.errorhandler(404)
def not_found(_e):
    return jsonify({"error": "Not found"}), 404


@app.errorhandler(500)
def server_error(_e):
    return jsonify({"error": "Something went wrong on our end."}), 500


if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1", port=int(os.environ.get("PORT", 5000)))
