"""Alat bantu untuk melihat data apa yang tersedia di WebAPI BPS dengan API key kamu.

Jalankan dari folder backend (venv aktif), setelah menaruh BPS_API_KEY di backend/.env:

    python scripts/cek_bps.py domain bogor        cari kode domain (wilayah) berdasarkan nama
    python scripts/cek_bps.py var 3201 penduduk   daftar variabel di domain 3201 yang judulnya memuat "penduduk"
    python scripts/cek_bps.py data 3201 123       isi tabel: variabel 123 di domain 3201

Tambahkan --raw di akhir untuk melihat jawaban JSON mentahnya.
Script ini hanya MEMBACA dari BPS dan tidak mengubah apa pun di project.
"""
import json
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

BASE = "https://webapi.bps.go.id/v1/api"
load_dotenv(Path(__file__).resolve().parent.parent / ".env")
KEY = (os.getenv("BPS_API_KEY") or "").strip()


def call(path: str, **params) -> dict:
    resp = httpx.get(f"{BASE}/{path}", params={**params, "key": KEY}, timeout=30, follow_redirects=True)
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict) and data.get("status") not in (None, "OK"):
        print(f"BPS menjawab status={data.get('status')!r}: {data.get('message') or data}")
    return data


def split_list(data: dict) -> tuple[dict, list]:
    """Jawaban daftar BPS berbentuk {"data": [info_halaman, [baris, ...]]}."""
    body = data.get("data") if isinstance(data, dict) else None
    if isinstance(body, list) and len(body) >= 2 and isinstance(body[1], list):
        return (body[0] if isinstance(body[0], dict) else {}), body[1]
    return {}, []


def cmd_domain(keyword: str, raw: bool) -> None:
    data = call("domain", type="all")
    if raw:
        print(json.dumps(data, indent=1, ensure_ascii=False)[:4000])
    _, rows = split_list(data)
    hits = [r for r in rows if keyword.lower() in str(r.get("domain_name", "")).lower()]
    print(f"{len(rows)} domain total, {len(hits)} cocok dengan '{keyword}':")
    for r in hits:
        print(f"  {r.get('domain_id')}  {r.get('domain_name')}")


def cmd_var(domain: str, keyword: str, raw: bool) -> None:
    page, pages, found = 1, 1, 0
    while page <= pages and page <= 300:
        data = call("list", model="var", domain=domain, lang="ind", page=page)
        if raw and page == 1:
            print(json.dumps(data, indent=1, ensure_ascii=False)[:4000])
        info, rows = split_list(data)
        if not rows:
            if page == 1:
                print("Tidak ada baris yang terbaca. Jalankan lagi dengan --raw dan kirim hasilnya.")
            break
        pages = int(info.get("pages") or 1)
        for r in rows:
            title = str(r.get("title", ""))
            if keyword.lower() in title.lower():
                found += 1
                print(f"  var={r.get('var_id')}  [{r.get('unit', '')}]  {title}  | subjek: {r.get('sub_name', '')}  | baris: {r.get('vertical', '')}")
        print(f"  ... halaman {page}/{pages}", file=sys.stderr)
        page += 1
    print(f"{found} variabel memuat '{keyword}' di domain {domain}.")


def cmd_data(domain: str, var: str, raw: bool) -> None:
    data = call("list", model="data", domain=domain, var=var, lang="ind")
    if raw:
        print(json.dumps(data, indent=1, ensure_ascii=False)[:6000])
        return
    if not isinstance(data, dict) or "datacontent" not in data:
        print("Bentuk jawaban tidak seperti yang diharapkan. Jalankan lagi dengan --raw dan kirim hasilnya.")
        return
    labels = lambda key: [f"{x.get('val')}={x.get('label')}" for x in data.get(key, [])]  # noqa: E731
    print("Variabel      :", labels("var"))
    print("Baris (vervar):", data.get("labelvervar"), "-", len(data.get("vervar", [])), "baris")
    for item in labels("vervar")[:60]:
        print("   ", item)
    print("Turunan       :", labels("turvar"))
    print("Tahun         :", labels("tahun"))
    print("Turunan tahun :", labels("turtahun"))
    content = data.get("datacontent", {})
    print(f"Isi data      : {len(content)} sel. Contoh 12 sel pertama (kunci -> nilai):")
    for k in list(content)[:12]:
        print("   ", k, "->", content[k])


def main() -> None:
    args = [a for a in sys.argv[1:] if a != "--raw"]
    raw = "--raw" in sys.argv
    if not KEY:
        sys.exit("BPS_API_KEY belum diisi di backend/.env")
    try:
        if len(args) == 2 and args[0] == "domain":
            cmd_domain(args[1], raw)
        elif len(args) == 3 and args[0] == "var":
            cmd_var(args[1], args[2], raw)
        elif len(args) == 3 and args[0] == "data":
            cmd_data(args[1], args[2], raw)
        else:
            print(__doc__)
    except httpx.HTTPError as exc:
        sys.exit(f"Gagal menghubungi WebAPI BPS: {exc}")


if __name__ == "__main__":
    main()
