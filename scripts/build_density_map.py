#!/usr/bin/env python3
from __future__ import annotations
import json, math, sqlite3, time
from pathlib import Path
from urllib.parse import urlencode
import requests

ROOT=Path(__file__).resolve().parents[1]
DB=ROOT/'data/housing_1960_2006.db'
OUT=ROOT/'site/index.html'
POINTS_JSON=ROOT/'site/density_cells.json'

CITY_URL='https://geodata.baltimorecity.gov/egis/rest/services/CityView/Realproperty_OB/FeatureServer/0'
COUNTY_URL='https://bcgisdata.baltimorecountymd.gov/arcgis/rest/services/Property/Property/MapServer/1'

CITY_WHERE="YEAR_BUILD >= 1960 AND YEAR_BUILD <= 2006 AND USEGROUP = 'R '"
COUNTY_WHERE="YEAR_BUILT >= '1960' AND YEAR_BUILT <= '2006' AND BRF_PROPERTY_TYPE = 'Residential'"


def get_json(url, params, tries=4):
    for i in range(tries):
        # POST avoids ArcGIS/IIS URL-length failures for large objectId batches.
        r=requests.post(url, data=params, timeout=60)
        try: data=r.json()
        except Exception: data={'error': {'message': r.text[:200]}}
        if 'error' not in data:
            return data
        if i==tries-1: raise RuntimeError(f'{url} {data.get("error")}')
        time.sleep(1+i)


def object_ids(url, where):
    data=get_json(url+'/query', {'f':'json','where':where,'returnIdsOnly':'true'})
    ids=data.get('objectIds') or []
    return sorted(ids)


def centroid_from_geom(geom):
    if not geom: return None
    if 'centroid' in geom and geom['centroid']:
        c=geom['centroid']; return c.get('y'), c.get('x')
    if 'x' in geom and 'y' in geom:
        return geom['y'], geom['x']
    rings=geom.get('rings') or []
    xs=[]; ys=[]
    for ring in rings:
        for x,y,*rest in ring:
            xs.append(x); ys.append(y)
    if not xs: return None
    return sum(ys)/len(ys), sum(xs)/len(xs)


def fetch_layer(name, url, where, out_fields, batch_size=1000, return_centroid=False):
    ids=object_ids(url, where)
    print(f'{name}: {len(ids)} records')
    rows=[]
    for start in range(0,len(ids),batch_size):
        batch=ids[start:start+batch_size]
        params={
            'f':'json','where':'1=1','objectIds': ','.join(map(str,batch)),
            'outFields': out_fields, 'returnGeometry':'true','outSR':'4326',
            'geometryPrecision':'5'
        }
        if return_centroid:
            params['returnCentroid']='true'
        data=get_json(url+'/query', params)
        for feat in data.get('features',[]):
            attrs=feat.get('attributes',{})
            ll=centroid_from_geom(feat.get('geometry',{}))
            if not ll: continue
            lat,lon=ll
            if not (-77.2 < lon < -76.0 and 38.9 < lat < 40.0):
                continue
            rows.append({**attrs, 'lat':lat, 'lon':lon, 'source':name})
        print(f'  {name} {min(start+batch_size,len(ids))}/{len(ids)} -> {len(rows)} pts')
    return rows


def init_db():
    DB.parent.mkdir(parents=True, exist_ok=True)
    con=sqlite3.connect(DB)
    con.execute('drop table if exists houses')
    con.execute('''create table houses(
        id integer primary key, source text, external_id text, year_built integer,
        lat real, lon real, neighborhood text, zip text, use_code text
    )''')
    con.execute('drop table if exists density_cells')
    con.execute('''create table density_cells(
        id integer primary key, lat real, lon real, count integer, intensity real
    )''')
    return con


def grid_cells(rows, step=0.006):
    # ~0.4 mi north/south. Good neighborhood-scale density without plotting every parcel.
    cells={}
    for r in rows:
        key=(round(r['lat']/step)*step, round(r['lon']/step)*step)
        cells[key]=cells.get(key,0)+1
    maxc=max(cells.values()) if cells else 1
    out=[{'lat':lat,'lon':lon,'count':cnt,'intensity':cnt/maxc} for (lat,lon),cnt in cells.items()]
    out.sort(key=lambda x:x['count'], reverse=True)
    return out


def render_html(cells, total, city_count, county_count, top_places):
    max_count=max((c['count'] for c in cells), default=1)
    cells_json=json.dumps(cells)
    top_html=''.join(f'<li><b>{place}</b>: {cnt:,}</li>' for place,cnt in top_places[:15])
    html=f'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Baltimore Housing Built 1960–2006 Density</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
<style>
body{{margin:0;background:#0a0d12;color:#f6f7fb;font-family:Inter,system-ui,sans-serif}}#map{{height:78vh;width:100%}}header{{padding:18px 22px;background:#111827;border-bottom:1px solid #263244}}h1{{margin:0;font-size:clamp(24px,4vw,46px)}}p{{color:#bac4d2;max-width:980px}}.panel{{padding:16px 22px;display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px;background:#0f1624}}.stat,.note{{border:1px solid #263244;border-radius:16px;padding:14px;background:#111827}}.stat b{{font-size:28px;color:#fff}}li{{margin:5px 0;color:#d7deeb}}.legend{{position:absolute;right:18px;bottom:28px;z-index:999;background:rgba(17,24,39,.92);padding:12px;border-radius:14px;border:1px solid #334155}}.bar{{height:12px;width:180px;background:linear-gradient(90deg,#fee2e2,#fb923c,#dc2626,#7f1d1d);border-radius:999px}}
</style></head><body>
<header><h1>Baltimore homes built 1960–2006 — density map</h1><p>Redder/larger circles indicate higher concentrations of residential properties with year built from 1960 through 2006. Data is parcel/property records from Baltimore City and Baltimore County ArcGIS services; points are parcel centroids aggregated to neighborhood-scale grid cells.</p></header>
<div id="map"></div><div class="legend"><b>Density</b><div class="bar"></div><small>low → high; max cell {max_count:,} parcels</small></div>
<section class="panel"><div class="stat"><b>{total:,}</b><br>Total residential parcel centroids</div><div class="stat"><b>{city_count:,}</b><br>Baltimore City</div><div class="stat"><b>{county_count:,}</b><br>Baltimore County</div><div class="note"><b>Top labeled places/ZIPs</b><ol>{top_html}</ol></div></section>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script><script>
const cells={cells_json};
const map=L.map('map').setView([39.36,-76.61],10);
L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',{{maxZoom:19, attribution:'© OpenStreetMap contributors'}}).addTo(map);
for (const c of cells){{
  const i=c.intensity;
  const radius=5+Math.sqrt(c.count)*1.35;
  const color=i>.65?'#7f1d1d':i>.38?'#dc2626':i>.18?'#f97316':'#fecaca';
  L.circleMarker([c.lat,c.lon],{{radius, color, fillColor:color, weight:1, fillOpacity:0.34+0.46*i}})
    .bindPopup(`<b>${{c.count.toLocaleString()}}</b> homes/parcels built 1960–2006 nearby`).addTo(map);
}}
</script></body></html>'''
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(html, encoding='utf-8')
    POINTS_JSON.write_text(json.dumps(cells, indent=2), encoding='utf-8')


def main():
    city=fetch_layer('Baltimore City', CITY_URL, CITY_WHERE, 'OBJECTID,YEAR_BUILD,NEIGHBOR,ZIP_CODE,USEGROUP', batch_size=1000, return_centroid=True)
    county=fetch_layer('Baltimore County', COUNTY_URL, COUNTY_WHERE, 'OBJECTID,YEAR_BUILT,CITY,ZIP_CODE,GIS_LU_CODE,LU_CODE,BRF_PROPERTY_TYPE', batch_size=2000, return_centroid=False)
    rows=city+county
    con=init_db()
    for r in rows:
        if r['source']=='Baltimore City':
            vals=(r['source'], str(r.get('OBJECTID')), int(r.get('YEAR_BUILD') or 0), r['lat'], r['lon'], (r.get('NEIGHBOR') or '').strip(), str(r.get('ZIP_CODE') or '').strip(), (r.get('USEGROUP') or '').strip())
        else:
            vals=(r['source'], str(r.get('OBJECTID')), int((r.get('YEAR_BUILT') or '0').strip() or 0), r['lat'], r['lon'], (r.get('CITY') or '').strip(), str(r.get('ZIP_CODE') or '').strip(), (r.get('GIS_LU_CODE') or r.get('LU_CODE') or '').strip())
        con.execute('insert into houses(source,external_id,year_built,lat,lon,neighborhood,zip,use_code) values (?,?,?,?,?,?,?,?)', vals)
    cells=grid_cells(rows)
    for c in cells:
        con.execute('insert into density_cells(lat,lon,count,intensity) values (?,?,?,?)',(c['lat'],c['lon'],c['count'],c['intensity']))
    con.commit()
    top=[]
    for row in con.execute("select coalesce(nullif(neighborhood,''), zip, 'Unknown') place, count(*) cnt from houses group by place order by cnt desc limit 20"):
        top.append((row[0], row[1]))
    render_html(cells, len(rows), len(city), len(county), top)
    print(f'wrote {OUT}')
    print(f'total={len(rows)} city={len(city)} county={len(county)} cells={len(cells)} db={DB}')

if __name__=='__main__': main()
