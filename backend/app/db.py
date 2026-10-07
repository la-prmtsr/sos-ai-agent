"""Penyimpanan off-chain. SQLite = satu file (backend/sos.db), tidak perlu install apa-apa.

Isi laporan yang sensitif (lokasi, nama pelapor) tinggal di sini.
Yang naik ke blockchain hanya hash-nya.
"""
import json
import sqlite3
import time
from typing import Any, Optional

from . import config

_JSON_FIELDS = ("payload", "plan", "verification", "log")

SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    code            TEXT PRIMARY KEY,   -- kode pendek, mis. SOS-8F3K2A
    report_id       TEXT NOT NULL,      -- bytes32 on-chain = keccak256(code)
    chat_id         INTEGER,            -- chat Telegram pelapor (untuk notifikasi)
    reporter_name   TEXT,
    status          TEXT NOT NULL,      -- REPORTED / VERIFIED / APPROVED / DELIVERED / REJECTED
    urgency         INTEGER DEFAULT 0,
    payload_text    TEXT NOT NULL,      -- JSON persis yang di-hash
    payload_hash    TEXT NOT NULL,
    plan_text       TEXT,               -- JSON rencana (action card) persis yang di-hash
    plan_hash       TEXT,
    verification    TEXT,               -- JSON hasil cek agent
    log             TEXT,               -- JSON list baris log agent
    tx_submit       TEXT,
    tx_verify       TEXT,
    tx_delivery     TEXT,
    decided_by      TEXT,
    notified        INTEGER DEFAULT 0,  -- 1 kalau pelapor sudah dikabari keputusan operator
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);
"""


REPORTERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS reporters (
    reporter_id   INTEGER PRIMARY KEY,   -- ID Telegram pelapor
    name          TEXT,
    trust         REAL NOT NULL,         -- trust score terakhir (0.05 - 0.99)
    untrusted     INTEGER DEFAULT 0,     -- 1 = ada di daftar untrusted
    reports       INTEGER DEFAULT 0,     -- jumlah laporan yang pernah dikirim
    last_verdict  TEXT,
    updated_at    REAL NOT NULL
);
"""


OPERATORS_SCHEMA = """
CREATE TABLE IF NOT EXISTS operator_requests (
    address     TEXT PRIMARY KEY,   -- alamat wallet (huruf kecil)
    name        TEXT NOT NULL,
    agency      TEXT NOT NULL,      -- BNPB Pusat / BPBD Provinsi / ...
    unit        TEXT NOT NULL,      -- wilayah atau unit kerja
    contact     TEXT,
    signature   TEXT NOT NULL,      -- tanda tangan wallet atas isi permintaan
    created_at  REAL NOT NULL
);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    with _connect() as conn:
        conn.executescript(SCHEMA)
        conn.executescript(REPORTERS_SCHEMA)
        conn.executescript(OPERATORS_SCHEMA)
        # database lama belum punya kolom ini: tambahkan tanpa menghapus data
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(reports)")}
        if "reporter_id" not in columns:
            conn.execute("ALTER TABLE reports ADD COLUMN reporter_id INTEGER")
        if "tx_decide" not in columns:
            conn.execute("ALTER TABLE reports ADD COLUMN tx_decide TEXT")
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(reporters)")}
        if "phone" not in columns:
            conn.execute("ALTER TABLE reporters ADD COLUMN phone TEXT")


def _to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["payload"] = json.loads(d["payload_text"])
    d["plan"] = json.loads(d["plan_text"]) if d.get("plan_text") else None
    d["verification"] = json.loads(d["verification"]) if d.get("verification") else None
    d["log"] = json.loads(d["log"]) if d.get("log") else []
    return d


def insert(report: dict[str, Any]) -> None:
    now = time.time()
    with _connect() as conn:
        conn.execute(
            """INSERT INTO reports
               (code, report_id, chat_id, reporter_id, reporter_name, status, payload_text, payload_hash, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                report["code"],
                report["report_id"],
                report.get("chat_id"),
                report.get("reporter_id"),
                report.get("reporter_name"),
                report["status"],
                report["payload_text"],
                report["payload_hash"],
                now,
                now,
            ),
        )


def update(code: str, **fields: Any) -> None:
    if not fields:
        return
    for key in ("verification", "log"):
        if key in fields and not isinstance(fields[key], str):
            fields[key] = json.dumps(fields[key], ensure_ascii=False)
    fields["updated_at"] = time.time()
    columns = ", ".join(f"{k} = ?" for k in fields)
    with _connect() as conn:
        conn.execute(f"UPDATE reports SET {columns} WHERE code = ?", (*fields.values(), code))


def get(code: str) -> Optional[dict[str, Any]]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM reports WHERE code = ?", (code,)).fetchone()
    return _to_dict(row) if row else None


def list_all() -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM reports ORDER BY created_at DESC").fetchall()
    return [_to_dict(r) for r in rows]


def list_by_status(*statuses: str) -> list[dict[str, Any]]:
    marks = ", ".join("?" for _ in statuses)
    with _connect() as conn:
        rows = conn.execute(f"SELECT * FROM reports WHERE status IN ({marks})", statuses).fetchall()
    return [_to_dict(r) for r in rows]


# ---------------------------------------------------------------- pelapor
def get_reporter(reporter_id: int) -> Optional[dict[str, Any]]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM reporters WHERE reporter_id = ?", (reporter_id,)).fetchone()
    return dict(row) if row else None


def save_reporter(reporter_id: int, name: Optional[str], trust: float, untrusted: bool, verdict: str) -> None:
    """Simpan trust score terbaru pelapor dan tambah hitungan laporannya."""
    now = time.time()
    with _connect() as conn:
        conn.execute(
            """INSERT INTO reporters (reporter_id, name, trust, untrusted, reports, last_verdict, updated_at)
               VALUES (?, ?, ?, ?, 1, ?, ?)
               ON CONFLICT(reporter_id) DO UPDATE SET
                   name = excluded.name, trust = excluded.trust, untrusted = excluded.untrusted,
                   reports = reports + 1, last_verdict = excluded.last_verdict, updated_at = excluded.updated_at""",
            (reporter_id, name, trust, int(untrusted), verdict, now),
        )


def set_reporter_phone(reporter_id: int, name: Optional[str], phone: str, trust: float) -> None:
    """Simpan nomor HP terverifikasi milik pelapor (membuat catatannya kalau belum ada)."""
    now = time.time()
    with _connect() as conn:
        conn.execute(
            """INSERT INTO reporters (reporter_id, name, trust, untrusted, reports, last_verdict, updated_at, phone)
               VALUES (?, ?, ?, 0, 0, NULL, ?, ?)
               ON CONFLICT(reporter_id) DO UPDATE SET
                   name = excluded.name, trust = excluded.trust, phone = excluded.phone, updated_at = excluded.updated_at""",
            (reporter_id, name, trust, now, phone),
        )


def reset_reporter(reporter_id: int, trust: float) -> None:
    """Operator memulihkan pelapor: keluar dari daftar untrusted, trust kembali ke nilai awal."""
    with _connect() as conn:
        conn.execute(
            "UPDATE reporters SET trust = ?, untrusted = 0, last_verdict = 'DIPULIHKAN OPERATOR', updated_at = ? WHERE reporter_id = ?",
            (trust, time.time(), reporter_id),
        )


def list_reporters() -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM reporters ORDER BY untrusted DESC, trust ASC, updated_at DESC").fetchall()
    return [dict(r) for r in rows]


# -------------------------------------------------- permintaan akses operator
def save_operator_request(address: str, name: str, agency: str, unit: str, contact: str, signature: str) -> None:
    with _connect() as conn:
        conn.execute(
            """INSERT INTO operator_requests (address, name, agency, unit, contact, signature, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(address) DO UPDATE SET
                   name = excluded.name, agency = excluded.agency, unit = excluded.unit,
                   contact = excluded.contact, signature = excluded.signature, created_at = excluded.created_at""",
            (address.lower(), name, agency, unit, contact, signature, time.time()),
        )


def get_operator_request(address: str) -> Optional[dict[str, Any]]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM operator_requests WHERE address = ?", (address.lower(),)).fetchone()
    return dict(row) if row else None


def list_operator_requests() -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM operator_requests ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]
