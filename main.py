"""
MediScan - Smart Medical Assistant
====================================
Features:
  1. Symptom Checker → recommends the right scan (no unnecessary radiation)
  2. Pill Scanner     → identify medicine from a photo (base64 upload)
  3. AI Chatbot       → guides you to the right doctor / gives advice
  4. Medicine Tracker → add meds, set times, mark doses, see streaks

Run:  python3 mediscan_app.py
Open: http://localhost:5050
"""

from flask import Flask, request, jsonify, render_template_string
import json, sqlite3, base64, os, datetime, pathlib, re

app = Flask(__name__)
DB = pathlib.Path("mediscan.db")

# ─── DATABASE ─────────────────────────────────────────────────────────────────
def get_db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    con = get_db()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS medicines (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        name      TEXT NOT NULL,
        dosage    TEXT,
        times     TEXT NOT NULL,   -- JSON list of "HH:MM" strings
        days      TEXT NOT NULL,   -- JSON list: ["Mon","Tue",...] or ["daily"]
        notes     TEXT,
        color     TEXT DEFAULT '#4f8ef7',
        created   TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS doses (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        med_id   INTEGER NOT NULL,
        due_date TEXT NOT NULL,    -- YYYY-MM-DD
        due_time TEXT NOT NULL,    -- HH:MM
        taken    INTEGER DEFAULT 0,
        taken_at TEXT
    );
    CREATE TABLE IF NOT EXISTS chat_history (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        role    TEXT,
        content TEXT,
        ts      TEXT DEFAULT (datetime('now'))
    );
    """)
    con.commit()
    con.close()

init_db()

# ─── SYMPTOM → TEST LOGIC ─────────────────────────────────────────────────────
SYMPTOM_TREE = {
    "chest pain": {
        "follow_ups": ["Is it sharp or pressure-like?", "Does it radiate to your arm or jaw?", "Any shortness of breath?"],
        "result": {
            "likely": "Cardiac / Musculoskeletal issue",
            "test": "ECG first, then Echocardiogram — CT only if PE suspected",
            "urgency": "red",
            "radiation": "ECG = zero radiation ✅"
        }
    },
    "stomach pain": {
        "follow_ups": ["Where exactly — upper, lower, left, right?", "Does it hurt more after eating?", "Any fever or vomiting?"],
        "result": {
            "likely": "Gastric / Liver / Appendix issue",
            "test": "Ultrasound abdomen — CT only if ultrasound inconclusive",
            "urgency": "yellow",
            "radiation": "Ultrasound = zero radiation ✅"
        }
    },
    "headache": {
        "follow_ups": ["Sudden or gradual?", "Any vision changes or neck stiffness?", "How long has it been?"],
        "result": {
            "likely": "Tension / Migraine / Sinusitis",
            "test": "MRI brain (no radiation) — CT only for trauma/acute bleed",
            "urgency": "yellow",
            "radiation": "MRI = zero radiation ✅"
        }
    },
    "back pain": {
        "follow_ups": ["Upper or lower back?", "Any leg weakness or numbness?", "After injury or gradual?"],
        "result": {
            "likely": "Musculoskeletal / Disc issue",
            "test": "MRI spine — X-ray only if fracture suspected",
            "urgency": "green",
            "radiation": "MRI = zero radiation ✅"
        }
    },
    "cough": {
        "follow_ups": ["Dry or with phlegm?", "Any blood in cough?", "For how many days?"],
        "result": {
            "likely": "Respiratory infection / Asthma / TB",
            "test": "Chest X-ray (low dose) — CT only if mass suspected",
            "urgency": "yellow",
            "radiation": "Chest X-ray = minimal radiation (0.1 mSv)"
        }
    },
    "knee pain": {
        "follow_ups": ["After injury or gradual?", "Any swelling or locking?", "Can you bear weight?"],
        "result": {
            "likely": "Ligament / Meniscus / Arthritis",
            "test": "MRI knee — X-ray only if fracture suspected",
            "urgency": "green",
            "radiation": "MRI = zero radiation ✅"
        }
    },
    "fever": {
        "follow_ups": ["How high? (°F or °C)", "Any rash or body ache?", "Travelled recently?"],
        "result": {
            "likely": "Infection — viral or bacterial",
            "test": "Blood CBC + CRP first — imaging rarely needed for fever alone",
            "urgency": "yellow",
            "radiation": "Blood test = zero radiation ✅"
        }
    },
}

def match_symptom(text):
    text = text.lower()
    for key in SYMPTOM_TREE:
        if key in text or any(w in text for w in key.split()):
            return key
    return None

# ─── AI CALL (Anthropic API via fetch in frontend) ───────────────────────────
# We call the Anthropic API from the browser-side JS for chatbot & pill scanner.
# Python backend handles data persistence and symptom logic.

# ─── ROUTES ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(HTML)

@app.route("/api/symptom/start", methods=["POST"])
def symptom_start():
    data = request.json
    symptom_text = data.get("text", "")
    matched = match_symptom(symptom_text)
    if matched:
        node = SYMPTOM_TREE[matched]
        return jsonify({"found": True, "key": matched, "questions": node["follow_ups"], "result": node["result"]})
    return jsonify({"found": False, "message": "Could not identify symptom pattern. Try keywords like 'chest pain', 'stomach pain', 'headache', 'cough', 'fever', 'back pain', 'knee pain'."})

# Medicine CRUD
@app.route("/api/medicines", methods=["GET"])
def get_medicines():
    con = get_db()
    rows = con.execute("SELECT * FROM medicines ORDER BY created DESC").fetchall()
    con.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/medicines", methods=["POST"])
def add_medicine():
    d = request.json
    con = get_db()
    con.execute("INSERT INTO medicines (name, dosage, times, days, notes, color) VALUES (?,?,?,?,?,?)",
                (d["name"], d.get("dosage",""), json.dumps(d["times"]), json.dumps(d["days"]), d.get("notes",""), d.get("color","#4f8ef7")))
    con.commit()
    med_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
    # auto-generate today's doses
    _generate_doses(con, med_id, d)
    con.commit()
    con.close()
    return jsonify({"ok": True, "id": med_id})

@app.route("/api/medicines/<int:mid>", methods=["DELETE"])
def delete_medicine(mid):
    con = get_db()
    con.execute("DELETE FROM medicines WHERE id=?", (mid,))
    con.execute("DELETE FROM doses WHERE med_id=?", (mid,))
    con.commit()
    con.close()
    return jsonify({"ok": True})

def _generate_doses(con, med_id, d):
    today = datetime.date.today().isoformat()
    times = d["times"] if isinstance(d["times"], list) else json.loads(d["times"])
    for t in times:
        existing = con.execute("SELECT id FROM doses WHERE med_id=? AND due_date=? AND due_time=?", (med_id, today, t)).fetchone()
        if not existing:
            con.execute("INSERT INTO doses (med_id, due_date, due_time) VALUES (?,?,?)", (med_id, today, t))

@app.route("/api/doses/today", methods=["GET"])
def today_doses():
    today = datetime.date.today().isoformat()
    con = get_db()
    # ensure today's doses exist for all meds
    meds = con.execute("SELECT * FROM medicines").fetchall()
    for m in meds:
        _generate_doses(con, m["id"], {"times": m["times"], "days": m["days"]})
    con.commit()
    rows = con.execute("""
        SELECT d.*, m.name, m.dosage, m.color, m.notes
        FROM doses d JOIN medicines m ON d.med_id = m.id
        WHERE d.due_date = ? ORDER BY d.due_time
    """, (today,)).fetchall()
    con.close()
    now = datetime.datetime.now().strftime("%H:%M")
    result = []
    for r in rows:
        row = dict(r)
        row["status"] = "taken" if r["taken"] else ("overdue" if r["due_time"] < now else "upcoming")
        result.append(row)
    return jsonify(result)

@app.route("/api/doses/<int:did>/take", methods=["POST"])
def take_dose(did):
    now = datetime.datetime.now().isoformat(timespec="seconds")
    con = get_db()
    con.execute("UPDATE doses SET taken=1, taken_at=? WHERE id=?", (now, did))
    con.commit()
    con.close()
    return jsonify({"ok": True})

@app.route("/api/doses/<int:did>/untake", methods=["POST"])
def untake_dose(did):
    con = get_db()
    con.execute("UPDATE doses SET taken=0, taken_at=NULL WHERE id=?", (did,))
    con.commit()
    con.close()
    return jsonify({"ok": True})

@app.route("/api/streak", methods=["GET"])
def streak():
    con = get_db()
    # adherence last 7 days
    rows = con.execute("""
        SELECT due_date, COUNT(*) as total, SUM(taken) as taken
        FROM doses WHERE due_date >= date('now','-6 days')
        GROUP BY due_date ORDER BY due_date
    """).fetchall()
    con.close()
    days = []
    for r in rows:
        pct = int(r["taken"]/r["total"]*100) if r["total"] else 0
        days.append({"date": r["due_date"], "total": r["total"], "taken": r["taken"], "pct": pct})
    return jsonify(days)

# ─── HTML FRONTEND ────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>MediScan — Smart Health Assistant</title>
<style>
  :root {
    --bg: #0d1117; --surface: #161b22; --card: #1c2128; --border: #30363d;
    --text: #e6edf3; --muted: #8b949e; --accent: #58a6ff;
    --green: #3fb950; --yellow: #d29922; --red: #f85149;
    --pill: #388bfd20; --radius: 12px; --font: 'Segoe UI', system-ui, sans-serif;
  }
  * { box-sizing:border-box; margin:0; padding:0 }
  body { background:var(--bg); color:var(--text); font-family:var(--font); min-height:100vh }
  /* NAV */
  nav { background:var(--surface); border-bottom:1px solid var(--border); padding:0 24px;
        display:flex; align-items:center; gap:8px; height:60px; position:sticky; top:0; z-index:100 }
  .logo { font-size:20px; font-weight:700; color:var(--accent); margin-right:auto }
  .logo span { color:var(--text); font-weight:300 }
  .nav-btn { background:none; border:none; color:var(--muted); cursor:pointer; padding:8px 14px;
             border-radius:8px; font-size:14px; font-weight:500; transition:.2s; white-space:nowrap }
  .nav-btn:hover,.nav-btn.active { background:var(--pill); color:var(--accent) }
  /* LAYOUT */
  main { max-width:860px; margin:0 auto; padding:32px 20px }
  .page { display:none } .page.active { display:block }
  h2 { font-size:22px; font-weight:600; margin-bottom:6px }
  .subtitle { color:var(--muted); font-size:14px; margin-bottom:28px }
  /* CARDS */
  .card { background:var(--card); border:1px solid var(--border); border-radius:var(--radius); padding:20px; margin-bottom:16px }
  /* INPUTS */
  input,textarea,select {
    width:100%; background:var(--surface); border:1px solid var(--border);
    color:var(--text); border-radius:8px; padding:10px 14px; font-size:14px; font-family:inherit;
    outline:none; transition:.2s
  }
  input:focus,textarea:focus,select:focus { border-color:var(--accent) }
  textarea { resize:vertical; min-height:90px }
  label { font-size:13px; color:var(--muted); margin-bottom:5px; display:block }
  .form-row { display:grid; grid-template-columns:1fr 1fr; gap:12px; margin-bottom:14px }
  @media(max-width:520px){ .form-row { grid-template-columns:1fr } }
  .form-group { margin-bottom:14px }
  /* BUTTONS */
  .btn { display:inline-flex; align-items:center; gap:6px; padding:9px 18px; border-radius:8px;
         font-size:14px; font-weight:500; cursor:pointer; border:none; transition:.2s }
  .btn-primary { background:var(--accent); color:#000 }
  .btn-primary:hover { opacity:.85 }
  .btn-outline { background:none; border:1px solid var(--border); color:var(--text) }
  .btn-outline:hover { border-color:var(--accent); color:var(--accent) }
  .btn-danger  { background:none; border:1px solid var(--red); color:var(--red) }
  .btn-danger:hover { background:var(--red); color:#fff }
  .btn-green  { background:var(--green); color:#000 }
  .btn-sm { padding:5px 12px; font-size:12px }
  /* BADGES */
  .badge { display:inline-block; padding:2px 10px; border-radius:20px; font-size:12px; font-weight:600 }
  .badge-red    { background:#f8514920; color:var(--red) }
  .badge-yellow { background:#d2992220; color:var(--yellow) }
  .badge-green  { background:#3fb95020; color:var(--green) }
  /* SYMPTOM */
  #sym-input-area { display:flex; gap:10px; margin-bottom:16px }
  #sym-input-area input { flex:1 }
  .question-bubble { background:var(--surface); border-left:3px solid var(--accent);
    padding:12px 16px; border-radius:8px; margin-bottom:10px; font-size:14px }
  .result-card { border-radius:var(--radius); padding:20px; margin-top:20px }
  .result-card.green { background:#3fb95015; border:1px solid var(--green) }
  .result-card.yellow{ background:#d2992215; border:1px solid var(--yellow) }
  .result-card.red   { background:#f8514915; border:1px solid var(--red) }
  .result-card h3 { font-size:17px; margin-bottom:12px }
  .result-row { display:flex; gap:8px; align-items:flex-start; margin-bottom:8px; font-size:14px }
  .result-label { color:var(--muted); min-width:100px; font-size:13px }
  /* PILL SCANNER */
  .upload-zone { border:2px dashed var(--border); border-radius:var(--radius);
    padding:40px; text-align:center; cursor:pointer; transition:.2s; position:relative }
  .upload-zone:hover { border-color:var(--accent); background:var(--pill) }
  .upload-zone input[type=file] { position:absolute; inset:0; opacity:0; cursor:pointer }
  #pill-preview { max-width:100%; max-height:220px; border-radius:8px; margin-top:14px; display:none }
  /* CHAT */
  #chat-messages { height:380px; overflow-y:auto; padding:16px; display:flex; flex-direction:column; gap:12px }
  .msg { max-width:76%; padding:10px 14px; border-radius:12px; font-size:14px; line-height:1.5; word-break:break-word }
  .msg.user { background:var(--accent); color:#000; align-self:flex-end; border-bottom-right-radius:4px }
  .msg.bot  { background:var(--surface); border:1px solid var(--border); align-self:flex-start; border-bottom-left-radius:4px }
  .msg.bot.loading { color:var(--muted) }
  #chat-input-row { display:flex; gap:10px; padding:14px 16px; border-top:1px solid var(--border) }
  #chat-input { flex:1 }
  /* MEDICINE */
  .med-card { display:flex; align-items:flex-start; gap:14px; padding:16px }
  .med-dot { width:12px; height:12px; border-radius:50%; margin-top:3px; flex-shrink:0 }
  .med-info { flex:1 }
  .med-name { font-weight:600; font-size:15px }
  .med-meta { font-size:13px; color:var(--muted); margin-top:2px }
  .dose-list { margin-top:0 }
  .dose-item { display:flex; align-items:center; gap:12px; padding:12px 16px;
    border-bottom:1px solid var(--border) }
  .dose-item:last-child { border-bottom:none }
  .dose-time { font-size:22px; font-weight:700; width:60px; flex-shrink:0; color:var(--accent) }
  .dose-info { flex:1 }
  .dose-name { font-weight:600 }
  .dose-dosage { font-size:13px; color:var(--muted) }
  .dose-check { width:32px; height:32px; border-radius:50%; border:2px solid var(--border);
    background:none; cursor:pointer; font-size:16px; display:flex; align-items:center;
    justify-content:center; transition:.2s; flex-shrink:0 }
  .dose-check.taken { background:var(--green); border-color:var(--green); color:#000 }
  .dose-check.overdue { border-color:var(--red) }
  /* STREAK */
  .streak-bar { display:flex; gap:6px; margin-top:12px }
  .streak-day { flex:1; text-align:center }
  .streak-fill { height:50px; border-radius:6px; background:var(--surface); border:1px solid var(--border);
    display:flex; align-items:flex-end; overflow:hidden }
  .streak-inner { width:100%; transition:.6s; border-radius:4px }
  .streak-label { font-size:11px; color:var(--muted); margin-top:4px }
  /* TIMES PICKER */
  .times-list { display:flex; flex-wrap:wrap; gap:8px; margin-top:6px }
  .time-chip { background:var(--surface); border:1px solid var(--border); border-radius:20px;
    padding:4px 12px; font-size:13px; display:flex; align-items:center; gap:6px }
  .time-chip button { background:none; border:none; color:var(--muted); cursor:pointer; font-size:15px; padding:0 }
  /* TABS */
  .tab-bar { display:flex; gap:4px; margin-bottom:20px; background:var(--surface);
    border:1px solid var(--border); border-radius:10px; padding:4px }
  .tab-btn { flex:1; text-align:center; padding:8px; border-radius:7px; cursor:pointer;
    font-size:13px; font-weight:500; border:none; background:none; color:var(--muted); transition:.2s }
  .tab-btn.active { background:var(--accent); color:#000 }
  .tab-panel { display:none } .tab-panel.active { display:block }
  /* SPINNER */
  @keyframes spin { to{ transform:rotate(360deg) } }
  .spin { display:inline-block; width:16px; height:16px; border:2px solid var(--border);
    border-top-color:var(--accent); border-radius:50%; animation:spin .7s linear infinite }
  /* EMPTY */
  .empty { text-align:center; padding:40px; color:var(--muted); font-size:14px }
  .empty .icon { font-size:40px; margin-bottom:10px }
  /* MODAL */
  .modal-overlay { position:fixed; inset:0; background:#000a; display:none; z-index:200;
    align-items:center; justify-content:center; padding:20px }
  .modal-overlay.open { display:flex }
  .modal { background:var(--card); border:1px solid var(--border); border-radius:var(--radius);
    padding:24px; width:100%; max-width:480px; max-height:90vh; overflow-y:auto }
  .modal h3 { margin-bottom:18px; font-size:18px }
  .modal-footer { display:flex; gap:10px; justify-content:flex-end; margin-top:20px }
  .color-row { display:flex; gap:8px; margin-top:6px }
  .color-swatch { width:28px; height:28px; border-radius:50%; cursor:pointer; border:2px solid transparent; transition:.2s }
  .color-swatch.selected { border-color:var(--text) }
  /* scrollbar */
  ::-webkit-scrollbar { width:6px } ::-webkit-scrollbar-track { background:var(--surface) }
  ::-webkit-scrollbar-thumb { background:var(--border); border-radius:3px }
</style>
</head>
<body>

<nav>
  <div class="logo">Medi<span>Scan</span></div>
  <button class="nav-btn active" onclick="showPage('symptoms')">🩺 Symptoms</button>
  <button class="nav-btn" onclick="showPage('pill')">💊 Pill ID</button>
  <button class="nav-btn" onclick="showPage('chat')">🤖 Chat</button>
  <button class="nav-btn" onclick="showPage('tracker')">⏰ Tracker</button>
</nav>

<main>

<!-- ═══════════════ SYMPTOM CHECKER ═══════════════ -->
<div class="page active" id="page-symptoms">
  <h2>🩺 Symptom Checker</h2>
  <p class="subtitle">Tell us what's wrong — we'll recommend the right test, not the most expensive one.</p>

  <div class="card">
    <div id="sym-input-area">
      <input id="sym-input" placeholder="e.g. I have stomach pain for 3 days…" onkeydown="if(event.key==='Enter')startSymptom()"/>
      <button class="btn btn-primary" onclick="startSymptom()">Analyse</button>
    </div>
    <div style="font-size:12px;color:var(--muted)">Try: chest pain · stomach pain · headache · back pain · cough · fever · knee pain</div>
  </div>

  <div id="sym-questions" style="display:none">
    <div class="card">
      <div style="font-size:13px;color:var(--muted);margin-bottom:10px">📋 A few quick follow-up questions:</div>
      <div id="q-list"></div>
      <button class="btn btn-primary" style="margin-top:12px" onclick="showResult()">Get Result →</button>
    </div>
  </div>

  <div id="sym-result"></div>
</div>

<!-- ═══════════════ PILL SCANNER ═══════════════ -->
<div class="page" id="page-pill">
  <h2>💊 Pill Identifier</h2>
  <p class="subtitle">Upload a photo of any pill or medicine — get full details instantly.</p>

  <div class="card">
    <div class="upload-zone" id="upload-zone" onclick="document.getElementById('pill-file').click()">
      <input type="file" id="pill-file" accept="image/*" onchange="previewPill(event)"/>
      <div id="upload-hint">
        <div style="font-size:36px">📷</div>
        <div style="margin-top:8px;font-weight:500">Click to upload a pill photo</div>
        <div style="font-size:12px;color:var(--muted);margin-top:4px">JPG, PNG supported</div>
      </div>
      <img id="pill-preview"/>
    </div>
    <button class="btn btn-primary" style="margin-top:14px;width:100%" onclick="scanPill()">🔍 Identify Pill</button>
  </div>

  <div id="pill-result"></div>
</div>

<!-- ═══════════════ CHATBOT ═══════════════ -->
<div class="page" id="page-chat">
  <h2>🤖 Medical Chatbot</h2>
  <p class="subtitle">Ask anything — which doctor to see, what a symptom means, what to do next.</p>
  <div class="card" style="padding:0;overflow:hidden">
    <div id="chat-messages">
      <div class="msg bot">👋 Hi! I'm MediScan AI. Tell me your symptoms or ask any health question. I'll guide you to the right doctor and what steps to take. <strong>Remember:</strong> I'm an assistant, not a replacement for professional medical care.</div>
    </div>
    <div id="chat-input-row">
      <input id="chat-input" placeholder="Ask me anything about your health…" onkeydown="if(event.key==='Enter')sendChat()"/>
      <button class="btn btn-primary" onclick="sendChat()">Send</button>
    </div>
  </div>
</div>

<!-- ═══════════════ MEDICINE TRACKER ═══════════════ -->
<div class="page" id="page-tracker">
  <h2>⏰ Medicine Tracker</h2>
  <p class="subtitle">Never miss a dose. Track your medicines and build healthy habits.</p>

  <div class="tab-bar">
    <button class="tab-btn active" onclick="switchTab('today')">Today's Doses</button>
    <button class="tab-btn" onclick="switchTab('meds')">My Medicines</button>
    <button class="tab-btn" onclick="switchTab('stats')">7-Day Stats</button>
  </div>

  <!-- TODAY TAB -->
  <div class="tab-panel active" id="tab-today">
    <div id="today-summary" class="card" style="display:flex;gap:20px;flex-wrap:wrap"></div>
    <div class="card" style="padding:0;overflow:hidden">
      <div id="dose-list" class="dose-list"></div>
    </div>
  </div>

  <!-- MEDS TAB -->
  <div class="tab-panel" id="tab-meds">
    <button class="btn btn-primary" style="margin-bottom:16px" onclick="openAddMed()">＋ Add Medicine</button>
    <div id="med-list"></div>
  </div>

  <!-- STATS TAB -->
  <div class="tab-panel" id="tab-stats">
    <div class="card">
      <div style="font-weight:600;margin-bottom:4px">Adherence — Last 7 Days</div>
      <div style="font-size:13px;color:var(--muted);margin-bottom:16px">How consistently you took your medicines</div>
      <div class="streak-bar" id="streak-bar"></div>
    </div>
    <div class="card" id="streak-tip"></div>
  </div>
</div>

</main>

<!-- ADD MED MODAL -->
<div class="modal-overlay" id="add-med-modal">
  <div class="modal">
    <h3>Add Medicine</h3>
    <div class="form-row">
      <div class="form-group">
        <label>Medicine Name *</label>
        <input id="m-name" placeholder="e.g. Metformin"/>
      </div>
      <div class="form-group">
        <label>Dosage</label>
        <input id="m-dosage" placeholder="e.g. 500mg"/>
      </div>
    </div>
    <div class="form-group">
      <label>Schedule Times * <span style="color:var(--muted)">(add one or more)</span></label>
      <div style="display:flex;gap:8px">
        <input type="time" id="m-time-input" style="flex:1"/>
        <button class="btn btn-outline btn-sm" onclick="addTime()">＋ Add</button>
      </div>
      <div class="times-list" id="times-list"></div>
    </div>
    <div class="form-group">
      <label>Days</label>
      <select id="m-days">
        <option value="daily">Every day</option>
        <option value="weekdays">Weekdays (Mon–Fri)</option>
        <option value="weekends">Weekends (Sat–Sun)</option>
        <option value="alternate">Alternate days</option>
      </select>
    </div>
    <div class="form-group">
      <label>Colour</label>
      <div class="color-row" id="color-row">
        <div class="color-swatch selected" style="background:#4f8ef7" data-c="#4f8ef7" onclick="pickColor(this)"></div>
        <div class="color-swatch" style="background:#3fb950" data-c="#3fb950" onclick="pickColor(this)"></div>
        <div class="color-swatch" style="background:#d29922" data-c="#d29922" onclick="pickColor(this)"></div>
        <div class="color-swatch" style="background:#f85149" data-c="#f85149" onclick="pickColor(this)"></div>
        <div class="color-swatch" style="background:#bc8cff" data-c="#bc8cff" onclick="pickColor(this)"></div>
        <div class="color-swatch" style="background:#ff7b72" data-c="#ff7b72" onclick="pickColor(this)"></div>
      </div>
    </div>
    <div class="form-group">
      <label>Notes</label>
      <input id="m-notes" placeholder="e.g. take after food"/>
    </div>
    <div class="modal-footer">
      <button class="btn btn-outline" onclick="closeAddMed()">Cancel</button>
      <button class="btn btn-primary" onclick="saveMed()">Save Medicine</button>
    </div>
  </div>
</div>

<script>
// ── UTILS ─────────────────────────────────────────────────────────────────────
const ANTHROPIC_API = "https://api.anthropic.com/v1/messages";

async function callClaude(messages, system="", tools=null) {
  const body = {
    model: "claude-sonnet-4-6",
    max_tokens: 1000,
    system: system || undefined,
    messages
  };
  if (tools) body.tools = tools;
  const res = await fetch(ANTHROPIC_API, {
    method:"POST",
    headers:{"Content-Type":"application/json"},
    body: JSON.stringify(body)
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error?.message || "API error");
  return data.content.filter(b=>b.type==="text").map(b=>b.text).join("\n");
}

// ── NAVIGATION ────────────────────────────────────────────────────────────────
function showPage(name) {
  document.querySelectorAll(".page").forEach(p=>p.classList.remove("active"));
  document.querySelectorAll(".nav-btn").forEach(b=>b.classList.remove("active"));
  document.getElementById("page-"+name).classList.add("active");
  event.currentTarget.classList.add("active");
  if(name==="tracker") { loadTodayDoses(); loadMeds(); loadStreak(); }
}

// ── SYMPTOM CHECKER ───────────────────────────────────────────────────────────
let symResult = null;

async function startSymptom() {
  const txt = document.getElementById("sym-input").value.trim();
  if(!txt) return;
  const res = await fetch("/api/symptom/start",{
    method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({text:txt})
  });
  const data = await res.json();
  document.getElementById("sym-result").innerHTML = "";
  if(!data.found) {
    document.getElementById("sym-questions").style.display = "none";
    document.getElementById("sym-result").innerHTML =
      `<div class="card" style="border-color:var(--yellow);color:var(--yellow)">${data.message}</div>`;
    return;
  }
  symResult = data.result;
  const ql = document.getElementById("q-list");
  ql.innerHTML = data.questions.map((q,i)=>`
    <div class="question-bubble">
      <b>Q${i+1}.</b> ${q}<br>
      <input style="margin-top:8px" placeholder="Your answer…" id="qa-${i}"/>
    </div>`).join("");
  document.getElementById("sym-questions").style.display = "block";
}

function showResult() {
  if(!symResult) return;
  const r = symResult;
  const colors = {red:"red",yellow:"yellow",green:"green"};
  const emojis = {red:"🔴",yellow:"🟡",green:"🟢"};
  document.getElementById("sym-result").innerHTML = `
    <div class="result-card ${colors[r.urgency]}">
      <h3>${emojis[r.urgency]} Assessment Result</h3>
      <div class="result-row"><span class="result-label">Likely cause</span><strong>${r.likely}</strong></div>
      <div class="result-row"><span class="result-label">Recommended</span><strong>${r.test}</strong></div>
      <div class="result-row"><span class="result-label">Radiation</span><span style="color:var(--green)">${r.radiation}</span></div>
      <div class="result-row"><span class="result-label">Urgency</span>
        <span class="badge badge-${r.urgency}">${r.urgency.toUpperCase()}</span>
      </div>
      <div style="margin-top:14px;padding:12px;background:var(--surface);border-radius:8px;font-size:13px;color:var(--muted)">
        ⚠️ This is a guidance tool only. Always consult a qualified doctor for diagnosis and treatment.
      </div>
    </div>`;
  document.getElementById("sym-questions").style.display = "none";
}

// ── PILL SCANNER ──────────────────────────────────────────────────────────────
let pillBase64 = null, pillMime = null;

function previewPill(e) {
  const file = e.target.files[0];
  if(!file) return;
  const reader = new FileReader();
  reader.onload = ev => {
    const raw = ev.target.result; // data:image/jpeg;base64,XXXX
    pillMime = file.type;
    pillBase64 = raw.split(",")[1];
    const img = document.getElementById("pill-preview");
    img.src = raw; img.style.display = "block";
    document.getElementById("upload-hint").style.display = "none";
  };
  reader.readAsDataURL(file);
}

async function scanPill() {
  const resDiv = document.getElementById("pill-result");
  if(!pillBase64) {
    resDiv.innerHTML = `<div class="card" style="border-color:var(--yellow);color:var(--yellow)">Please upload a pill photo first.</div>`;
    return;
  }
  resDiv.innerHTML = `<div class="card" style="text-align:center"><div class="spin"></div> Analysing pill… this may take a moment</div>`;
  try {
    const text = await callClaude([{
      role:"user",
      content:[
        {type:"image", source:{type:"base64", media_type:pillMime, data:pillBase64}},
        {type:"text", text:`You are a pharmaceutical expert. Analyse this pill image and provide:
1. **Medicine Name** (if identifiable)
2. **Active Ingredients**
3. **Common Uses** (what it treats)
4. **Typical Dosage**
5. **Side Effects** (common ones)
6. **Warnings / Who should avoid it**
7. **Alternative names / generics**

If the image is unclear or not a pill, say so. Format clearly with bold headers. Add a disclaimer at the end.`}
      ]
    }]);
    resDiv.innerHTML = `<div class="card"><div style="font-weight:600;margin-bottom:12px;font-size:15px">💊 Pill Analysis</div>
      <div style="font-size:14px;line-height:1.7;white-space:pre-wrap">${text}</div></div>`;
  } catch(err) {
    resDiv.innerHTML = `<div class="card" style="border-color:var(--red);color:var(--red)">Error: ${err.message}</div>`;
  }
}

// ── CHATBOT ───────────────────────────────────────────────────────────────────
const chatHistory = [];

async function sendChat() {
  const inp = document.getElementById("chat-input");
  const msg = inp.value.trim();
  if(!msg) return;
  inp.value = "";

  appendMsg("user", msg);
  chatHistory.push({role:"user", content:msg});

  const loading = appendMsg("bot loading", "⏳ Thinking…");
  try {
    const reply = await callClaude(
      chatHistory,
      `You are MediScan AI, a friendly and knowledgeable medical assistant. 
Your role:
- Help users understand their symptoms in simple terms
- Recommend which type of doctor to see (GP, cardiologist, neurologist, etc.)
- Explain what tests might be needed and WHY
- Provide general health advice and home remedies where appropriate
- Always recommend seeing a real doctor for diagnosis
- Mention which tests avoid radiation vs which use radiation
- Keep answers concise, warm, and in simple non-technical language
- Use bullet points for clarity
Always end responses about serious symptoms with: "🏥 Please visit a doctor promptly."`
    );
    loading.classList.remove("loading");
    loading.innerHTML = reply.replace(/\n/g,"<br>").replace(/\*\*(.*?)\*\*/g,"<strong>$1</strong>");
    chatHistory.push({role:"assistant", content:reply});
  } catch(err) {
    loading.classList.remove("loading");
    loading.innerHTML = `❌ ${err.message}`;
  }
}

function appendMsg(cls, text) {
  const div = document.createElement("div");
  div.className = "msg "+cls;
  div.innerHTML = text;
  const container = document.getElementById("chat-messages");
  container.appendChild(div);
  container.scrollTop = container.scrollHeight;
  return div;
}

// ── MEDICINE TRACKER ──────────────────────────────────────────────────────────
let medTimes = [];
let selectedColor = "#4f8ef7";

function switchTab(t) {
  document.querySelectorAll(".tab-btn").forEach(b=>b.classList.remove("active"));
  document.querySelectorAll(".tab-panel").forEach(p=>p.classList.remove("active"));
  document.getElementById("tab-"+t).classList.add("active");
  event.currentTarget.classList.add("active");
  if(t==="stats") loadStreak();
  if(t==="meds") loadMeds();
  if(t==="today") loadTodayDoses();
}

async function loadTodayDoses() {
  const res = await fetch("/api/doses/today");
  const doses = await res.json();
  const listEl = document.getElementById("dose-list");
  const sumEl = document.getElementById("today-summary");

  const total = doses.length;
  const taken = doses.filter(d=>d.status==="taken").length;
  const overdue = doses.filter(d=>d.status==="overdue").length;
  const upcoming = doses.filter(d=>d.status==="upcoming").length;
  const pct = total ? Math.round(taken/total*100) : 0;

  sumEl.innerHTML = total===0 ? `<div style="color:var(--muted);font-size:14px">No medicines scheduled today. Add a medicine to get started.</div>` :
    `<div style="text-align:center;min-width:80px">
      <div style="font-size:28px;font-weight:700;color:var(--accent)">${pct}%</div>
      <div style="font-size:12px;color:var(--muted)">taken today</div>
    </div>
    <div style="flex:1;display:flex;gap:16px;flex-wrap:wrap;align-items:center">
      <div><span style="color:var(--green);font-weight:600">${taken}</span> <span style="color:var(--muted);font-size:13px">taken</span></div>
      <div><span style="color:var(--red);font-weight:600">${overdue}</span> <span style="color:var(--muted);font-size:13px">overdue</span></div>
      <div><span style="color:var(--accent);font-weight:600">${upcoming}</span> <span style="color:var(--muted);font-size:13px">upcoming</span></div>
    </div>`;

  if(doses.length===0) {
    listEl.innerHTML = `<div class="empty"><div class="icon">💊</div>No doses scheduled for today.<br>Add a medicine to track it here.</div>`;
    return;
  }
  listEl.innerHTML = doses.map(d=>`
    <div class="dose-item">
      <div class="dose-time">${d.due_time}</div>
      <div class="dose-info">
        <div class="dose-name" style="color:${d.color}">${d.name}</div>
        <div class="dose-dosage">${d.dosage||''}${d.notes?' · '+d.notes:''}</div>
        ${d.status==='overdue'&&!d.taken?`<div style="font-size:11px;color:var(--red);margin-top:2px">⚠ Overdue</div>`:''}
        ${d.taken_at?`<div style="font-size:11px;color:var(--green);margin-top:2px">✓ Taken at ${d.taken_at.split('T')[1]?.slice(0,5)||d.taken_at.slice(11,16)}</div>`:''}
      </div>
      <button class="dose-check ${d.taken?'taken':d.status==='overdue'?'overdue':''}"
        onclick="toggleDose(${d.id},${d.taken},this)">
        ${d.taken?'✓':''}
      </button>
    </div>`).join("");
}

async function toggleDose(id, taken, btn) {
  await fetch(`/api/doses/${id}/${taken?'untake':'take'}`, {method:"POST"});
  loadTodayDoses();
}

async function loadMeds() {
  const res = await fetch("/api/medicines");
  const meds = await res.json();
  const el = document.getElementById("med-list");
  if(meds.length===0) {
    el.innerHTML = `<div class="empty"><div class="icon">💊</div>No medicines added yet.<br>Click "Add Medicine" to start tracking.</div>`;
    return;
  }
  el.innerHTML = meds.map(m=>{
    const times = JSON.parse(m.times).join(", ");
    const days = m.days==='["daily"]'||m.days==='daily'?'Every day':JSON.parse(m.days).join(", ");
    return `<div class="card med-card">
      <div class="med-dot" style="background:${m.color}"></div>
      <div class="med-info">
        <div class="med-name">${m.name}${m.dosage?' <span style="color:var(--muted);font-weight:400;font-size:13px">'+m.dosage+'</span>':''}</div>
        <div class="med-meta">⏰ ${times} · ${days}${m.notes?' · '+m.notes:''}</div>
      </div>
      <button class="btn btn-danger btn-sm" onclick="deleteMed(${m.id})">Delete</button>
    </div>`;
  }).join("");
}

async function deleteMed(id) {
  if(!confirm("Delete this medicine and all its doses?")) return;
  await fetch(`/api/medicines/${id}`, {method:"DELETE"});
  loadMeds(); loadTodayDoses();
}

async function loadStreak() {
  const res = await fetch("/api/streak");
  const days = await res.json();
  const bar = document.getElementById("streak-bar");
  const tip = document.getElementById("streak-tip");
  if(days.length===0) {
    bar.innerHTML = `<div style="color:var(--muted);font-size:13px">No data yet. Start taking medicines to see your streak!</div>`;
    tip.innerHTML = "";
    return;
  }
  const avg = days.reduce((a,d)=>a+d.pct,0)/days.length;
  const colors = pct => pct>=80?"var(--green)":pct>=50?"var(--yellow)":"var(--red)";
  const dayNames = ["Sun","Mon","Tue","Wed","Thu","Fri","Sat"];
  bar.innerHTML = days.map(d=>{
    const label = dayNames[new Date(d.date).getDay()];
    return `<div class="streak-day">
      <div class="streak-fill"><div class="streak-inner" style="height:${d.pct}%;background:${colors(d.pct)}"></div></div>
      <div class="streak-label">${label}<br><b>${d.pct}%</b></div>
    </div>`;
  }).join("");
  const emoji = avg>=80?"🏆":avg>=60?"👍":"💪";
  tip.innerHTML = `<div style="font-weight:600;margin-bottom:4px">${emoji} 7-Day Average: ${Math.round(avg)}%</div>
    <div style="font-size:13px;color:var(--muted)">${avg>=80?"Excellent adherence! Keep it up.":avg>=60?"Good progress — try to catch any missed doses earlier.":"Don't give up! Consistency is key to your recovery."}</div>`;
}

// ── ADD MED MODAL ──────────────────────────────────────────────────────────────
function openAddMed() {
  medTimes = [];
  selectedColor = "#4f8ef7";
  document.getElementById("m-name").value="";
  document.getElementById("m-dosage").value="";
  document.getElementById("m-notes").value="";
  document.getElementById("m-time-input").value="";
  document.getElementById("times-list").innerHTML="";
  document.querySelectorAll(".color-swatch").forEach(s=>s.classList.remove("selected"));
  document.querySelector(".color-swatch").classList.add("selected");
  document.getElementById("add-med-modal").classList.add("open");
}
function closeAddMed() { document.getElementById("add-med-modal").classList.remove("open"); }

function addTime() {
  const t = document.getElementById("m-time-input").value;
  if(!t || medTimes.includes(t)) return;
  medTimes.push(t);
  renderTimes();
}
function removeTime(t) { medTimes = medTimes.filter(x=>x!==t); renderTimes(); }
function renderTimes() {
  document.getElementById("times-list").innerHTML = medTimes.map(t=>
    `<div class="time-chip">${t}<button onclick="removeTime('${t}')">×</button></div>`).join("");
}
function pickColor(el) {
  document.querySelectorAll(".color-swatch").forEach(s=>s.classList.remove("selected"));
  el.classList.add("selected");
  selectedColor = el.dataset.c;
}

async function saveMed() {
  const name = document.getElementById("m-name").value.trim();
  if(!name) { alert("Medicine name is required"); return; }
  if(medTimes.length===0) { alert("Add at least one time"); return; }
  const dayMap = {
    daily:["daily"],
    weekdays:["Mon","Tue","Wed","Thu","Fri"],
    weekends:["Sat","Sun"],
    alternate:["alternate"]
  };
  const daysVal = document.getElementById("m-days").value;
  await fetch("/api/medicines",{
    method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({
      name, dosage:document.getElementById("m-dosage").value,
      times:medTimes, days:dayMap[daysVal]||["daily"],
      notes:document.getElementById("m-notes").value,
      color:selectedColor
    })
  });
  closeAddMed();
  loadMeds(); loadTodayDoses();
}

// ── INIT ──────────────────────────────────────────────────────────────────────
loadTodayDoses();

// Auto-refresh today's doses every 60s so overdue status updates
setInterval(()=>{
  if(document.getElementById("tab-today").classList.contains("active")) loadTodayDoses();
}, 60000);
</script>
</body>
</html>"""

if __name__ == "__main__":
   
    app.run(debug=False, port=5050, host="0.0.0.0")