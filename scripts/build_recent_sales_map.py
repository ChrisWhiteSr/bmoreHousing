#!/usr/bin/env python3
"""Build a Baltimore recent-transfer density map.

Produces a static Leaflet page showing residential properties whose latest
recorded sale/transfer date is within the last N years.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import time
from datetime import date, datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "recent_sales.db"
OUT = ROOT / "site" / "recent_sales.html"
POINTS_JSON = ROOT / "site" / "recent_sales_cells.json"

CITY_URL = "https://geodata.baltimorecity.gov/egis/rest/services/CityView/Realproperty_OB/FeatureServer/0"
SDAT_URL = "https://opendata.maryland.gov/resource/jpfc-qkxp.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (OpenClaw; Baltimore housing map)"}

MIN_LON, MAX_LON = -77.02, -76.28
MIN_LAT, MAX_LAT = 39.18, 39.73


def parse_city_sale_date(value: object) -> date | None:
    text = str(value or "").strip()
    if len(text) != 8 or not text.isdigit():
        return None
    try:
        return datetime.strptime(text, "%m%d%Y").date()
    except ValueError:
        return None


def parse_sdat_transfer_date(value: object) -> date | None:
    text = str(value or "").strip()
    for fmt in ("%Y.%m.%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:19], fmt).date()
        except ValueError:
            pass
    return None


def get_json(url: str, *, method: str = "get", params: dict | None = None, data: dict | None = None, tries: int = 4):
    for i in range(tries):
        r = requests.post(url, data=data, headers=HEADERS, timeout=90) if method == "post" else requests.get(url, params=params, headers=HEADERS, timeout=90)
        try:
            payload = r.json()
        except Exception:
            payload = {"error": {"message": r.text[:300], "status": r.status_code}}
        if r.status_code < 400 and "error" not in payload:
            return payload
        if i == tries - 1:
            raise RuntimeError(f"{url} failed: HTTP {r.status_code}: {payload.get('error')}")
        time.sleep(1 + i)


def centroid_from_geom(geom: dict | None):
    if not geom:
        return None
    if geom.get("centroid"):
        c = geom["centroid"]
        return c.get("y"), c.get("x")
    if "x" in geom and "y" in geom:
        return geom["y"], geom["x"]
    rings = geom.get("rings") or []
    xs, ys = [], []
    for ring in rings:
        for x, y, *_ in ring:
            xs.append(x)
            ys.append(y)
    if not xs:
        return None
    return sum(ys) / len(ys), sum(xs) / len(xs)


def fetch_city(cutoff: date) -> list[dict]:
    ids_payload = get_json(
        CITY_URL + "/query",
        method="post",
        data={"f": "json", "where": "USEGROUP = 'R ' AND SALEDATE IS NOT NULL", "returnIdsOnly": "true"},
    )
    ids = sorted(ids_payload.get("objectIds") or [])
    rows: list[dict] = []
    for start in range(0, len(ids), 1000):
        batch = ids[start : start + 1000]
        payload = get_json(
            CITY_URL + "/query",
            method="post",
            data={
                "f": "json",
                "where": "1=1",
                "objectIds": ",".join(map(str, batch)),
                "outFields": "OBJECTID,SALEDATE,YEAR_BUILD,NEIGHBOR,ZIP_CODE,USEGROUP",
                "returnGeometry": "true",
                "returnCentroid": "true",
                "outSR": "4326",
                "geometryPrecision": "5",
            },
        )
        for feat in payload.get("features", []):
            attrs = feat.get("attributes", {})
            sale_date = parse_city_sale_date(attrs.get("SALEDATE"))
            if not sale_date or sale_date < cutoff:
                continue
            ll = centroid_from_geom(feat.get("geometry"))
            if not ll:
                continue
            lat, lon = ll
            if MIN_LON <= lon <= MAX_LON and MIN_LAT <= lat <= MAX_LAT:
                rows.append(
                    {
                        "source": "Baltimore City",
                        "external_id": str(attrs.get("OBJECTID") or ""),
                        "transfer_date": sale_date.isoformat(),
                        "lat": lat,
                        "lon": lon,
                        "place": (attrs.get("NEIGHBOR") or attrs.get("ZIP_CODE") or "").strip(),
                        "zip": str(attrs.get("ZIP_CODE") or "").strip(),
                    }
                )
        print(f"city {min(start + 1000, len(ids))}/{len(ids)} -> {len(rows)} recent")
    return rows


def fetch_county(cutoff: date) -> list[dict]:
    select = ",".join(
        [
            "account_id_mdp_field_acctid",
            "county_name_mdp_field_cntyname",
            "mdp_latitude_mdp_field_digycord_converted_to_wgs84",
            "mdp_longitude_mdp_field_digxcord_converted_to_wgs84",
            "mdp_street_address_city_mdp_field_city",
            "mdp_street_address_zip_code_mdp_field_zipcode",
            "land_use_code_mdp_field_lu_desclu_sdat_field_50",
            "sales_segment_1_transfer_date_yyyy_mm_dd_mdp_field_tradate_sdat_field_89",
        ]
    )
    rows: list[dict] = []
    limit = 50000
    offset = 0
    while True:
        payload = get_json(
            SDAT_URL,
            params={
                "$select": select,
                "$where": "county_name_mdp_field_cntyname='Baltimore County'",
                "$limit": str(limit),
                "$offset": str(offset),
            },
        )
        if not payload:
            break
        for r in payload:
            land_use = (r.get("land_use_code_mdp_field_lu_desclu_sdat_field_50") or "").lower()
            if not land_use.startswith("residential"):
                continue
            transfer_date = parse_sdat_transfer_date(r.get("sales_segment_1_transfer_date_yyyy_mm_dd_mdp_field_tradate_sdat_field_89"))
            if not transfer_date or transfer_date < cutoff:
                continue
            try:
                lat = float(r.get("mdp_latitude_mdp_field_digycord_converted_to_wgs84"))
                lon = float(r.get("mdp_longitude_mdp_field_digxcord_converted_to_wgs84"))
            except (TypeError, ValueError):
                continue
            if MIN_LON <= lon <= MAX_LON and MIN_LAT <= lat <= MAX_LAT:
                rows.append(
                    {
                        "source": "Baltimore County",
                        "external_id": str(r.get("account_id_mdp_field_acctid") or ""),
                        "transfer_date": transfer_date.isoformat(),
                        "lat": lat,
                        "lon": lon,
                        "place": (r.get("mdp_street_address_city_mdp_field_city") or r.get("mdp_street_address_zip_code_mdp_field_zipcode") or "").strip(),
                        "zip": str(r.get("mdp_street_address_zip_code_mdp_field_zipcode") or "").strip(),
                    }
                )
        offset += len(payload)
        print(f"county {offset} scanned -> {len(rows)} recent")
        if len(payload) < limit:
            break
    return rows


def grid_cells(rows: list[dict], step: float = 0.006) -> list[dict]:
    cells: dict[tuple[float, float], int] = {}
    for r in rows:
        key = (round(r["lat"] / step) * step, round(r["lon"] / step) * step)
        cells[key] = cells.get(key, 0) + 1
    maxc = max(cells.values()) if cells else 1
    out = [{"lat": lat, "lon": lon, "count": cnt, "intensity": cnt / maxc} for (lat, lon), cnt in cells.items()]
    out.sort(key=lambda x: x["count"], reverse=True)
    return out


def write_db(rows: list[dict], cells: list[dict]) -> None:
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.execute("drop table if exists recent_transfers")
    con.execute(
        """create table recent_transfers(
        id integer primary key, source text, external_id text, transfer_date text,
        lat real, lon real, place text, zip text
    )"""
    )
    con.execute("drop table if exists density_cells")
    con.execute("create table density_cells(id integer primary key, lat real, lon real, count integer, intensity real)")
    con.executemany(
        "insert into recent_transfers(source,external_id,transfer_date,lat,lon,place,zip) values (?,?,?,?,?,?,?)",
        [(r["source"], r["external_id"], r["transfer_date"], r["lat"], r["lon"], r["place"], r["zip"]) for r in rows],
    )
    con.executemany(
        "insert into density_cells(lat,lon,count,intensity) values (?,?,?,?)",
        [(c["lat"], c["lon"], c["count"], c["intensity"]) for c in cells],
    )
    con.commit()
    con.close()


def render_html(cells: list[dict], rows: list[dict], years: int, cutoff: date) -> None:
    city_count = sum(1 for r in rows if r["source"] == "Baltimore City")
    county_count = sum(1 for r in rows if r["source"] == "Baltimore County")
    max_count = max((c["count"] for c in cells), default=1)
    place_counts: dict[str, int] = {}
    for r in rows:
        place = r["place"] or r["zip"] or "Unknown"
        place_counts[place] = place_counts.get(place, 0) + 1
    top_html = "".join(f"<li><b>{place}</b>: {cnt:,}</li>" for place, cnt in sorted(place_counts.items(), key=lambda x: x[1], reverse=True)[:15])
    cells_json = json.dumps(cells)
    html = f'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Baltimore homes changed hands in last {years} years</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
<style>
body{{margin:0;background:#0a0d12;color:#f6f7fb;font-family:Inter,system-ui,sans-serif}}#map{{height:78vh;width:100%}}header{{padding:18px 22px;background:#111827;border-bottom:1px solid #263244}}h1{{margin:0;font-size:clamp(24px,4vw,46px)}}p{{color:#bac4d2;max-width:980px}}.panel{{padding:16px 22px;display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px;background:#0f1624}}.stat,.note{{border:1px solid #263244;border-radius:16px;padding:14px;background:#111827}}.stat b{{font-size:28px;color:#fff}}li{{margin:5px 0;color:#d7deeb}}.legend{{position:absolute;right:18px;bottom:28px;z-index:999;background:rgba(17,24,39,.92);padding:12px;border-radius:14px;border:1px solid #334155}}.bar{{height:12px;width:180px;background:linear-gradient(90deg,#dbeafe,#60a5fa,#2563eb,#1e3a8a);border-radius:999px}}.nav{{margin-top:10px}}.nav a{{color:#93c5fd}}
</style></head><body>
<header><h1>Baltimore homes changed hands in the last {years} years</h1><p>Residential parcels with latest recorded sale/transfer date on or after {cutoff.isoformat()}. City data uses Baltimore City SALEDATE; county data uses Maryland SDAT transfer date. Circles are aggregated into neighborhood-scale grid cells.</p><p class="nav"><a href="index.html">Original built-year density map</a></p></header>
<div id="map"></div><div class="legend"><b>Recent transfer density</b><div class="bar"></div><small>low → high; max cell {max_count:,} parcels</small></div>
<section class="panel"><div class="stat"><b>{len(rows):,}</b><br>Recent residential transfers</div><div class="stat"><b>{city_count:,}</b><br>Baltimore City</div><div class="stat"><b>{county_count:,}</b><br>Baltimore County</div><div class="note"><b>Top places/ZIPs</b><ol>{top_html}</ol></div></section>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script><script>
const cells={cells_json};
const map=L.map('map').setView([39.36,-76.61],10);
L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',{{maxZoom:19, attribution:'© OpenStreetMap contributors'}}).addTo(map);
for (const c of cells){{
  const i=c.intensity;
  const radius=5+Math.sqrt(c.count)*1.35;
  const color=i>.65?'#1e3a8a':i>.38?'#2563eb':i>.18?'#60a5fa':'#dbeafe';
  L.circleMarker([c.lat,c.lon],{{radius, color, fillColor:color, weight:1, fillOpacity:0.34+0.46*i}})
    .bindPopup(`<b>${{c.count.toLocaleString()}}</b> recent transfers nearby`).addTo(map);
}}
</script></body></html>'''
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(html, encoding="utf-8")
    POINTS_JSON.write_text(json.dumps(cells, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--years", type=int, default=3)
    parser.add_argument("--skip-city", action="store_true")
    parser.add_argument("--skip-county", action="store_true")
    args = parser.parse_args()
    today = date.today()
    cutoff = date(today.year - args.years, today.month, today.day)
    rows: list[dict] = []
    if not args.skip_city:
        rows.extend(fetch_city(cutoff))
    if not args.skip_county:
        rows.extend(fetch_county(cutoff))
    cells = grid_cells(rows)
    write_db(rows, cells)
    render_html(cells, rows, args.years, cutoff)
    print(f"wrote {OUT}")
    print(f"total={len(rows)} city={sum(1 for r in rows if r['source']=='Baltimore City')} county={sum(1 for r in rows if r['source']=='Baltimore County')} cells={len(cells)} db={DB}")


if __name__ == "__main__":
    main()
