"""Cek pos asal bantuan (kantor BPBD) yang akan dipilih Agent 2 untuk satu titik.

Jalankan dari folder backend (venv aktif):

    python scripts/cek_pos.py 0.3411 101.0260        (Kampar, Riau)
    python scripts/cek_pos.py -6.7123 106.8451       (Caringin, Bogor)

Menampilkan pos terdekat, waktu tempuh lewat jalan menurut OSRM, dan pos yang dipilih.
Tidak mengubah apa pun di project.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, tools  # noqa: E402


async def main(lat: float, lon: float) -> None:
    total = len(tools.staging_areas())
    if total:
        print(f"Daftar pos    : {total} pos dari {config.STAGING_PATH.name}")
    else:
        print("Daftar pos    : TIDAK ADA, memakai cadangan DEPOT di .env")
    origin = await tools.pick_origin(lat, lon)
    print("Kandidat      :")
    for c in origin["candidates"]:
        if c.get("duration_min") is not None:
            road = f"{c['duration_min']} menit, {c['distance_km']} km lewat jalan"
        else:
            road = "waktu tempuh tidak tersedia"
        print(f"   {'>>' if c.get('chosen') else '  '} {c['name']}: {c['straight_km']} km garis lurus, {road}")
    print(f"Dipilih       : {origin['name']}")
    print(f"Alasan        : {origin['chosen_by']}")
    route = origin["route"]
    print(f"Rute          : {route['distance_km']} km, {route['duration_min']} menit ({route['source']})")
    if not route["ok"]:
        print(f"Catatan       : OSRM tidak memberi rute ({route.get('error', '')}). Angka di atas perkiraan kasar.")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    asyncio.run(main(float(sys.argv[1]), float(sys.argv[2])))
