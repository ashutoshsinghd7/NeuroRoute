"""Request logging (CP9). One row per request, including refusals.
Supports dual-backend execution (SQLite and PostgreSQL).
"""
import json
import logging
import time
from typing import Any, Optional

from src.gateway.db import get_backend, get_connection, q

logger = logging.getLogger(__name__)


def init_db():
    """Create the requests table if it does not already exist.
    In SQLite mode: initializes table.
    In PostgreSQL mode: schema creation is deferred to G3 DDL migrations.
    """
    if get_backend() != "sqlite":
        return

    try:
        with get_connection() as c:
            with c:
                c.execute(
                    """
                    CREATE TABLE IF NOT EXISTS requests(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        time TEXT,
                        prompt_preview TEXT,
                        constraints TEXT,
                        estimated_level INTEGER,
                        confidence REAL,
                        chosen_model TEXT,
                        status TEXT,
                        total_latency INTEGER,
                        cost REAL,
                        attempts_json TEXT,
                        rejected_json TEXT,
                        workspace_id INTEGER
                    );
                    """
                )
    except Exception as e:
        logger.error("Failed to initialize requests table: %s", e)


def log_request(req: dict[str, Any], resp: dict[str, Any], workspace_id: Optional[int] = None) -> None:
    """Log an incoming routing request and decision trace.
    Errors are logged via the logging module without interrupting the request.
    """
    try:
        t = resp.get("trace", {})
        cx = t.get("complexity", {"level": 0, "confidence": 0.0})
        constraints = {k: v for k, v in req.items() if k != "prompt"}

        sql_sqlite = """
            INSERT INTO requests(
                time, prompt_preview, constraints, estimated_level, confidence,
                chosen_model, status, total_latency, cost, attempts_json, rejected_json,
                workspace_id
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        sql_postgres = """
            INSERT INTO requests(
                time, prompt_preview, constraints, estimated_level, confidence,
                chosen_model, status, total_latency, cost, attempts_json, rejected_json,
                workspace_id
            ) VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """

        with get_connection() as c:
            with c:
                c.execute(
                    q(sql_sqlite, sql_postgres),
                    (
                        time.strftime("%H:%M:%S"),
                        req.get("prompt", "")[:60],
                        json.dumps(constraints),
                        cx.get("level", 0),
                        cx.get("confidence", 0.0),
                        resp.get("chosen_model"),
                        resp.get("status"),
                        t.get("total_latency_ms", 0),
                        t.get("cost", 0.0),
                        json.dumps(t.get("attempts", [])),
                        json.dumps(t.get("rejected", [])),
                        workspace_id,
                    ),
                )
    except Exception as e:
        logger.error("Failed to log request: %s", e)


def get_history(limit: int = 50, workspace_id: Optional[int] = None) -> list[dict[str, Any]]:
    """Retrieve logged requests for a specific workspace.
    Returns empty list if workspace_id is None. Old rows with NULL workspace_id are never exposed.
    """
    if workspace_id is None:
        return []

    try:
        sql = q(
            "SELECT * FROM requests WHERE workspace_id = ? ORDER BY id DESC LIMIT ?",
            "SELECT * FROM requests WHERE workspace_id = %s ORDER BY id DESC LIMIT %s",
        )
        with get_connection() as c:
            cursor = c.execute(sql, (workspace_id, limit))
            rows = cursor.fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        logger.error("Failed to retrieve request history: %s", e)
        return []
