"""Bot Telegram: formulir laporan bencana dengan tombol (Fase 1).

Alurnya: lokasi -> jumlah pengungsi -> kelompok rentan & kebutuhan -> akses jalan -> korban kritis.
Bot memakai long polling, jadi tidak butuh domain / URL publik: cukup dijalankan di laptop.
"""
import re

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    User,
)

from . import config, db, service

router = Router()


class ReportForm(StatesGroup):
    contact = State()
    location = State()
    disaster = State()
    headcount = State()
    needs = State()
    road = State()
    critical = State()
    critical_count = State()


NEEDS = [("BALITA", "👶 Balita"), ("LANSIA", "👵 Lansia"), ("TENDA", "⛺ Tenda"), ("MEDIS", "💊 Medis darurat")]
ROADS = [
    ("TRUCK_OK", "🚚 Mobil/truk aman"),
    ("TRAIL_BIKE_ONLY", "🏍 Hanya motor trail"),
    ("BOAT_ONLY", "🚤 Terisolir air/perahu"),
    ("UNKNOWN", "❓ Tidak tahu"),
]
# warna prioritas, sama dengan di dashboard: tinggi merah, sedang kuning, rendah hijau
PRIORITY_ICON = {"TINGGI": "🔴", "SEDANG": "🟡", "RENDAH": "🟢"}
DISASTERS = [
    ("GEMPA", "🌍 Gempa bumi"),
    ("BANJIR", "🌊 Banjir"),
    ("LONGSOR", "⛰ Tanah longsor"),
    ("KEBAKARAN", "🔥 Kebakaran"),
    ("ANGIN", "🌪 Angin kencang"),
    ("LAINNYA", "➕ Lainnya"),
]


def _buttons(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=text, callback_data=data) for data, text in row] for row in rows]
    )


def _needs_keyboard(selected: list[str]) -> InlineKeyboardMarkup:
    rows = [[(f"need:{key}", ("✅ " if key in selected else "") + label)] for key, label in NEEDS]
    rows.append([("need:done", "Lanjut ➡️")])
    return _buttons(rows)


# ------------------------------------------------------------------ perintah
@router.message(CommandStart())
@router.message(Command("lapor"))
async def start_report(message: Message, state: FSMContext) -> None:
    await state.clear()
    user = message.from_user
    record = db.get_reporter(user.id) if user else None
    if record and record.get("phone"):
        # nomor HP sudah pernah diverifikasi: langsung ke lokasi
        await _ask_location(message, state, "SOS AI siap menerima laporan.")
        return
    await state.set_state(ReportForm.contact)
    keyboard = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Bagikan nomor HP", request_contact=True)], [KeyboardButton(text="Lewati")]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await message.answer(
        "SOS AI siap menerima laporan.\n\n"
        "Bagikan nomor HP kamu supaya laporan lebih dipercaya dan operator bisa menghubungi balik. "
        "Langkah ini opsional: tekan Lewati kalau tidak mau.",
        reply_markup=keyboard,
    )


@router.message(ReportForm.contact, F.contact)
async def got_contact(message: Message, state: FSMContext) -> None:
    user, contact = message.from_user, message.contact
    # Telegram menyertakan user_id pemilik kontak: harus sama dengan pengirim pesan
    if not user or contact.user_id != user.id:
        await message.answer("Itu bukan nomor dari akun Telegram ini. Pakai tombol \"Bagikan nomor HP\", atau tekan Lewati.")
        return
    result = service.verify_phone(user.id, user.full_name, contact.phone_number)
    note = "Nomor HP terverifikasi."
    if result["volunteer"]:
        note += f" Kamu terdaftar sebagai relawan {result['volunteer']['org']}."
    await _ask_location(message, state, note)


@router.message(ReportForm.contact, F.text, ~F.text.startswith("/"))
async def skip_contact(message: Message, state: FSMContext) -> None:
    await _ask_location(message, state, "Baik, lanjut tanpa nomor HP.")


async def _ask_location(message: Message, state: FSMContext, intro: str) -> None:
    await state.set_state(ReportForm.location)
    keyboard = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📍 Share lokasi bencana", request_location=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await message.answer(
        f"{intro}\n\n"
        "Kirim lokasi posko/pengungsian dengan tombol di bawah.\n"
        "Kalau pakai Telegram di laptop (tombol lokasi tidak muncul), ketik koordinatnya, "
        "contoh: -6.7123, 106.8451",
        reply_markup=keyboard,
    )


@router.message(Command("batal"))
async def cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Laporan dibatalkan. Ketik /lapor untuk mulai lagi.", reply_markup=ReplyKeyboardRemove())


@router.message(Command("id"))
async def show_id(message: Message) -> None:
    user_id = message.from_user.id if message.from_user else 0
    record = db.get_reporter(user_id) or {}
    phone = record.get("phone") or ""
    volunteer = service.agents.tools.volunteer_lookup(phone) if phone else None
    registered = "terdaftar" if (volunteer or user_id in config.REGISTERED_VOLUNTEERS) else "belum terdaftar"
    await message.answer(
        f"ID Telegram kamu: {user_id} ({registered} sebagai relawan)\n"
        f"Nomor HP: {'terverifikasi ' + service.agents.tools.mask_phone(phone) if phone else 'belum dibagikan'}"
    )


# ------------------------------------------------------------- 1. lokasi
async def _ask_disaster(message: Message, state: FSMContext, lat: float, lon: float, source: str) -> None:
    """source: "GPS" kalau lokasi dibagikan dari HP, "MANUAL" kalau koordinatnya diketik."""
    await state.update_data(lat=lat, lon=lon, geo_source=source, needs=[])
    await state.set_state(ReportForm.disaster)
    await message.answer(f"Lokasi diterima: {lat:.4f}, {lon:.4f}", reply_markup=ReplyKeyboardRemove())
    await message.answer(
        "Bencana apa yang terjadi?",
        reply_markup=_buttons([[(f"dis:{key}", label) for key, label in DISASTERS[i : i + 2]] for i in range(0, len(DISASTERS), 2)]),
    )


@router.callback_query(ReportForm.disaster, F.data.startswith("dis:"))
async def got_disaster(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(disaster=callback.data.split(":", 1)[1])
    await state.set_state(ReportForm.headcount)
    await callback.answer()
    await callback.message.answer(
        "Berapa estimasi jumlah pengungsi di titik kamu?",
        reply_markup=_buttons(
            [
                [("hc:<50", "Kurang dari 50"), ("hc:50-200", "50 - 200")],
                [("hc:200-500", "200 - 500"), ("hc:500-1000", "500 - 1000")],
                [("hc:>1000", "Lebih dari 1000")],
            ]
        ),
    )


@router.message(ReportForm.location, F.location)
async def got_location(message: Message, state: FSMContext) -> None:
    await _ask_disaster(message, state, message.location.latitude, message.location.longitude, "GPS")


@router.message(ReportForm.location, F.text)
async def got_location_text(message: Message, state: FSMContext) -> None:
    match = re.fullmatch(r"\s*(-?\d+(?:[.,]\d+)?)\s*[,; ]\s*(-?\d+(?:[.,]\d+)?)\s*", message.text or "")
    if match:
        lat, lon = (float(g.replace(",", ".")) for g in match.groups())
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            await _ask_disaster(message, state, lat, lon, "MANUAL")
            return
    await message.answer("Koordinat belum terbaca. Ketik seperti ini: -6.7123, 106.8451 (atau /batal).")


# ---------------------------------------------------- 2. jumlah pengungsi
@router.callback_query(ReportForm.headcount, F.data.startswith("hc:"))
async def got_headcount(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(headcount=callback.data.split(":", 1)[1])
    await state.set_state(ReportForm.needs)
    await callback.answer()
    await callback.message.answer(
        "Kelompok rentan & kebutuhan paling mendesak? Boleh pilih lebih dari satu, lalu tekan Lanjut.",
        reply_markup=_needs_keyboard([]),
    )


# ------------------------------------------------- 3. kebutuhan (multi-pilih)
@router.callback_query(ReportForm.needs, F.data.startswith("need:"))
async def got_need(callback: CallbackQuery, state: FSMContext) -> None:
    key = callback.data.split(":", 1)[1]
    data = await state.get_data()
    selected: list[str] = list(data.get("needs", []))

    if key != "done":
        if key in selected:
            selected.remove(key)
        else:
            selected.append(key)
        await state.update_data(needs=selected)
        await callback.message.edit_reply_markup(reply_markup=_needs_keyboard(selected))
        await callback.answer()
        return

    await state.set_state(ReportForm.road)
    await callback.answer()
    await callback.message.answer(
        "Kondisi akses jalan ke posko saat ini?",
        reply_markup=_buttons([[(f"road:{key}", label)] for key, label in ROADS]),
    )


# ------------------------------------------------------------ 4. akses jalan
@router.callback_query(ReportForm.road, F.data.startswith("road:"))
async def got_road(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(road=callback.data.split(":", 1)[1])
    await state.set_state(ReportForm.critical)
    await callback.answer()
    await callback.message.answer(
        "Apakah ada korban kritis yang butuh dievakuasi ke rumah sakit?",
        reply_markup=_buttons([[("crit:yes", "🚑 Ada"), ("crit:no", "❌ Tidak ada")]]),
    )


# ---------------------------------------------------------- 5. korban kritis
@router.callback_query(ReportForm.critical, F.data.startswith("crit:"))
async def got_critical(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if callback.data == "crit:yes":
        await state.set_state(ReportForm.critical_count)
        await callback.message.answer("Berapa orang? Ketik angkanya.")
        return
    await state.update_data(critical_count=0)
    await _finish(callback.message, state, callback.from_user)


@router.message(ReportForm.critical_count, F.text)
async def got_critical_count(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text.isdigit() or not (1 <= int(text) <= 500):
        await message.answer("Ketik angka saja, contoh: 2")
        return
    await state.update_data(critical_count=int(text))
    await _finish(message, state, message.from_user)


# ------------------------------------------------------------------ selesai
async def _finish(message: Message, state: FSMContext, user: User | None) -> None:
    data = await state.get_data()
    await state.clear()
    payload = {
        "geo": {"lat": round(data["lat"], 5), "lon": round(data["lon"], 5)},
        "geo_source": data.get("geo_source", "MANUAL"),
        "disaster_type": data.get("disaster", "LAINNYA"),
        "headcount_range": data["headcount"],
        "needs": sorted(data.get("needs", [])),
        "road_status": data["road"],
        "critical_count": data.get("critical_count", 0),
    }
    await message.answer("⏳ Laporan diterima. Agent sedang memverifikasi, tunggu sebentar...")
    try:
        report = await service.process_report(
            payload,
            chat_id=message.chat.id,
            reporter_id=user.id if user else None,
            reporter_name=user.full_name if user else None,
        )
    except Exception:  # noqa: BLE001
        service.logger.exception("Gagal memproses laporan")
        await message.answer("Maaf, laporan gagal diproses. Coba kirim ulang dengan /lapor.")
        return

    card = report["plan"]
    lines = [f"📋 Laporan {report['code']} tercatat."]
    if card["recommendation"] == "KIRIM":
        lines += [
            f"Lokasi: {card['target']}",
            f"Prioritas: {PRIORITY_ICON.get(card['urgency_level'], '')} {card['urgency_level']} ({card['urgency_score']}/100)",
            f"Verifikasi: {card['verification']['verdict'].lower()}, keyakinan {card['verification']['confidence'].lower()}",
            f"Rencana: {card['fleet']}, perkiraan {card['eta_min']} menit",
            "",
            "Sekarang menunggu persetujuan operator. Kamu akan dikabari di sini.",
        ]
    else:
        # detail verifikasi (termasuk status untrusted) hanya untuk operator, tidak dikirim ke pelapor
        lines += [
            "Laporan ini belum lolos verifikasi otomatis dan diteruskan ke operator untuk ditinjau.",
            "Kalau kondisinya darurat, kirim ulang dengan /lapor dan pastikan lokasi serta jumlah pengungsi sudah tepat.",
        ]
    if report.get("tx_submit"):
        lines.append(f"\nBukti on-chain: {report['tx_submit'][:18]}...")
    await message.answer("\n".join(lines))


# ------------------------------------------------- konfirmasi bantuan sampai
@router.callback_query(F.data.startswith("delivered:"))
async def delivered(callback: CallbackQuery) -> None:
    code = callback.data.split(":", 1)[1]
    await callback.answer("Mencatat...")
    ok, text = await service.confirm_delivery(code)
    if ok:
        await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(("📦 " if ok else "⚠️ ") + f"{code}: {text}")


# --------------------------------------------------------- tombol kedaluwarsa
@router.callback_query()
async def stale_button(callback: CallbackQuery) -> None:
    await callback.answer("Tombol ini sudah tidak berlaku. Ketik /lapor untuk mulai lagi.", show_alert=True)


@router.message()
async def fallback(message: Message) -> None:
    await message.answer("Ketik /lapor untuk mengirim laporan bencana, atau /batal untuk membatalkan.")


def create() -> tuple[Bot, Dispatcher]:
    bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    return bot, dp
