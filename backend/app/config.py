"""Semua pengaturan dibaca dari backend/.env di sini, supaya file lain tinggal import."""
import json
import os
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = BACKEND_DIR.parent
DASHBOARD_DIR = PROJECT_DIR / "dashboard"

load_dotenv(BACKEND_DIR / ".env")


def _get(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


# Nomor versi paket. Dashboard menampilkan dan membandingkannya, supaya ketahuan kalau
# ada file yang belum ikut tertimpa saat update.
VERSION = "11"

# Telegram
TELEGRAM_BOT_TOKEN = _get("TELEGRAM_BOT_TOKEN")
REGISTERED_VOLUNTEERS = {
    int(x) for x in _get("REGISTERED_VOLUNTEERS").replace(" ", "").split(",") if x.lstrip("-").isdigit()
}

# Blockchain
RPC_URL = _get("RPC_URL", "http://127.0.0.1:8545")
CHAIN_ID = int(_get("CHAIN_ID", "31337"))
CHAIN_NAME = _get("CHAIN_NAME", "Hardhat Lokal")
EXPLORER_URL = _get("EXPLORER_URL").rstrip("/")
CONTRACT_ADDRESS = _get("CONTRACT_ADDRESS")
RELAYER_PRIVATE_KEY = _get("RELAYER_PRIVATE_KEY")
CHAIN_ENABLED = bool(CONTRACT_ADDRESS and RELAYER_PRIVATE_KEY)

ABI_PATH = Path(__file__).resolve().parent / "abi.json"
CONTRACT_ABI = json.loads(ABI_PATH.read_text(encoding="utf-8"))

# Pos asal bantuan.
# Sumber utama: daftar kantor BPBD di data/bpbd_staging_areas.json. Agent 2 memilih pos
# terdekat ke lokasi laporan. DEPOT_* hanya cadangan kalau file itu tidak ada atau kosong.
STAGING_PATH = BACKEND_DIR / "data" / "bpbd_staging_areas.json"
# berapa pos terdekat (garis lurus) yang dibandingkan waktu tempuhnya lewat jalan
STAGING_CANDIDATES = max(1, int(_get("STAGING_CANDIDATES", "3")))
# di atas jarak ini kartu dispatch diberi peringatan "pos asal jauh"
STAGING_FAR_KM = float(_get("STAGING_FAR_KM", "150"))
DEPOT_NAME = _get("DEPOT_NAME", "Pos cadangan (DEPOT di .env)")
DEPOT_LAT = float(_get("DEPOT_LAT", "-6.1643"))
DEPOT_LON = float(_get("DEPOT_LON", "106.8142"))

# LLM (opsional)
LLM_API_KEY = _get("LLM_API_KEY")
LLM_BASE_URL = _get("LLM_BASE_URL").rstrip("/")
LLM_MODEL = _get("LLM_MODEL")
LLM_ENABLED = bool(LLM_API_KEY and LLM_BASE_URL and LLM_MODEL)
# true = model di LLM_MODEL bisa membaca gambar (dipakai Agent 2 untuk snapshot CCTV)
LLM_VISION_ENABLED = LLM_ENABLED and _get("LLM_VISION", "false").lower() in ("1", "true", "yes")

# Data acuan verifikasi (Agent 2)
DATA_DIR = BACKEND_DIR / "data"
CENSUS_PATH = DATA_DIR / "penduduk_desa.csv"
CCTV_PATH = DATA_DIR / "cctv.csv"
VOLUNTEERS_PATH = DATA_DIR / "relawan.csv"
CENSUS_MAX_KM = float(_get("CENSUS_MAX_KM", "1"))
# estimasi penduduk di sekitar titik laporan (WorldPop) dan angka resmi kecamatan (WebAPI BPS)
WORLDPOP_ENABLED = _get("WORLDPOP_ENABLED", "true").lower() in ("1", "true", "yes")
POP_RADIUS_KM = float(_get("POP_RADIUS_KM", "2"))
# batas waktu (detik) supaya pipeline agent tidak tertahan oleh layanan yang lambat
WORLDPOP_TIMEOUT = float(_get("WORLDPOP_TIMEOUT", "15"))
BPS_TIMEOUT = float(_get("BPS_TIMEOUT", "10"))
BPS_API_KEY = _get("BPS_API_KEY")
# key gratis NASA FIRMS untuk titik api satelit (dipakai kalau jenis bencana = kebakaran)
FIRMS_MAP_KEY = _get("FIRMS_MAP_KEY")
# konfirmasi silang: laporan lain dalam radius dan rentang waktu ini dianggap menguatkan
NEARBY_KM = float(_get("NEARBY_KM", "3"))
NEARBY_HOURS = float(_get("NEARBY_HOURS", "24"))
BPS_REGIONS_PATH = DATA_DIR / "bps_wilayah.csv"
CCTV_MAX_KM = float(_get("CCTV_MAX_KM", "5"))

# Lain-lain
ALLOW_SIMULATE = _get("ALLOW_SIMULATE", "true").lower() in ("1", "true", "yes")
DB_PATH = BACKEND_DIR / "sos.db"
