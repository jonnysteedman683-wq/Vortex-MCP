"""Read-only SPINE organ survey and optional advisory Vortex task records.

Registry and organ roots must be explicitly configured by the server operator.
No filesystem paths are returned to MCP callers or copied into task records.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

MAX_REGISTRY_BYTES = 256 * 1024
MAX_ORGANS = 100
MAX_TASKS = 20
MAX_SENTINEL_LENGTH = 200
VALID_EXPECTED = {"present", "absent"}
VALID_CANON = {"yes", "no", "undecided"}
WORK_STATUS = {"DEGRADED", "MISSING", "UNDECIDED"}
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SAFE_ROLE = re.compile(r"^[A-Za-z][A-Za-z0-9 _.-]{0,99}$")
SECRET_COMPONENTS = {
    ".env", ".ssh", ".aws", ".azure", ".gnupg", "secrets", "credentials",
    "private", "private_keys", "tokens", "passwords",
}


def classify(expected: str, exists: bool, sentinel_present: bool, canon: str) -> str:
    if expected not in VALID_EXPECTED:
        raise ValueError("invalid expected state")
    if canon not in VALID_CANON:
        raise ValueError("invalid canon state")
    if not exists:
        return "MISSING" if expected == "present" else "ABSENT"
    if expected == "absent":
        return "UNEXPECTED"
    if not sentinel_present:
        return "DEGRADED"
    if canon == "undecided":
        return "UNDECIDED"
    return "LIVE"


def _configured_registry() -> Path:
    configured = os.environ.get("VORTEX_SPINE_REGISTRY", "").strip()
    if not configured:
        raise ValueError("SPINE registry is not configured")
    path = Path(configured).expanduser()
    if not path.is_absolute():
        raise ValueError("SPINE registry must be an absolute path")
    return path


def _safe_path(path: Path) -> Path:
    if ".." in path.parts:
        raise ValueError("SPINE paths must not contain traversal components")
    absolute = Path(os.path.abspath(path))
    # Reject symlinked components, including a symlinked registry file/root.
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        if current.is_symlink() or getattr(current, "is_junction", lambda: False)():
            raise ValueError("SPINE paths must not traverse symbolic links or junctions")
    return absolute


def _trusted_roots() -> list[Path]:
    raw = os.environ.get("VORTEX_SPINE_ORGAN_ROOTS", "").strip()
    if not raw:
        raise ValueError("SPINE organ roots are not configured")
    roots = []
    for item in raw.split(os.pathsep):
        if not item.strip():
            continue
        root = _safe_path(Path(item.strip()).expanduser())
        if not root.is_absolute() or not root.is_dir():
            raise ValueError("SPINE organ roots must be existing absolute directories")
        roots.append(root)
    if not roots:
        raise ValueError("SPINE organ roots are not configured")
    return roots


def _within(path: Path, roots: list[Path]) -> bool:
    return any(path == root or root in path.parents for root in roots)


def _sensitive_path(path: Path) -> bool:
    for component in path.parts:
        normalized = component.lower()
        if normalized in SECRET_COMPONENTS:
            return True
        if normalized.startswith(".env") or any(
            marker in normalized for marker in ("secret", "credential", "password", "token", "private_key")
        ):
            return True
        if normalized in {"id_rsa", "id_ed25519", "id_ecdsa", "id_dsa"}:
            return True
    return False


def _validate_sentinel(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > MAX_SENTINEL_LENGTH or "\\" in value:
        raise ValueError("invalid SPINE sentinel")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("invalid SPINE sentinel")
    if any(part.lower() in SECRET_COMPONENTS for part in parts):
        raise ValueError("SPINE sentinel cannot target secret paths")
    return value


def _load_registry(registry_path: str | None) -> tuple[list[dict[str, Any]], str]:
    configured = _safe_path(_configured_registry())
    supplied = configured if registry_path is None else Path(registry_path).expanduser()
    if not supplied.is_absolute():
        raise ValueError("SPINE registry path must be absolute")
    supplied = _safe_path(supplied)
    if supplied != configured:
        raise ValueError("SPINE registry path must match VORTEX_SPINE_REGISTRY")
    if not supplied.is_file():
        raise ValueError("configured SPINE registry is unavailable")
    if supplied.stat().st_size > MAX_REGISTRY_BYTES:
        raise ValueError("SPINE registry exceeds the size limit")
    roots = _trusted_roots()
    try:
        raw = supplied.read_bytes()
        document = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("SPINE registry could not be read as JSON") from exc
    if not isinstance(document, dict) or set(document) != {"organs"}:
        raise ValueError("SPINE registry must contain only an organs list")
    organs = document["organs"]
    if not isinstance(organs, list) or len(organs) > MAX_ORGANS:
        raise ValueError("SPINE registry organs must be a bounded list")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for organ in organs:
        if not isinstance(organ, dict) or set(organ) - {"id", "role", "canon", "expected", "path", "sentinel"}:
            raise ValueError("SPINE registry contains an invalid organ entry")
        organ_id, role = organ.get("id"), organ.get("role")
        if not isinstance(organ_id, str) or not SAFE_NAME.fullmatch(organ_id) or organ_id in seen:
            raise ValueError("SPINE organ IDs must be unique safe identifiers")
        if not isinstance(role, str) or not SAFE_ROLE.fullmatch(role):
            raise ValueError("SPINE organ role is invalid")
        expected, canon = organ.get("expected"), organ.get("canon", "no")
        if not isinstance(expected, str) or expected not in VALID_EXPECTED:
            raise ValueError("SPINE organ expected state is invalid")
        if not isinstance(canon, str) or canon not in VALID_CANON:
            raise ValueError("SPINE organ canon state is invalid")
        path_value = organ.get("path")
        if not isinstance(path_value, str) or not path_value or len(path_value) > 1024:
            raise ValueError("SPINE organ path is invalid")
        organ_path = _safe_path(Path(path_value).expanduser())
        if not organ_path.is_absolute() or not _within(organ_path, roots) or _sensitive_path(organ_path):
            raise ValueError("SPINE organ path is outside configured safe roots")
        sentinel = _validate_sentinel(organ.get("sentinel"))
        exists = organ_path.is_dir()
        sentinel_path = _safe_path(organ_path.joinpath(*(sentinel.split("/") if sentinel else []))) if sentinel else None
        if sentinel_path is not None and _sensitive_path(sentinel_path):
            raise ValueError("SPINE sentinel cannot target secret paths")
        if sentinel_path is not None and not _within(sentinel_path, roots):
            raise ValueError("SPINE sentinel is outside configured safe roots")
        sentinel_present = sentinel_path.is_file() if exists and sentinel_path is not None else True
        status = classify(expected, exists, sentinel_present, canon)
        rows.append({"id": organ_id, "role": role, "status": status, "canon": canon})
        seen.add(organ_id)
    return rows, hashlib.sha256(raw).hexdigest()


def survey_organs(registry_path: str | None = None) -> tuple[list[dict[str, Any]], str]:
    """Return validated, path-free organ status rows and a registry fingerprint."""
    return _load_registry(registry_path)


def advisory_task_goals(rows: list[dict[str, Any]]) -> list[tuple[str, str]]:
    return [
        (row["id"], f"SPINE advisory: review {row['status']} organ {row['id']} ({row['role']}); read-only, no repair authorized")
        for row in rows if row["status"] in WORK_STATUS
    ][:MAX_TASKS]
