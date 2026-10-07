"""Tool yang dipanggil para agent. Semuanya sumber data publik, tanpa API key.

Tiap tool mengembalikan dict dengan "ok": True/False. Kalau sumber datanya
tidak bisa dihubungi, agent tetap jalan dan mencatat bahwa datanya tidak tersedia,
bukan mengarang hasil.
"""
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

from . import config

_HEADERS = {"User-Agent": "SOS-AI/1.0 (hackathon disaster relief demo)"}
_TIMEOUT = httpx.Timeout(10.0)


async def _get_json(url: str, params: Optional[dict[str, Any]] = None, timeout: Optional[float] = None) -> Any:
    limit = httpx.Timeout(timeout) if timeout else _TIMEOUT
    async with httpx.AsyncClient(timeout=limit, headers=_HEADERS, follow_redirects=True) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        return resp.json()


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Jarak garis lurus antara dua titik di bumi."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


async def reverse_geocode(lat: float, lon: float) -> dict[str, Any]:
    """Koordinat -> nama tempat (OpenStreetMap Nominatim)."""
    try:
        data = await _get_json(
            "https://nominatim.openstreetmap.org/reverse",
            {"format": "jsonv2", "lat": lat, "lon": lon, "zoom": 14, "accept-language": "id"},
        )
        addr = data.get("address", {})
        keys = ("hamlet", "village", "suburb", "town", "municipality", "city_district", "county", "city", "state")
        parts: list[str] = []
        for k in keys:
            v = addr.get(k)
            if v and v not in parts:
                parts.append(v)
        name = ", ".join(parts[:3]) or data.get("display_name") or f"{lat:.4f}, {lon:.4f}"
        # "parts" = semua nama wilayah (desa, kecamatan, kabupaten, provinsi) untuk dicocokkan dengan data BPS
        return {"ok": True, "name": name, "parts": parts}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "name": f"{lat:.4f}, {lon:.4f}", "parts": [], "error": str(exc)[:120]}


async def recent_earthquakes(lat: float, lon: float, radius_km: int = 200, days: int = 7) -> dict[str, Any]:
    """Gempa M4+ di sekitar titik laporan dalam beberapa hari terakhir (USGS)."""
    try:
        start = datetime.now(timezone.utc) - timedelta(days=days)
        data = await _get_json(
            "https://earthquake.usgs.gov/fdsnws/event/1/query",
            {
                "format": "geojson",
                "latitude": lat,
                "longitude": lon,
                "maxradiuskm": radius_km,
                "starttime": start.strftime("%Y-%m-%dT%H:%M:%S"),
                "minmagnitude": 4,
                "orderby": "time",
                "limit": 5,
            },
        )
        events = []
        for f in data.get("features", []):
            props, coords = f.get("properties", {}), f.get("geometry", {}).get("coordinates", [None, None, None])
            when = datetime.fromtimestamp(props.get("time", 0) / 1000, tz=timezone.utc)
            events.append(
                {
                    "magnitude": props.get("mag"),
                    "place": props.get("place"),
                    "time_utc": when.strftime("%Y-%m-%d %H:%M"),
                    "distance_km": round(haversine_km(lat, lon, coords[1], coords[0]), 1)
                    if coords[0] is not None
                    else None,
                    "lat": coords[1],
                    "lon": coords[0],
                }
            )
        return {"ok": True, "radius_km": radius_km, "days": days, "events": events}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "events": [], "error": str(exc)[:120]}


async def current_weather(lat: float, lon: float) -> dict[str, Any]:
    """Cuaca sekarang + total hujan 24 jam dan 72 jam terakhir (Open-Meteo)."""
    try:
        data = await _get_json(
            "https://api.open-meteo.com/v1/forecast",
            {
                "latitude": lat,
                "longitude": lon,
                "current": "temperature_2m,precipitation,wind_speed_10m,wind_gusts_10m,weather_code",
                "hourly": "precipitation",
                "past_days": 3,
                "forecast_days": 1,
                "timezone": "auto",
            },
        )
        cur = data.get("current", {})
        now = cur.get("time", "")
        hourly = data.get("hourly", {})
        past = [
            p or 0.0
            for t, p in zip(hourly.get("time", []), hourly.get("precipitation", []))
            if t <= now
        ]
        return {
            "ok": True,
            "temperature_c": cur.get("temperature_2m"),
            "wind_kmh": cur.get("wind_speed_10m"),
            "gust_kmh": cur.get("wind_gusts_10m"),
            "rain_now_mm": cur.get("precipitation"),
            "rain_24h_mm": round(sum(past[-24:]), 1),
            "rain_72h_mm": round(sum(past[-72:]), 1),
            "weather_code": cur.get("weather_code"),
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:120]}


async def fire_hotspots(lat: float, lon: float, radius_km: float = 15, days: int = 2) -> dict[str, Any]:
    """Titik api dari satelit (NASA FIRMS, sensor VIIRS) di sekitar lokasi. Butuh FIRMS_MAP_KEY gratis."""
    if not config.FIRMS_MAP_KEY:
        return {"ok": False, "reason": "FIRMS_MAP_KEY belum diisi di .env"}
    dlat = radius_km / 111.32
    dlon = radius_km / (111.32 * max(0.05, math.cos(math.radians(lat))))
    area = f"{lon - dlon:.4f},{lat - dlat:.4f},{lon + dlon:.4f},{lat + dlat:.4f}"  # barat,selatan,timur,utara
    url = f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/{config.FIRMS_MAP_KEY}/VIIRS_SNPP_NRT/{area}/{days}"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0), headers=_HEADERS, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
        lines = [ln for ln in resp.text.splitlines() if ln.strip()]
        if not lines or "latitude" not in lines[0].lower():
            return {"ok": False, "reason": f"FIRMS menjawab: {resp.text.strip()[:80] or 'kosong'}"}
        import csv as _csv

        spots = []
        for row in _csv.DictReader(lines):
            try:
                d = haversine_km(lat, lon, float(row["latitude"]), float(row["longitude"]))
            except (KeyError, ValueError):
                continue
            if d <= radius_km:
                spots.append({"distance_km": round(d, 1), "date": row.get("acq_date", ""), "time": row.get("acq_time", "")})
        spots.sort(key=lambda x: x["distance_km"])
        return {"ok": True, "count": len(spots), "nearest_km": spots[0]["distance_km"] if spots else None, "radius_km": radius_km, "days": days}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"FIRMS tidak bisa dihubungi ({str(exc)[:60]})"}


async def road_route(lat_from: float, lon_from: float, lat_to: float, lon_to: float) -> dict[str, Any]:
    """Rute jalan dari posko ke lokasi (OSRM). Kalau gagal, pakai jarak garis lurus."""
    straight = round(haversine_km(lat_from, lon_from, lat_to, lon_to), 1)
    try:
        data = await _get_json(
            f"https://router.project-osrm.org/route/v1/driving/{lon_from},{lat_from};{lon_to},{lat_to}",
            {"overview": "simplified", "geometries": "geojson"},
        )
        route = data["routes"][0]
        return {
            "ok": True,
            "source": "OSRM",
            "distance_km": round(route["distance"] / 1000, 1),
            "duration_min": round(route["duration"] / 60),
            "straight_km": straight,
            # GeoJSON menyimpan [lon, lat]; peta (Leaflet) butuh [lat, lon]
            "path": [[lat, lon] for lon, lat in route["geometry"]["coordinates"]],
        }
    except Exception as exc:  # noqa: BLE001
        # perkiraan kasar: jalan sebenarnya ~1.4x garis lurus, kecepatan rata-rata 30 km/jam
        est_km = round(straight * 1.4, 1)
        return {
            "ok": False,
            "source": "perkiraan garis lurus",
            "distance_km": est_km,
            "duration_min": round(est_km / 30 * 60),
            "straight_km": straight,
            "path": [[lat_from, lon_from], [lat_to, lon_to]],
            "error": str(exc)[:120],
        }


# ------------------------------------------------------------- pos asal bantuan
_staging_cache: dict[str, Any] = {"mtime": None, "rows": []}


def staging_areas() -> list[dict[str, Any]]:
    """Daftar pos asal bantuan (kantor BPBD) dari data/bpbd_staging_areas.json.

    File dibaca ulang hanya kalau berubah, jadi menambah baris cukup simpan file lalu
    kirim laporan baru. Baris yang koordinatnya rusak dilewati, bukan membuat server gagal.
    """
    import json as _json

    path = config.STAGING_PATH
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return []
    if _staging_cache["mtime"] == mtime:
        return _staging_cache["rows"]
    rows: list[dict[str, Any]] = []
    try:
        raw = _json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        raw = []
    for i, item in enumerate(raw if isinstance(raw, list) else []):
        try:
            lat, lon = float(item["lat"]), float(item["lon"])
            name = str(item["name"]).strip()
        except (KeyError, TypeError, ValueError):
            continue
        if not name or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        rows.append({"id": str(item.get("id") or f"POS-{i + 1}"), "name": name, "lat": lat, "lon": lon})
    _staging_cache.update(mtime=mtime, rows=rows)
    return rows


def nearest_staging_areas(lat: float, lon: float, limit: Optional[int] = None) -> list[dict[str, Any]]:
    """Beberapa pos terdekat menurut garis lurus, terdekat dulu."""
    rows = [
        {**area, "straight_km": round(haversine_km(area["lat"], area["lon"], lat, lon), 1)}
        for area in staging_areas()
    ]
    rows.sort(key=lambda a: a["straight_km"])
    return rows[: limit or config.STAGING_CANDIDATES]


async def route_table(origins: list[dict[str, Any]], lat_to: float, lon_to: float) -> dict[str, Any]:
    """Waktu tempuh dari beberapa titik asal ke satu tujuan dalam SATU permintaan (OSRM table).

    Dipakai untuk membandingkan pos, supaya server OSRM publik tidak dipanggil berkali-kali.
    Titik yang tidak tersambung jalan dikembalikan sebagai None.
    """
    coords = ";".join(f"{o['lon']},{o['lat']}" for o in origins) + f";{lon_to},{lat_to}"
    try:
        data = await _get_json(
            f"https://router.project-osrm.org/table/v1/driving/{coords}",
            {
                "sources": ";".join(str(i) for i in range(len(origins))),
                "destinations": str(len(origins)),
                "annotations": "duration,distance",
            },
        )
        durations = data["durations"]
        distances = data.get("distances") or [[None]] * len(origins)
        rows = []
        for i in range(len(origins)):
            sec, meter = durations[i][0], distances[i][0]
            rows.append(
                {
                    "duration_min": round(sec / 60) if sec is not None else None,
                    "distance_km": round(meter / 1000, 1) if meter is not None else None,
                }
            )
        return {"ok": True, "rows": rows}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"OSRM tidak bisa dihubungi ({str(exc)[:60]})"}


async def pick_origin(lat: float, lon: float) -> dict[str, Any]:
    """Memilih pos asal bantuan untuk satu lokasi laporan, lalu menghitung rutenya.

    1. Ambil beberapa pos terdekat menurut garis lurus.
    2. Bandingkan waktu tempuh lewat jalan (OSRM). Yang tercepat dipilih: pos yang paling
       dekat di peta belum tentu paling cepat kalau terhalang gunung atau laut.
    3. Kalau OSRM tidak bisa dihubungi, atau tidak ada jalur darat, pakai yang terdekat
       menurut garis lurus dan katakan begitu.
    """
    candidates = nearest_staging_areas(lat, lon)
    if not candidates:
        fallback = {
            "id": "DEPOT",
            "name": config.DEPOT_NAME,
            "lat": config.DEPOT_LAT,
            "lon": config.DEPOT_LON,
            "straight_km": round(haversine_km(config.DEPOT_LAT, config.DEPOT_LON, lat, lon), 1),
        }
        route = await road_route(fallback["lat"], fallback["lon"], lat, lon)
        return {
            **fallback,
            "source": "DEPOT di .env (daftar pos BPBD tidak ditemukan)",
            "chosen_by": "satu-satunya pos",
            "compared": False,
            "candidates": [{**fallback, "duration_min": None, "distance_km": None, "chosen": True}],
            "route": route,
        }

    chosen, chosen_by, compared = candidates[0], "satu-satunya pos dalam daftar", False
    for c in candidates:
        c["duration_min"] = c["distance_km"] = None
    if len(candidates) > 1:
        table = await route_table(candidates, lat, lon)
        if table["ok"]:
            for c, row in zip(candidates, table["rows"]):
                c.update(row)
            reachable = [c for c in candidates if c["duration_min"] is not None]
            if reachable:
                chosen = min(reachable, key=lambda c: (c["duration_min"], c["straight_km"]))
                chosen_by, compared = "waktu tempuh tercepat lewat jalan (OSRM)", True
            else:
                chosen_by = "garis lurus terdekat (OSRM tidak menemukan jalur darat dari pos mana pun)"
        else:
            chosen_by = f"garis lurus terdekat ({table['reason']})"
    for c in candidates:
        c["chosen"] = c is chosen

    route = await road_route(chosen["lat"], chosen["lon"], lat, lon)
    return {
        "id": chosen["id"],
        "name": chosen["name"],
        "lat": chosen["lat"],
        "lon": chosen["lon"],
        "straight_km": chosen["straight_km"],
        "source": f"daftar pos BPBD ({len(staging_areas())} pos)",
        "chosen_by": chosen_by,
        "compared": compared,
        "candidates": candidates,
        "route": route,
    }


async def llm_write(system: str, user: str) -> Optional[str]:
    """Minta LLM menulis teks pendek. Kembalikan None kalau LLM tidak diatur / gagal."""
    if not config.LLM_ENABLED:
        return None
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            resp = await client.post(
                f"{config.LLM_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {config.LLM_API_KEY}"},
                json={
                    "model": config.LLM_MODEL,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "temperature": 0.2,
                },
            )
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"]
            return text.strip() or None
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------
# Verifikasi berlapis Agent 2: tabel penduduk -> CCTV -> sensor publik
# --------------------------------------------------------------------------
import base64
import csv
import json
import re


def _read_csv(path: Any) -> list[dict[str, str]]:
    """Baca CSV acuan. Baris kosong dan baris yang diawali '#' diabaikan."""
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            rows = [line for line in f if line.strip() and not line.lstrip().startswith("#")]
        return [{k.strip(): (v or "").strip() for k, v in row.items() if k} for row in csv.DictReader(rows)]
    except FileNotFoundError:
        return []


def _nearest(rows: list[dict[str, str]], lat: float, lon: float, max_km: float) -> Optional[tuple[dict[str, str], float]]:
    best: Optional[tuple[dict[str, str], float]] = None
    for row in rows:
        try:
            d = haversine_km(lat, lon, float(row["lat"]), float(row["lon"]))
        except (KeyError, ValueError):
            continue
        if d <= max_km and (best is None or d < best[1]):
            best = (row, d)
    return best


def census_lookup(lat: float, lon: float) -> dict[str, Any]:
    """Cari jumlah penduduk wilayah terdekat di backend/data/penduduk_desa.csv."""
    hit = _nearest(_read_csv(config.CENSUS_PATH), lat, lon, config.CENSUS_MAX_KM)
    if not hit:
        return {"found": False, "reason": f"tidak ada wilayah dalam radius {config.CENSUS_MAX_KM:g} km di tabel penduduk"}
    row, dist = hit
    try:
        population = int(float(row.get("penduduk", "")))
    except ValueError:
        return {"found": False, "reason": f"kolom penduduk untuk {row.get('wilayah')} bukan angka"}
    return {
        "found": True,
        "area": row.get("wilayah") or "wilayah tanpa nama",
        "regency": row.get("kabupaten", ""),
        "population": population,
        "distance_km": round(dist, 1),
        "source": row.get("sumber") or "sumber tidak dicatat",
    }


def _parse_json_object(text: str) -> Optional[dict[str, Any]]:
    """Ambil objek JSON pertama dari jawaban LLM (yang kadang dibungkus ```json ... ```)."""
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


async def _vision_assess(image: bytes, mime: str) -> Optional[dict[str, Any]]:
    """Minta model vision menilai satu gambar CCTV. None kalau gagal."""
    prompt = (
        "Ini satu gambar dari kamera CCTV jalan. Nilai apakah gambar ini menunjukkan bencana atau "
        "dampaknya (banjir, longsor, bangunan rusak, jalan putus, kebakaran, pengungsian). "
        "Jawab HANYA dengan JSON: "
        '{"bencana": true/false, "jenis": "...", "keyakinan": 0.0-1.0, "deskripsi": "satu kalimat"}. '
        "Kalau gambar gelap, buram, atau tidak jelas, isi bencana=false dan keyakinan rendah."
    )
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(45.0)) as client:
            resp = await client.post(
                f"{config.LLM_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {config.LLM_API_KEY}"},
                json={
                    "model": config.LLM_MODEL,
                    "temperature": 0,
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": prompt},
                                {
                                    "type": "image_url",
                                    "image_url": {"url": f"data:{mime};base64,{base64.b64encode(image).decode()}"},
                                },
                            ],
                        }
                    ],
                },
            )
            resp.raise_for_status()
            data = _parse_json_object(resp.json()["choices"][0]["message"]["content"])
        if not data or "bencana" not in data:
            return None
        return {
            "disaster": bool(data.get("bencana")),
            "kind": str(data.get("jenis") or "")[:60],
            "confidence": max(0.0, min(1.0, float(data.get("keyakinan") or 0))),
            "description": str(data.get("deskripsi") or "")[:200],
        }
    except Exception:  # noqa: BLE001
        return None


async def cctv_check(lat: float, lon: float) -> dict[str, Any]:
    """Tingkat 1 bukti lapangan: kamera CCTV terdekat + penilaian model vision.

    "available": False berarti tingkat ini tidak bisa dipakai (alasannya di "reason"),
    dan Agent 2 turun ke sensor publik.
    """
    hit = _nearest(_read_csv(config.CCTV_PATH), lat, lon, config.CCTV_MAX_KM)
    if not hit:
        return {"available": False, "reason": f"tidak ada kamera terdaftar dalam radius {config.CCTV_MAX_KM:g} km"}
    cam, dist = hit
    info = {"camera": cam.get("nama") or "kamera tanpa nama", "distance_km": round(dist, 1), "snapshot_url": cam.get("snapshot_url", "")}

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_HEADERS, follow_redirects=True) as client:
            resp = await client.get(info["snapshot_url"])
            resp.raise_for_status()
        mime = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        if not mime.startswith("image/"):
            return {**info, "available": False, "reason": f"{info['camera']} tidak mengembalikan gambar diam ({mime or 'tipe tidak dikenal'})"}
        if len(resp.content) > 6_000_000:
            return {**info, "available": False, "reason": f"gambar dari {info['camera']} terlalu besar"}
    except Exception as exc:  # noqa: BLE001
        return {**info, "available": False, "reason": f"{info['camera']} tidak bisa diakses ({str(exc)[:60]})"}

    if not config.LLM_VISION_ENABLED:
        return {**info, "available": False, "reason": f"gambar {info['camera']} didapat, tapi model vision belum diatur (LLM_VISION)"}

    verdict = await _vision_assess(resp.content, mime)
    if not verdict:
        return {**info, "available": False, "reason": f"model vision gagal menilai gambar {info['camera']}"}
    return {**info, "available": True, **verdict}


# --------------------------------------------------------------------------
# Sumber angka penduduk untuk cek kewajaran Agent 2
#   1. tabel lokal (census_lookup di atas)  : angka persis yang diisi tim
#   2. WorldPop                             : estimasi penduduk dalam radius titik laporan
#   3. WebAPI BPS                           : angka resmi tingkat kecamatan / kabupaten
# --------------------------------------------------------------------------
import asyncio
import time

_WORLDPOP_URL = "https://api.worldpop.org/v1/services/stats"
_WORLDPOP_TASK_URL = "https://api.worldpop.org/v1/tasks"
_BPS_URL = "https://webapi.bps.go.id/v1/api/list"


def _circle_polygon(lat: float, lon: float, radius_km: float, points: int = 24) -> dict[str, Any]:
    """Lingkaran di sekitar satu titik sebagai geometri GeoJSON (koordinat [lon, lat])."""
    ring = []
    for i in range(points):
        angle = 2 * math.pi * i / points
        dlat = (radius_km / 111.32) * math.sin(angle)
        dlon = (radius_km / (111.32 * max(0.05, math.cos(math.radians(lat))))) * math.cos(angle)
        ring.append([round(lon + dlon, 6), round(lat + dlat, 6)])
    ring.append(ring[0])
    return {"type": "Polygon", "coordinates": [ring]}


_cache_mem: dict[str, dict[str, Any]] = {}


def _cache_file(name: str):
    return config.DATA_DIR / f"cache_{name}.json"


def _cache_get(name: str, key: str) -> Optional[Any]:
    if name not in _cache_mem:
        try:
            _cache_mem[name] = json.loads(_cache_file(name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _cache_mem[name] = {}
    return _cache_mem[name].get(key)


def _cache_put(name: str, key: str, value: Any) -> None:
    _cache_get(name, key)  # pastikan sudah dimuat
    _cache_mem[name][key] = value
    try:
        _cache_file(name).write_text(json.dumps(_cache_mem[name], ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass  # cache hanya untuk kecepatan; gagal menulis bukan masalah


async def worldpop_population(lat: float, lon: float, radius_km: Optional[float] = None) -> dict[str, Any]:
    """Estimasi jumlah penduduk dalam radius titik laporan (WorldPop 2020, grid 100 m)."""
    radius = radius_km or config.POP_RADIUS_KM
    if not config.WORLDPOP_ENABLED:
        return {"ok": False, "reason": "WorldPop dimatikan (WORLDPOP_ENABLED=false)"}
    # Titik yang sama (dibulatkan ~100 m) tidak ditanyakan dua kali: jawabannya disimpan di
    # data/cache_worldpop.json, jadi laporan berikutnya dari lokasi itu langsung terjawab.
    cache_key = f"{lat:.3f},{lon:.3f},{radius:g}"
    cached = _cache_get("worldpop", cache_key)
    if cached is not None:
        return {**cached, "cached": True}
    geometry = _circle_polygon(lat, lon, radius)
    deadline = time.monotonic() + config.WORLDPOP_TIMEOUT

    async def ask(geojson: dict[str, Any]) -> dict[str, Any]:
        return await _get_json(
            _WORLDPOP_URL,
            {"dataset": "wpgppop", "year": 2020, "geojson": json.dumps(geojson, separators=(",", ":")), "runasync": "false"},
            timeout=config.WORLDPOP_TIMEOUT,
        )

    try:
        data = await ask(geometry)
        if data.get("error") and "geojson" in str(data.get("error_message", "")).lower():
            # sebagian versi layanan meminta FeatureCollection, bukan geometri polos
            data = await ask({"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {}, "geometry": geometry}]})
        # layanan bisa menjawab "sedang diproses": tunggu hasilnya lewat taskid
        while data.get("status") != "finished" and not data.get("error") and data.get("taskid") and time.monotonic() < deadline:
            await asyncio.sleep(1.5)
            data = await _get_json(f"{_WORLDPOP_TASK_URL}/{data['taskid']}", timeout=15)
        if data.get("error"):
            raise RuntimeError(str(data.get("error_message") or "layanan mengembalikan error"))
        total = (data.get("data") or {}).get("total_population")
        if total is None:
            raise RuntimeError("hasil belum selesai dalam batas waktu")
        result = {
            "ok": True,
            "population": int(round(float(total))),
            "radius_km": radius,
            "year": 2020,
            "source": "WorldPop 2020 (estimasi, grid 100 m)",
        }
        _cache_put("worldpop", cache_key, result)
        return result
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"WorldPop tidak memberi hasil dalam {config.WORLDPOP_TIMEOUT:g} detik ({str(exc)[:60] or 'timeout'})"}


def _to_number(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(" ", "")
    # format Indonesia: 12.345,6  ->  12345.6
    if re.fullmatch(r"-?\d{1,3}(\.\d{3})+(,\d+)?", text):
        text = text.replace(".", "").replace(",", ".")
    else:
        text = text.replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def _area_key(name: str) -> str:
    """Samakan penulisan nama wilayah: huruf kecil, tanpa awalan 'Kecamatan' / 'Kabupaten' / 'Kota'."""
    key = name.casefold().strip()
    key = re.sub(r"^(kecamatan|kec\.?|kabupaten|kab\.?|kota)\s+", "", key)
    return re.sub(r"[^a-z0-9]+", "", key)


def _bps_region(parts: list[str]) -> Optional[dict[str, str]]:
    """Cari baris data/bps_wilayah.csv yang kata kuncinya cocok dengan salah satu nama wilayah."""
    keys = {_area_key(p) for p in parts}
    for row in _read_csv(config.BPS_REGIONS_PATH):
        if row.get("kata_kunci") and row.get("domain") and _area_key(row["kata_kunci"]) in keys:
            return row
    return None


async def _bps_candidates(domain: str) -> list[tuple[str, str]]:
    """Daftar variabel 'Jumlah Penduduk Menurut Kecamatan' di satu domain BPS, yang paling polos dulu."""
    found: list[tuple[int, str, str]] = []
    page, pages = 1, 1
    while page <= pages and page <= 15:
        data = await _get_json(_BPS_URL, {"model": "var", "domain": domain, "lang": "ind", "page": page, "key": config.BPS_API_KEY}, timeout=8)
        body = data.get("data") if isinstance(data, dict) else None
        if not (isinstance(body, list) and len(body) >= 2 and isinstance(body[1], list)):
            break
        pages = int((body[0] or {}).get("pages") or 1) if isinstance(body[0], dict) else 1
        for row in body[1]:
            title = str(row.get("title", ""))
            low = title.casefold()
            if "penduduk" in low and "kecamatan" in low and "jumlah" in low:
                # judul paling pendek biasanya tabel paling polos (tanpa rincian jenis kelamin / umur)
                score = len(title) + (40 if any(w in low for w in ("kepadatan", "laju", "rasio", "miskin", "umur", "agama")) else 0)
                found.append((score, str(row.get("var_id")), title))
        page += 1
    found.sort()
    return [(var_id, title) for _, var_id, title in found[:4]]


async def _bps_table(domain: str, forced_var: str) -> tuple[Optional[dict[str, Any]], str]:
    """Ambil tabel penduduk per kecamatan untuk satu domain. Kembalikan (tabel, alasan gagal).

    Hasilnya (berhasil maupun gagal) diingat, supaya laporan berikutnya tidak menunggu lagi.
    """
    cached = _cache_get("bps", domain)
    if cached and time.time() - cached.get("saved_at", 0) < (86400 if cached.get("values") else 600):
        return (cached if cached.get("values") else None), cached.get("reason", "")

    candidates = [(forced_var, "variabel dari bps_wilayah.csv")] if forced_var else await _bps_candidates(domain)
    reason = f"variabel 'jumlah penduduk menurut kecamatan' tidak ditemukan di domain BPS {domain}"
    for var_id, title in candidates:
        table = await _get_json(_BPS_URL, {"model": "data", "domain": domain, "var": var_id, "lang": "ind", "key": config.BPS_API_KEY}, timeout=8)
        if not isinstance(table, dict) or "datacontent" not in table:
            availability = table.get("data-availability") if isinstance(table, dict) else None
            reason = f"BPS tidak mengembalikan tabel untuk domain {domain} var {var_id}" + (f" ({availability})" if availability else "")
            continue
        year, values = _bps_latest_values(table)
        if values:
            result = {"var": var_id, "title": title, "year": year, "values": values, "saved_at": time.time()}
            _cache_put("bps", domain, result)
            return result, ""
        reason = f"tabel BPS domain {domain} var {var_id} tidak berisi angka"
    _cache_put("bps", domain, {"values": None, "reason": reason, "saved_at": time.time()})
    return None, reason


def _bps_latest_values(table: dict[str, Any]) -> tuple[str, dict[str, float]]:
    """Dari jawaban tabel dinamis BPS, ambil nilai tahun terbaru per baris (kecamatan).

    Kunci tiap sel = id baris + id variabel + id turunan + id tahun + id turunan tahun.
    """
    var_id = table["var"][0]["val"]
    content = table.get("datacontent") or {}
    years = sorted(table.get("tahun") or [], key=lambda t: str(t.get("label")), reverse=True)
    derived = table.get("turvar") or [{"val": 0, "label": ""}]
    periods = table.get("turtahun") or [{"val": 0, "label": ""}]
    totals = [d for d in derived if re.search(r"jumlah|total|\+", str(d.get("label", "")), flags=re.IGNORECASE)]
    # kalau ada kolom "Jumlah" pakai itu; kalau turunannya laki-laki/perempuan saja, jumlahkan
    use, add_up = (totals[:1], False) if totals else (derived, len(derived) > 1)

    for year in years:
        values: dict[str, float] = {}
        for row in table.get("vervar") or []:
            found = []
            for d in use:
                for period in periods:
                    num = _to_number(content.get(f"{row['val']}{var_id}{d['val']}{year['val']}{period['val']}"))
                    if num is not None:
                        found.append(num)
                        break
            if found:
                values[str(row.get("label", ""))] = sum(found) if add_up else found[0]
        if values:
            return str(year.get("label")), values
    return "", {}


async def bps_population(parts: list[str]) -> dict[str, Any]:
    """Angka penduduk resmi BPS untuk kecamatan (atau total kabupaten) tempat laporan berada."""
    if not config.BPS_API_KEY:
        return {"ok": False, "reason": "BPS_API_KEY belum diisi di .env"}
    if not parts:
        return {"ok": False, "reason": "nama wilayah tidak diketahui (reverse geocode gagal)"}
    region = _bps_region(parts)
    if not region:
        return {"ok": False, "reason": f"kabupaten/kota untuk '{', '.join(parts[-2:])}' belum terdaftar di data/bps_wilayah.csv"}
    domain = region["domain"]
    try:
        # seluruh pencarian dibatasi waktunya supaya pipeline tidak tertahan oleh BPS
        table, reason = await asyncio.wait_for(_bps_table(domain, region.get("var_penduduk", "")), timeout=config.BPS_TIMEOUT)
    except asyncio.TimeoutError:
        return {"ok": False, "reason": f"WebAPI BPS tidak menjawab dalam {config.BPS_TIMEOUT:g} detik"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"WebAPI BPS tidak bisa dihubungi ({str(exc)[:80]})"}
    if not table:
        return {"ok": False, "reason": reason}
    var_id, title, year, values = table["var"], table["title"], table["year"], table["values"]

    source = f"WebAPI BPS, domain {domain}, var {var_id} ({title}), tahun {year}"
    by_key = {_area_key(label): (label, value) for label, value in values.items()}
    region_key = _area_key(region["kata_kunci"])
    # kecamatan = nama wilayah yang cocok dengan salah satu baris tabel, selain nama kabupatennya sendiri
    for part in parts:
        key = _area_key(part)
        if key in by_key and key != region_key:
            label, value = by_key[key]
            return {"ok": True, "level": "kecamatan", "area": f"Kec. {label}", "population": int(round(value)), "year": year, "source": source}
    # kecamatan tidak dikenali: pakai total kabupaten (baris total kalau ada, kalau tidak jumlah semua baris)
    total_rows = [v for label, v in values.items() if _area_key(label) == region_key or re.search(r"jumlah|total", label, flags=re.IGNORECASE)]
    total = max(total_rows) if total_rows else sum(values.values())
    return {"ok": True, "level": "kabupaten", "area": region["kata_kunci"], "population": int(round(total)), "year": year, "source": source}


# --------------------------------------------------------------------------
# Nomor HP pelapor dan daftar relawan
# --------------------------------------------------------------------------
def normalize_phone(raw: str) -> str:
    """Samakan penulisan nomor HP: hanya angka, awalan 0 atau 8 dijadikan 62."""
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("0"):
        digits = "62" + digits[1:]
    elif digits.startswith("8"):
        digits = "62" + digits
    return digits


def mask_phone(phone: str) -> str:
    """628123456789 -> +6281•••••789 (untuk ditampilkan, nomor lengkap tidak keluar dari server)."""
    if not phone:
        return ""
    return f"+{phone[:4]}{'•' * max(3, len(phone) - 7)}{phone[-3:]}"


def volunteer_lookup(phone: str) -> Optional[dict[str, Any]]:
    """Cari nomor HP di backend/data/relawan.csv. Kembalikan data relawan atau None."""
    target = normalize_phone(phone)
    if not target:
        return None
    for row in _read_csv(config.VOLUNTEERS_PATH):
        if normalize_phone(row.get("nomor_hp", "")) != target:
            continue
        area = None
        try:
            area = {"lat": float(row["lat"]), "lon": float(row["lon"]), "radius_km": float(row["radius_km"])}
        except (KeyError, ValueError):
            pass  # wilayah tugas tidak diisi
        return {"name": row.get("nama", ""), "org": row.get("organisasi", "") or "organisasi tidak dicatat", "area": area}
    return None
