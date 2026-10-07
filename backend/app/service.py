"""Alur kerja inti: laporan masuk -> catat on-chain -> agent -> tunggu operator -> kabari pelapor."""
import asyncio
import json
import logging
import secrets
from datetime import datetime, timezone
from typing import Any, Optional

from . import agents, chain, config, db

logger = logging.getLogger("sos")

# Diisi oleh main.py kalau bot Telegram aktif. Dipakai untuk mengirim notifikasi.
bot: Any = None

# Hanya satu proses pencatatan ke blockchain dalam satu waktu. Tanpa kunci ini, pengecekan
# berkala (poll_loop) bisa ikut mencatat laporan yang sedang dicatat process_report, dan
# percobaan kedua ditolak contract (WrongStatus / ReportExists).
_anchor_lock = asyncio.Lock()

_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # tanpa huruf/angka yang mirip (0/O, 1/I)


def new_code() -> str:
    return "SOS-" + "".join(secrets.choice(_CODE_ALPHABET) for _ in range(6))


def verify_phone(reporter_id: int, name: Optional[str], raw_phone: str) -> dict[str, Any]:
    """Pelapor membagikan kontaknya di bot (bot sudah memastikan itu nomor miliknya sendiri).

    Nomor disimpan di tabel pelapor, tidak pernah ikut ke data yang di-hash ke blockchain.
    Trust score: pelapor baru mulai dari nilai awal dengan bonus nomor HP; pelapor lama
    mendapat bonus itu sekali. Relawan terdaftar (tidak untrusted) minimal 0,95.
    """
    phone = agents.tools.normalize_phone(raw_phone)
    volunteer = agents.tools.volunteer_lookup(phone)
    registered = bool(volunteer) or reporter_id in config.REGISTERED_VOLUNTEERS
    record = db.get_reporter(reporter_id)
    if not record:
        trust = agents.base_trust(registered, True)
    else:
        trust = float(record["trust"])
        if not record.get("phone"):
            trust += agents.TRUST_PHONE_BONUS
        if registered and not record["untrusted"]:
            trust = max(trust, agents.TRUST_REGISTERED)
        trust = round(max(0.05, min(0.99, trust)), 2)
    db.set_reporter_phone(reporter_id, name, phone, trust)
    return {"phone": phone, "volunteer": volunteer, "registered": registered, "trust": trust}


def find_nearby(payload: dict[str, Any], reporter_id: Optional[int]) -> list[dict[str, Any]]:
    """Konfirmasi silang: laporan dari PELAPOR LAIN di dekat titik ini dalam beberapa jam terakhir.

    Yang dihitung hanya laporan dari Telegram (punya ID pelapor), bukan laporan simulasi,
    bukan laporan yang sudah ditolak, dan jenis bencananya harus sama (atau salah satunya "Lainnya").
    """
    lat, lon = payload["geo"]["lat"], payload["geo"]["lon"]
    kind = payload.get("disaster_type") or "LAINNYA"
    since = datetime.now(timezone.utc).timestamp() - config.NEARBY_HOURS * 3600
    found = []
    for r in db.list_all():
        if r["created_at"] < since or r["status"] == "REJECTED":
            continue
        other_id = r.get("reporter_id")
        if not other_id or other_id == reporter_id:
            continue
        if (r["plan"] or {}).get("recommendation") == "TOLAK":
            continue
        other = r["payload"]
        other_kind = other.get("disaster_type") or "LAINNYA"
        if kind != other_kind and "LAINNYA" not in (kind, other_kind):
            continue
        distance = agents.tools.haversine_km(lat, lon, other["geo"]["lat"], other["geo"]["lon"])
        if distance <= config.NEARBY_KM:
            found.append({"code": r["code"], "distance_km": round(distance, 1), "disaster_type": other_kind})
    return found


def canonical(obj: Any) -> str:
    """JSON dengan urutan key tetap. Teks inilah yang di-hash dan disimpan apa adanya."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


async def process_report(
    payload: dict[str, Any],
    chat_id: Optional[int] = None,
    reporter_id: Optional[int] = None,
    reporter_name: Optional[str] = None,
) -> dict[str, Any]:
    """Dipanggil bot (atau tombol simulasi) setelah formulir laporan lengkap."""
    code = new_code()
    payload = {
        **payload,
        "code": code,
        "reported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        # angka acak supaya hash tidak bisa ditebak dengan mencoba-coba isi laporan
        "nonce": secrets.token_hex(8),
    }
    payload_text = canonical(payload)

    db.insert(
        {
            "code": code,
            "report_id": chain.report_id(code),
            "chat_id": chat_id,
            "reporter_id": reporter_id,
            "reporter_name": reporter_name,
            "status": "REPORTED",
            "payload_text": payload_text,
            "payload_hash": chain.keccak_text(payload_text),
        }
    )

    # Fase 2: pipeline agent. Pelapor yang sudah dikenal membawa trust score terakhirnya.
    record = db.get_reporter(reporter_id) if reporter_id else None
    nearby = find_nearby(payload, reporter_id)
    state = await agents.run_pipeline(code, payload, reporter_id, record, nearby)
    card = state["card"]

    # simpan trust score baru. Sekali masuk daftar untrusted, pelapor baru keluar setelah
    # trust-nya naik lagi ke 0.60 (lewat laporan yang terkonfirmasi) atau dipulihkan operator.
    ver = state["verification"]
    if reporter_id:
        was_untrusted = bool(record and record["untrusted"])
        untrusted = bool(ver.get("mark_untrusted")) or (was_untrusted and ver["trust_after"] < 0.60)
        db.save_reporter(reporter_id, reporter_name, ver["trust_after"], untrusted, ver["verdict"])
    plan_text = canonical(card)

    db.update(
        code,
        status="VERIFIED",
        urgency=card["urgency_score"],
        plan_text=plan_text,
        plan_hash=chain.keccak_text(plan_text),
        verification=state["verification"],
        log=state["log"],
    )

    # catat hash laporan + hash rencana ke blockchain
    await ensure_anchored(code)
    return db.get(code)  # type: ignore[return-value]


async def ensure_anchored(code: str) -> None:
    """Pastikan laporan sudah tercatat on-chain. Aman dipanggil berulang (melengkapi yang kurang)."""
    if not config.CHAIN_ENABLED:
        return
    async with _anchor_lock:
        # baca ulang SETELAH mendapat kunci: mungkin sudah dicatat oleh pemanggil sebelumnya
        r = db.get(code)
        if not r:
            return
        log = list(r["log"])
        try:
            if not r["tx_submit"]:
                tx = await asyncio.to_thread(chain.submit_report, code, r["payload_hash"])
                log.append(f"[CHAIN] submitReport tercatat: {tx}")
                db.update(code, tx_submit=tx, log=log)
            if r["plan_hash"] and not r["tx_verify"]:
                tx = await asyncio.to_thread(chain.mark_verified, code, r["plan_hash"], r["urgency"])
                log.append(f"[CHAIN] markVerified tercatat: {tx}")
                db.update(code, tx_verify=tx, log=log)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Gagal mencatat %s ke blockchain", code)
            msg = f"[CHAIN] GAGAL mencatat ke blockchain: {str(exc)[:200]}"
            if not log or log[-1] != msg:
                log.append(msg)
                db.update(code, log=log)


async def sync_report(code: str, tx_hash: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Samakan status di database dengan status di blockchain, lalu kabari pelapor kalau ada keputusan.

    tx_hash = hash transaksi keputusan operator (dikirim dashboard), disimpan supaya bisa ditautkan.
    """
    r = db.get(code)
    if not r or not config.CHAIN_ENABLED:
        return r
    await ensure_anchored(code)
    if tx_hash and len(tx_hash) == 66 and tx_hash.startswith("0x") and not r.get("tx_decide"):
        db.update(code, tx_decide=tx_hash)

    onchain = await asyncio.to_thread(chain.get_report, code)
    status = onchain["status"]
    if status in ("APPROVED", "REJECTED", "DELIVERED") and r["status"] != status:
        log = list(db.get(code)["log"])  # type: ignore[index]
        if status in ("APPROVED", "REJECTED"):
            verb = "menyetujui" if status == "APPROVED" else "menolak"
            log.append(f"[OPERATOR] Wallet {onchain['decided_by']} {verb} laporan ini on-chain")
        db.update(code, status=status, decided_by=onchain["decided_by"], log=log)

    await notify_decision(code)
    return db.get(code)


async def decide_offchain(code: str, approve: bool) -> Optional[dict[str, Any]]:
    """Hanya untuk mode tanpa chain: keputusan operator disimpan di database saja."""
    r = db.get(code)
    if not r or r["status"] != "VERIFIED":
        return r
    if approve and (r["plan"] or {}).get("recommendation") != "KIRIM":
        raise ValueError("Laporan ini direkomendasikan tolak dan tidak punya rencana pengiriman")
    status = "APPROVED" if approve else "REJECTED"
    log = list(r["log"]) + [f"[OPERATOR] Laporan {'disetujui' if approve else 'ditolak'} (mode tanpa chain)"]
    db.update(code, status=status, decided_by="off-chain", log=log)
    await notify_decision(code)
    return db.get(code)


async def notify_decision(code: str) -> None:
    """Fase 3: kabari pelapor di Telegram, sekali saja."""
    r = db.get(code)
    if not r or r["notified"] or r["status"] not in ("APPROVED", "REJECTED", "DELIVERED"):
        return
    db.update(code, notified=1)
    if not bot or not r["chat_id"]:
        return
    try:
        if r["status"] == "REJECTED":
            await bot.send_message(
                r["chat_id"],
                f"Laporan {code} tidak disetujui operator. Kalau kondisi berubah, kirim laporan baru dengan /lapor.",
            )
            return

        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        card = r["plan"] or {}
        lines = [f"✅ Laporan {code} disetujui operator."]
        if card.get("recommendation") == "KIRIM":
            lines.append(f"{card['fleet']} berangkat dari {card['origin']}, perkiraan tiba {card['eta_min']} menit.")
            if card.get("cargo_back"):
                lines.append(f"{card['cargo_back']} saat armada kembali.")
        else:
            # disetujui langsung lewat contract walau agent merekomendasikan tolak: belum ada rencana otomatis
            lines.append("Operator menindaklanjuti laporan ini secara manual.")
        lines.append("Tekan tombol di bawah setelah bantuan benar-benar sampai.")
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="📦 Bantuan sudah sampai", callback_data=f"delivered:{code}")]]
        )
        await bot.send_message(r["chat_id"], "\n".join(lines), reply_markup=keyboard)
    except Exception:  # noqa: BLE001
        logger.exception("Gagal mengirim notifikasi Telegram untuk %s", code)


async def confirm_delivery(code: str) -> tuple[bool, str]:
    """Pelapor menekan 'Bantuan sudah sampai'."""
    r = db.get(code)
    if not r:
        return False, "Laporan tidak ditemukan."
    if r["status"] == "DELIVERED":
        return True, "Sudah tercatat sebelumnya. Terima kasih."
    if r["status"] != "APPROVED":
        return False, "Laporan ini belum disetujui operator."

    log = list(r["log"])
    tx = None
    if config.CHAIN_ENABLED:
        try:
            tx = await asyncio.to_thread(chain.confirm_delivery, code)
            log.append(f"[CHAIN] confirmDelivery tercatat: {tx}")
        except Exception as exc:  # noqa: BLE001
            logger.exception("confirmDelivery gagal untuk %s", code)
            return False, f"Gagal mencatat ke blockchain: {str(exc)[:120]}"
    log.append("[PELAPOR] Bantuan dikonfirmasi sampai")
    db.update(code, status="DELIVERED", tx_delivery=tx, log=log)
    return True, "Tercatat. Terima kasih, laporan ditutup."


async def poll_loop(interval: float = 6.0) -> None:
    """Cek berkala: apakah operator sudah memutuskan laporan yang menunggu?"""
    while True:
        await asyncio.sleep(interval)
        for r in db.list_by_status("VERIFIED"):
            try:
                await sync_report(r["code"])
            except Exception:  # noqa: BLE001
                logger.exception("Sinkronisasi %s gagal", r["code"])
