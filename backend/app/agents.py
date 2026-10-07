"""Pipeline multi-agent SOS AI, disusun dengan LangGraph StateGraph.

    triage -> verification -> allocation -> fleet -> supervisor
                    \\__________(laporan tidak valid)________/

Tiap agent adalah satu "node": menerima state, mengembalikan potongan state baru.
Field "log" otomatis digabung (lihat Annotated[..., operator.add]), dan itulah
yang ditampilkan di dashboard. Jadi log-nya asli, bukan teks hiasan.

Agent 2 memverifikasi berlapis:
  1. Cek penduduk : jumlah pengungsi yang dilaporkan vs penduduk di sekitar titik laporan.
                    Sumber: tabel lokal -> estimasi WorldPop (dicek silang dengan angka
                    resmi BPS) -> angka BPS saja.
  2. Bukti lapangan: CCTV terdekat (model vision). Kalau CCTV tidak tersedia / tidak
                     mengonfirmasi, pakai sensor publik (gempa USGS, curah hujan).
  Putusan:
    bukti ada                    -> TERKONFIRMASI, trust naik (lebih besar kalau angka laporannya janggal)
    bukti tidak ada, angka wajar -> BELUM TERKONFIRMASI, lanjut tapi ditandai untuk operator
    bukti tidak ada, angka janggal -> TIDAK VALID, trust turun, pelapor masuk daftar untrusted
"""
import asyncio
import json
import math
import operator
import time
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from . import config, tools

# Pilihan jumlah pengungsi di bot Telegram -> angka yang dipakai untuk menghitung bantuan.
# Dua pilihan terakhir adalah format lama, dipertahankan supaya laporan lama tetap terbaca.
HEADCOUNT_MID = {"<50": 30, "50-200": 120, "200-500": 350, "500-1000": 750, ">1000": 1200, "50-150": 100, ">150": 200}
# batas bawah tiap pilihan: dipakai untuk cek penduduk ("minimal sekian orang")
HEADCOUNT_MIN = {"<50": 1, "50-200": 50, "200-500": 200, "500-1000": 500, ">1000": 1001, "50-150": 50, ">150": 151}
# tambahan skor urgensi menurut besar pengungsian
HEADCOUNT_BONUS = {"<50": 0, "50-200": 6, "200-500": 10, "500-1000": 13, ">1000": 15, "50-150": 8, ">150": 15}

ROAD_LABEL = {
    "TRUCK_OK": "Mobil/truk bisa lewat",
    "TRAIL_BIKE_ONLY": "Hanya motor trail",
    "BOAT_ONLY": "Terisolir air, butuh perahu",
    "UNKNOWN": "Belum diketahui",
}

# armada per kondisi jalan: (nama, kapasitas kg per unit, pengali waktu tempuh, kursi evakuasi per unit)
FLEET = {
    "TRUCK_OK": ("Truk logistik", 1500, 1.0, 4),
    "TRAIL_BIKE_ONLY": ("Motor trail relawan", 40, 1.5, 1),
    "BOAT_ONLY": ("Perahu karet + truk pengantar", 300, 2.2, 6),
    # pelapor tidak tahu kondisi jalan: rencana hati-hati, ETA dihitung dengan cadangan waktu
    "UNKNOWN": ("Truk logistik + 2 motor trail pembuka jalan", 1500, 1.5, 4),
}

# jenis bencana yang bisa dipilih di bot
DISASTER_LABEL = {
    "GEMPA": "Gempa bumi",
    "BANJIR": "Banjir",
    "LONGSOR": "Tanah longsor",
    "KEBAKARAN": "Kebakaran",
    "ANGIN": "Angin kencang / puting beliung",
    "LAINNYA": "Lainnya",
}

# Poin kecocokan verifikasi. Makin banyak tanda independen yang cocok, makin tinggi keyakinannya.
POINTS_GPS = 1          # lokasi dibagikan dari GPS HP, bukan diketik
POINTS_POPULATION = 1   # jumlah pengungsi wajar terhadap data penduduk
POINTS_REGISTERED = 1   # pelapor relawan terdaftar
POINTS_PHONE = 1        # nomor HP dibagikan dan terverifikasi milik akun Telegram itu
POINTS_DUTY_AREA = 1    # titik laporan ada di dalam wilayah tugas relawan
POINTS_SIGNAL = 2       # sinyal alam cocok dengan jenis bencana yang dilaporkan
POINTS_NEARBY = 2       # ada laporan pelapor lain di dekatnya
POINTS_CCTV = 2         # CCTV memperlihatkan bencana
CONFIDENCE_HIGH = 4     # poin >= ini : keyakinan TINGGI
CONFIDENCE_MEDIUM = 2   # poin >= ini : keyakinan SEDANG, di bawahnya RENDAH
PRIORITY_HIGH = 75      # skor urgensi >= ini : prioritas TINGGI
PRIORITY_MEDIUM = 55    # skor urgensi >= ini : prioritas SEDANG, di bawahnya RENDAH

COLD_CHAIN_LIMIT_MIN = 120

# aturan trust score
TRUST_REGISTERED = 0.95
TRUST_UNREGISTERED = 0.60
TRUST_PHONE_BONUS = 0.10           # pelapor membagikan nomor HP terverifikasi
TRUST_UP_CONFIRMED = 0.05          # angka laporan wajar + bukti lapangan ada
TRUST_UP_CONFIRMED_ANOMALY = 0.10  # angka laporan janggal, tapi bencananya terbukti
TRUST_DOWN_FAILED = 0.25           # angka laporan janggal DAN tidak ada bukti lapangan
# Pengungsi boleh melebihi angka acuan sampai batas ini (pengungsi datang dari wilayah tetangga).
# Makin kasar sumbernya, makin longgar: estimasi WorldPop diberi kelonggaran lebih besar.
POP_TOLERANCE = {"tabel": 1.2, "worldpop": 1.5, "bps": 1.0}
CCTV_MIN_CONFIDENCE = 0.6


class State(TypedDict, total=False):
    code: str
    payload: dict[str, Any]
    reporter_id: int
    reporter: dict[str, Any]  # catatan pelapor dari database: {"trust": .., "untrusted": ..} atau {}
    nearby: list[dict[str, Any]]  # laporan pelapor lain di dekat titik ini (dicari service.py)
    triage: dict[str, Any]
    verification: dict[str, Any]
    allocation: dict[str, Any]
    fleet: dict[str, Any]
    card: dict[str, Any]
    log: Annotated[list[str], operator.add]


def _ms(start: float) -> str:
    return f"{(time.perf_counter() - start) * 1000:.0f} ms"


def _clamp_trust(value: float) -> float:
    return round(max(0.05, min(0.99, value)), 2)


def base_trust(registered: bool, has_phone: bool) -> float:
    """Trust score awal pelapor yang belum punya riwayat."""
    if registered:
        return TRUST_REGISTERED
    return _clamp_trust(TRUST_UNREGISTERED + (TRUST_PHONE_BONUS if has_phone else 0))


# --------------------------------------------------------------------------- 1
async def triage_agent(state: State) -> dict[str, Any]:
    """Merapikan laporan mentah dan mengambil trust score pelapor."""
    t0 = time.perf_counter()
    p = state["payload"]
    lat, lon = p["geo"]["lat"], p["geo"]["lon"]

    record = state.get("reporter") or {}
    phone = record.get("phone") or ""
    # relawan terdaftar = nomor HP-nya ada di data/relawan.csv, atau ID Telegram-nya ada di .env
    volunteer = tools.volunteer_lookup(phone) if phone else None
    registered = bool(volunteer) or state.get("reporter_id") in config.REGISTERED_VOLUNTEERS
    base = base_trust(registered, bool(phone))
    # pelapor yang sudah punya catatan memakai trust score terakhirnya
    trust = _clamp_trust(float(record["trust"])) if "trust" in record else base
    untrusted = bool(record.get("untrusted"))

    # apakah titik laporan ada di wilayah tugas relawan itu?
    duty = None
    if volunteer and volunteer["area"]:
        area = volunteer["area"]
        distance = tools.haversine_km(lat, lon, area["lat"], area["lon"])
        duty = {"in_area": distance <= area["radius_km"], "distance_km": round(distance, 1), "radius_km": area["radius_km"]}

    place = await tools.reverse_geocode(lat, lon)

    reporter_type = "Relawan terdaftar" if registered else "Pelapor belum terdaftar"
    triage = {
        "trust_score": trust,
        "reporter_type": reporter_type,
        "registered": registered,
        "untrusted": untrusted,
        "phone_verified": bool(phone),
        "volunteer": {"name": volunteer["name"], "org": volunteer["org"]} if volunteer else None,
        "duty_area": duty,
        "known_reporter": bool(record.get("reports")),
        "place_name": place["name"],
        "place_parts": place.get("parts", []),
        "headcount_estimate": HEADCOUNT_MID.get(p["headcount_range"], 30),
    }
    history = f"riwayat {record['reports']} laporan" if record.get("reports") else "laporan pertama"
    log = [
        ">>> [AGENT 1: INGESTION & TRIAGE]",
        f"[Tool] reverse_geocode({lat:.4f}, {lon:.4f}) -> {place['name']}"
        + ("" if place["ok"] else "  (Nominatim tidak tersedia, pakai koordinat)"),
        f"  - Pelapor: {reporter_type} (trust score awal {trust:.2f}, {history})",
    ]
    log.append("  - Nomor HP: terverifikasi lewat Telegram" if phone else "  - Nomor HP: tidak dibagikan")
    if volunteer:
        note = f"  - Relawan terdaftar: {volunteer['org']}"
        if duty:
            note += (
                f", titik laporan di dalam wilayah tugas ({duty['distance_km']} km dari pusatnya)"
                if duty["in_area"] else f", titik laporan DI LUAR wilayah tugas ({duty['distance_km']} km, batas {duty['radius_km']:g} km)"
            )
        log.append(note)
    if untrusted:
        log.append("  - PERINGATAN: pelapor ini ada di daftar untrusted")
    log += [
        f"  - Estimasi pengungsi: {p['headcount_range']} jiwa (dihitung sebagai {triage['headcount_estimate']})",
        f"  - Selesai dalam {_ms(t0)}",
    ]
    return {"triage": triage, "log": log}


# --------------------------------------------------------------------------- 2
def _fmt(n: int) -> str:
    """12345 -> '12.345' (format angka Indonesia)."""
    return f"{n:,}".replace(",", ".")


def _population_reference(table: dict[str, Any], worldpop: dict[str, Any], bps: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Pilih angka penduduk acuan dari sumber yang tersedia. Kembalikan (acuan, catatan cek silang)."""
    notes: list[str] = []
    if table["found"]:
        ref = {"found": True, "kind": "tabel", "population": table["population"],
               "area": table["area"], "source": f"tabel lokal: {table['source']}"}
        return ref, notes
    if worldpop["ok"]:
        population = worldpop["population"]
        area = f"dalam radius {worldpop['radius_km']:g} km dari titik laporan"
        if bps["ok"]:
            if population > bps["population"]:
                # estimasi untuk satu lingkaran kecil tidak mungkin melebihi seluruh kecamatan/kabupaten
                notes.append(
                    f"Cek silang: estimasi WorldPop ({_fmt(population)}) melebihi angka resmi {bps['area']} "
                    f"({_fmt(bps['population'])}), jadi dibatasi ke angka BPS"
                )
                population = bps["population"]
            else:
                notes.append(
                    f"Cek silang: estimasi WorldPop ({_fmt(population)}) konsisten dengan angka resmi "
                    f"{bps['area']} ({_fmt(bps['population'])} jiwa, BPS {bps['year']})"
                )
        ref = {"found": True, "kind": "worldpop", "population": population, "area": area, "source": worldpop["source"]}
        return ref, notes
    if bps["ok"]:
        ref = {"found": True, "kind": "bps", "population": bps["population"], "area": bps["area"], "source": bps["source"]}
        notes.append(f"Hanya angka tingkat {bps['level']} yang tersedia: cek ini hanya menangkap klaim yang sangat besar")
        return ref, notes
    return {"found": False, "reason": "tidak ada sumber angka penduduk yang menjawab"}, notes


def _census_check(headcount_range: str, ref: dict[str, Any]) -> tuple[str, str]:
    """Bandingkan laporan dengan penduduk acuan. Kembalikan (status, penjelasan)."""
    if not ref["found"]:
        return "TIDAK ADA DATA", f"data penduduk tidak tersedia ({ref['reason']})"
    minimum = HEADCOUNT_MIN[headcount_range]
    population = ref["population"]
    where = f"{ref['area']} ({_fmt(population)} jiwa)"
    if minimum > population * POP_TOLERANCE[ref["kind"]]:
        return "JANGGAL", f"laporan {headcount_range} jiwa melebihi penduduk {where}"
    return "WAJAR", f"laporan {headcount_range} jiwa masuk akal dibanding penduduk {where}"


def _signal_match(kind: str, quakes: dict[str, Any], weather: dict[str, Any], fires: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Cocokkan jenis bencana yang dilaporkan dengan sinyal alam yang relevan.

    Kembalikan (sinyal yang cocok, catatan tentang sumber yang tidak tersedia).
    Tidak adanya sinyal BUKAN bukti laporan palsu: banjir lokal bisa terjadi tanpa hujan
    tercatat di titik itu, kebakaran rumah tidak terlihat satelit, dan seterusnya.
    """
    found: list[str] = []
    missing: list[str] = []

    def quake_signal(min_mag: float, max_km: float) -> None:
        if not quakes["ok"]:
            missing.append("data gempa USGS tidak tersedia")
            return
        hits = [e for e in quakes["events"] if (e["magnitude"] or 0) >= min_mag and (e["distance_km"] or 999) <= max_km]
        if hits:
            top = max(hits, key=lambda e: e["magnitude"] or 0)
            found.append(f"gempa M{top['magnitude']} berjarak {top['distance_km']} km ({top['time_utc']} UTC)")

    def rain_signal() -> None:
        if not weather["ok"]:
            missing.append("data cuaca Open-Meteo tidak tersedia")
            return
        r24, r72 = weather.get("rain_24h_mm") or 0, weather.get("rain_72h_mm") or 0
        if r24 >= 30 or r72 >= 80:
            found.append(f"hujan {r24} mm/24 jam, {r72} mm/72 jam")

    def wind_signal() -> None:
        if not weather["ok"]:
            missing.append("data cuaca Open-Meteo tidak tersedia")
            return
        wind, gust = weather.get("wind_kmh") or 0, weather.get("gust_kmh") or 0
        if wind >= 40 or gust >= 60:
            found.append(f"angin {wind} km/jam, hembusan {gust} km/jam")

    def fire_signal() -> None:
        if not fires["ok"]:
            missing.append(f"titik api satelit tidak tersedia ({fires['reason']})")
        elif fires["count"]:
            found.append(f"{fires['count']} titik api satelit dalam {fires['radius_km']:g} km (terdekat {fires['nearest_km']} km)")

    if kind == "GEMPA":
        quake_signal(4.5, 150)
    elif kind == "BANJIR":
        rain_signal()
    elif kind == "LONGSOR":
        rain_signal()
        quake_signal(5.0, 100)
    elif kind == "KEBAKARAN":
        fire_signal()
    elif kind == "ANGIN":
        wind_signal()
    else:  # LAINNYA: sinyal kuat apa pun dihitung
        quake_signal(5.0, 100)
        rain_signal()
        wind_signal()
    return found, list(dict.fromkeys(missing))


async def verification_agent(state: State) -> dict[str, Any]:
    """Cek penduduk, lalu cari bukti lapangan: CCTV dulu, kalau tidak ada pakai sensor publik."""
    t0 = time.perf_counter()
    p = state["payload"]
    triage = state["triage"]
    lat, lon = p["geo"]["lat"], p["geo"]["lon"]
    trust_before = triage["trust_score"]
    log = [">>> [AGENT 2: GEOSPATIAL & SANITY VERIFICATION]"]

    # cek kewajaran dasar (salah ketik koordinat bukan kebohongan: trust tidak diubah)
    problems = []
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        problems.append("Koordinat tidak masuk akal")
    if p["headcount_range"] not in HEADCOUNT_MID:
        problems.append("Jumlah pengungsi tidak dikenali")
    if problems:
        log.append(f"  - Laporan TIDAK VALID: {'; '.join(problems)}")
        return {
            "verification": {
                "valid": False, "verdict": "TIDAK VALID", "problems": problems,
                "trust_before": trust_before, "trust_after": trust_before, "trust_delta": 0.0, "mark_untrusted": False,
            },
            "log": log,
        }

    disaster = p.get("disaster_type") or "LAINNYA"
    if disaster not in DISASTER_LABEL:
        disaster = "LAINNYA"

    async def no_fire_check() -> dict[str, Any]:
        return {"ok": False, "reason": "tidak diperiksa untuk jenis bencana ini"}

    # Semua sumber diambil bersamaan supaya cepat: angka penduduk, CCTV, sensor publik,
    # dan pos asal bantuan (kantor BPBD terdekat) berikut rutenya.
    table = tools.census_lookup(lat, lon)
    worldpop, bps, cctv, quakes, weather, fires, origin = await asyncio.gather(
        tools.worldpop_population(lat, lon),
        tools.bps_population(triage.get("place_parts", [])),
        tools.cctv_check(lat, lon),
        tools.recent_earthquakes(lat, lon),
        tools.current_weather(lat, lon),
        tools.fire_hotspots(lat, lon) if disaster == "KEBAKARAN" else no_fire_check(),
        tools.pick_origin(lat, lon),
    )
    route = origin.pop("route")

    # ---- langkah 1: cek penduduk
    if table["found"]:
        log.append(f"[Tool] census_lookup -> {table['area']}: {_fmt(table['population'])} jiwa ({table['distance_km']} km; {table['source']})")
    if worldpop["ok"]:
        log.append(f"[Tool] worldpop_population -> {_fmt(worldpop['population'])} jiwa dalam radius {worldpop['radius_km']:g} km ({worldpop['source']})")
    else:
        log.append(f"[Tool] worldpop_population -> {worldpop['reason']}")
    if bps["ok"]:
        log.append(f"[Tool] bps_population -> {bps['area']}: {_fmt(bps['population'])} jiwa ({bps['source']})")
    else:
        log.append(f"[Tool] bps_population -> {bps['reason']}")

    census, cross_notes = _population_reference(table, worldpop, bps)
    log += [f"  - {note}" for note in cross_notes]
    census_status, census_note = _census_check(p["headcount_range"], census)
    log.append(f"  - Cek penduduk: {census_status} ({census_note})")

    # ---- langkah 2: bukti lapangan (CCTV -> sinyal alam sesuai jenis bencana -> laporan lain di dekatnya)
    cctv_confirms = bool(cctv["available"] and cctv["disaster"] and cctv["confidence"] >= CCTV_MIN_CONFIDENCE)
    if cctv["available"]:
        log.append(
            f"[Tool] cctv_check -> {cctv['camera']} ({cctv['distance_km']} km): "
            f"{'TERLIHAT BENCANA' if cctv['disaster'] else 'tidak terlihat bencana'}"
            f"{' (' + cctv['kind'] + ')' if cctv['disaster'] and cctv['kind'] else ''}, keyakinan {cctv['confidence']:.2f}. {cctv['description']}"
        )
    else:
        log.append(f"[Tool] cctv_check -> CCTV tidak tersedia: {cctv['reason']}. Lanjut ke sinyal alam")

    if quakes["ok"]:
        if quakes["events"]:
            top = max(quakes["events"], key=lambda e: e["magnitude"] or 0)
            log.append(
                f"[Tool] recent_earthquakes -> {len(quakes['events'])} gempa M4+ dalam radius 200 km (7 hari). "
                f"Terbesar M{top['magnitude']} {top['place']}, {top['distance_km']} km dari lokasi"
            )
        else:
            log.append("[Tool] recent_earthquakes -> tidak ada gempa M4+ dalam radius 200 km (7 hari)")
    else:
        log.append("[Tool] recent_earthquakes -> USGS tidak bisa dihubungi, dilewati")

    if weather["ok"]:
        log.append(
            f"[Tool] current_weather -> hujan {weather['rain_24h_mm']} mm/24 jam, {weather.get('rain_72h_mm')} mm/72 jam, "
            f"angin {weather['wind_kmh']} km/jam, suhu {weather['temperature_c']}°C"
        )
    else:
        log.append("[Tool] current_weather -> Open-Meteo tidak bisa dihubungi, dilewati")

    if disaster == "KEBAKARAN":
        log.append(
            f"[Tool] fire_hotspots -> {fires['count']} titik api satelit dalam {fires['radius_km']:g} km ({fires['days']} hari)"
            if fires["ok"] else f"[Tool] fire_hotspots -> {fires['reason']}"
        )

    # pos asal bantuan: pos terdekat dibandingkan, yang tercepat lewat jalan dipilih
    def _cand(c: dict[str, Any]) -> str:
        road = f", {c['duration_min']} menit lewat jalan" if c.get("duration_min") is not None else ""
        return f"{c['name']} {c['straight_km']:g} km{road}"

    log.append(f"[Tool] nearest_staging_area -> {'; '.join(_cand(c) for c in origin['candidates'])} ({origin['source']})")
    log.append(f"  - Pos asal dipilih: {origin['name']} ({origin['chosen_by']})")
    log.append(
        f"[Tool] road_route({origin['name']} -> lokasi) -> {route['distance_km']} km, "
        f"{route['duration_min']} menit ({route['source']})"
    )

    # sinyal alam yang relevan untuk jenis bencana yang dilaporkan
    signals, signal_gaps = _signal_match(disaster, quakes, weather, fires)
    label = DISASTER_LABEL[disaster]
    if signals:
        log.append(f"  - Pencocokan sinyal untuk '{label}': COCOK ({'; '.join(signals)})")
    else:
        gap = f" Catatan: {'; '.join(signal_gaps)}." if signal_gaps else ""
        log.append(f"  - Pencocokan sinyal untuk '{label}': tidak ada sinyal pendukung.{gap}")

    # konfirmasi silang: laporan dari pelapor lain di dekat titik ini
    nearby = state.get("nearby") or []
    if nearby:
        nearest = min(nearby, key=lambda n: n["distance_km"])
        log.append(
            f"  - Konfirmasi silang: {len(nearby)} laporan pelapor lain dalam {config.NEARBY_KM:g} km "
            f"({config.NEARBY_HOURS:g} jam terakhir), terdekat {nearest['code']} berjarak {nearest['distance_km']} km"
        )
    else:
        log.append(f"  - Konfirmasi silang: belum ada laporan pelapor lain dalam {config.NEARBY_KM:g} km")

    gps = p.get("geo_source") == "GPS"
    log.append("  - Lokasi: dibagikan dari GPS perangkat" if gps else "  - Lokasi: diketik manual atau simulasi (tidak terverifikasi GPS)")

    evidence = []
    if cctv_confirms:
        evidence.append(f"Kamera {cctv['camera']}: {cctv['description'] or cctv['kind'] or 'terlihat bencana'}")
    evidence += [f"Sinyal {label.lower()}: {sig}" for sig in signals]
    if nearby:
        evidence.append(f"{len(nearby)} laporan pelapor lain di dekatnya ({', '.join(n['code'] for n in nearby[:3])})")
    confirmed = bool(evidence)

    # ---- poin kecocokan -> tingkat keyakinan
    matches: list[tuple[str, int]] = []
    if gps:
        matches.append(("Lokasi dari GPS perangkat", POINTS_GPS))
    if census_status == "WAJAR":
        matches.append(("Jumlah pengungsi wajar terhadap data penduduk", POINTS_POPULATION))
    if triage["phone_verified"]:
        matches.append(("Nomor HP terverifikasi lewat Telegram", POINTS_PHONE))
    if triage["registered"]:
        matches.append(("Pelapor relawan terdaftar", POINTS_REGISTERED))
    if triage["duty_area"] and triage["duty_area"]["in_area"]:
        matches.append(("Lokasi sesuai wilayah tugas relawan", POINTS_DUTY_AREA))
    if signals:
        matches.append((f"Sinyal alam cocok dengan {label.lower()}", POINTS_SIGNAL))
    if nearby:
        matches.append(("Dikuatkan laporan pelapor lain", POINTS_NEARBY))
    if cctv_confirms:
        matches.append(("CCTV memperlihatkan bencana", POINTS_CCTV))
    points = sum(pt for _, pt in matches)
    confidence = "TINGGI" if points >= CONFIDENCE_HIGH else "SEDANG" if points >= CONFIDENCE_MEDIUM else "RENDAH"

    # ---- putusan + trust score
    mark_untrusted = False
    if confirmed:
        verdict, valid = "TERKONFIRMASI", True
        delta = TRUST_UP_CONFIRMED_ANOMALY if census_status == "JANGGAL" else TRUST_UP_CONFIRMED
        why = "bukti lapangan ada" + (", meski angka laporan janggal terhadap data penduduk" if census_status == "JANGGAL" else "")
    elif census_status == "JANGGAL":
        verdict, valid = "TIDAK VALID", False
        delta = -TRUST_DOWN_FAILED
        mark_untrusted = True
        why = "angka laporan janggal terhadap data penduduk DAN tidak ada bukti lapangan"
    else:
        verdict, valid = "BELUM TERKONFIRMASI", True
        delta = 0.0
        why = "belum ada bukti lapangan (CCTV, sinyal alam, atau laporan lain); operator perlu menilai"
    trust_after = _clamp_trust(trust_before + delta)

    # jumlah pengungsi tidak mungkin jauh melebihi penduduk wilayah
    headcount = triage["headcount_estimate"]
    headcount_adjusted = None
    # (angka tingkat kecamatan/kabupaten terlalu kasar untuk dipakai menyesuaikan)
    if census["found"] and census["kind"] != "bps" and headcount > census["population"]:
        headcount_adjusted = max(1, census["population"])

    road_unknown = p["road_status"] == "UNKNOWN"
    verification = {
        "valid": valid,
        "verdict": verdict,
        "verdict_reason": why,
        "problems": [] if valid else [f"Tidak lolos verifikasi: {why}"],
        "disaster_type": disaster,
        "disaster_label": label,
        "geo_source": "GPS" if gps else "MANUAL",
        "phone_verified": triage["phone_verified"],
        "volunteer": triage["volunteer"],
        "duty_area": triage["duty_area"],
        "census": census,
        "population_sources": {"table": table, "worldpop": worldpop, "bps": bps},
        "census_status": census_status,
        "census_note": census_note,
        "cctv": cctv,
        "signals": signals,
        "signal_gaps": signal_gaps,
        "nearby": nearby,
        "hazards": signals,
        "evidence": evidence,
        "hazard_evidence": "ADA BUKTI" if confirmed else "BELUM ADA BUKTI",
        "matches": [{"label": lbl, "points": pt} for lbl, pt in matches],
        "match_points": points,
        "confidence": confidence,
        "earthquakes": quakes,
        "weather": weather,
        "fires": fires,
        "origin": origin,
        "route": route,
        "headcount_adjusted": headcount_adjusted,
        "trust_before": trust_before,
        "trust_after": trust_after,
        "trust_delta": round(trust_after - trust_before, 2),
        "mark_untrusted": mark_untrusted,
        # jujur soal batas sistem: kondisi jalan belum bisa dicek otomatis
        "road_status_source": "Pelapor tidak tahu kondisi jalan" if road_unknown else "Klaim pelapor (belum diverifikasi independen)",
    }
    summary = ", ".join(f"{lbl} (+{pt})" for lbl, pt in matches) or "tidak ada tanda yang cocok"
    log.append(f"  - Keyakinan verifikasi: {confidence} ({points} poin: {summary})")
    line = f"  - Putusan: {verdict} ({why}). Trust score {trust_before:.2f} -> {trust_after:.2f}"
    log.append(("  - PERINGATAN:" + line[3:]) if not valid else line)
    if mark_untrusted:
        log.append("  - PERINGATAN: pelapor dimasukkan ke daftar untrusted")
    if headcount_adjusted is not None:
        log.append(f"  - Jumlah pengungsi untuk perhitungan disesuaikan ke penduduk wilayah: {headcount} -> {headcount_adjusted}")
    if road_unknown:
        log.append("  - Kondisi jalan: pelapor tidak tahu, rencana armada dibuat hati-hati")
    else:
        log.append(f"  - Kondisi jalan '{ROAD_LABEL.get(p['road_status'], p['road_status'])}': klaim pelapor, belum diverifikasi")
    log.append(f"  - Selesai dalam {_ms(t0)}")
    return {"verification": verification, "log": log}


def route_after_verification(state: State) -> str:
    """Conditional edge: hanya laporan valid yang lanjut ke Agent 3."""
    return "allocation" if state["verification"]["valid"] else "supervisor"


# --------------------------------------------------------------------------- 3
async def allocation_agent(state: State) -> dict[str, Any]:
    """Menyusun isi bantuan sesuai siapa yang ada di lokasi, lalu menghitung skor urgensi."""
    t0 = time.perf_counter()
    p = state["payload"]
    ver = state["verification"]
    triage = state["triage"]
    n = ver.get("headcount_adjusted") or triage["headcount_estimate"]
    needs = set(p.get("needs", []))
    critical = int(p.get("critical_count", 0))

    items: list[dict[str, Any]] = [
        {"item": "Paket makanan siap saji", "qty": math.ceil(n / 4), "unit": "dus", "kg": math.ceil(n / 4) * 3},
        {"item": "Air minum", "qty": math.ceil(n * 3 / 19), "unit": "galon", "kg": math.ceil(n * 3 / 19) * 19},
    ]
    if "BALITA" in needs:
        box = math.ceil(n * 0.15 / 2)
        items.append({"item": "MPASI & biskuit balita", "qty": box, "unit": "box", "kg": box * 2})
        items.append({"item": "Popok balita", "qty": box, "unit": "pack", "kg": box * 1})
    if "LANSIA" in needs:
        box = math.ceil(n * 0.2 / 2)
        items.append({"item": "Bubur lansia", "qty": box, "unit": "box", "kg": box * 2})
        items.append({"item": "Popok dewasa", "qty": math.ceil(box / 2), "unit": "pack", "kg": math.ceil(box / 2) * 2})
    if "TENDA" in needs:
        tents = math.ceil(n / 25)
        items.append({"item": "Tenda komunal", "qty": tents, "unit": "unit", "kg": tents * 25})
    cold_chain = "MEDIS" in needs
    if cold_chain:
        items.append({"item": "Kit medis darurat + cold-box obat", "qty": 1, "unit": "set", "kg": 12})

    total_kg = sum(i["kg"] for i in items)

    # skor urgensi 0-100, tiap komponen bisa dijelaskan ke juri/operator
    score = 35
    reasons = ["dasar 35"]
    size_bonus = HEADCOUNT_BONUS[p["headcount_range"]]
    if size_bonus:
        score += size_bonus
        reasons.append(f"+{size_bonus} jumlah pengungsi")
    if "BALITA" in needs:
        score += 8
        reasons.append("+8 ada balita")
    if "LANSIA" in needs:
        score += 8
        reasons.append("+8 ada lansia")
    if cold_chain:
        score += 10
        reasons.append("+10 butuh medis darurat")
    if critical > 0:
        bonus = min(15, 9 + 3 * critical)
        score += bonus
        reasons.append(f"+{bonus} korban kritis")
    road_bonus = {"TRUCK_OK": 0, "TRAIL_BIKE_ONLY": 5, "BOAT_ONLY": 9, "UNKNOWN": 5}.get(p["road_status"], 0)
    if road_bonus:
        score += road_bonus
        reasons.append(f"+{road_bonus} akses {'belum diketahui' if p['road_status'] == 'UNKNOWN' else 'sulit'}")
    # makin banyak tanda verifikasi yang cocok, makin tinggi prioritasnya
    match_bonus = min(15, 3 * ver.get("match_points", 0))
    if match_bonus:
        score += match_bonus
        reasons.append(f"+{match_bonus} kecocokan verifikasi ({ver['match_points']} poin, keyakinan {ver['confidence']})")
    if triage["untrusted"]:
        score -= 10
        reasons.append("-10 pelapor di daftar untrusted")
    elif ver["trust_after"] < 0.9:
        score -= 5
        reasons.append("-5 trust score pelapor di bawah 0.90")
    score = max(0, min(100, score))
    level = "TINGGI" if score >= PRIORITY_HIGH else "SEDANG" if score >= PRIORITY_MEDIUM else "RENDAH"

    allocation = {
        "items": items,
        "headcount_used": n,
        "total_kg": total_kg,
        "cold_chain": cold_chain,
        "urgency_score": score,
        "urgency_level": level,
        "score_breakdown": reasons,
    }
    log = [">>> [AGENT 3: ALLOCATION & HUMANITARIAN EQUITY]", f"  - Dihitung untuk {n} jiwa"]
    log += [f"  - {i['item']}: {i['qty']} {i['unit']} (~{i['kg']} kg)" for i in items]
    log.append(f"  - Total muatan: ~{total_kg} kg" + (" | cold-chain: maks 120 menit di jalan" if cold_chain else ""))
    log.append(f"  - Skor urgensi: {score}/100 ({level}) = {', '.join(reasons)}")
    log.append(f"  - Selesai dalam {_ms(t0)}")
    return {"allocation": allocation, "log": log}


# --------------------------------------------------------------------------- 4
async def fleet_agent(state: State) -> dict[str, Any]:
    """Memilih armada yang bisa lewat, menghitung jumlah unit, ETA, dan evakuasi balik."""
    t0 = time.perf_counter()
    p = state["payload"]
    alloc = state["allocation"]
    route = state["verification"]["route"]
    origin = state["verification"]["origin"]
    critical = int(p.get("critical_count", 0))

    name, capacity, time_factor, seats = FLEET.get(p["road_status"], FLEET["TRUCK_OK"])
    units_cargo = max(1, math.ceil(alloc["total_kg"] / capacity))
    units_evac = math.ceil(critical / seats) if critical else 0
    units = max(units_cargo, units_evac)
    eta = round(route["duration_min"] * time_factor)

    warnings = []
    if alloc["cold_chain"] and eta > COLD_CHAIN_LIMIT_MIN:
        warnings.append(f"ETA {eta} menit melewati batas cold-chain {COLD_CHAIN_LIMIT_MIN} menit")
    if p["road_status"] == "TRAIL_BIKE_ONLY" and units > 8:
        warnings.append(f"Butuh {units} motor trail: pertimbangkan beberapa gelombang pengiriman")
    if not route["ok"]:
        warnings.append("Rute dihitung dari garis lurus (OSRM tidak tersedia atau tidak ada jalur darat), ETA kasar")
    if route["distance_km"] > config.STAGING_FAR_KM:
        warnings.append(
            f"Pos asal terdekat dalam daftar berjarak {route['distance_km']:g} km: "
            "cek apakah ada pos BPBD kabupaten/kota yang lebih dekat, atau pakai jalur udara/laut"
        )
    if p["road_status"] == "UNKNOWN":
        warnings.insert(0, "Akses jalan belum diketahui: motor trail berangkat dulu sebagai pembuka jalan, truk menyusul setelah jalur dipastikan")

    fleet = {
        "vehicle": name,
        "units": units,
        "eta_min": eta,
        "distance_km": route["distance_km"],
        "origin": origin["name"],
        "origin_id": origin["id"],
        "reverse_evac": {"patients": critical, "note": "Pasien kritis dibawa saat armada kembali"} if critical else None,
        "warnings": warnings,
    }
    log = [
        ">>> [AGENT 4: FLEET & REVERSE LOGISTICS]",
        f"  - Armada: {units}x {name} dari {origin['name']}",
        f"  - Jarak {route['distance_km']} km, ETA {eta} menit (waktu mobil x{time_factor})",
    ]
    if critical:
        log.append(f"  - Evakuasi balik: {critical} pasien kritis ikut armada saat kembali")
    log += [f"  - PERINGATAN: {w}" for w in warnings]
    log.append(f"  - Selesai dalam {_ms(t0)}")
    return {"fleet": fleet, "log": log}


# ------------------------------------------------------------------ supervisor
def _verification_summary(ver: dict[str, Any]) -> dict[str, Any]:
    """Ringkasan verifikasi untuk ditaruh di kartu dispatch."""
    cctv = ver.get("cctv") or {}
    return {
        "verdict": ver["verdict"],
        "disaster": ver.get("disaster_label", ""),
        "confidence": ver.get("confidence", ""),
        "match_points": ver.get("match_points", 0),
        "matches": [m["label"] for m in ver.get("matches", [])],
        "location": "GPS perangkat" if ver.get("geo_source") == "GPS" else "diketik manual / simulasi",
        "census": ver.get("census_note", ""),
        "evidence": ver.get("evidence", []),
        "cctv": (f"{cctv.get('camera')} ({cctv.get('distance_km')} km)" if cctv.get("available") else "tidak tersedia"),
        "trust_before": ver["trust_before"],
        "trust_after": ver["trust_after"],
    }


async def supervisor_agent(state: State) -> dict[str, Any]:
    """Merangkum semuanya jadi satu kartu rekomendasi untuk operator manusia."""
    t0 = time.perf_counter()
    p = state["payload"]
    ver = state["verification"]
    triage = state["triage"]
    log = [">>> [SUPERVISOR: COMMAND CENTER SYNTHESIS]"]

    if not ver["valid"]:
        card = {
            "code": state["code"],
            "recommendation": "TOLAK",
            "target": triage["place_name"],
            "urgency_score": 0,
            "urgency_level": "TIDAK VALID",
            "verification": _verification_summary(ver),
            "reporter_untrusted": bool(ver.get("mark_untrusted") or triage["untrusted"]),
            "reasoning": "; ".join(ver["problems"])
            + (". Pelapor dimasukkan ke daftar untrusted." if ver.get("mark_untrusted") else ""),
        }
        log.append("  - Rekomendasi: TOLAK (laporan tidak lolos verifikasi)")
        log.append("  - Status: MENUNGGU KEPUTUSAN OPERATOR (human-in-the-loop)")
        log.append(f"  - Selesai dalam {_ms(t0)}")
        return {"card": card, "log": log}

    alloc, fleet = state["allocation"], state["fleet"]

    verdict_sentence = {
        "TERKONFIRMASI": "Laporan terkonfirmasi oleh bukti lapangan.",
        "BELUM TERKONFIRMASI": "Belum ada bukti lapangan independen, jadi keputusan bergantung pada penilaian operator.",
    }[ver["verdict"]]
    fallback = (
        f"{triage['reporter_type']} melaporkan {ver['disaster_label'].lower()} dengan {p['headcount_range']} pengungsi di {triage['place_name']}. "
        f"{verdict_sentence} Keyakinan verifikasi {ver['confidence']}, prioritas {alloc['urgency_level']} ({alloc['urgency_score']}/100). "
        f"Direkomendasikan {fleet['units']}x {fleet['vehicle']} dari {fleet['origin']} "
        f"({fleet['distance_km']:g} km) dengan ETA {fleet['eta_min']} menit. "
        + ("Pelapor tidak tahu kondisi jalan, jadi armada disusun hati-hati." if p["road_status"] == "UNKNOWN" else "Kondisi jalan masih berupa klaim pelapor.")
    )
    facts = {
        "lokasi": triage["place_name"],
        "pelapor": triage["reporter_type"],
        "trust_score": ver["trust_after"],
        "pengungsi": p["headcount_range"],
        "kebutuhan": p.get("needs", []),
        "korban_kritis": p.get("critical_count", 0),
        "akses_jalan_klaim_pelapor": ROAD_LABEL.get(p["road_status"]),
        "jenis_bencana": ver["disaster_label"],
        "putusan_verifikasi": ver["verdict"],
        "keyakinan_verifikasi": ver["confidence"],
        "tanda_yang_cocok": [m["label"] for m in ver["matches"]],
        "cek_penduduk": ver["census_note"],
        "bukti_lapangan": ver["evidence"],
        "skor_urgensi": alloc["urgency_score"],
        "rincian_skor": alloc["score_breakdown"],
        "armada": f"{fleet['units']}x {fleet['vehicle']}",
        "pos_asal": fleet["origin"],
        "jarak_km": fleet["distance_km"],
        "eta_menit": fleet["eta_min"],
        "peringatan": fleet["warnings"],
    }
    written = await tools.llm_write(
        system=(
            "Kamu supervisor pusat komando bantuan bencana. Tulis alasan rekomendasi untuk operator "
            "dalam 2-3 kalimat bahasa Indonesia yang lugas. Pakai HANYA fakta yang diberikan, jangan "
            "menambah angka atau klaim baru. Sebutkan dengan jelas hal yang belum terverifikasi."
        ),
        user=json.dumps(facts, ensure_ascii=False),
    )
    reasoning_source = "LLM" if written else "template"

    warnings = list(fleet["warnings"])
    if ver["verdict"] == "BELUM TERKONFIRMASI":
        warnings.insert(0, "Belum ada bukti lapangan (CCTV, sinyal alam, atau laporan lain)")
    if triage["untrusted"]:
        warnings.insert(0, "Pelapor ada di daftar untrusted")

    card = {
        "code": state["code"],
        "recommendation": "KIRIM",
        "target": triage["place_name"],
        "urgency_score": alloc["urgency_score"],
        "urgency_level": alloc["urgency_level"],
        "verification": _verification_summary(ver),
        "reporter_untrusted": triage["untrusted"],
        "road_access": ROAD_LABEL.get(p["road_status"], p["road_status"]),
        "road_access_source": ver["road_status_source"],
        "fleet": f"{fleet['units']}x {fleet['vehicle']}",
        "origin": fleet["origin"],
        "origin_id": fleet["origin_id"],
        "distance_km": fleet["distance_km"],
        "eta_min": fleet["eta_min"],
        "cargo_out": [f"{i['qty']} {i['unit']} {i['item']}" for i in alloc["items"]],
        "cargo_back": f"Evakuasi {fleet['reverse_evac']['patients']} pasien kritis" if fleet["reverse_evac"] else None,
        "warnings": warnings,
        "reasoning": written or fallback,
        "reasoning_source": reasoning_source,
    }
    log.append(f"  - Rekomendasi: KIRIM {card['fleet']} ke {card['target']}")
    log.append(f"  - Alasan ditulis oleh: {reasoning_source}")
    log.append("  - Status: MENUNGGU PERSETUJUAN OPERATOR (human-in-the-loop)")
    log.append(f"  - Selesai dalam {_ms(t0)}")
    return {"card": card, "log": log}


# ----------------------------------------------------------------------- graph
def build_graph():
    g = StateGraph(State)
    g.add_node("triage", triage_agent)
    g.add_node("verification", verification_agent)
    g.add_node("allocation", allocation_agent)
    g.add_node("fleet", fleet_agent)
    g.add_node("supervisor", supervisor_agent)

    g.add_edge(START, "triage")
    g.add_edge("triage", "verification")
    g.add_conditional_edges(
        "verification", route_after_verification, {"allocation": "allocation", "supervisor": "supervisor"}
    )
    g.add_edge("allocation", "fleet")
    g.add_edge("fleet", "supervisor")
    g.add_edge("supervisor", END)
    return g.compile()


GRAPH = build_graph()


async def run_pipeline(
    code: str,
    payload: dict[str, Any],
    reporter_id: int | None,
    reporter: dict[str, Any] | None = None,
    nearby: list[dict[str, Any]] | None = None,
) -> State:
    start = time.perf_counter()
    state: State = await GRAPH.ainvoke(
        {
            "code": code,
            "payload": payload,
            "reporter_id": reporter_id or 0,
            "reporter": reporter or {},
            "nearby": nearby or [],
            "log": [f"[SYSTEM] Laporan {code} masuk ke StateGraph"],
        }
    )
    state["log"].append(f"[SYSTEM] Pipeline selesai dalam {time.perf_counter() - start:.1f} detik")
    return state
