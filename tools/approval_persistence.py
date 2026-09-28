"""Durable storage for RELEASED gateway approval cards (TJS-270).

TJS-226 lets a dangerous-command approval RELEASE the agent thread instead of
parking it: the card stays queued, the turn ends, and the user's later tap
resumes the run through a wake turn. That makes the card outlive its turn --
but it lived only in ``tools.approval._gateway_queues``, a module dict, so it
did not outlive the PROCESS. A gateway restart between "card sent" and "user
taps" silently destroyed a live approval: the platform message and its buttons
were still on screen, the tap found nothing, and the run that was waiting on it
never resumed.

This module is the disk half of that lifecycle. It stores only what a restarted
process needs to make an already-sent card answerable again:

* the card payload exactly as it was handed to the platform (already redacted
  by the caller -- nothing is redacted here, and nothing raw is ever stored),
* the operation fingerprint, so a "once" answer can still be redeemed by the
  resumed turn,
* an optional platform reference (e.g. Telegram's callback id) so the adapter
  that owns the buttons can rehydrate its own id -> session map.

Records are removed as soon as the approval is resolved or its session is
cleared, so the file holds pending cards only. It is written 0600 under
``$HERMES_HOME/state`` and is inert for any gateway that never releases a
thread (``approvals.release_thread: false``), which is the fleet default.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

STORE_FILENAME = "released-approvals.json"
SALT_FILENAME = "approval-fingerprint.salt"

# A released card has no deadline (that is the point of TJS-222/226), but a
# record nobody ever answers must not be replayed into a session forever.
# Anything older than this is dropped on load.
MAX_RECORD_AGE_SECONDS = 30 * 24 * 60 * 60

# Serialises the read-modify-write cycle within this process. Cross-process
# safety comes from the atomic replace below: a concurrent writer can lose a
# record, never corrupt the file.
_io_lock = threading.RLock()


def _state_dir() -> Path:
    from hermes_constants import get_hermes_home

    path = Path(get_hermes_home()) / "state"
    path.mkdir(parents=True, exist_ok=True)
    return path


def store_path() -> Path:
    """Absolute path of the released-approval store for the active profile."""
    return _state_dir() / STORE_FILENAME


def salt_path() -> Path:
    """Absolute path of the durable operation-fingerprint salt."""
    return _state_dir() / SALT_FILENAME


def _write_atomic(path: Path, payload: bytes) -> None:
    """Replace *path* with *payload*, 0600, atomically."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f"{path.name}.", suffix=".tmp")
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def load_fingerprint_salt() -> bytes:
    """Return the salt used for operation fingerprints, creating it once.

    TJS-256 salted the fingerprint with ``os.urandom`` per process, which was
    right while grants could not outlive the process. A restored card must be
    comparable against a fingerprint the NEW process computes for the retried
    operation, so the salt has to be stable across restarts -- otherwise a
    "once" answer given after a restart can never be redeemed and the user is
    asked a second time for the command they just approved.

    The digest stays non-reversible and the raw command is still never stored;
    the salt is a local 0600 file alongside the rest of the Hermes home.
    """
    path = salt_path()
    with _io_lock:
        try:
            existing = path.read_bytes()
            if len(existing) >= 16:
                return existing
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("Could not read approval fingerprint salt: %s", exc)
            return os.urandom(16)
        salt = os.urandom(16)
        try:
            _write_atomic(path, salt)
        except OSError as exc:
            # A process-local salt still works for everything except a
            # restart-restored "once" grant; degrade instead of failing the
            # approval path.
            logger.warning("Could not persist approval fingerprint salt: %s", exc)
        return salt


def _read_records() -> list[dict]:
    try:
        raw = store_path().read_bytes()
    except FileNotFoundError:
        return []
    except OSError as exc:
        logger.warning("Could not read released-approval store: %s", exc)
        return []
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        logger.warning("Released-approval store is unreadable, ignoring it: %s", exc)
        return []
    records = data.get("records") if isinstance(data, dict) else None
    return [r for r in (records or []) if isinstance(r, dict)]


def _write_records(records: list[dict]) -> None:
    payload = json.dumps(
        {"version": 1, "records": records}, ensure_ascii=False
    ).encode("utf-8")
    _write_atomic(store_path(), payload)


def _mutate(fn) -> None:
    """Apply *fn* to the record list and persist the result. Never raises."""
    try:
        with _io_lock:
            records = _read_records()
            updated = fn(records)
            if updated is None:
                return
            if not updated:
                try:
                    store_path().unlink()
                    return
                except FileNotFoundError:
                    return
                except OSError:
                    pass
            _write_records(updated)
    except Exception:
        # Persistence is a durability improvement layered onto a working
        # in-process path. Failing the approval itself because the disk is
        # unhappy would be a worse outcome than losing restart durability.
        logger.warning("Released-approval store update failed", exc_info=True)


def record_released(
    session_key: str,
    request_id: str,
    data: dict,
    fingerprint: str = "",
    platform_ref: Optional[dict] = None,
) -> None:
    """Persist one released card so a restart can still answer it."""
    if not session_key or not request_id:
        return
    record = {
        "session_key": session_key,
        "request_id": request_id,
        "data": dict(data or {}),
        "fingerprint": fingerprint or "",
        "platform_ref": dict(platform_ref) if platform_ref else None,
        "created_at": time.time(),
    }

    def _apply(records: list[dict]) -> list[dict]:
        kept = [
            r for r in records
            if not (r.get("session_key") == session_key
                    and r.get("request_id") == request_id)
        ]
        kept.append(record)
        return kept

    _mutate(_apply)


def attach_platform_ref(session_key: str, request_id: str, ref: dict) -> None:
    """Attach the platform's own handle (button/callback id) to a record.

    The adapter learns its handle only after the card has been sent, so this
    is a second write rather than part of :func:`record_released`.
    """
    if not session_key or not request_id or not ref:
        return

    def _apply(records: list[dict]) -> Optional[list[dict]]:
        touched = False
        for r in records:
            if (r.get("session_key") == session_key
                    and r.get("request_id") == request_id):
                r["platform_ref"] = dict(ref)
                touched = True
        return records if touched else None

    _mutate(_apply)


def forget_released(session_key: str, request_id: str) -> None:
    """Drop one record (the card was resolved, or never reached the user)."""
    if not session_key or not request_id:
        return

    def _apply(records: list[dict]) -> list[dict]:
        return [
            r for r in records
            if not (r.get("session_key") == session_key
                    and r.get("request_id") == request_id)
        ]

    _mutate(_apply)


def forget_session(session_key: str) -> None:
    """Drop every record for a session (session reset, /new, /stop)."""
    if not session_key:
        return

    def _apply(records: list[dict]) -> list[dict]:
        return [r for r in records if r.get("session_key") != session_key]

    _mutate(_apply)


def load_released(max_age_seconds: float = MAX_RECORD_AGE_SECONDS) -> list[dict]:
    """Return pending records, pruning anything too old to replay.

    The prune is written back so an abandoned card is dropped once rather than
    re-evaluated on every start.
    """
    now = time.time()
    fresh: list[dict] = []
    stale = 0
    try:
        with _io_lock:
            for record in _read_records():
                created = record.get("created_at")
                age = now - float(created) if isinstance(created, (int, float)) else None
                if age is not None and age > max_age_seconds:
                    stale += 1
                    continue
                if not record.get("session_key") or not record.get("request_id"):
                    stale += 1
                    continue
                fresh.append(record)
            if stale:
                if fresh:
                    _write_records(fresh)
                else:
                    try:
                        store_path().unlink()
                    except (FileNotFoundError, OSError):
                        pass
    except Exception:
        logger.warning("Could not load released-approval store", exc_info=True)
        return []
    if stale:
        logger.info(
            "Dropped %d released approval record(s) older than %.0f days",
            stale, max_age_seconds / 86400.0,
        )
    return fresh


def platform_refs_for(platform: str) -> list[dict]:
    """Pending records whose platform reference names *platform*.

    Adapters use this on connect to rebuild the id -> session map their button
    callbacks look up, so a card sent before the restart stays clickable.
    """
    out: list[dict] = []
    for record in load_released():
        ref: Any = record.get("platform_ref")
        if isinstance(ref, dict) and str(ref.get("platform") or "") == platform:
            out.append(record)
    return out
