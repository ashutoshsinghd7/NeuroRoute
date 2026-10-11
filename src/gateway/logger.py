"""SQLite logging (CP9). One row per request, including refusals."""
import json
import os
import sqlite3
import time

DB_PATH = os.environ.get("NEUROROUTE_DB", "/tmp/neuroroute.db" if os.environ.get("VERCEL") else "neuroroute.db")


def init_db():
    try:
        with sqlite3.connect(DB_PATH) as c:
            c.execute("""CREATE TABLE IF NOT EXISTS requests(
                id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT, prompt_preview TEXT, constraints TEXT,
                estimated_level INTEGER, confidence REAL, chosen_model TEXT, status TEXT,
                total_latency INTEGER, cost REAL, attempts_json TEXT, rejected_json TEXT)""")
    except Exception:
        pass


def log_request(req: dict, resp: dict):
    try:
        t, cx = resp["trace"], resp["trace"]["complexity"]
        constraints = {k: v for k, v in req.items() if k != "prompt"}
        with sqlite3.connect(DB_PATH) as c:
            c.execute("INSERT INTO requests(time,prompt_preview,constraints,estimated_level,confidence,"
                      "chosen_model,status,total_latency,cost,attempts_json,rejected_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                      (time.strftime("%H:%M:%S"), req["prompt"][:60], json.dumps(constraints), cx["level"],
                       cx["confidence"], resp["chosen_model"], resp["status"], t["total_latency_ms"],
                       t["cost"], json.dumps(t["attempts"]), json.dumps(t["rejected"])))
    except Exception:
        pass


def get_history(limit=50):
    try:
        with sqlite3.connect(DB_PATH) as c:
            c.row_factory = sqlite3.Row
            rows = c.execute("SELECT * FROM requests ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []

