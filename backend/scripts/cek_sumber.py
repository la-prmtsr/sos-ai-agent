"""Cek apakah sumber angka penduduk Agent 2 bisa dijangkau dari laptop ini.

Jalankan dari folder backend (venv aktif):

    python scripts/cek_sumber.py -6.7123 106.8451

Menampilkan hasil tiga sumber untuk titik itu: tabel lokal, WorldPop, dan WebAPI BPS.
Tidak mengubah apa pun di project.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, tools  # noqa: E402


async def main(lat: float, lon: float) -> None:
    place = await tools.reverse_geocode(lat, lon)
    print(f"Lokasi        : {place['name']}")
    print(f"Nama wilayah  : {place.get('parts') or '(reverse geocode gagal)'}")
    print()
    table = tools.census_lookup(lat, lon)
    print("1. Tabel lokal :", f"{table['area']} = {table['population']} jiwa ({table['source']})" if table["found"] else table["reason"])

    print(f"2. WorldPop    : menghitung penduduk dalam radius {config.POP_RADIUS_KM:g} km (bisa 5-25 detik)...")
    wp = await tools.worldpop_population(lat, lon)
    print("   hasil       :", f"{wp['population']} jiwa ({wp['source']})" if wp["ok"] else wp["reason"])

    print("3. WebAPI BPS  : mencari angka kecamatan...")
    bps = await tools.bps_population(place.get("parts", []))
    print("   hasil       :", f"{bps['area']} = {bps['population']} jiwa ({bps['source']})" if bps["ok"] else bps["reason"])


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    asyncio.run(main(float(sys.argv[1]), float(sys.argv[2])))
