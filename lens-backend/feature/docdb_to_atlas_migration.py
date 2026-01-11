#!/usr/bin/env python3
from __future__ import annotations

import argparse
import gzip
import itertools
import math
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Thread
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from collections import defaultdict
from zoneinfo import ZoneInfo

from bson import decode_file_iter, json_util
from bson.decimal128 import Decimal128
from pymongo import MongoClient
from tqdm import tqdm

MAX_DEPTH = 95  # headroom under Atlas limit
MARKER_KEY = "__serialized__"
SOCKET_TIMEOUT_MS = None
STATE_FILENAME = "last_run.json"
DEFAULT_TIMESTAMP_FIELD = os.environ.get("DOCDB_TIMESTAMP_FIELD", "updatedAt")
LOCAL_TZ = ZoneInfo("Asia/Kolkata")

# Preferred timestamp fields in order of importance (used for incremental cutoff detection)
TIMESTAMP_FIELD_PRIORITY = [
    "updatedAt",  # Most common, auto-managed by ODMs
    "updated.date",  # Nested variant, used in some FPT collections
    "lastModified",  # Alternate used in older schemas
    "modifiedAt",  # Common in legacy/user schemas
    "meta.updatedAt",  # Meta-based audit objects
    "meta.timestamp",  # Meta fallback for older audit versions
    "lastUpdate",  # Sometimes used in manual integrations
    "timestamp",  # Generic timestamp
    "createdAt",  # Fallback when no update field exists
]

SYSTEM_DATABASES = {"admin", "local", "config"}


def handle_sigterm(signum, _frame) -> None:
    """Handle termination signals to allow a graceful shutdown."""
    logging.getLogger("migration").info("Received SIGTERM, shutting down.")
    sys.exit(0)


signal.signal(signal.SIGTERM, handle_sigterm)
signal.signal(signal.SIGINT, handle_sigterm)


@dataclass
class RestoreStats:
    success: List[str]
    errors: List[str]
    zero_byte: List[str]


class MigrationError(RuntimeError):
    """Raised when the migration fails."""


def assert_readonly_docdb(uri: str) -> None:
    """Fail fast if the DocumentDB URI is not safe."""
    if not uri:
        raise MigrationError("DocDB URI missing. Use --docdb-uri.")
    lowered = uri.lower()
    if "mongodb.net" in lowered or "atlas" in lowered:
        raise MigrationError("DocDB URI appears to point to Atlas. Check your arguments.")
    if "writeconcern" in lowered or "w=" in lowered:
        raise MigrationError("Write concern found in DocDB URI — not allowed.")
    logging.getLogger("migration").info("DocDB URI validated: read-only mode enforced.")


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    """Parse CLI arguments and resolve filesystem targets."""
    parser = argparse.ArgumentParser(
        description="Migrate DocumentDB dumps into MongoDB Atlas."
    )
    parser.add_argument(
        "--atlas-uri",
        default=os.environ.get("ATLAS_URI"),
        help="MongoDB Atlas connection string (default: env ATLAS_URI)",
    )
    parser.add_argument(
        "--docdb-uri",
        default=os.environ.get("DOCDB_URI"),
        help="DocumentDB read-only source URI.",
    )
    parser.add_argument(
        "--dump-dir",
        default=os.environ.get("DUMPDIR"),
        type=Path,
        help="Path to an existing mongodump output directory (default: env DUMPDIR)",
    )
    parser.add_argument(
        "--delete-local-after",
        action="store_true",
        help="Delete local dump after successful push.",
    )
    parser.add_argument(
        "--num-workers",
        type=int,
        default=8,
        help="Number of insertion workers per collection for mongorestore.",
    )
    parser.add_argument(
        "--num-parallel-collections",
        type=int,
        default=os.cpu_count() or 4,
        help="Number of collections to restore in parallel (default: CPU cores).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Skip confirmation prompts (dangerous).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run validation steps without executing destructive operations.",
    )
    parser.add_argument(
        "--mode",
        choices=("fresh", "incremental"),
        help="Migration mode: 'fresh' drops & reloads everything; 'incremental' avoids drops.",
    )
    parser.add_argument(
        "--database",
        action="append",
        dest="databases",
        help="Limit migration to the specified database (repeatable).",
    )
    parser.add_argument(
        "--timestamp-field",
        default=os.environ.get("DOCDB_TIMESTAMP_FIELD", "auto"),
        help="Field used for incremental timestamp filtering (default: auto-detect).",
    )
    parser.add_argument(
        "--dir",
        dest="base_dir",
        type=Path,
        required=True,
        help="Base directory for dumps, logs, and state files (must already exist).",
    )
    parser.add_argument(
        "--init-last-run",
        action="store_true",
        help="Scan the selected cluster for the latest timestamp and initialize last_run.json.",
    )
    parser.add_argument(
        "--init-source",
        choices=("docdb", "atlas"),
        default="atlas",
        help="Cluster to scan when initializing last_run.json (default: atlas).",
    )
    parser.add_argument(
        "--match-index-names",
        action="store_true",
        help="Ensure Atlas index names match DocumentDB metadata by recreating mismatches.",
    )

    args = parser.parse_args(argv)

    if not args.atlas_uri:
        parser.error("Atlas URI is required (set --atlas-uri or ATLAS_URI).")
    if not args.docdb_uri:
        parser.error("DocDB URI is required (set --docdb-uri or DOCDB_URI).")
    if args.base_dir:
        args.base_dir = args.base_dir.expanduser().resolve()
        if not args.base_dir.exists() or not args.base_dir.is_dir():
            parser.error(f"--dir must point to an existing directory: {args.base_dir}")
    else:
        parser.error("--dir is required.")

    if args.dump_dir:
        args.dump_dir = args.dump_dir.expanduser().resolve()
    else:
        args.dump_dir = None

    if args.databases:
        args.databases = sorted({db.strip() for db in args.databases if db and db.strip()})
        if not args.databases:
            args.databases = None
    args.work_dir = args.base_dir
    run_id = datetime.now(LOCAL_TZ).strftime("%Y%m%d-%H%M%S")
    args.log_dir = (args.work_dir / f"logs_{run_id}").resolve()
    args.dump_workers = 2
    args.mongorestore_bin = shutil.which("mongorestore") or "mongorestore"
    args.mongodump_bin = shutil.which("mongodump") or "mongodump"
    args.mongosh_bin = shutil.which("mongosh") or "mongosh"
    if isinstance(args.timestamp_field, str) and args.timestamp_field.lower() == "auto":
        args.timestamp_field = None
    args.last_run_path = args.work_dir / STATE_FILENAME

    return args


def configure_logging(log_dir: Path) -> None:
    """Configure console and file logging for the migration run."""
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "run.log"

    handlers: List[logging.Handler] = []

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter("%(message)s"))
    handlers.append(console_handler)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    handlers.append(file_handler)

    logging.basicConfig(level=logging.DEBUG, handlers=handlers)
    logging.getLogger("migration").info("Logging to %s", log_file)


def ensure_file_exists(path: Path) -> None:
    """Ensure the provided path exists and points to a directory."""
    if not path.exists():
        raise MigrationError(f"Required path does not exist: {path}")
    if not path.is_dir():
        raise MigrationError(f"Dump path must be a directory: {path}")


def ensure_binary_available(binary: str) -> None:
    """Verify the required external binary is available in PATH."""
    if shutil.which(binary) is None:
        raise MigrationError(f"Required binary not found in PATH: {binary}")


def prompt_confirmation(message: str) -> None:
    """Prompt the operator to confirm a potentially destructive action."""
    response = input(f"{message} [y/N] ").strip().lower()
    if response not in {"y", "yes"}:
        raise MigrationError("Operation aborted by user.")


def _format_local_timestamp(dt: datetime) -> str:
    """Return timestamp formatted like DocDB local time."""
    return dt.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S.%f")


def _parse_timestamp(value: str) -> datetime:
    """Parse stored timestamp strings, tolerating legacy ISO8601 forms."""
    cleaned = value.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(cleaned)
    except ValueError:
        dt = datetime.strptime(cleaned, "%Y-%m-%d %H:%M:%S.%f")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return dt


def _format_docdb_query_iso(ts: str) -> str:
    """Convert stored local timestamp string into DocDB-friendly ISO string."""
    dt = _parse_timestamp(ts)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _get_nested_value(document: dict, dotted_field: str) -> Optional[object]:
    """Return a nested field value using dot notation, or None if missing."""
    current: object = document
    for part in dotted_field.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


# --- Timestamp state helpers -------------------------------------------------

def load_last_run_timestamp(path: Path) -> dict:
    """Load per-DB last_run.json (backward compatible with global form)."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        payload = {"timestamp": None, "by_db": {}}
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("migration").warning(
            "Failed to read last run timestamp from %s: %s", path, exc
        )
        payload = {"timestamp": None, "by_db": {}}
    if "by_db" not in payload or not isinstance(payload["by_db"], dict):
        payload["by_db"] = {}
    return payload


def persist_last_run_state(path: Path, state: dict) -> None:
    """Persist the last-run state atomically."""
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2)
    tmp_path.replace(path)


def write_last_run_timestamp(
    path: Path, db_name: Optional[str], ts: str
) -> None:
    """Update per-DB timestamp and global header timestamp."""
    state = load_last_run_timestamp(path)
    if "by_db" not in state or not isinstance(state["by_db"], dict):
        state["by_db"] = {}
    if db_name:
        state["by_db"][db_name] = ts
        state["timestamp"] = _format_local_timestamp(datetime.now(LOCAL_TZ))
    else:
        state["timestamp"] = ts
    persist_last_run_state(path, state)


def log_last_run_summary(state: dict, logger: logging.Logger) -> None:
    """Emit a summary of the stored per-database incremental timestamps."""
    logger.info("Last run global timestamp: %s", state.get("timestamp"))
    for db_name, ts in state.get("by_db", {}).items():
        logger.info(" - %s last synced at %s", db_name, ts)


# --- Atlas timestamp discovery ------------------------------------------------

def initialize_last_run_from_cluster(
    uri: str,
    timestamp_field: str,
    output_path: Path,
    databases: Optional[Sequence[str]] = None,
    source_name: str = "DocumentDB",
) -> None:
    """
    Scan a MongoDB-compatible cluster (DocumentDB or Atlas) to find the newest timestamp.
    Records the max timestamp and shows summary of contributing collections.
    """
    logger = logging.getLogger("migration")
    client = MongoClient(uri)
    latest_ts: Optional[datetime] = None
    latest_ns: Optional[str] = None
    contributing = 0
    scanned = 0
    allowed_dbs = set(databases or [])

    try:
        for db_name in client.list_database_names():
            if db_name in SYSTEM_DATABASES:
                continue
            if allowed_dbs and db_name not in allowed_dbs:
                continue

            db = client[db_name]
            try:
                collection_names = db.list_collection_names()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to list collections for %s: %s", db_name, exc)
                continue

            for coll_name in collection_names:
                scanned += 1
                coll = db[coll_name]
                try:
                    doc = coll.find_one(
                        filter={timestamp_field: {"$exists": True}},
                        sort=[(timestamp_field, -1)],
                        projection={timestamp_field: 1, "_id": 0},
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Failed to query %s.%s for timestamp: %s",
                        db_name,
                        coll_name,
                        exc,
                    )
                    continue

                ts_value = _get_nested_value(doc, timestamp_field) if doc else None
                if ts_value in (None, "", {}, []):
                    logger.debug(
                        "Skipping %s.%s (no %s field present)",
                        db_name,
                        coll_name,
                        timestamp_field,
                    )
                    continue
                try:
                    if isinstance(ts_value, dict) and "$date" in ts_value:
                        ts_value = datetime.fromisoformat(
                            ts_value["$date"].replace("Z", "+00:00")
                        )
                    elif not isinstance(ts_value, datetime):
                        continue
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Failed to parse timestamp from %s.%s: %s",
                        db_name,
                        coll_name,
                        exc,
                    )
                    continue

                contributing += 1
                if latest_ts is None or ts_value > latest_ts:
                    latest_ts = ts_value
                    latest_ns = f"{db_name}.{coll_name}"

        if latest_ts is None:
            raise MigrationError(
                f"No documents containing {timestamp_field!r} were found in {source_name}."
            )

        write_last_run_timestamp(
            output_path, None, _format_local_timestamp(latest_ts)
        )

        logger.info(
            "Initialized %s with timestamp %s (from %s).",
            output_path,
            _format_local_timestamp(latest_ts),
            latest_ns or "unknown collection",
        )
        logger.info(
            "Scanned %d collection(s); %d had valid %r timestamps.",
            scanned,
            contributing,
            timestamp_field,
        )

    finally:
        client.close()


def detect_collection_timestamp_field(
    coll,
    db_name: str,
    coll_name: str,
    *,
    logger: logging.Logger,
    candidates: Optional[Sequence[str]] = None,
) -> Optional[str]:
    """Return first matching timestamp field for a collection according to priority list."""
    field_order = list(candidates) if candidates else TIMESTAMP_FIELD_PRIORITY
    for candidate in field_order:
        try:
            sample = coll.find_one(
                {candidate: {"$exists": True, "$ne": None}},
                {candidate: 1, "_id": 0},
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "Timestamp probe failed for %s.%s field %s: %s",
                db_name,
                coll_name,
                candidate,
                exc,
            )
            continue
        value = _get_nested_value(sample, candidate) if sample else None
        if value not in (None, "", {}, []):
            logger.debug(
                "Detected timestamp field %s.%s → %s (sample=%s)",
                db_name,
                coll_name,
                candidate,
                value,
            )
            return candidate
    logger.debug(
        "No valid timestamp field detected for %s.%s; will fallback to %s",
        db_name,
        coll_name,
        TIMESTAMP_FIELD_PRIORITY[-1],
    )
    return None


def detect_timestamp_fields_for_databases(
    uri: str,
    databases: Optional[Sequence[str]] = None,
    candidates: Optional[Sequence[str]] = None,
) -> tuple[dict[tuple[str, str], str], list[tuple[str, str]]]:
    """
    Build a mapping of namespace -> preferred timestamp field using the priority list.
    Returns mapping and list of namespaces with no detected field.
    """
    logger = logging.getLogger("migration")
    mapping: dict[tuple[str, str], str] = {}
    missing: list[tuple[str, str]] = []
    allowed_dbs = set(databases or [])
    client = MongoClient(uri)
    try:
        for db_name in client.list_database_names():
            if db_name in SYSTEM_DATABASES:
                continue
            if allowed_dbs and db_name not in allowed_dbs:
                continue
            db = client[db_name]
            try:
                collection_names = db.list_collection_names()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to list collections for %s: %s", db_name, exc)
                continue
            max_workers = min(16, max(1, (os.cpu_count() or 4) * 2))
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_map = {
                    executor.submit(
                        detect_collection_timestamp_field,
                        db[coll_name],
                        db_name,
                        coll_name,
                        logger=logger,
                        candidates=candidates,
                    ): coll_name
                    for coll_name in collection_names
                }
                for future in as_completed(future_map):
                    coll_name = future_map[future]
                    try:
                        field = future.result()
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "Error scanning %s.%s for timestamp field: %s",
                            db_name,
                            coll_name,
                            exc,
                        )
                        missing.append((db_name, coll_name))
                        continue
                    if field is None:
                        missing.append((db_name, coll_name))
                    else:
                        mapping[(db_name, coll_name)] = field
    finally:
        client.close()
    return mapping, missing


# --- Shell command helpers ---------------------------------------------------

def stream_command(
    command: Sequence[str],
    log_path: Path,
    *,
    check: bool = False,
    env: dict | None = None,
    redact: Sequence[str] | None = None,
) -> int:
    """Execute a command, streaming output to logs and returning the exit code."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    redactions = tuple(filter(None, redact or ()))
    redacted_cmd = []
    for part in command:
        if any(redaction and redaction in part for redaction in redactions):
            redacted_cmd.append("<REDACTED>")
        else:
            redacted_cmd.append(part)
    logging.getLogger("migration").debug("Running command: %s", " ".join(redacted_cmd))

    with log_path.open("w", encoding="utf-8") as logfile:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            text=True,
        )
        assert process.stdout is not None
        for line in process.stdout:
            logfile.write(line)
            logfile.flush()
            logging.getLogger("command").info(line.rstrip())
        return_code = process.wait()

    if check and return_code != 0:
        raise MigrationError(
            f"Command {' '.join(redacted_cmd)} failed with exit code {return_code}"
        )
    return return_code


# --- BSON helpers ------------------------------------------------------------

def iterate_bson_files(dump_dir: Path) -> Iterable[Path]:
    """Yield BSON (and BSON.GZ) files found beneath the dump directory."""
    files = set(dump_dir.rglob("*.bson"))
    files.update(dump_dir.rglob("*.bson.gz"))
    return sorted(files)


def collection_name_from_path(bson_path: Path) -> str:
    """Derive the collection name from a BSON dump filename."""
    name = bson_path.name
    if name.endswith(".bson.gz"):
        return name[:-8]
    if name.endswith(".bson"):
        return name[:-5]
    return bson_path.stem


def _numeric_from_ejson(value: object) -> float:
    """Convert numeric Extended JSON representations into floats when possible."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return 0.0
    if isinstance(value, dict):
        for key in ("$numberDouble", "$numberLong", "$numberInt", "$numberDecimal"):
            if key in value:
                try:
                    return float(value[key])
                except (TypeError, ValueError):
                    return 0.0
    return 0.0


def _dewrap_numeric(value: object, fallback: Optional[object] = None) -> object:
    """
    Collapse DocumentDB numeric wrappers like {"$numberInt": "1"} down to scalars.
    Returns fallback (or original value) when conversion fails.
    """
    if isinstance(value, dict):
        for key in ("$numberInt", "$numberLong"):
            if key in value:
                try:
                    return int(value[key])
                except (TypeError, ValueError):
                    return fallback if fallback is not None else value
        for key in ("$numberDouble", "$numberDecimal"):
            if key in value:
                try:
                    return float(value[key])
                except (TypeError, ValueError):
                    return fallback if fallback is not None else value
    elif isinstance(value, str):
        if value in ("text", "hashed", "2dsphere", "2d"):
            return value
        try:
            return int(value)
        except ValueError:
            try:
                return float(value)
            except ValueError:
                return fallback if fallback is not None else value
    return value


def _sanitize_special_numeric(value: object) -> object:
    """Convert NaN/Inf (float or Decimal128) into None for Atlas compatibility."""
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if isinstance(value, dict) and len(value) == 1:
        key, inner = next(iter(value.items()))
        if key in {"$numberDouble", "$numberDecimal"}:
            try:
                numeric = float(inner)
            except (TypeError, ValueError):
                numeric = float("nan")
            if math.isnan(numeric) or math.isinf(numeric):
                return None
            return numeric
        if key in {"$numberInt", "$numberLong"}:
            try:
                return int(inner)
            except (TypeError, ValueError):
                return None
    if isinstance(value, Decimal128):
        try:
            if value.is_nan() or value.is_infinite():
                return None
        except AttributeError:
            pass
        return value
    if isinstance(value, dict):
        return {key: _sanitize_special_numeric(val) for key, val in value.items()}
    if isinstance(value, list):
        return [_sanitize_special_numeric(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_sanitize_special_numeric(item) for item in value)
    return value


def _serialize_subtree(obj: object) -> Dict[str, object]:
    """Wrap a sub-document in Extended JSON to avoid depth limits."""
    return {
        MARKER_KEY: True,
        "format": "extended_json",
        "data": json_util.dumps(obj, ensure_ascii=False),
    }


def serialize_deep(
    obj: object, *, depth: int = 0, max_depth: int = MAX_DEPTH
) -> object:
    if depth >= max_depth:
        return _serialize_subtree(obj)
    if isinstance(obj, dict):
        return {
            key: serialize_deep(value, depth=depth + 1, max_depth=max_depth)
            for key, value in obj.items()
        }
    if isinstance(obj, list):
        return [
            serialize_deep(item, depth=depth + 1, max_depth=max_depth)
            for item in obj
        ]
    return obj


def spinner(stop_event: Event, text: str = "Working") -> None:
    """Display a simple terminal spinner until stop_event is set."""
    spinner_cycle = itertools.cycle(["|", "/", "-", "\\"])
    while not stop_event.is_set():
        sys.stdout.write(f"\r{text} {next(spinner_cycle)}")
        sys.stdout.flush()
        time.sleep(0.1)
    sys.stdout.write(f"\r{text} ✅\n")
    sys.stdout.flush()


def fetch_cluster_size_bytes(
    mongosh_bin: str, uri: str, logger: Optional[logging.Logger] = None
) -> Optional[int]:
    """Return total cluster size in bytes by running listDatabases."""
    cmd = [
        mongosh_bin,
        uri,
        "--quiet",
        "--eval",
        "EJSON.stringify(db.adminCommand({listDatabases: 1, nameOnly: false}))",
    ]
    try:
        output = subprocess.check_output(
            cmd, text=True, stderr=subprocess.DEVNULL
        )
        data = json.loads(output)
        size = sum(
            _numeric_from_ejson(db.get("sizeOnDisk", 0))
            for db in data.get("databases", [])
        )
        return int(size)
    except Exception as exc:
        if logger:
            logger.warning("Failed to fetch size info: %s", exc)
            return None
def iterate_bson_documents(bson_path: Path) -> Iterable[Dict[str, object]]:
    """Stream decoded BSON documents from a dump file (gzip-aware)."""
    opener = gzip.open if bson_path.suffix == ".gz" else open
    with opener(bson_path, "rb") as fh:
        for document in decode_file_iter(fh):
            yield document


def manual_restore_with_serialization(
    *,
    atlas_uri: str,
    bson_path: Path,
    db_name: str,
    coll_name: str,
    drop_collection: bool,
) -> Dict[str, int]:
    """Restore documents manually, applying serialization for excessive depth."""
    client = MongoClient(atlas_uri)
    try:
        collection = client[db_name][coll_name]
        if drop_collection:
            collection.drop()
        inserted = 0
        failures = 0
        for raw_doc in iterate_bson_documents(bson_path):
            try:
                prepared = serialize_deep(_sanitize_special_numeric(raw_doc))
                collection.insert_one(prepared)
                inserted += 1
            except Exception as exc:  # noqa: BLE001
                logging.getLogger("migration").warning(
                    "Manual restore failed for %s.%s document %s: %s",
                    db_name,
                    coll_name,
                    raw_doc.get("_id"),
                    exc,
                )
                failures += 1
        logging.getLogger("migration").info(
            "Manual restore for %s.%s inserted %d document(s) with %d failure(s).",
            db_name,
            coll_name,
            inserted,
            failures,
        )
        return {"inserted": inserted, "failures": failures}
    finally:
        client.close()


def restore_collections(
    *,
    atlas_uri: str,
    dump_dir: Path,
    mongorestore_bin: str,
    log_dir: Path,
    drop_collections: bool,
    num_workers: int,
    num_parallel_collections: int,
    dry_run: bool,
    databases: Optional[Sequence[str]] = None,
) -> RestoreStats:
    """Replay BSON files into Atlas and collect statistics for the cycle."""
    results = RestoreStats(success=[], errors=[], zero_byte=[])
    restore_log_dir = log_dir / "restore"
    restore_log_dir.mkdir(parents=True, exist_ok=True)

    logging.getLogger("migration").info(
        "Starting per-collection restore with progress bar …"
    )
    gzip_present = any(dump_dir.rglob("*.bson.gz"))
    allowed_dbs = set(databases or [])
    bson_files = [
        path
        for path in iterate_bson_files(dump_dir)
        if path.parent.name not in SYSTEM_DATABASES
        and (not allowed_dbs or path.parent.name in allowed_dbs)
    ]

    if not bson_files:
        logging.getLogger("migration").info(
            "No collections to restore after filtering; skipping mongorestore."
        )
        return results

    with tqdm(
        total=len(bson_files),
        desc="📦 Restoring Collections",
        ncols=80,
        unit="coll",
    ) as pbar:
        for bson_path in bson_files:
            db_name = bson_path.parent.name
            coll_name = collection_name_from_path(bson_path)
            namespace = f"{db_name}.{coll_name}"
            log_file = restore_log_dir / f"{db_name}.{coll_name}.log"

            if bson_path.stat().st_size == 0:
                logging.getLogger("migration").warning(
                    "Creating empty collection for %s (0-byte dump).", namespace
                )
                results.zero_byte.append(namespace)
                try:
                    client = MongoClient(atlas_uri)
                    try:
                        client[db_name].create_collection(coll_name)
                    finally:
                        client.close()
                except Exception as exc:  # noqa: BLE001
                    logging.getLogger("migration").error(
                        "Failed to create empty collection %s: %s",
                        namespace,
                        exc,
                    )
                pbar.update(1)
                continue

            command: List[str] = [
                mongorestore_bin,
                f"--uri={atlas_uri}",
                f"--dir={str(dump_dir)}",
                f"--nsInclude={namespace}",
                "--noOptionsRestore",
                f"--numInsertionWorkersPerCollection={num_workers}",
                "-vv",
            ]
            if num_parallel_collections > 1:
                command.insert(
                    -1, f"--numParallelCollections={max(1, num_parallel_collections)}"
                )
            if drop_collections:
                command.append("--drop")
            if gzip_present:
                command.append("--gzip")

            handled = False
            return_code: Optional[int] = None
            if dry_run:
                logging.getLogger("migration").info(
                    "Dry run: would restore %s", namespace
                )
                handled = True
            else:
                return_code = stream_command(command, log_file, redact=[atlas_uri])
                if return_code == 0:
                    results.success.append(namespace)
                    handled = True
                else:
                    try:
                        log_text = Path(log_file).read_text(
                            encoding="utf-8", errors="ignore"
                        )
                    except OSError:
                        log_text = ""
                    if (
                        "exceeds 180 levels of nesting" in log_text
                        or "exceeds maximum document depth" in log_text
                    ):
                        logging.getLogger("migration").info(
                            "Retrying %s via manual serialized restore due to depth error.",
                            namespace,
                        )
                        stats = manual_restore_with_serialization(
                            atlas_uri=atlas_uri,
                            bson_path=bson_path,
                            db_name=db_name,
                            coll_name=coll_name,
                            drop_collection=drop_collections,
                        )
                        if stats["failures"] == 0:
                            results.success.append(f"{namespace} (manual)")
                            handled = True
                        else:
                            logging.getLogger("migration").error(
                                "Manual restore for %s had %d failures.",
                                namespace,
                                stats["failures"],
                            )
            if not handled and not dry_run:
                reason = f"{namespace} (exit {return_code})" if return_code is not None else namespace
                results.errors.append(reason)
            pbar.update(1)
    return results


def _normalize_index_key(key: dict) -> dict:
    """Normalize a metadata index key into Atlas-friendly numeric values."""
    cleaned_key: Dict[str, object] = {}
    for key_field, key_value in key.items():
        normalized = _dewrap_numeric(key_value, fallback=1)
        if isinstance(normalized, str):
            if normalized in ("text", "hashed", "2dsphere", "2d"):
                cleaned_key[key_field] = normalized
                continue
            try:
                normalized = int(normalized)
            except (TypeError, ValueError):
                normalized = 1
        elif not isinstance(normalized, (int, float)):
            normalized = 1
        cleaned_key[key_field] = normalized
    return cleaned_key


def _load_metadata_indexes(meta_path: Path) -> Tuple[str, str, List[dict]]:
    """Parse a metadata JSON file and return the cleaned index definitions."""
    metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    indexes: List[dict] = []
    for index in metadata.get("indexes", []):
        name = index.get("name")
        key = index.get("key")
        if not name or name == "_id_" or key is None:
            continue
        if isinstance(key, dict):
            key = _normalize_index_key(key)
        options = {
            k: v
            for k, v in index.items()
            if k not in {"key", "name", "ns"} and v is not None
        }
        options.pop("background", None)
        options.pop("v", None)
        for opt_name, opt_value in list(options.items()):
            options[opt_name] = _dewrap_numeric(opt_value, fallback=opt_value)
        if "expireAfterSeconds" in options:
            options["expireAfterSeconds"] = int(
                _dewrap_numeric(options["expireAfterSeconds"], fallback=0)
            )
        indexes.append(
            {
                "name": name,
                "key": key,
                "options": options,
            }
        )
    coll_name = meta_path.stem.replace(".metadata", "")
    db_name = meta_path.parent.name
    return db_name, coll_name, indexes


# --- Atlas Admin API helpers -------------------------------------------------

def reconcile_indexes(
    *,
    atlas_uri: str,
    dump_dir: Path,
    mongosh_bin: Optional[str] = None,  # noqa: ARG001 - retained for CLI compatibility
    log_dir: Path,
    dry_run: bool,
    databases: Optional[Sequence[str]] = None,
    match_index_names: bool = False,
) -> None:
    """Fast, parallel index reconciliation using PyMongo instead of mongosh."""
    logger = logging.getLogger("migration")
    metadata_files = sorted(dump_dir.rglob("*.metadata.json"))
    if not metadata_files:
        logger.info("No metadata files found; skipping index reconciliation.")
        return

    allowed_dbs = set(databases or [])
    logger.info("Reconciling indexes directly using PyMongo …")

    client = MongoClient(atlas_uri)
    index_log = log_dir / "index_reconciliation.log"

    def reconcile_one(meta_path: Path) -> str:
        try:
            metadata = json.loads(meta_path.read_text(encoding="utf-8"))
            coll_name = meta_path.stem.replace(".metadata", "")
            db_name = meta_path.parent.name
            if allowed_dbs and db_name not in allowed_dbs:
                return f"⏭️ Skipped {db_name}.{coll_name} (not in allowed DB list)"

            indexes = metadata.get("indexes", [])
            if len(indexes) <= 1:
                return f"⏭️ Skipped {db_name}.{coll_name} (no secondary indexes)"

            db = client[db_name]
            coll = db[coll_name]
            existing_indexes = {
                json.dumps(idx["key"], sort_keys=True): idx["name"]
                for idx in coll.list_indexes()
            }

            created = []
            for idx in indexes:
                name = idx.get("name")
                key = idx.get("key")
                if not name or name == "_id_" or not key:
                    continue

                cleaned = {
                    field: (
                        _dewrap_numeric(value, fallback=1)
                        if not isinstance(value, str)
                        else value
                    )
                    for field, value in key.items()
                }

                key_json = json.dumps(cleaned, sort_keys=True)
                existing_name = existing_indexes.get(key_json)

                if existing_name == name:
                    continue

                if existing_name and match_index_names:
                    if dry_run:
                        logger.info(
                            "[DRY-RUN] Would drop index %s (key %s) to recreate as %s on %s.%s",
                            existing_name,
                            key_json,
                            name,
                            db_name,
                            coll_name,
                        )
                    else:
                        try:
                            coll.drop_index(existing_name)
                            logger.info(
                                "Dropped index %s (key %s); recreating as %s on %s.%s",
                                existing_name,
                                key_json,
                                name,
                                db_name,
                                coll_name,
                            )
                        except Exception as exc:  # noqa: BLE001
                            logger.warning(
                                "Failed to drop index %s on %s.%s: %s",
                                existing_name,
                                db_name,
                                coll_name,
                                exc,
                            )
                    existing_indexes.pop(key_json, None)
                elif existing_name:
                    continue

                options = {
                    opt_name: _dewrap_numeric(opt_value, fallback=opt_value)
                    for opt_name, opt_value in idx.items()
                    if opt_name not in {"key", "name", "ns"} and opt_value is not None
                }
                options.pop("background", None)
                options.pop("v", None)
                if "expireAfterSeconds" in options:
                    options["expireAfterSeconds"] = int(
                        _dewrap_numeric(options["expireAfterSeconds"], fallback=0)
                    )

                if dry_run:
                    logger.info(
                        "[DRY-RUN] Would create index %s on %s.%s",
                        name,
                        db_name,
                        coll_name,
                    )
                else:
                    coll.create_index(list(cleaned.items()), name=name, **options)
                    created.append(name)
                    existing_indexes[key_json] = name

            if created:
                return (
                    f"✅ Created {len(created)} index(es) on {db_name}.{coll_name}: {created}"
                )
            return f"✔️ All indexes present on {db_name}.{coll_name}"
        except Exception as exc:  # noqa: BLE001
            return f"⚠️ Error processing {meta_path}: {exc}"

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(reconcile_one, path) for path in metadata_files]
        for future in tqdm(
            as_completed(futures),
            total=len(futures),
            desc="🔍 Index reconciliation",
            ncols=90,
        ):
            outcome = future.result()
            logger.info(outcome)
            with index_log.open("a", encoding="utf-8") as lf:
                lf.write(outcome + "\n")

    client.close()


def _run_mongodump_command(
    command: List[str],
    log_path: Path,
    spinner_label: str,
    redact: Sequence[str],
) -> None:
    """Execute a single mongodump command while displaying a spinner."""
    stop_event = Event()
    spin_thread = Thread(target=spinner, args=(stop_event, spinner_label))
    spin_thread.daemon = True
    spin_thread.start()
    try:
        stream_command(command, log_path, check=True, redact=redact)
    finally:
        stop_event.set()
        spin_thread.join()


def create_docdb_dump(
    docdb_uri: str,
    work_dir: Path,
    mongodump_bin: str,
    databases: Optional[Sequence[str]] = None,
    *,
    num_parallel_collections: int,
    dump_workers: int,
    mode: str,
    timestamp_field: Optional[str],
    timestamp_state: dict,
    last_run_path: Path,
    dry_run: bool,
) -> Path:
    """Runs mongodump from DocumentDB read-only endpoint."""
    run_id = datetime.now(LOCAL_TZ).strftime("%Y%m%d-%H%M%S")
    dump_dir = work_dir / f"dump_{run_id}"
    dump_dir.mkdir(parents=True, exist_ok=True)

    base_command = [
        mongodump_bin,
        f"--uri={docdb_uri}",
        f"--out={str(dump_dir)}",
        "--ssl",
        "--quiet",
    ]

    if mode == "incremental":
        logger = logging.getLogger("migration")
        by_db: dict = timestamp_state.get("by_db", {})
        global_since: Optional[str] = timestamp_state.get("timestamp")
        candidates = None if timestamp_field is None else [timestamp_field]
        mapping, missing = detect_timestamp_fields_for_databases(
            docdb_uri, databases, candidates=candidates
        )
        db_collections: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for (db_name, coll_name), field in mapping.items():
            db_collections[db_name].append((coll_name, field))
        missing_by_db: dict[str, list[str]] = defaultdict(list)
        for db_name, coll_name in missing:
            missing_by_db[db_name].append(coll_name)

        if databases:
            target_dbs = sorted(set(databases))
        else:
            target_dbs = sorted(set(db_collections) | set(missing_by_db))

        if target_dbs:
            for db_name in target_dbs:
                db_since = by_db.get(db_name) or global_since
                if isinstance(db_since, datetime):
                    db_since = _format_local_timestamp(db_since)
                elif isinstance(db_since, str):
                    try:
                        db_since = _format_local_timestamp(_parse_timestamp(db_since))
                    except Exception:
                        pass
                if db_since:
                    logger.info("Using last run timestamp for %s: %s", db_name, db_since)
                else:
                    logger.warning("No prior timestamp for %s; performing full dump.", db_name)

                commands: list[tuple[List[str], Path, str]] = []
                for coll_name, field in db_collections.get(db_name, []):
                    command: List[str] = [
                        mongodump_bin,
                        f"--uri={docdb_uri}",
                        f"--out={str(dump_dir)}",
                        "--ssl",
                        "--quiet",
                        f"--db={db_name}",
                        f"--collection={coll_name}",
                    ]
                    if db_since:
                        query = json.dumps(
                            {field: {"$gte": {"$date": _format_docdb_query_iso(db_since)}}},
                            separators=(",", ":"),
                        )
                        command.append(f"--query={query}")
                    commands.append(
                        (command, dump_dir / f"dump.{db_name}.{coll_name}.log", f"📦 Dumping {db_name}.{coll_name}")
                    )

                for coll_name in missing_by_db.get(db_name, []):
                    command = [
                        mongodump_bin,
                        f"--uri={docdb_uri}",
                        f"--out={str(dump_dir)}",
                        "--ssl",
                        "--quiet",
                        f"--db={db_name}",
                        f"--collection={coll_name}",
                    ]
                    commands.append(
                        (
                            command,
                            dump_dir / f"dump.{db_name}.{coll_name}.log",
                            f"📦 Dumping {db_name}.{coll_name} (full)",
                        )
                    )

                if not commands:
                    logger.debug("No collections to dump for %s.", db_name)
                    continue

                max_workers_db = max(1, min(len(commands), dump_workers))
                with ThreadPoolExecutor(max_workers=max_workers_db) as executor:
                    futures = [
                        executor.submit(_run_mongodump_command, cmd, log_path, label, [docdb_uri])
                        for cmd, log_path, label in commands
                    ]
                    for future in as_completed(futures):
                        future.result()

                if not dry_run:
                    new_ts = _format_local_timestamp(datetime.now(LOCAL_TZ))
                    write_last_run_timestamp(last_run_path, db_name, new_ts)
                    timestamp_state.setdefault("by_db", {})[db_name] = new_ts

            logger.info("DocDB dump completed: %s", dump_dir)
            return dump_dir
        else:
            logger.warning(
                "No timestamp-enabled collections detected; falling back to full dump."
            )

    if databases:
        unique_dbs = sorted(set(databases))
        max_workers = max(1, min(len(unique_dbs), dump_workers))
        logging.getLogger("migration").info(
            "Dumping %d database(s) with %d concurrent worker(s).",
            len(unique_dbs),
            max_workers,
        )
        futures = []
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            for db_name in unique_dbs:
                command = base_command + [
                    f"--db={db_name}",
                    f"--numParallelCollections={max(1, num_parallel_collections)}",
                ]
                log_path = dump_dir / f"dump.{db_name}.log"
                futures.append(
                    executor.submit(
                        _run_mongodump_command,
                        command,
                        log_path,
                        f"📦 Dumping {db_name}",
                        [docdb_uri],
                    )
                )
            for future in as_completed(futures):
                future.result()
    else:
        log_path = dump_dir / "dump.log"
        logging.getLogger("migration").info("Dumping all databases …")
        base_command.append(f"--numParallelCollections={max(1, num_parallel_collections)}")
        _run_mongodump_command(
            base_command,
            log_path,
            "📦 Dumping from DocDB",
            redact=[docdb_uri],
        )

    logging.getLogger("migration").info("DocDB dump completed: %s", dump_dir)
    return dump_dir


def clean_old_logs(work_dir: Path, keep: int = 5) -> None:
    """Prune older log directories, keeping only the most recent batches."""
    logs = sorted(
        (path for path in work_dir.glob("logs_*") if path.is_dir()),
        key=os.path.getmtime,
    )
    if len(logs) <= keep:
        return
    for old in logs[:-keep]:
        shutil.rmtree(old, ignore_errors=True)
        logging.getLogger("migration").info("Deleted old log dir: %s", old)


def write_summary(stats: RestoreStats, log_dir: Path) -> None:
    """Persist per-cycle success/error summaries to disk."""
    log_dir.mkdir(parents=True, exist_ok=True)
    success_path = log_dir / "restore.success.txt"
    error_path = log_dir / "restore.errors.txt"
    zero_path = log_dir / "restore.zero_byte_skips.txt"

    success_path.write_text("\n".join(stats.success), encoding="utf-8")
    error_path.write_text("\n".join(stats.errors), encoding="utf-8")
    zero_path.write_text("\n".join(stats.zero_byte), encoding="utf-8")

    logging.getLogger("migration").info("")
    logging.getLogger("migration").info("Restore summary:")
    logging.getLogger("migration").info("  Success : %d", len(stats.success))
    logging.getLogger("migration").info("  Errors  : %d", len(stats.errors))
    logging.getLogger("migration").info("  Zero    : %d", len(stats.zero_byte))
    logging.getLogger("migration").info("Logs written to %s", log_dir)


def run_sync_cycle(args) -> RestoreStats:
    """Execute a single dump/restore cycle and return aggregated statistics."""
    clean_old_logs(args.work_dir)
    created_dump = False
    state = load_last_run_timestamp(args.last_run_path)
    log_last_run_summary(state, logging.getLogger("migration"))
    if args.mode == "incremental":
        by_db = state.get("by_db", {})
        if by_db:
            logging.getLogger("migration").info(
                "Incremental sync enabled for %d database(s).", len(by_db)
            )
        elif state.get("timestamp"):
            logging.getLogger("migration").info(
                "Legacy global timestamp detected; will apply per-db fallback."
            )
        else:
            logging.getLogger("migration").info(
                "No previous incremental state found; capturing full data set."
            )

    if args.dump_dir:
        dump_dir = args.dump_dir
        ensure_file_exists(dump_dir)
    else:
        dump_dir = create_docdb_dump(
            args.docdb_uri,
            args.work_dir,
            args.mongodump_bin,
            args.databases,
            num_parallel_collections=args.num_parallel_collections,
            dump_workers=args.dump_workers,
            mode=args.mode or "fresh",
            timestamp_field=args.timestamp_field,
            timestamp_state=state,
            last_run_path=args.last_run_path,
            dry_run=args.dry_run,
        )
        created_dump = True

    cycle_id = dump_dir.name
    if cycle_id.startswith("dump_"):
        cycle_id = cycle_id[5:]
    cycle_log_dir = args.log_dir / f"cycle_{cycle_id}"
    cycle_log_dir.mkdir(parents=True, exist_ok=True)
    cycle_start_dt: Optional[datetime] = None
    try:
        cycle_start_dt = datetime.strptime(cycle_id, "%Y%m%d-%H%M%S").replace(
            tzinfo=LOCAL_TZ
        )
    except ValueError:
        cycle_start_dt = None

    logging.getLogger("migration").info("Dump directory: %s", dump_dir)
    logging.getLogger("migration").info("Cycle log directory: %s", cycle_log_dir)
    logging.getLogger("migration").info(
        "Skipping full Atlas DB drop; using per-collection drop instead."
    )
    logging.getLogger("migration").info(
        "Atlas collection deletions are disabled in simplified mode."
    )

    stats = restore_collections(
        atlas_uri=args.atlas_uri,
        dump_dir=dump_dir,
        mongorestore_bin=args.mongorestore_bin,
        log_dir=cycle_log_dir,
        drop_collections=args.drop_collections,
        num_workers=args.num_workers,
        num_parallel_collections=args.num_parallel_collections,
        dry_run=args.dry_run,
        databases=args.databases,
    )

    reconcile_indexes(
        atlas_uri=args.atlas_uri,
        dump_dir=dump_dir,
        mongosh_bin=args.mongosh_bin,
        log_dir=cycle_log_dir,
        dry_run=args.dry_run,
        databases=args.databases,
        match_index_names=args.match_index_names,
    )

    write_summary(stats, cycle_log_dir)

    if args.dry_run:
        logging.getLogger("migration").info(
            "Dry run: not updating last run timestamp file."
        )
    elif not stats.errors:
        completion_dt = datetime.now(LOCAL_TZ)
        logging.getLogger("migration").info(
            "Run completed at %s (IST).", _format_local_timestamp(completion_dt)
        )
        baseline_dt = cycle_start_dt or completion_dt
        state = load_last_run_timestamp(args.last_run_path)
        safe_dt = completion_dt
        if args.mode == "fresh":
            safety_window = timedelta(minutes=5)
            safe_dt = baseline_dt - safety_window
            if safe_dt > completion_dt:
                safe_dt = completion_dt
            logging.getLogger("migration").info(
                "Fresh mode: stamping per-database last-run at %s (IST) "
                "to protect updates during the migration window.",
                _format_local_timestamp(safe_dt),
            )
            touched_dbs = set()

            def _collect_db(namespace: str) -> None:
                base = namespace.split(" ", 1)[0]
                if "." in base:
                    touched_dbs.add(base.split(".", 1)[0])

            for namespace in stats.success:
                _collect_db(namespace)
            for namespace in stats.zero_byte:
                _collect_db(namespace)
            if args.databases:
                touched_dbs.update(args.databases)

            if touched_dbs:
                per_db = state.setdefault("by_db", {})
                for db_name in sorted(touched_dbs):
                    per_db[db_name] = _format_local_timestamp(safe_dt)
        else:
            safe_dt = completion_dt

        state["timestamp"] = _format_local_timestamp(safe_dt)

        persist_last_run_state(args.last_run_path, state)

    if args.delete_local_after and created_dump and not args.dry_run and not stats.errors:
        shutil.rmtree(dump_dir, ignore_errors=True)
        logging.getLogger("migration").info("Deleted local dump: %s", dump_dir)

    return stats

def estimate_migration_info(docdb_uri: str, atlas_uri: str, mongosh_bin: str) -> None:
    """Fetch and print DB sizes and estimated migration metrics."""

    def run_eval(uri: str, name: str) -> int:
        try:
            size = fetch_cluster_size_bytes(mongosh_bin, uri)
            if size is None:
                raise RuntimeError("Unknown error")
            print(f"Total size in {name}: {size / 1024 / 1024 / 1024:.2f} GB")
            return size
        except Exception as exc:
            print(f"Failed to fetch {name} info: {exc}")
            return 0

    print("📊 Collecting database size information...\n")
    docdb_size = run_eval(docdb_uri, "DocumentDB")
    atlas_size = run_eval(atlas_uri, "Atlas")

    if docdb_size:
        required_gb = docdb_size * 1.1 / (1024**3)
        print(f"\n💾 Recommended local disk space: {required_gb:.2f} GB")
        est_time = (docdb_size / (30 * 1024 * 1024)) / 60  # 30 MB/s baseline
        print(f"⏱️  Estimated migration time: {est_time:.1f} minutes")
    print("\n✅ Testing summary complete.")


def main(argv: Sequence[str]) -> int:
    """Program entry point coordinating argument parsing and execution."""
    start_ts = time.time()
    try:
        args = parse_args(argv)

        assert_readonly_docdb(args.docdb_uri)

        if args.init_last_run:
            configure_logging(args.log_dir)
            init_field = args.timestamp_field or TIMESTAMP_FIELD_PRIORITY[0]
            if args.init_source == "atlas":
                initialize_last_run_from_cluster(
                    args.atlas_uri,
                    init_field,
                    args.last_run_path,
                    args.databases,
                    "Atlas",
                )
            else:
                initialize_last_run_from_cluster(
                    args.docdb_uri,
                    init_field,
                    args.last_run_path,
                    args.databases,
                    "DocumentDB",
                )
            return 0

        if args.mode is None:
            if sys.stdin.isatty():
                print()
                print("Select migration mode:")
                print("  1) Fresh migration (drop and fully replace data)")
                print("  2) Incremental migration (insert/update only)")
                choice = input("Enter choice [1/2]: ").strip()
                if choice == "1":
                    args.mode = "fresh"
                elif choice == "2":
                    args.mode = "incremental"
                else:
                    print("Invalid choice. Aborting.")
                    return 1
            else:
                args.mode = "fresh"

        if args.mode == "incremental":
            logging.getLogger("migration").info("Mode: incremental (non-destructive)")
            args.drop_collections = False
        else:
            args.mode = "fresh"
            args.drop_collections = True
            logging.getLogger("migration").info("Mode: fresh (destructive)")

        if args.dry_run:
            ensure_binary_available(args.mongosh_bin)
            estimate_migration_info(args.docdb_uri, args.atlas_uri, args.mongosh_bin)

        configure_logging(args.log_dir)

        logging.getLogger("migration").info("Work directory: %s", args.work_dir)
        logging.getLogger("migration").info("Base log directory : %s", args.log_dir)

        ensure_binary_available(args.mongorestore_bin)
        ensure_binary_available(args.mongosh_bin)
        ensure_binary_available(args.mongodump_bin)

        args.work_dir.mkdir(parents=True, exist_ok=True)

        docdb_size_bytes = fetch_cluster_size_bytes(
            args.mongosh_bin, args.docdb_uri, logging.getLogger("migration")
        )
        if docdb_size_bytes:
            required_bytes = int(docdb_size_bytes * 1.1)
            free_bytes = shutil.disk_usage(args.work_dir).free
            logging.getLogger("migration").info(
                "DocDB size estimate: %.2f GB; free space: %.2f GB",
                docdb_size_bytes / 1024 / 1024 / 1024,
                free_bytes / 1024 / 1024 / 1024,
            )
            if free_bytes < required_bytes:
                needed_gb = required_bytes / 1024 / 1024 / 1024
                raise MigrationError(
                    f"Insufficient free space in {args.work_dir}: need ~{needed_gb:.2f} GB"
                )

        if args.dump_dir:
            logging.getLogger("migration").info(
                "Using existing dump directory: %s", args.dump_dir
            )

        stats = run_sync_cycle(args)
        if stats.errors and not args.dry_run:
            raise MigrationError("One or more collections failed to restore.")

        return 0

    except MigrationError as exc:
        logging.getLogger("migration").error("Migration error: %s", exc)
        exit_code = 1
    except KeyboardInterrupt:
        logging.getLogger("migration").error("Interrupted by user.")
        exit_code = 130
    else:
        exit_code = 0
    finally:
        duration = time.time() - start_ts
        logging.getLogger("migration").info(
            "Total runtime: %.1f minutes (%.1f seconds)",
            duration / 60.0,
            duration,
        )

    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
