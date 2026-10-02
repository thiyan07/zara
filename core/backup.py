"""Backup / restore for Zara's SQLite state. Operator-gated, never silent,
never destructive: restore writes to a NEW file and reports row counts."""
from __future__ import annotations
import os
import shutil
import sqlite3
from datetime import datetime


def backup_sqlite(src_path: str, backup_dir: str = "backups") -> dict:
    if not os.path.isfile(src_path):
        raise FileNotFoundError(f"no database at {src_path}")
    os.makedirs(backup_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = os.path.basename(src_path)
    dest = os.path.join(backup_dir, f"{base}.{stamp}.bak")
    if not os.path.realpath(dest).startswith(os.path.realpath(backup_dir)):
        raise ValueError("backup destination escapes backup dir")
    src = sqlite3.connect(src_path)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return {"source": src_path, "backup": dest,
            "bytes": os.path.getsize(dest)}


def restore_sqlite(backup_path: str, dest_path: str) -> dict:
    if not os.path.isfile(backup_path):
        raise FileNotFoundError(f"no backup at {backup_path}")
    if os.path.exists(dest_path):
        raise FileExistsError(
            f"refusing to overwrite {dest_path}; choose a new path")
    shutil.copy2(backup_path, dest_path)
    con = sqlite3.connect(dest_path)
    try:
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
    finally:
        con.close()
    return {"restored": dest_path, "tables": tables}
