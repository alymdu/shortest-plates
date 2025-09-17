# app.py
import os
import json
import time
import threading
from datetime import datetime
from typing import List, Optional, Dict, Any

import requests
from flask import Flask, jsonify, render_template_string, request, abort

# ----------------- Config -----------------
BASE_URL_L = os.getenv('BASE_URL_L')
BASE_URL_R = os.getenv('BASE_URL_R')
SLEEP_SECONDS = int(os.getenv("SLEEP_SECONDS", "60"))     # how long between checks
BLOCK_SLEEP = int(os.getenv("BLOCK_SLEEP", "150"))        # when "max attempts" hits
DATA_FILE = os.getenv("DATA_FILE", "results.jsonl")       # append-only storage
CONTROL_TOKEN = os.getenv("CONTROL_TOKEN")                # optional simple protection for /start and /stop

# -------------- Flask setup --------------
app = Flask(__name__)
app.debug = False

# Background worker state
worker_thread: Optional[threading.Thread] = None
stop_event = threading.Event()
state_lock = threading.Lock()
state: Dict[str, Any] = {
    "running": False,
    "last_plate": None,
    "last_status": None,
    "last_checked_at": None,
    "queue_remaining": None,
}

# -------------- Utility: storage --------------
def save_result(plate: str, status: str, note: str = "") -> None:
    """Append one JSON object per line. Dependency-free & safe to read while writing."""
    record = {
        "plate": plate,
        "status": status,                # e.g., 'issued', 'available', 'blocked', 'error'
        "note": note,                    # optional raw hints
        "checked_at": datetime.now().isoformat(timespec="seconds"),
    }
    os.makedirs(os.path.dirname(DATA_FILE) or ".", exist_ok=True)
    # Write a full line; readers skip malformed lines defensively
    with open(DATA_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

def load_results(limit: Optional[int] = None) -> List[Dict[str, Any]]:
    if not os.path.exists(DATA_FILE):
        return []
    rows: List[Dict[str, Any]] = []
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                # If a line was being written while we read, skip it
                continue
    rows.reverse()  # newest first
    return rows[:limit] if limit else rows

# -------------- Your original logic --------------
def generate_combinations() -> List[str]:
    letters = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
    # doubles = [ch * 2 for ch in letters]
    two_letters = [a + b for a in letters for b in letters]     # AA AB AC ..         
    # digits = [f"{i:02d}" for i in range(100)]      # 00..99
    return two_letters # + digits

def check_plate(url: str) -> str:
    try:
        r = requests.get(
            url,
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0 (compatible; PlateChecker/1.0)"},
        )
        return r.text or ""
    except Exception as e:
        return f"__ERROR__ {e}"

def parse_status(response_text: str) -> (str, str):
    """
    Map raw HTML/text to a compact status.
    Returns (status, note). Status is one of: issued, available, blocked, error, unknown
    """
    if response_text.startswith("__ERROR__"):
        return "error", response_text
    if "You have reached the maximum plate preview attempts" in response_text:
        return "blocked", "rate-limited"
    if "Plate is issued" in response_text:
        return "issued", ""
    # Heuristic: if page loads and doesn't say issued/blocked, treat as available/other
    if "available" in response_text.lower():
        return "available", ""
    return "unknown", response_text[:200]  # stash a snippet

# -------------- Background worker --------------
def runner_loop():
    combos = generate_combinations()
    total = len(combos)
    with state_lock:
        state["running"] = True
        state["queue_remaining"] = total

    for idx, plate in enumerate(combos, start=1):
        if stop_event.is_set():
            break

        url = f"{BASE_URL_L}{plate}{BASE_URL_R}"
        raw = check_plate(url)
        status, note = parse_status(raw)
        save_result(plate, status, note)

        with state_lock:
            state["last_plate"] = plate
            state["last_status"] = status
            state["last_checked_at"] = datetime.now().isoformat(timespec="seconds")
            state["queue_remaining"] = total - idx

        # Respect block message
        if status == "blocked":
            time.sleep(BLOCK_SLEEP)
        else:
            time.sleep(SLEEP_SECONDS)

    with state_lock:
        state["running"] = False

# -------------- Routes --------------
def _require_token():
    if CONTROL_TOKEN and request.args.get("token") != CONTROL_TOKEN:
        abort(401, "Missing or invalid token")

@app.get("/start")
def start():
    """Start the background checker (once). Use ?token=... if CONTROL_TOKEN is set."""
    _require_token()
    global worker_thread
    if state["running"]:
        return jsonify({"ok": True, "message": "Already running"}), 200

    stop_event.clear()
    worker_thread = threading.Thread(target=runner_loop, daemon=True)
    worker_thread.start()
    return jsonify({"ok": True, "message": "Started"}), 202

@app.get("/stop")
def stop():
    """Signal the checker to stop after the current sleep. Use ?token=... if CONTROL_TOKEN is set."""
    _require_token()
    if not state["running"]:
        return jsonify({"ok": True, "message": "Not running"}), 200
    stop_event.set()
    return jsonify({"ok": True, "message": "Stopping"}), 202

@app.get("/status")
def status():
    with state_lock:
        return jsonify(state)

@app.get("/results.json")
def results_json():
    limit = request.args.get("limit", type=int)
    return jsonify(load_results(limit))

@app.get("/results")
def results_html():
    rows = load_results()
    html = """
    <!doctype html>
    <html lang="en"><head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width,initial-scale=1">
      <title>Plate check results</title>
      <style>
        body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; margin: 2rem; }
        h1 { margin-bottom: 0.25rem; }
        .meta { color:#666; margin-bottom: 1rem; }
        table { border-collapse: collapse; width: 100%; }
        th, td { padding: 0.6rem 0.8rem; border-bottom: 1px solid #e5e7eb; text-align: left; }
        th { background: #f9fafb; font-weight: 600; }
        tr:hover td { background: #f6f8fa; }
        .badge { display:inline-block; padding:0.15rem 0.5rem; border-radius:999px; font-size:0.8rem; }
        .issued { background:#d1fae5; }
        .available { background:#e0e7ff; }
        .blocked { background:#fde68a; }
        .error { background:#fecaca; }
        .unknown { background:#e5e7eb; }
        .pill { display:inline-block; padding:0.1rem 0.45rem; border:1px solid #ddd; border-radius:999px; font-size:0.8rem; color:#444; }
      </style>
    </head><body>
      <h1>Plate check results</h1>
      <div class="meta">
        <span id="run" class="pill">Status: {{ 'running' if running else 'stopped' }}</span>
        {% if last_plate %}<span class="pill">Last: {{ last_plate }} ({{ last_status }}) at {{ last_checked_at }}</span>{% endif %}
        {% if queue_remaining is not none %}<span class="pill">Remaining: {{ queue_remaining }}</span>{% endif %}
      </div>
      {% if rows %}
      <table>
        <thead><tr><th>Plate</th><th>Status</th><th>Checked at</th><th>Note</th></tr></thead>
        <tbody>
          {% for r in rows %}
          {% set s = (r.get('status') or 'unknown').lower() %}
          <tr>
            <td>{{ r.get('plate','') }}</td>
            <td><span class="badge {{ s }}">{{ r.get('status','unknown') }}</span></td>
            <td>{{ r.get('checked_at','') }}</td>
            <td>{{ r.get('note','') }}</td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
      {% else %}
        <p>No results yet. Hit <code>/start</code> to begin checking.</p>
      {% endif %}
    </body></html>
    """
    with state_lock:
        ctx = {
            "running": state["running"],
            "last_plate": state["last_plate"],
            "last_status": state["last_status"],
            "last_checked_at": state["last_checked_at"],
            "queue_remaining": state["queue_remaining"],
        }
    return render_template_string(html, rows=rows, **ctx)

# -------------- Entrypoint --------------
if __name__ == "__main__":
    # Important: disable the reloader so the worker doesn't start twice
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")), debug=False, use_reloader=False)
