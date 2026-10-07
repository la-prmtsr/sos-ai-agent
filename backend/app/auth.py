"""Masuk dan daftar operator dengan wallet (tanpa kata sandi).

Cara kerjanya:
  1. Dashboard meminta "tantangan": sebuah pesan teks berisi nonce acak.
  2. Wallet menandatangani pesan itu (gratis, bukan transaksi).
  3. Backend memulihkan alamat dari tanda tangan. Kalau cocok, pemilik wallet terbukti.
  4. Peran dibaca langsung dari smart contract: owner(), isOperator(alamat).

Pendaftaran hanya mencatat PERMINTAAN akses. Yang memberi kuasa tetap contract:
pemilik contract harus memanggil setOperator(alamat, true) dari wallet-nya sendiri.

Catatan batas: sesi disimpan di memori (hilang saat backend di-restart), dan endpoint
laporan belum mewajibkan sesi. Pengaman yang sebenarnya ada di contract: hanya wallet
operator yang bisa menyetujui laporan.
"""
import asyncio
import secrets
import time
from datetime import datetime, timezone
from typing import Any, Optional

from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field
from web3 import Web3

from . import chain, config, db

router = APIRouter(prefix="/api/auth")

CHALLENGE_TTL = 5 * 60        # tantangan berlaku 5 menit
SESSION_TTL = 12 * 60 * 60    # sesi berlaku 12 jam
AGENCIES = ["BNPB Pusat", "BPBD Provinsi", "BPBD Kabupaten/Kota", "Lainnya"]

_challenges: dict[tuple[str, str], dict[str, Any]] = {}  # (alamat, tujuan) -> tantangan
_sessions: dict[str, dict[str, Any]] = {}                # token -> sesi


def _clean(text: str, limit: int) -> str:
    """Rapikan isian formulir: satu baris, tanpa spasi berlebih, dibatasi panjangnya."""
    return " ".join((text or "").split())[:limit]


def _address(raw: str) -> str:
    if not Web3.is_address(raw or ""):
        raise HTTPException(400, "Alamat wallet tidak valid")
    return Web3.to_checksum_address(raw)


def _require_chain() -> None:
    if not config.CHAIN_ENABLED:
        raise HTTPException(409, "Mode tanpa chain: masuk dengan wallet belum aktif")


async def _role(address: str) -> str:
    """Peran sebuah alamat menurut smart contract."""
    owner = await asyncio.to_thread(chain.owner)
    if owner.lower() == address.lower():
        return "owner"
    if await asyncio.to_thread(chain.is_operator, address):
        return "operator"
    return "pending" if db.get_operator_request(address) else "unregistered"


def _profile(address: str) -> Optional[dict[str, Any]]:
    row = db.get_operator_request(address)
    return {"name": row["name"], "agency": row["agency"], "unit": row["unit"]} if row else None


class ChallengeBody(BaseModel):
    address: str
    purpose: str = Field(pattern="^(login|register)$")
    name: str = ""
    agency: str = ""
    unit: str = ""
    contact: str = ""


@router.post("/challenge")
async def challenge(body: ChallengeBody) -> dict[str, Any]:
    """Langkah 1: buat pesan yang harus ditandatangani wallet."""
    _require_chain()
    address = _address(body.address)
    nonce = secrets.token_hex(8)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    fields: dict[str, str] = {}

    if body.purpose == "register":
        fields = {
            "name": _clean(body.name, 80),
            "agency": _clean(body.agency, 40),
            "unit": _clean(body.unit, 80),
            "contact": _clean(body.contact, 80),
        }
        if len(fields["name"]) < 3 or len(fields["unit"]) < 2:
            raise HTTPException(400, "Nama dan wilayah atau unit kerja wajib diisi")
        if fields["agency"] not in AGENCIES:
            raise HTTPException(400, "Pilih instansi dari daftar")
        lines = [
            "SOS AI: permintaan akses operator",
            "",
            f"Nama: {fields['name']}",
            f"Instansi: {fields['agency']}",
            f"Wilayah atau unit: {fields['unit']}",
        ]
    else:
        lines = ["SOS AI: masuk ke command center", ""]

    lines += [
        f"Wallet: {address}",
        f"Jaringan: {config.CHAIN_NAME} ({config.CHAIN_ID})",
        f"Waktu: {stamp}",
        f"Nonce: {nonce}",
        "",
        "Menandatangani pesan ini gratis dan tidak mengirim transaksi apa pun.",
    ]
    message = "\n".join(lines)
    _challenges[(address, body.purpose)] = {"message": message, "fields": fields, "expires": time.time() + CHALLENGE_TTL}
    return {"message": message}


class SignedBody(BaseModel):
    address: str
    signature: str


def _verify(address: str, purpose: str, signature: str) -> dict[str, Any]:
    """Langkah 3: pastikan tanda tangan memang dibuat oleh wallet itu atas tantangan kita."""
    entry = _challenges.pop((address, purpose), None)
    if not entry or entry["expires"] < time.time():
        raise HTTPException(400, "Permintaan masuk sudah kedaluwarsa. Ulangi dari awal.")
    try:
        signer = Account.recover_message(encode_defunct(text=entry["message"]), signature=signature)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, "Tanda tangan tidak terbaca") from exc
    if signer.lower() != address.lower():
        raise HTTPException(401, "Tanda tangan bukan dari wallet ini")
    return entry


def _new_session(address: str, role: str) -> str:
    now = time.time()
    for token in [t for t, s in _sessions.items() if s["expires"] < now]:
        _sessions.pop(token, None)
    token = secrets.token_urlsafe(24)
    _sessions[token] = {"address": address, "role": role, "expires": now + SESSION_TTL}
    return token


@router.post("/login")
async def login(body: SignedBody) -> dict[str, Any]:
    _require_chain()
    address = _address(body.address)
    _verify(address, "login", body.signature)
    role = await _role(address)
    return {
        "token": _new_session(address, role),
        "address": address,
        "role": role,
        "profile": _profile(address),
        "expires_in": SESSION_TTL,
    }


@router.post("/register")
async def register(body: SignedBody) -> dict[str, Any]:
    _require_chain()
    address = _address(body.address)
    entry = _verify(address, "register", body.signature)
    f = entry["fields"]
    db.save_operator_request(address, f["name"], f["agency"], f["unit"], f["contact"], body.signature)
    role = await _role(address)
    return {
        "token": _new_session(address, role),
        "address": address,
        "role": role,
        "profile": _profile(address),
        "expires_in": SESSION_TTL,
    }


def _session(authorization: Optional[str]) -> dict[str, Any]:
    token = (authorization or "").removeprefix("Bearer ").strip()
    session = _sessions.get(token)
    if not session or session["expires"] < time.time():
        raise HTTPException(401, "Sesi berakhir. Masuk lagi dengan wallet.")
    return session


@router.get("/requests")
async def requests(authorization: Optional[str] = Header(default=None)) -> list[dict[str, Any]]:
    """Daftar permintaan akses. Hanya untuk pemilik contract (dicek ulang ke contract)."""
    _require_chain()
    session = _session(authorization)
    if (await asyncio.to_thread(chain.owner)).lower() != session["address"].lower():
        raise HTTPException(403, "Hanya pemilik contract yang bisa melihat permintaan akses")
    rows = db.list_operator_requests()
    states = await asyncio.gather(*(asyncio.to_thread(chain.is_operator, r["address"]) for r in rows))
    return [
        {"address": r["address"], "name": r["name"], "agency": r["agency"], "unit": r["unit"],
         "contact": r["contact"], "created_at": r["created_at"], "is_operator": bool(active)}
        for r, active in zip(rows, states)
    ]
