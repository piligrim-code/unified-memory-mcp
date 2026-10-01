from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


REQUIRED_TABLES = {"memories", "memories_fts", "chunks", "handoffs"}
PROFILE_LIMITS = {
    "max_records": (1, 1_000_000, "UNIFIED_MEMORY_MAX_MEMORIES"),
    "max_content_bytes": (1, 10 * 1024 * 1024 * 1024, "UNIFIED_MEMORY_MAX_BYTES"),
    "retention_days": (1, 3650, "UNIFIED_MEMORY_RETENTION_DAYS"),
    "max_handoffs": (1, 100_000, "UNIFIED_MEMORY_MAX_HANDOFFS"),
    "handoff_retention_days": (1, 3650, "UNIFIED_MEMORY_HANDOFF_RETENTION_DAYS"),
}
DEFAULT_DEPLOYMENT_PROFILE = {
    "schema_version": 1,
    "mode": "scoped-single-tenant",
    "allowed_scopes": ["global", "mcp-memory"],
    "allowed_projects": ["mcp-memory"],
    "limits": {
        "max_records": 10_000,
        "max_content_bytes": 50 * 1024 * 1024,
        "retention_days": 365,
        "max_handoffs": 2_000,
        "handoff_retention_days": 90,
    },
}
BROAD_WINDOWS_SIDS = {
    "S-1-1-0",       # Everyone
    "S-1-5-7",       # Anonymous
    "S-1-5-11",      # Authenticated Users
    "S-1-5-32-545",  # Builtin Users
    "S-1-5-32-546",  # Builtin Guests
    "S-1-5-32-547",  # Power Users
}


def _is_link_or_junction(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


def safe_absolute_path(path: Path) -> Path:
    expanded = path.expanduser()
    absolute = Path(os.path.abspath(os.fspath(expanded)))
    cursor = Path(absolute.anchor)
    start = 1 if absolute.anchor else 0
    for part in absolute.parts[start:]:
        cursor /= part
        if _is_link_or_junction(cursor):
            raise ValueError("Path contains a symlink or junction")
    return absolute


def _labels(value: Any, name: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{name} must be a non-empty list")
    labels = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"{name} must contain strings")
        label = item.strip()
        if (
            not label
            or len(label) > 128
            or "," in label
            or any(ord(char) < 32 for char in label)
        ):
            raise ValueError(f"{name} contains an invalid label")
        labels.append(label)
    if len(set(labels)) != len(labels):
        raise ValueError(f"{name} contains duplicate labels")
    return sorted(labels)


def load_deployment_profile(path: Path) -> dict[str, Any]:
    profile_path = safe_absolute_path(path)
    if not profile_path.is_file():
        raise ValueError("Deployment profile is missing or unsafe")
    try:
        value = json.loads(profile_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Deployment profile is not valid UTF-8 JSON") from exc
    expected_keys = {
        "schema_version", "mode", "allowed_scopes", "allowed_projects", "limits"
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise ValueError("Deployment profile has missing or unknown fields")
    if value["schema_version"] != 1 or value["mode"] != "scoped-single-tenant":
        raise ValueError("Deployment profile schema or mode is unsupported")
    limits = value["limits"]
    if not isinstance(limits, dict) or set(limits) != set(PROFILE_LIMITS):
        raise ValueError("Deployment profile limits have missing or unknown fields")
    normalized_limits = {}
    for name, (minimum, maximum, _) in PROFILE_LIMITS.items():
        setting = limits[name]
        if isinstance(setting, bool) or not isinstance(setting, int):
            raise ValueError(f"Deployment profile limit {name} must be an integer")
        if not minimum <= setting <= maximum:
            raise ValueError(
                f"Deployment profile limit {name} must be within {minimum}..{maximum}"
            )
        normalized_limits[name] = setting
    return {
        "schema_version": 1,
        "mode": "scoped-single-tenant",
        "allowed_scopes": _labels(value["allowed_scopes"], "allowed_scopes"),
        "allowed_projects": _labels(value["allowed_projects"], "allowed_projects"),
        "limits": normalized_limits,
    }


def profile_environment(profile: dict[str, Any]) -> dict[str, str]:
    environment = {
        "UNIFIED_MEMORY_ALLOWED_SCOPES": ",".join(profile["allowed_scopes"]),
        "UNIFIED_MEMORY_ALLOWED_PROJECTS": ",".join(profile["allowed_projects"]),
    }
    for name, (_, _, variable) in PROFILE_LIMITS.items():
        environment[variable] = str(profile["limits"][name])
    return dict(sorted(environment.items()))


def write_default_deployment_profile(output: Path) -> Path:
    output = safe_absolute_path(output)
    if output.exists():
        raise ValueError("Deployment profile destination must not exist")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(
            json.dumps(
                DEFAULT_DEPLOYMENT_PROFILE,
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
    return output


def _nearest_existing(path: Path) -> Path:
    cursor = path
    while not cursor.exists():
        if cursor.parent == cursor:
            raise ValueError("No existing parent for deployment path")
        cursor = cursor.parent
    return cursor


def _windows_acl(path: Path) -> dict[str, Any]:
    script = r"""
$ErrorActionPreference = 'Stop'
$acl = Get-Acl -LiteralPath $env:UNIFIED_MEMORY_PREFLIGHT_PATH
$owner = try {
  (New-Object System.Security.Principal.NTAccount($acl.Owner)).Translate([System.Security.Principal.SecurityIdentifier]).Value
} catch { [string]$acl.Owner }
$rules = @($acl.Access | ForEach-Object {
  $sid = try { $_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value } catch { $_.IdentityReference.Value }
  [ordered]@{ sid = $sid; type = $_.AccessControlType.ToString(); rights = $_.FileSystemRights.ToString(); inherited = $_.IsInherited }
})
[ordered]@{ owner_sid = $owner; rules = $rules } | ConvertTo-Json -Depth 4 -Compress
"""
    environment = {
        name: value
        for name, value in os.environ.items()
        if name.upper() in {"PATH", "PATHEXT", "SYSTEMDRIVE", "SYSTEMROOT", "WINDIR"}
    }
    environment["UNIFIED_MEMORY_PREFLIGHT_PATH"] = str(path)
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        timeout=15,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode != 0:
        raise RuntimeError("Unable to inspect Windows ACL")
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Windows ACL probe returned invalid JSON") from exc
    rules = value.get("rules") or []
    if isinstance(rules, dict):
        rules = [rules]
    owner_sid = str(value.get("owner_sid") or "")
    allowed_sids = {owner_sid, "S-1-3-0", "S-1-5-18", "S-1-5-32-544"}
    broad = {
        str(rule.get("sid"))
        for rule in rules
        if rule.get("type") == "Allow" and str(rule.get("sid")) not in allowed_sids
    }
    if owner_sid in BROAD_WINDOWS_SIDS:
        broad.add(owner_sid)
    return {
        "platform": "windows",
        "owner_present": bool(owner_sid),
        "broad_allow_sids": sorted(broad),
        "no_broad_access": bool(owner_sid) and not broad,
    }


def inspect_access_boundary(path: Path) -> dict[str, Any]:
    target = _nearest_existing(safe_absolute_path(path))
    if os.name == "nt":
        return _windows_acl(target)
    mode = stat.S_IMODE(target.stat().st_mode)
    return {
        "platform": os.name,
        "mode": f"{mode:04o}",
        "no_broad_access": mode & 0o077 == 0,
    }


def _storage_is_local(path: Path) -> bool:
    if os.name != "nt":
        return True
    get_drive_type = ctypes.windll.kernel32.GetDriveTypeW
    get_drive_type.argtypes = [ctypes.c_wchar_p]
    get_drive_type.restype = ctypes.c_uint
    drive_type = get_drive_type(str(Path(path.anchor)))
    return drive_type != 4  # DRIVE_REMOTE


def deployment_preflight(
    profile_path: Path,
    database: Path,
    backup_root: Path,
    *,
    allow_new_database: bool = False,
) -> dict[str, Any]:
    profile = load_deployment_profile(profile_path)
    database = safe_absolute_path(database)
    backup_root = safe_absolute_path(backup_root)
    if not database.is_absolute() or not backup_root.is_absolute():
        raise ValueError("Deployment database and backup root must be absolute")
    if database.parent == backup_root:
        raise ValueError("Backup root must be separate from the database directory")
    if backup_root.exists() and not backup_root.is_dir():
        raise ValueError("Backup root must be a directory")
    database_exists = database.exists()
    if database_exists and not database.is_file():
        raise ValueError("Deployment database must be a regular file")
    if not database_exists and not allow_new_database:
        raise ValueError("Deployment database is missing; use --allow-new-database explicitly")
    database_boundary = inspect_access_boundary(database if database_exists else database.parent)
    backup_boundary = inspect_access_boundary(backup_root)
    checks = {
        "profile_is_bounded": True,
        "database_is_existing_or_explicitly_new": database_exists or allow_new_database,
        "database_integrity": inspect_database(database)["ok"] if database_exists else True,
        "database_parent_writable": os.access(_nearest_existing(database.parent), os.W_OK),
        "backup_parent_writable": os.access(_nearest_existing(backup_root), os.W_OK),
        "database_storage_is_not_remote": _storage_is_local(database),
        "backup_storage_is_not_remote": _storage_is_local(backup_root),
        "database_acl_has_no_broad_access": database_boundary["no_broad_access"],
        "backup_acl_has_no_broad_access": backup_boundary["no_broad_access"],
    }
    return {
        "schema_version": 1,
        "kind": "unified_memory_deployment_preflight",
        "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "verdict": "PASS" if all(checks.values()) else "FAIL",
        "checks": checks,
        "database": {"exists": database_exists, "integrity_checked": database_exists},
        "access": {"database": database_boundary, "backup": backup_boundary},
        "profile": profile,
        "environment": profile_environment(profile),
        "content_included": False,
    }


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_database(path: Path) -> dict[str, Any]:
    resolved = safe_absolute_path(path)
    if not resolved.is_file():
        raise ValueError("SQLite database is missing or unsafe")
    connection = sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True)
    try:
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
            )
        }
    finally:
        connection.close()
    missing = sorted(REQUIRED_TABLES - tables)
    return {
        "ok": quick_check == "ok" and not missing,
        "quick_check": quick_check,
        "missing_tables": missing,
    }


def database_counts(path: Path) -> dict[str, Any]:
    resolved = safe_absolute_path(path)
    if not inspect_database(resolved)["ok"]:
        raise ValueError("SQLite database failed integrity or schema validation")
    connection = sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True)
    try:
        memories = int(connection.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
        handoffs = int(connection.execute("SELECT COUNT(*) FROM handoffs").fetchone()[0])
        statuses = {
            str(row[0]): int(row[1])
            for row in connection.execute(
                "SELECT status, COUNT(*) FROM handoffs GROUP BY status ORDER BY status"
            )
        }
    finally:
        connection.close()
    return {"memories": memories, "handoffs": handoffs, "handoffs_by_status": statuses}


def backup(source: Path, destination: Path) -> tuple[Path, Path]:
    source = safe_absolute_path(source)
    destination = safe_absolute_path(destination)
    manifest_path = destination.with_suffix(destination.suffix + ".manifest.json")
    if destination.exists() or manifest_path.exists():
        raise ValueError("Refusing to overwrite an existing backup or manifest")
    source_status = inspect_database(source)
    if not source_status["ok"]:
        raise ValueError("Source database failed integrity or schema validation")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + f".tmp-{os.getpid()}")
    if temporary.exists():
        raise ValueError("Temporary backup path already exists")
    source_connection = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    output_connection = sqlite3.connect(temporary)
    try:
        source_connection.backup(output_connection)
        output_connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        output_connection.close()
        source_connection.close()
    status = inspect_database(temporary)
    if not status["ok"]:
        temporary.unlink(missing_ok=True)
        raise ValueError("Backup failed integrity or schema validation")
    temporary.replace(destination)
    manifest = {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "database_file": destination.name,
        "bytes": destination.stat().st_size,
        "sha256": sha256(destination),
        "quick_check": status["quick_check"],
        "required_tables": sorted(REQUIRED_TABLES),
    }
    with manifest_path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(manifest, ensure_ascii=True, indent=2, sort_keys=True) + "\n")
    return destination, manifest_path


def verify_backup(backup_path: Path, manifest_path: Path) -> dict[str, Any]:
    backup_path = safe_absolute_path(backup_path)
    manifest_path = safe_absolute_path(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ValueError("Backup manifest schema_version must be 1")
    if manifest.get("database_file") != backup_path.name:
        raise ValueError("Backup filename does not match its manifest")
    if manifest.get("bytes") != backup_path.stat().st_size:
        raise ValueError("Backup size does not match its manifest")
    if manifest.get("sha256") != sha256(backup_path):
        raise ValueError("Backup SHA-256 does not match its manifest")
    status = inspect_database(backup_path)
    if not status["ok"]:
        raise ValueError("Backup failed integrity or schema validation")
    return {
        "verified": True,
        "bytes": backup_path.stat().st_size,
        "sha256": manifest["sha256"],
        "quick_check": status["quick_check"],
    }


def restore_copy(backup_path: Path, manifest_path: Path, destination: Path) -> Path:
    verify_backup(backup_path, manifest_path)
    destination = safe_absolute_path(destination)
    if destination.exists():
        raise ValueError("Restore destination must not exist")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + f".tmp-{os.getpid()}")
    if temporary.exists():
        raise ValueError("Temporary restore path already exists")
    source_connection = sqlite3.connect(
        f"file:{backup_path.expanduser().resolve().as_posix()}?mode=ro", uri=True
    )
    output_connection = sqlite3.connect(temporary)
    try:
        source_connection.backup(output_connection)
    finally:
        output_connection.close()
        source_connection.close()
    if not inspect_database(temporary)["ok"]:
        temporary.unlink(missing_ok=True)
        raise ValueError("Restored database failed integrity or schema validation")
    temporary.replace(destination)
    return destination


def _run_stdio(
    database: Path,
    requests: tuple[dict[str, Any], ...],
    *,
    extra_environment: dict[str, str] | None = None,
    timeout_seconds: float = 15.0,
) -> dict[Any, dict[str, Any]]:
    payload = "".join(json.dumps(item, ensure_ascii=True) + "\n" for item in requests)
    allowed_environment = {
        "PATH", "PATHEXT", "SYSTEMDRIVE", "SYSTEMROOT", "TEMP", "TMP", "WINDIR"
    }
    environment = {
        name: value for name, value in os.environ.items() if name.upper() in allowed_environment
    }
    environment.update({
        "UNIFIED_MEMORY_DB": str(database),
        "MEMORY_AUTOSYNC": "0",
        "PYTHONIOENCODING": "utf-8",
    })
    if extra_environment:
        environment.update(extra_environment)
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "server"],
            cwd=Path(__file__).resolve().parent,
            env=environment,
            input=payload,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(1.0, float(timeout_seconds)),
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("stdio MCP probe failed to run") from exc
    if completed.returncode != 0:
        raise RuntimeError("stdio MCP probe exited non-zero")
    responses = []
    for line in completed.stdout.splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeError("stdio MCP probe returned invalid JSON") from exc
        if isinstance(value, dict):
            responses.append(value)
    return {item.get("id"): item for item in responses}


def _stdio_policy_probe(
    database: Path,
    timeout_seconds: float = 15.0,
    *,
    extra_environment: dict[str, str] | None = None,
) -> dict[str, Any]:
    requests = (
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}},
        },
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "memory_policy", "arguments": {}},
        },
    )
    by_id = _run_stdio(
        database,
        requests,
        extra_environment=extra_environment,
        timeout_seconds=timeout_seconds,
    )
    initialized = by_id.get(1, {}).get("result", {})
    if initialized.get("serverInfo", {}).get("name") != "unified-memory":
        raise RuntimeError("restored stdio MCP initialize failed")
    tool_result = by_id.get(2, {}).get("result", {})
    if tool_result.get("isError") is not False:
        raise RuntimeError("restored stdio MCP policy probe failed")
    blocks = tool_result.get("content") or []
    try:
        policy = json.loads(blocks[0]["text"])
    except (IndexError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("restored stdio MCP policy result is invalid") from exc
    usage = policy.get("usage") or {}
    return {
        "server_name": "unified-memory",
        "server_version": str(initialized.get("serverInfo", {}).get("version", "")),
        "protocol_version": str(initialized.get("protocolVersion", "")),
        "memory_records": int(usage.get("records", -1)),
        "handoff_records": int((usage.get("handoffs") or {}).get("records", -1)),
    }


def _stdio_tool_result(response: dict[str, Any], name: str) -> dict[str, Any]:
    result = response.get("result") or {}
    if result.get("isError") is not False:
        raise RuntimeError("stdio MCP %s probe failed" % name)
    blocks = result.get("content") or []
    try:
        return json.loads(blocks[0]["text"])
    except (IndexError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("stdio MCP %s result is invalid" % name) from exc


def restart_drill(work_root: Path, timeout_seconds: float = 15.0) -> tuple[dict[str, Any], Path]:
    """Prove that two fresh stdio processes preserve one bounded policy/database."""
    root = safe_absolute_path(work_root)
    if root.exists() and any(root.iterdir()):
        raise ValueError("Restart drill work root must be empty")
    root.mkdir(parents=True, exist_ok=True)
    database = root / "memory.db"
    policy_environment = {
        "UNIFIED_MEMORY_ALLOWED_SCOPES": "restart-drill",
        "UNIFIED_MEMORY_ALLOWED_PROJECTS": "restart-drill",
        "UNIFIED_MEMORY_MAX_MEMORIES": "4",
        "UNIFIED_MEMORY_MAX_BYTES": "4096",
        "UNIFIED_MEMORY_MAX_HANDOFFS": "4",
    }
    first_requests = (
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}},
        },
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "memory_save",
                "arguments": {
                    "content": "synthetic restart drill marker",
                    "scope": "restart-drill",
                    "source": "acceptance",
                },
            },
        },
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "memory_policy", "arguments": {}},
        },
    )
    second_requests = (
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}},
        },
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "memory_policy", "arguments": {}},
        },
    )
    first = _run_stdio(
        database,
        first_requests,
        extra_environment=policy_environment,
        timeout_seconds=timeout_seconds,
    )
    second = _run_stdio(
        database,
        second_requests,
        extra_environment=policy_environment,
        timeout_seconds=timeout_seconds,
    )
    first_policy = _stdio_tool_result(first[3], "first policy")
    second_policy = _stdio_tool_result(second[2], "second policy")
    saved = _stdio_tool_result(first[2], "memory save")
    first_usage = first_policy.get("usage") or {}
    second_usage = second_policy.get("usage") or {}
    first_limits = first_policy.get("limits") or {}
    second_limits = second_policy.get("limits") or {}
    first_auth = first_policy.get("authorization") or {}
    second_auth = second_policy.get("authorization") or {}
    checks = {
        "first_process_initialized": first.get(1, {}).get("result", {}).get("serverInfo", {}).get("name") == "unified-memory",
        "second_process_initialized": second.get(1, {}).get("result", {}).get("serverInfo", {}).get("name") == "unified-memory",
        "synthetic_marker_saved": saved.get("saved") is True,
        "memory_count_survives_restart": int(first_usage.get("records", -1)) == 1 and int(second_usage.get("records", -1)) == 1,
        "scope_allowlist_survives_restart": first_auth.get("allowed_scopes") == ["restart-drill"] and second_auth.get("allowed_scopes") == ["restart-drill"],
        "project_allowlist_survives_restart": first_auth.get("allowed_projects") == ["restart-drill"] and second_auth.get("allowed_projects") == ["restart-drill"],
        "quotas_survive_restart": first_limits == second_limits == {
            "max_records": 4,
            "max_content_bytes": 4096,
            "retention_days": 0,
            "max_handoffs": 4,
            "handoff_retention_days": 0,
        },
    }
    counts = database_counts(database)
    report = {
        "schema_version": 1,
        "kind": "stdio_restart_drill",
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "verdict": "PASS" if all(checks.values()) else "FAIL",
        "checks": checks,
        "processes": 2,
        "database": {"sha256": sha256(database), "counts": counts},
        "policy": {
            "allowed_scopes": second_auth.get("allowed_scopes"),
            "allowed_projects": second_auth.get("allowed_projects"),
            "limits": second_limits,
        },
        "content_included": False,
    }
    report_path = root / "restart-report.json"
    with report_path.open("x", encoding="utf-8", newline="") as stream:
        stream.write(json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True) + "\n")
    return report, report_path


def recovery_drill(source: Path, work_root: Path) -> tuple[dict[str, Any], Path]:
    root = safe_absolute_path(work_root)
    if root.exists() and any(root.iterdir()):
        raise ValueError("Recovery drill work root must be empty")
    root.mkdir(parents=True, exist_ok=True)
    source_counts = database_counts(source)
    backup_path, manifest_path = backup(source, root / "backup" / "memory.db")
    restored = restore_copy(
        backup_path,
        manifest_path,
        root / "restored" / "memory.db",
    )
    restored_counts = database_counts(restored)
    probe = _stdio_policy_probe(restored)
    checks = {
        "source_integrity": inspect_database(source)["ok"],
        "backup_verified": verify_backup(backup_path, manifest_path)["verified"],
        "restore_integrity": inspect_database(restored)["ok"],
        "logical_counts_match": restored_counts == source_counts,
        "stdio_memory_count_matches": probe["memory_records"] == restored_counts["memories"],
        "stdio_handoff_count_matches": probe["handoff_records"] == restored_counts["handoffs"],
        "stdio_initialize": probe["server_name"] == "unified-memory" and bool(probe["server_version"]),
    }
    report = {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "verdict": "PASS" if all(checks.values()) else "FAIL",
        "checks": checks,
        "source": {"sha256": sha256(safe_absolute_path(source)), "counts": source_counts},
        "backup": {"sha256": sha256(backup_path), "manifest_sha256": sha256(manifest_path)},
        "restore": {"sha256": sha256(restored), "counts": restored_counts},
        "stdio_probe": probe,
        "content_included": False,
    }
    report_path = root / "recovery-report.json"
    with report_path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True) + "\n")
    return report, report_path


def write_preflight_report(report: dict[str, Any], output: Path) -> Path:
    output = safe_absolute_path(output)
    if output.exists():
        raise ValueError("Preflight report destination must not exist")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True) + "\n")
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="Unified Memory backup administration")
    subparsers = parser.add_subparsers(dest="command", required=True)
    backup_parser = subparsers.add_parser("backup")
    backup_parser.add_argument("--db", type=Path, required=True)
    backup_parser.add_argument("--output", type=Path, required=True)
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--backup", type=Path, required=True)
    verify_parser.add_argument("--manifest", type=Path, required=True)
    restore_parser = subparsers.add_parser("restore-copy")
    restore_parser.add_argument("--backup", type=Path, required=True)
    restore_parser.add_argument("--manifest", type=Path, required=True)
    restore_parser.add_argument("--output", type=Path, required=True)
    drill_parser = subparsers.add_parser("recovery-drill")
    drill_parser.add_argument("--db", type=Path, required=True)
    drill_parser.add_argument("--work-root", type=Path, required=True)
    restart_parser = subparsers.add_parser("restart-drill")
    restart_parser.add_argument("--work-root", type=Path, required=True)
    preflight_parser = subparsers.add_parser("deployment-preflight")
    preflight_parser.add_argument("--profile", type=Path, required=True)
    preflight_parser.add_argument("--db", type=Path, required=True)
    preflight_parser.add_argument("--backup-root", type=Path, required=True)
    preflight_parser.add_argument("--allow-new-database", action="store_true")
    preflight_parser.add_argument("--report", type=Path)
    profile_parser = subparsers.add_parser("write-deployment-profile")
    profile_parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "backup":
        output, manifest = backup(args.db, args.output)
        print(f"backup created: {output} manifest={manifest}")
    elif args.command == "verify":
        result = verify_backup(args.backup, args.manifest)
        print(f"backup verified: bytes={result['bytes']} sha256={result['sha256']}")
    elif args.command == "restore-copy":
        output = restore_copy(args.backup, args.manifest, args.output)
        print(f"restore copy created: {output}")
    elif args.command == "recovery-drill":
        report, report_path = recovery_drill(args.db, args.work_root)
        print(
            "recovery drill: %s memories=%d handoffs=%d report=%s"
            % (
                report["verdict"],
                report["restore"]["counts"]["memories"],
                report["restore"]["counts"]["handoffs"],
                report_path,
            )
        )
        return 0 if report["verdict"] == "PASS" else 1
    elif args.command == "restart-drill":
        report, report_path = restart_drill(args.work_root)
        print(
            "restart drill: %s processes=%d memories=%d report=%s"
            % (
                report["verdict"],
                report["processes"],
                report["database"]["counts"]["memories"],
                report_path,
            )
        )
        return 0 if report["verdict"] == "PASS" else 1
    elif args.command == "deployment-preflight":
        report = deployment_preflight(
            args.profile,
            args.db,
            args.backup_root,
            allow_new_database=args.allow_new_database,
        )
        report_path = write_preflight_report(report, args.report) if args.report else None
        print(
            "deployment preflight: %s checks=%d report=%s"
            % (
                report["verdict"],
                len(report["checks"]),
                report_path or "not-written",
            )
        )
        return 0 if report["verdict"] == "PASS" else 1
    else:
        output = write_default_deployment_profile(args.output)
        print(f"deployment profile created: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
