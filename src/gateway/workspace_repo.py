"""Repository for workspace and model slot operations.
All SQL operations for workspaces live here.
Supports dual-backend execution (SQLite and PostgreSQL).
"""
from datetime import datetime, timedelta, timezone
import sqlite3
from typing import Any, Optional

from src.gateway.db import (
    DB_INTEGRITY_ERRORS,
    execute_insert_returning_id,
    get_connection,
    is_postgres,
    q,
)
from src.gateway.schemas import load_registry
from src.gateway.secrets_store import encrypt_secret, decrypt_secret, get_last4


class InvalidSlotError(Exception):
    """Raised when slot number is not between 1 and 6."""
    pass


class SlotOccupiedError(Exception):
    """Raised when attempting to add a model to an already occupied slot."""
    pass


class SlotEmptyError(Exception):
    """Raised when operating on a slot that contains no model."""
    pass


class DuplicateModelNameError(Exception):
    """Raised when a model name already exists in the workspace."""
    pass


def _utc_now_str() -> str:
    """Return ISO format current UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


def slot_row_to_model(row: Any) -> dict[str, Any]:
    """Convert a database slot row into the dictionary format used across NeuroRoute.
    Preserves the exact keys expected by the routing engine:
    name, capability, context_limit, cost_per_1k, latency_ms, failure_rate, status, plus enabled.
    Includes BYOK status fields, but NEVER returns api_key_encrypted.
    Supports both sqlite3.Row and dict_row.
    """
    keys = row.keys() if hasattr(row, "keys") else row
    raw_has_key = bool(row["api_key_encrypted"]) if "api_key_encrypted" in keys and row["api_key_encrypted"] else False

    return {
        "slot": row["slot_number"],
        "slot_number": row["slot_number"],
        "name": row["model_name"],
        "model_name": row["model_name"],
        "provider": row["provider"],
        "model_id": row["model_id"],
        "capability": int(row["capability"]),
        "context_limit": int(row["context_limit"]),
        "cost_per_1k": float(row["cost_per_1k"]),
        "latency_ms": int(row["latency_ms"]),
        "failure_rate": float(row["failure_rate"]),
        "status": row["status"],
        "enabled": bool(row["enabled"]),
        "execution_mode": row["execution_mode"] if "execution_mode" in keys and row["execution_mode"] else "simulated",
        "has_key": raw_has_key,
        "key_last4": row["key_last4"] if "key_last4" in keys else None,
        "cost_per_1k_input": float(row["cost_per_1k_input"]) if "cost_per_1k_input" in keys and row["cost_per_1k_input"] is not None else None,
        "cost_per_1k_output": float(row["cost_per_1k_output"]) if "cost_per_1k_output" in keys and row["cost_per_1k_output"] is not None else None,
        "max_output_tokens": int(row["max_output_tokens"]) if "max_output_tokens" in keys and row["max_output_tokens"] is not None else None,
        "status_reason": row["status_reason"] if "status_reason" in keys else None,
        "last_measured_latency_ms": int(row["last_measured_latency_ms"]) if "last_measured_latency_ms" in keys and row["last_measured_latency_ms"] is not None else None,
        "created_at": (
            row["created_at"].isoformat()
            if hasattr(row["created_at"], "isoformat")
            else (str(row["created_at"]) if row["created_at"] is not None else None)
        ),
        "updated_at": (
            row["updated_at"].isoformat()
            if hasattr(row["updated_at"], "isoformat")
            else (str(row["updated_at"]) if row["updated_at"] is not None else None)
        ),
    }


def create_workspace(token_hash: str, ttl_days: int = 30) -> int:
    """Create a new workspace row and return its ID."""
    now = datetime.now(timezone.utc)
    created_at = now.isoformat()
    expires_at = (now + timedelta(days=ttl_days)).isoformat()

    sql_sqlite = """
        INSERT INTO workspaces(session_token_hash, created_at, last_seen_at, expires_at)
        VALUES (?, ?, ?, ?)
    """
    sql_postgres = """
        INSERT INTO workspaces(session_token_hash, created_at, last_seen_at, expires_at)
        VALUES (%s, %s, %s, %s)
        RETURNING id
    """

    with get_connection() as conn:
        with conn:
            return execute_insert_returning_id(
                conn,
                sql_sqlite,
                sql_postgres,
                (token_hash, created_at, created_at, expires_at),
            )


def seed_default_models(conn: Any, workspace_id: int) -> None:
    """Seed the default 6 models into slots 1-6 for a newly created workspace.
    Runs inside the caller's active connection and transaction.
    """
    now = _utc_now_str()
    defaults = load_registry()
    sql_sqlite = """
        INSERT INTO model_slots(
            workspace_id, slot_number, model_name, provider, model_id,
            capability, context_limit, cost_per_1k, latency_ms,
            failure_rate, status, enabled, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    sql_postgres = """
        INSERT INTO model_slots(
            workspace_id, slot_number, model_name, provider, model_id,
            capability, context_limit, cost_per_1k, latency_ms,
            failure_rate, status, enabled, created_at, updated_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """
    query = q(sql_sqlite, sql_postgres)

    for slot_idx, model in enumerate(defaults[:6], start=1):
        conn.execute(
            query,
            (
                workspace_id,
                slot_idx,
                model["name"],
                "simulated",
                model["name"],
                model["capability"],
                model["context_limit"],
                model["cost_per_1k"],
                model["latency_ms"],
                model.get("failure_rate", 0.0),
                model.get("status", "up"),
                1,
                now,
                now,
            ),
        )


def create_workspace_with_defaults(token_hash: str, ttl_days: int = 30) -> int:
    """Create workspace and seed default models inside a single transaction."""
    now = datetime.now(timezone.utc)
    created_at = now.isoformat()
    expires_at = (now + timedelta(days=ttl_days)).isoformat()

    sql_sqlite = """
        INSERT INTO workspaces(session_token_hash, created_at, last_seen_at, expires_at)
        VALUES (?, ?, ?, ?)
    """
    sql_postgres = """
        INSERT INTO workspaces(session_token_hash, created_at, last_seen_at, expires_at)
        VALUES (%s, %s, %s, %s)
        RETURNING id
    """

    with get_connection() as conn:
        with conn:
            workspace_id = execute_insert_returning_id(
                conn,
                sql_sqlite,
                sql_postgres,
                (token_hash, created_at, created_at, expires_at),
            )
            seed_default_models(conn, workspace_id)
            return workspace_id


SLIDING_WINDOW_DAYS = 30
SLIDING_THROTTLE_SECONDS = 3600  # 1 hour write throttle


def _to_utc_datetime(val: Any) -> datetime:
    """Normalize SQLite ISO string or PostgreSQL datetime object to UTC datetime.
    Fails safely by returning epoch/min datetime if val is None or unparseable.
    """
    if val is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    if isinstance(val, datetime):
        if val.tzinfo is None:
            return val.replace(tzinfo=timezone.utc)
        return val.astimezone(timezone.utc)
    s = str(val).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def get_workspace_by_token_hash(
    token_hash: str,
    slide_expiration: bool = True,
    throttle_seconds: int = SLIDING_THROTTLE_SECONDS,
) -> Optional[dict[str, Any]]:
    """Retrieve workspace by session token hash if not expired.
    
    If slide_expiration is True and the elapsed time since last_seen_at exceeds
    throttle_seconds (default 1 hour), atomically advances last_seen_at and
    expires_at to UTC now + 30 days.
    
    Semantic Decision:
    last_seen_at represents the timestamp of the last successful sliding-expiration
    update (throttled to at most once per throttle_seconds). This avoids write
    amplification on high-frequency requests while keeping last_seen_at within 1 hour
    of real activity.
    
    Safeguards:
    - Atomically updates WHERE id = :id AND expires_at > :now to prevent reviving expired workspaces.
    - Checks cursor.rowcount: if 0 rows are affected (e.g. concurrent expiration), does NOT report
      success and returns None so caller can safely handle session re-resolution.
    """
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    with get_connection() as conn:
        with conn:
            cursor = conn.execute(
                q(
                    "SELECT * FROM workspaces WHERE session_token_hash = ? AND expires_at > ?",
                    "SELECT * FROM workspaces WHERE session_token_hash = %s AND expires_at > %s",
                ),
                (token_hash, now_iso),
            )
            row = cursor.fetchone()
            if not row:
                return None

            ws_dict = dict(row)
            ws_id = ws_dict["id"]

            if not slide_expiration:
                ws_dict["_slid"] = False
                return ws_dict

            # Check if write is needed based on throttling or approaching expiration
            last_seen_val = ws_dict.get("last_seen_at") or ws_dict.get("created_at")
            last_seen_dt = _to_utc_datetime(last_seen_val)
            elapsed_seconds = (now - last_seen_dt).total_seconds()

            expires_val = ws_dict.get("expires_at")
            expires_at_utc = _to_utc_datetime(expires_val)
            remaining_seconds = (expires_at_utc - now).total_seconds()

            if elapsed_seconds >= throttle_seconds or remaining_seconds <= 86400:
                new_expires_dt = now + timedelta(days=SLIDING_WINDOW_DAYS)
                new_expires_iso = new_expires_dt.isoformat()

                cur = conn.execute(
                    q(
                        """
                        UPDATE workspaces
                        SET last_seen_at = ?, expires_at = ?
                        WHERE id = ? AND expires_at > ?
                        """,
                        """
                        UPDATE workspaces
                        SET last_seen_at = %s, expires_at = %s
                        WHERE id = %s AND expires_at > %s
                        """,
                    ),
                    (now_iso, new_expires_iso, ws_id, now_iso),
                )

                # Check affected-row count: if 0 rows were affected, do not report success.
                if cur.rowcount <= 0:
                    return None

                ws_dict["last_seen_at"] = now_iso
                ws_dict["expires_at"] = new_expires_iso
                ws_dict["_slid"] = True
            else:
                ws_dict["_slid"] = False

            return ws_dict


def list_slots(workspace_id: int) -> list[dict[str, Any]]:
    """Return exactly 6 slot entries (slots 1 to 6) for the workspace.
    Occupied slots contain the model dict; empty slots have model=None.
    """
    with get_connection() as conn:
        cursor = conn.execute(
            q(
                """
                SELECT * FROM model_slots
                WHERE workspace_id = ?
                ORDER BY slot_number ASC
                """,
                """
                SELECT * FROM model_slots
                WHERE workspace_id = %s
                ORDER BY slot_number ASC
                """,
            ),
            (workspace_id,),
        )
        rows = cursor.fetchall()

    slots_map = {row["slot_number"]: row for row in rows}
    result = []
    for slot_num in range(1, 7):
        if slot_num in slots_map:
            result.append({
                "slot": slot_num,
                "empty": False,
                "model": slot_row_to_model(slots_map[slot_num]),
            })
        else:
            result.append({
                "slot": slot_num,
                "empty": True,
                "model": None,
            })
    return result


def get_slot(workspace_id: int, slot: int) -> Optional[dict[str, Any]]:
    """Retrieve the model in a given slot or None if empty."""
    if slot < 1 or slot > 6:
        raise InvalidSlotError(f"Slot number must be between 1 and 6, got {slot}")
    with get_connection() as conn:
        cursor = conn.execute(
            q(
                """
                SELECT * FROM model_slots
                WHERE workspace_id = ? AND slot_number = ?
                """,
                """
                SELECT * FROM model_slots
                WHERE workspace_id = %s AND slot_number = %s
                """,
            ),
            (workspace_id, slot),
        )
        row = cursor.fetchone()
        return slot_row_to_model(row) if row else None


def _extract_key_data(data: dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    """Extract, encrypt raw key and get last4 digits."""
    api_key_val = data.get("api_key")
    if hasattr(api_key_val, "get_secret_value"):
        raw_key = api_key_val.get_secret_value()
    elif api_key_val is not None:
        raw_key = str(api_key_val)
    else:
        raw_key = ""
    raw_key = raw_key.strip()
    if raw_key:
        return encrypt_secret(raw_key), get_last4(raw_key)
    return None, None


def add_model(workspace_id: int, slot: int, data: dict[str, Any]) -> dict[str, Any]:
    """Add a model to an empty slot in the workspace."""
    if slot < 1 or slot > 6:
        raise InvalidSlotError(f"Slot number must be between 1 and 6, got {slot}")

    now = _utc_now_str()
    execution_mode = data.get("execution_mode", "simulated")
    api_key_encrypted, key_last4 = _extract_key_data(data)

    cost_in = data.get("cost_per_1k_input")
    cost_out = data.get("cost_per_1k_output")
    max_out = data.get("max_output_tokens") if data.get("max_output_tokens") is not None else 512
    status_reason = data.get("status_reason")
    last_latency = data.get("last_measured_latency_ms")

    sql_sqlite = """
        INSERT INTO model_slots(
            workspace_id, slot_number, model_name, provider, model_id,
            capability, context_limit, cost_per_1k, latency_ms,
            failure_rate, status, enabled, execution_mode,
            api_key_encrypted, key_last4, cost_per_1k_input,
            cost_per_1k_output, max_output_tokens, status_reason,
            last_measured_latency_ms, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    sql_postgres = """
        INSERT INTO model_slots(
            workspace_id, slot_number, model_name, provider, model_id,
            capability, context_limit, cost_per_1k, latency_ms,
            failure_rate, status, enabled, execution_mode,
            api_key_encrypted, key_last4, cost_per_1k_input,
            cost_per_1k_output, max_output_tokens, status_reason,
            last_measured_latency_ms, created_at, updated_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """

    with get_connection() as conn:
        try:
            with conn:
                conn.execute(
                    q(sql_sqlite, sql_postgres),
                    (
                        workspace_id,
                        slot,
                        data["model_name"],
                        data.get("provider", "simulated"),
                        data["model_id"],
                        data["capability"],
                        data["context_limit"],
                        data["cost_per_1k"],
                        data["latency_ms"],
                        data.get("failure_rate", 0.0),
                        data.get("status", "up"),
                        1 if data.get("enabled", True) else 0,
                        execution_mode,
                        api_key_encrypted,
                        key_last4,
                        cost_in,
                        cost_out,
                        max_out,
                        status_reason,
                        last_latency,
                        now,
                        now,
                    ),
                )
        except DB_INTEGRITY_ERRORS as e:
            err_msg = str(e).lower()
            if "slot_number" in err_msg:
                raise SlotOccupiedError(f"Slot {slot} is already occupied.") from e
            if "model_name" in err_msg:
                raise DuplicateModelNameError(
                    f"Model with name '{data['model_name']}' already exists in workspace."
                ) from e
            # Unambiguous fallback check via DB
            cursor = conn.execute(
                q(
                    "SELECT slot_number, model_name FROM model_slots WHERE workspace_id = ?",
                    "SELECT slot_number, model_name FROM model_slots WHERE workspace_id = %s",
                ),
                (workspace_id,),
            )
            for r in cursor.fetchall():
                if r["slot_number"] == slot:
                    raise SlotOccupiedError(f"Slot {slot} is already occupied.") from e
                if r["model_name"] == data["model_name"]:
                    raise DuplicateModelNameError(
                        f"Model with name '{data['model_name']}' already exists in workspace."
                    ) from e
            raise

    return get_slot(workspace_id, slot)  # type: ignore


def replace_model(workspace_id: int, slot: int, data: dict[str, Any]) -> dict[str, Any]:
    """Fully replace the model in an occupied slot (slot number unchanged).
    Preserves existing API key if key is omitted/blank and provider is unchanged.
    """
    if slot < 1 or slot > 6:
        raise InvalidSlotError(f"Slot number must be between 1 and 6, got {slot}")

    now = _utc_now_str()
    with get_connection() as conn:
        with conn:
            cursor = conn.execute(
                q(
                    "SELECT * FROM model_slots WHERE workspace_id = ? AND slot_number = ?",
                    "SELECT * FROM model_slots WHERE workspace_id = %s AND slot_number = %s",
                ),
                (workspace_id, slot),
            )
            existing_row = cursor.fetchone()
            if not existing_row:
                raise SlotEmptyError(f"Slot {slot} is empty; cannot replace.")

            new_encrypted, new_last4 = _extract_key_data(data)
            provider = data.get("provider", "simulated")
            if new_encrypted is not None:
                api_key_encrypted = new_encrypted
                key_last4 = new_last4
            elif provider == existing_row["provider"]:
                # Preserve existing key
                api_key_encrypted = existing_row["api_key_encrypted"]
                key_last4 = existing_row["key_last4"]
            else:
                api_key_encrypted = None
                key_last4 = None

            execution_mode = data.get("execution_mode", "simulated")
            cost_in = data.get("cost_per_1k_input")
            cost_out = data.get("cost_per_1k_output")
            max_out = data.get("max_output_tokens") if data.get("max_output_tokens") is not None else (existing_row.get("max_output_tokens") or 512)
            status_reason = data.get("status_reason")
            last_latency = data.get("last_measured_latency_ms")

            sql_sqlite = """
                UPDATE model_slots SET
                    model_name = ?,
                    provider = ?,
                    model_id = ?,
                    capability = ?,
                    context_limit = ?,
                    cost_per_1k = ?,
                    latency_ms = ?,
                    failure_rate = ?,
                    status = ?,
                    enabled = ?,
                    execution_mode = ?,
                    api_key_encrypted = ?,
                    key_last4 = ?,
                    cost_per_1k_input = ?,
                    cost_per_1k_output = ?,
                    max_output_tokens = ?,
                    status_reason = ?,
                    last_measured_latency_ms = ?,
                    updated_at = ?
                WHERE workspace_id = ? AND slot_number = ?
            """
            sql_postgres = """
                UPDATE model_slots SET
                    model_name = %s,
                    provider = %s,
                    model_id = %s,
                    capability = %s,
                    context_limit = %s,
                    cost_per_1k = %s,
                    latency_ms = %s,
                    failure_rate = %s,
                    status = %s,
                    enabled = %s,
                    execution_mode = %s,
                    api_key_encrypted = %s,
                    key_last4 = %s,
                    cost_per_1k_input = %s,
                    cost_per_1k_output = %s,
                    max_output_tokens = %s,
                    status_reason = %s,
                    last_measured_latency_ms = %s,
                    updated_at = %s
                WHERE workspace_id = %s AND slot_number = %s
            """

            try:
                conn.execute(
                    q(sql_sqlite, sql_postgres),
                    (
                        data["model_name"],
                        provider,
                        data["model_id"],
                        data["capability"],
                        data["context_limit"],
                        data["cost_per_1k"],
                        data["latency_ms"],
                        data.get("failure_rate", 0.0),
                        data.get("status", "up"),
                        1 if data.get("enabled", True) else 0,
                        execution_mode,
                        api_key_encrypted,
                        key_last4,
                        cost_in,
                        cost_out,
                        max_out,
                        status_reason,
                        last_latency,
                        now,
                        workspace_id,
                        slot,
                    ),
                )
            except DB_INTEGRITY_ERRORS as e:
                if "model_name" in str(e).lower():
                    raise DuplicateModelNameError(
                        f"Model with name '{data['model_name']}' already exists in workspace."
                    ) from e
                raise

    return get_slot(workspace_id, slot)  # type: ignore


def patch_model(workspace_id: int, slot: int, update_data: dict[str, Any]) -> dict[str, Any]:
    """Partially update status, latency_ms, failure_rate, status_reason, or last_measured_latency_ms."""
    if slot < 1 or slot > 6:
        raise InvalidSlotError(f"Slot number must be between 1 and 6, got {slot}")

    with get_connection() as conn:
        with conn:
            cursor = conn.execute(
                q(
                    "SELECT id FROM model_slots WHERE workspace_id = ? AND slot_number = ?",
                    "SELECT id FROM model_slots WHERE workspace_id = %s AND slot_number = %s",
                ),
                (workspace_id, slot),
            )
            if not cursor.fetchone():
                raise SlotEmptyError(f"Slot {slot} is empty; cannot update.")

            ph = "%s" if is_postgres() else "?"
            set_clauses = []
            values: list[Any] = []
            for field in ("status", "latency_ms", "failure_rate", "status_reason", "last_measured_latency_ms"):
                if field in update_data and update_data[field] is not None:
                    set_clauses.append(f"{field} = {ph}")
                    values.append(update_data[field])

            if set_clauses:
                set_clauses.append(f"updated_at = {ph}")
                values.append(_utc_now_str())
                values.extend([workspace_id, slot])
                query = f"UPDATE model_slots SET {', '.join(set_clauses)} WHERE workspace_id = {ph} AND slot_number = {ph}"
                conn.execute(query, tuple(values))

    return get_slot(workspace_id, slot)  # type: ignore


def remove_model(workspace_id: int, slot: int) -> dict[str, Any]:
    """Remove model from slot, leaving it empty."""
    if slot < 1 or slot > 6:
        raise InvalidSlotError(f"Slot number must be between 1 and 6, got {slot}")

    with get_connection() as conn:
        with conn:
            cursor = conn.execute(
                q(
                    "DELETE FROM model_slots WHERE workspace_id = ? AND slot_number = ?",
                    "DELETE FROM model_slots WHERE workspace_id = %s AND slot_number = %s",
                ),
                (workspace_id, slot),
            )
            if cursor.rowcount == 0:
                raise SlotEmptyError(f"Slot {slot} is already empty.")

    return {"message": f"Model removed from slot {slot}", "slot": slot}


def toggle_enabled(workspace_id: int, slot: int) -> dict[str, Any]:
    """Toggle enabled flag (0 <-> 1) for the model in the given slot."""
    if slot < 1 or slot > 6:
        raise InvalidSlotError(f"Slot number must be between 1 and 6, got {slot}")

    now = _utc_now_str()
    with get_connection() as conn:
        with conn:
            cursor = conn.execute(
                q(
                    "SELECT id FROM model_slots WHERE workspace_id = ? AND slot_number = ?",
                    "SELECT id FROM model_slots WHERE workspace_id = %s AND slot_number = %s",
                ),
                (workspace_id, slot),
            )
            if not cursor.fetchone():
                raise SlotEmptyError(f"Slot {slot} is empty; cannot toggle.")

            conn.execute(
                q(
                    """
                    UPDATE model_slots
                    SET enabled = CASE WHEN enabled = 1 THEN 0 ELSE 1 END,
                        updated_at = ?
                    WHERE workspace_id = ? AND slot_number = ?
                    """,
                    """
                    UPDATE model_slots
                    SET enabled = CASE WHEN enabled = 1 THEN 0 ELSE 1 END,
                        updated_at = %s
                    WHERE workspace_id = %s AND slot_number = %s
                    """,
                ),
                (now, workspace_id, slot),
            )

    return get_slot(workspace_id, slot)  # type: ignore


def record_test_run(
    workspace_id: int,
    slot_number: int,
    success: bool,
    latency_ms: int,
    message: str,
) -> None:
    """Record a model simulation test run into model_test_runs table."""
    now = _utc_now_str()
    with get_connection() as conn:
        with conn:
            conn.execute(
                q(
                    """
                    INSERT INTO model_test_runs(
                        workspace_id, slot_number, success, latency_ms, message, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    """
                    INSERT INTO model_test_runs(
                        workspace_id, slot_number, success, latency_ms, message, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                ),
                (
                    workspace_id,
                    slot_number,
                    1 if success else 0,
                    latency_ms,
                    message,
                    now,
                ),
            )


def load_routing_models(workspace_id: int) -> list[dict[str, Any]]:
    """Return list of models for OCCUPIED slots in the workspace ordered by slot_number.
    Each model includes standard routing keys, plus enabled (bool) and slot.
    """
    with get_connection() as conn:
        cursor = conn.execute(
            q(
                """
                SELECT * FROM model_slots
                WHERE workspace_id = ?
                ORDER BY slot_number ASC
                """,
                """
                SELECT * FROM model_slots
                WHERE workspace_id = %s
                ORDER BY slot_number ASC
                """,
            ),
            (workspace_id,),
        )
        rows = cursor.fetchall()
        return [slot_row_to_model(row) for row in rows]


def reset_workspace(workspace_id: int) -> list[dict[str, Any]]:
    """Reset workspace fleet: in ONE transaction deletes all slots and returns empty list []."""
    with get_connection() as conn:
        with conn:
            conn.execute(
                q(
                    "DELETE FROM model_slots WHERE workspace_id = ?",
                    "DELETE FROM model_slots WHERE workspace_id = %s",
                ),
                (workspace_id,),
            )
    return []


def load_demo_models(workspace_id: int) -> list[dict[str, Any]]:
    """Fill only empty slots (1-6) with simulated registry models, never overwriting occupied ones.
    Returns the full 6-slot structure (list_slots format).
    """
    now = _utc_now_str()
    defaults = load_registry()
    with get_connection() as conn:
        with conn:
            cursor = conn.execute(
                q(
                    "SELECT slot_number, model_name FROM model_slots WHERE workspace_id = ?",
                    "SELECT slot_number, model_name FROM model_slots WHERE workspace_id = %s",
                ),
                (workspace_id,),
            )
            existing_rows = cursor.fetchall()
            occupied_slots = {r["slot_number"] for r in existing_rows}
            existing_names = {r["model_name"] for r in existing_rows}

            sql_sqlite = """
                INSERT INTO model_slots(
                    workspace_id, slot_number, model_name, provider, model_id,
                    capability, context_limit, cost_per_1k, latency_ms,
                    failure_rate, status, enabled, execution_mode,
                    cost_per_1k_input, cost_per_1k_output, max_output_tokens,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
            sql_postgres = """
                INSERT INTO model_slots(
                    workspace_id, slot_number, model_name, provider, model_id,
                    capability, context_limit, cost_per_1k, latency_ms,
                    failure_rate, status, enabled, execution_mode,
                    cost_per_1k_input, cost_per_1k_output, max_output_tokens,
                    created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """
            insert_q = q(sql_sqlite, sql_postgres)

            for slot_idx, model in enumerate(defaults[:6], start=1):
                if slot_idx in occupied_slots:
                    continue
                model_name = model["name"]
                if model_name in existing_names:
                    model_name = f"{model_name}-{slot_idx}"

                conn.execute(
                    insert_q,
                    (
                        workspace_id,
                        slot_idx,
                        model_name,
                        "simulated",
                        model["name"],
                        model["capability"],
                        model["context_limit"],
                        model["cost_per_1k"],
                        model["latency_ms"],
                        model.get("failure_rate", 0.0),
                        model.get("status", "up"),
                        1,
                        "simulated",
                        model["cost_per_1k"],
                        model["cost_per_1k"],
                        min(model.get("max_output_tokens", 2048), 4096),
                        now,
                        now,
                    ),
                )
                existing_names.add(model_name)
    return list_slots(workspace_id)


def get_slot_connection_data(workspace_id: int, model_name: str) -> Optional[dict[str, Any]]:
    """Fetch raw provider connection data (including decrypted API key) for an active model.
    Used strictly in-memory during live call execution; never exposed over HTTP.
    """
    with get_connection() as conn:
        cursor = conn.execute(
            q(
                """
                SELECT slot_number, provider, model_id, execution_mode,
                       api_key_encrypted, max_output_tokens,
                       cost_per_1k_input, cost_per_1k_output
                FROM model_slots
                WHERE workspace_id = ? AND model_name = ?
                """,
                """
                SELECT slot_number, provider, model_id, execution_mode,
                       api_key_encrypted, max_output_tokens,
                       cost_per_1k_input, cost_per_1k_output
                FROM model_slots
                WHERE workspace_id = %s AND model_name = %s
                """,
            ),
            (workspace_id, model_name),
        )
        row = cursor.fetchone()
        if not row:
            return None

        decrypted_key = None
        if row["api_key_encrypted"]:
            decrypted_key = decrypt_secret(row["api_key_encrypted"])

        return {
            "slot_number": row["slot_number"],
            "provider": row["provider"],
            "model_id": row["model_id"],
            "execution_mode": row["execution_mode"] or "simulated",
            "api_key": decrypted_key,
            "max_output_tokens": row["max_output_tokens"],
            "cost_per_1k_input": row["cost_per_1k_input"],
            "cost_per_1k_output": row["cost_per_1k_output"],
        }


def update_slot_status(workspace_id: int, slot: int, status: str, reason: Optional[str] = None) -> None:
    """Update status ('up', 'down') and status_reason for a slot."""
    if status not in ("up", "down"):
        status = "down" if "down" in status else "up"
    now = _utc_now_str()
    with get_connection() as conn:
        with conn:
            conn.execute(
                q(
                    """
                    UPDATE model_slots
                    SET status = ?, status_reason = ?, updated_at = ?
                    WHERE workspace_id = ? AND slot_number = ?
                    """,
                    """
                    UPDATE model_slots
                    SET status = %s, status_reason = %s, updated_at = %s
                    WHERE workspace_id = %s AND slot_number = %s
                    """,
                ),
                (status, reason, now, workspace_id, slot),
            )


def update_measured_latency(workspace_id: int, slot: int, latency_ms: int) -> None:
    """Update last_measured_latency_ms for a slot."""
    now = _utc_now_str()
    with get_connection() as conn:
        with conn:
            conn.execute(
                q(
                    """
                    UPDATE model_slots
                    SET last_measured_latency_ms = ?, updated_at = ?
                    WHERE workspace_id = ? AND slot_number = ?
                    """,
                    """
                    UPDATE model_slots
                    SET last_measured_latency_ms = %s, updated_at = %s
                    WHERE workspace_id = %s AND slot_number = %s
                    """,
                ),
                (latency_ms, now, workspace_id, slot),
            )


def find_slot_by_model_name(workspace_id: int, name: str) -> Optional[int]:
    """Find the slot number for a model by its name within the workspace.
    Returns slot number (1-6) or None if not found.
    """
    with get_connection() as conn:
        cursor = conn.execute(
            q(
                "SELECT slot_number FROM model_slots WHERE workspace_id = ? AND model_name = ?",
                "SELECT slot_number FROM model_slots WHERE workspace_id = %s AND model_name = %s",
            ),
            (workspace_id, name),
        )
        row = cursor.fetchone()
        return row["slot_number"] if row else None
