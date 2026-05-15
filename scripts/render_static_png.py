#!/usr/bin/env python3
from pathlib import Path
import sqlite3, math
from PIL import Image, ImageDraw, ImageFont, ImageFilter

ROOT=Path('/home/varmint/Code/baltimore-housing-density')
DB=ROOT/'data/housing_1960_2006.db'
OUT=ROOT/'site/baltimore_housing_density_1960_2006.png'
W,H=1600,1600
PAD=90
# Approx bbox covering Baltimore City + County
MIN_LON,MAX_LON=-77.02,-76.28
MIN_LAT,MAX_LAT=39.18,39.73

def xy(lat,lon):
    x=PAD+(lon-MIN_LON)/(MAX_LON-MIN_LON)*(W-2*PAD)
    y=H-PAD-(lat-MIN_LAT)/(MAX_LAT-MIN_LAT)*(H-2*PAD)
    return x,y

con=sqlite3.connect(DB)
cells=[dict(lat=r[0],lon=r[1],count=r[2],intensity=r[3]) for r in con.execute('select lat,lon,count,intensity from density_cells')]
total=con.execute('select count(*) from houses').fetchone()[0]
city=con.execute("select count(*) from houses where source='Baltimore City'").fetchone()[0]
county=con.execute("select count(*) from houses where source='Baltimore County'").fetchone()[0]
top=con.execute("select coalesce(nullif(neighborhood,''), zip, 'Unknown') place,count(*) c from houses group by place order by c desc limit 10").fetchall()
maxc=max(c['count'] for c in cells)

img=Image.new('RGB',(W,H),'#08111f')
# subtle background grid
base=Image.new('RGBA',(W,H),(0,0,0,0)); d=ImageDraw.Draw(base)
for i in range(0,W,80): d.line((i,0,i,H),fill=(255,255,255,10))
for j in range(0,H,80): d.line((0,j,W,j),fill=(255,255,255,10))
# draw heat blurred layer
heat=Image.new('RGBA',(W,H),(0,0,0,0)); hd=ImageDraw.Draw(heat)
for c in sorted(cells,key=lambda z:z['count']):
    x,y=xy(c['lat'],c['lon'])
    inten=c['intensity']
    r=5+math.sqrt(c['count'])*2.2
    alpha=int(35+190*inten)
    if inten>.65: col=(127,29,29,alpha)
    elif inten>.38: col=(220,38,38,alpha)
    elif inten>.18: col=(249,115,22,alpha)
    else: col=(254,202,202,alpha)
    hd.ellipse((x-r,y-r,x+r,y+r),fill=col)
heat=heat.filter(ImageFilter.GaussianBlur(2))
img=Image.alpha_composite(img.convert('RGBA'), base)
img=Image.alpha_composite(img, heat)
d=ImageDraw.Draw(img)
# title panel
try:
    font_big=ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',52)
    font=ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',26)
    font_bold=ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',30)
    font_small=ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',21)
except Exception:
    font_big=font=font_bold=font_small=None

d.rounded_rectangle((45,40,W-45,190),radius=28,fill=(9,18,32,230),outline=(70,90,120,200),width=2)
d.text((70,58),'Baltimore homes built 1960–2006',fill='white',font=font_big)
d.text((72,124),f'Red = highest density. {total:,} residential parcel centroids • City {city:,} • County {county:,} • max grid cell {maxc:,}',fill=(204,213,225),font=font)
# labels approximate
labels=[('Owings Mills',39.42,-76.78),('Randallstown',39.37,-76.80),('Reisterstown',39.47,-76.83),('Perry Hall',39.41,-76.46),('Dundalk',39.25,-76.52),('Catonsville',39.27,-76.73),('Towson',39.40,-76.61),('Baltimore City',39.30,-76.61),('Cockeysville',39.48,-76.64)]
for name,lat,lon in labels:
    x,y=xy(lat,lon)
    d.rounded_rectangle((x-6,y-18,x+len(name)*12+12,y+18),radius=8,fill=(8,13,23,185))
    d.text((x+4,y-14),name,fill=(255,255,255),font=font_small)
# legend/top places
x0,y0=W-520,H-430
d.rounded_rectangle((x0,y0,W-45,H-45),radius=26,fill=(9,18,32,235),outline=(70,90,120,200),width=2)
d.text((x0+24,y0+22),'Top places / ZIP labels',fill='white',font=font_bold)
y=y0+68
for place,cnt in top:
    label=(place or 'Unknown')[:26]
    d.text((x0+28,y),f'{label}: {cnt:,}',fill=(218,226,238),font=font_small); y+=31
# color legend
d.text((70,H-78),'Density scale:',fill=(218,226,238),font=font_small)
for i in range(240):
    t=i/239
    if t>.65: col=(127,29,29)
    elif t>.38: col=(220,38,38)
    elif t>.18: col=(249,115,22)
    else: col=(254,202,202)
    d.line((230+i,H-68,230+i,H-44),fill=col)
d.text((485,H-72),'low → high',fill=(218,226,238),font=font_small)
OUT.parent.mkdir(parents=True,exist_ok=True)
img.convert('RGB').save(OUT,quality=94)
print(OUT)
