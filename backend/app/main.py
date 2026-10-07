"""Titik masuk aplikasi. Jalankan dari folder backend:

    uvicorn app.main:app --port 8000

Satu proses ini menjalankan: API, dashboard (http://localhost:8000), bot Telegram, dan
pengecekan status on-chain.
"""
import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import agents, auth, chain, config, db, service, tools

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("sos")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    tasks: list[asyncio.Task] = []
    bot = None

    if config.TELEGRAM_BOT_TOKEN:
        from . import telegram_bot

        bot, dp = telegram_bot.create()
        service.bot = bot
        tasks.append(asyncio.create_task(dp.start_polling(bot, handle_signals=False)))
        logger.info("Bot Telegram: AKTIF")
    else:
        logger.warning("Bot Telegram: MATI (TELEGRAM_BOT_TOKEN kosong)")

    if config.CHAIN_ENABLED:
        tasks.append(asyncio.create_task(service.poll_loop()))
        logger.info("Blockchain: AKTIF, chain %s, contract %s", config.CHAIN_ID, config.CONTRACT_ADDRESS)
    else:
        logger.warning("Blockchain: MATI (mode tanpa chain). Isi CONTRACT_ADDRESS + RELAYER_PRIVATE_KEY.")

    logger.info("LLM: %s", "AKTIF" if config.LLM_ENABLED else "MATI (alasan rekomendasi pakai template)")
    logger.info("Dashboard: http://localhost:8000")

    yield

    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    if bot:
        await bot.session.close()


app = FastAPI(title="SOS AI", lifespan=lifespan)


def _public(r: dict[str, Any]) -> dict[str, Any]:
    """Buang data yang tidak perlu keluar dari server (chat Telegram pelapor)."""
    return {k: v for k, v in r.items() if k not in ("chat_id", "notified")}


def _must_get(code: str) -> dict[str, Any]:
    r = db.get(code)
    if not r:
        raise HTTPException(404, "Laporan tidak ditemukan")
    return r


@app.get("/api/config")
async def get_config() -> dict[str, Any]:
    return {
        "version": config.VERSION,
        "chainEnabled": config.CHAIN_ENABLED,
        "chainId": config.CHAIN_ID,
        "chainName": config.CHAIN_NAME,
        "rpcUrl": config.RPC_URL,
        "explorerUrl": config.EXPLORER_URL,
        "contractAddress": config.CONTRACT_ADDRESS,
        "abi": config.CONTRACT_ABI,
        # semua pos asal bantuan (kantor BPBD) untuk ditampilkan di peta
        "staging": tools.staging_areas(),
        # tidak dipakai lagi sejak v9. Tetap dikirim supaya dashboard versi lama yang masih
        # tersimpan di browser tidak macet, dan bisa menampilkan peringatan "Versi tidak cocok".
        "depot": {"name": config.DEPOT_NAME, "lat": config.DEPOT_LAT, "lon": config.DEPOT_LON},
        "telegramEnabled": bool(config.TELEGRAM_BOT_TOKEN),
        "llmEnabled": config.LLM_ENABLED,
        "allowSimulate": config.ALLOW_SIMULATE,
    }


@app.get("/api/reports")
async def list_reports() -> list[dict[str, Any]]:
    return [_public(r) for r in db.list_all()]


@app.get("/api/reports/{code}")
async def get_report(code: str) -> dict[str, Any]:
    return _public(_must_get(code))


class SyncBody(BaseModel):
    tx_hash: str | None = None


@app.post("/api/reports/{code}/sync")
async def sync_report(code: str, body: SyncBody | None = None) -> dict[str, Any]:
    """Dipanggil dashboard setelah transaksi operator masuk blok."""
    _must_get(code)
    try:
        r = await service.sync_report(code, body.tx_hash if body else None)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Gagal membaca blockchain: {str(exc)[:160]}") from exc
    return _public(r)  # type: ignore[arg-type]


@app.get("/api/reports/{code}/onchain")
async def read_onchain(code: str) -> dict[str, Any]:
    """Cadangan untuk dashboard kalau browser tidak bisa menghubungi RPC secara langsung."""
    _must_get(code)
    if not config.CHAIN_ENABLED:
        raise HTTPException(404, "Mode tanpa chain")
    try:
        return await asyncio.to_thread(chain.get_report, code)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Gagal membaca blockchain: {str(exc)[:160]}") from exc


class Decision(BaseModel):
    approve: bool


@app.post("/api/reports/{code}/decide-offchain")
async def decide_offchain(code: str, decision: Decision) -> dict[str, Any]:
    """Hanya aktif di mode tanpa chain. Kalau chain aktif, keputusan WAJIB lewat wallet."""
    if config.CHAIN_ENABLED:
        raise HTTPException(403, "Blockchain aktif: keputusan harus ditandatangani wallet operator")
    _must_get(code)
    try:
        r = await service.decide_offchain(code, decision.approve)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _public(r)  # type: ignore[arg-type]


@app.get("/api/reporters")
async def list_reporters() -> list[dict[str, Any]]:
    """Daftar pelapor beserta trust score. Yang untrusted muncul paling atas.

    Nomor HP hanya keluar dalam bentuk tersamar.
    """
    out = []
    for r in db.list_reporters():
        phone = r.pop("phone", None) or ""
        volunteer = agents.tools.volunteer_lookup(phone) if phone else None
        out.append({**r, "phone_masked": agents.tools.mask_phone(phone), "volunteer_org": volunteer["org"] if volunteer else ""})
    return out


@app.post("/api/reporters/{reporter_id}/reset")
async def reset_reporter(reporter_id: int) -> dict[str, Any]:
    """Operator memulihkan pelapor dari daftar untrusted."""
    if not db.get_reporter(reporter_id):
        raise HTTPException(404, "Pelapor tidak ditemukan")
    record = db.get_reporter(reporter_id)
    phone = record.get("phone") or ""  # type: ignore[union-attr]
    registered = reporter_id in config.REGISTERED_VOLUNTEERS or bool(phone and agents.tools.volunteer_lookup(phone))
    db.reset_reporter(reporter_id, agents.base_trust(registered, bool(phone)))
    return {"ok": True}


SAMPLES = [
    {
        "geo": {"lat": -6.7123, "lon": 106.8451},
        "disaster_type": "LONGSOR",
        "headcount_range": "50-200",
        "needs": ["BALITA", "LANSIA", "MEDIS"],
        "road_status": "TRAIL_BIKE_ONLY",
        "critical_count": 2,
    },
    {
        "geo": {"lat": -6.7935, "lon": 107.0731},
        "disaster_type": "GEMPA",
        "headcount_range": "500-1000",
        "needs": ["BALITA", "MEDIS", "TENDA"],
        "road_status": "UNKNOWN",
        "critical_count": 0,
    },
    {
        "geo": {"lat": -6.5889, "lon": 106.9856},
        "disaster_type": "BANJIR",
        "headcount_range": "<50",
        "needs": ["LANSIA", "TENDA"],
        "road_status": "BOAT_ONLY",
        "critical_count": 1,
    },
    {
        # sengaja janggal: ">1000 pengungsi" di kawasan puncak Gunung Gede yang nyaris tak berpenghuni
        "geo": {"lat": -6.7870, "lon": 106.9820},
        "disaster_type": "BANJIR",
        "headcount_range": ">1000",
        "needs": ["TENDA"],
        "road_status": "TRUCK_OK",
        "critical_count": 0,
    },
    {
        # luar Jawa: Bangkinang, Kampar. Pos asal seharusnya BPBD Provinsi Riau, bukan pos di Jawa
        "geo": {"lat": 0.3411, "lon": 101.0260},
        "disaster_type": "BANJIR",
        "headcount_range": "200-500",
        "needs": ["BALITA", "TENDA"],
        "road_status": "TRUCK_OK",
        "critical_count": 0,
    },
    {
        # Sigi, Sulawesi Tengah. Pos asal seharusnya BPBD Sulawesi Tengah (Palu)
        "geo": {"lat": -1.0500, "lon": 119.8900},
        "disaster_type": "GEMPA",
        "headcount_range": "50-200",
        "needs": ["MEDIS", "TENDA"],
        "road_status": "UNKNOWN",
        "critical_count": 3,
    },
]


@app.post("/api/simulate")
async def simulate() -> dict[str, Any]:
    """Buat laporan contoh tanpa Telegram (untuk tes dan demo)."""
    if not config.ALLOW_SIMULATE:
        raise HTTPException(403, "Simulasi dimatikan (ALLOW_SIMULATE=false)")
    sample = SAMPLES[len(db.list_all()) % len(SAMPLES)]
    r = await service.process_report({**sample, "geo_source": "SIMULASI"}, reporter_name="Laporan contoh")
    return _public(r)


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "version": config.VERSION, "relayer": chain.relayer_address() if config.CHAIN_ENABLED else None}


app.include_router(auth.router)


# Halaman: landing page di "/", masuk/daftar di "/login", command center di "/app"
def _page(name: str) -> FileResponse:
    return FileResponse(config.DASHBOARD_DIR / name, headers={"Cache-Control": "no-cache"})


@app.get("/", include_in_schema=False)
async def landing_page() -> FileResponse:
    return _page("landing.html" if (config.DASHBOARD_DIR / "landing.html").exists() else "index.html")


@app.get("/login", include_in_schema=False)
async def login_page() -> FileResponse:
    return _page("login.html")


@app.get("/app", include_in_schema=False)
async def app_page() -> FileResponse:
    return _page("index.html")


# Dashboard (file statis) dipasang paling akhir supaya tidak menutupi /api/...
class _FreshStatic(StaticFiles):
    """File dashboard selalu dicek ulang ke server, supaya browser tidak memakai app.js lama
    setelah update. Folder vendor (Leaflet, ethers) tidak pernah berubah, jadi boleh disimpan."""

    async def get_response(self, path: str, scope):  # type: ignore[override]
        response = await super().get_response(path, scope)
        if not path.replace("\\", "/").startswith("vendor/"):
            response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/", _FreshStatic(directory=config.DASHBOARD_DIR, html=True), name="dashboard")
