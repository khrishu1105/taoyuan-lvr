# -*- coding: utf-8 -*-
"""桃園實登統一 ETL：預售(_b) + 中古(_a建物) + 土地(_a土地) → SQLite + 前端 JSON。
資料源：內政部不動產成交案件實際資訊供應系統（開放資料，每旬更新）。"""
import zipfile, csv, io, os, json, sqlite3, statistics, glob
from collections import defaultdict, Counter

BASE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(BASE, "raw")
DB = os.path.join(BASE, "data.db")
DATADIR = os.path.join(BASE, "data")
os.makedirs(DATADIR, exist_ok=True)
PING = 3.305785

def roc_to_ad(s):
    s = (s or "").strip()
    if not s.isdigit() or len(s) < 6: return None
    y = int(s[:-4]) + 1911; mm = s[-4:-2]; dd = s[-2:]
    if y < 2000 or y > 2027 or not ("01" <= mm <= "12"): return None  # 濾髒日期(如2101/1922)
    return f"{y}-{mm}-{dd}"

def roc_year(s):
    s = (s or "").strip()
    if not s.isdigit() or len(s) < 6: return None
    return int(s[:-4]) + 1911

def num(s):
    try: return float(str(s).replace(",", "").strip())
    except: return None

def read_csv_from_zip(z, suffix):
    cands = [n for n in z.namelist() if n.lower().endswith(suffix)]
    if not cands: return None
    return list(csv.reader(io.StringIO(z.read(cands[0]).decode("utf-8-sig"))))

presale, resale, land = [], [], []
presale_parcel = {}   # (區,地段,地號) -> 建案名；用來把建案名反貼到中古(交屋後轉售)

for zp in sorted(glob.glob(os.path.join(RAW, "*.zip"))):
    season = os.path.basename(zp).replace(".zip", "")
    ad_year = 1911 + int(season[:3])
    z = zipfile.ZipFile(zp)

    # ---- 預售屋 (_b) + 土地子表(_b_land 供地號字典) ----
    rr = read_csv_from_zip(z, "h_lvr_land_b.csv")
    proj_by_eid = {}   # 編號 -> (建案名, 區)
    if rr:
        for r in rr[2:]:
            if len(r) < 30: continue
            u = num(r[22]); up = round(u*PING/10000,2) if u else None
            t = num(r[21])
            proj = (r[28] or "").strip() or "（未填建案名）"
            eid = (r[27] or "").strip() if len(r) > 27 else ""
            if eid and proj != "（未填建案名）": proj_by_eid[eid] = (proj, r[0].strip())
            presale.append({"season":season,"d":r[0].strip(),"proj":proj,
                "addr":(r[2] or "").strip(),"date":roc_to_ad(r[7]),
                "rm":int(num(r[16]) or 0),"hl":int(num(r[17]) or 0),"ba":int(num(r[18]) or 0),
                "fl":(r[9] or "").strip(),"tf":(r[10] or "").strip(),"bt":(r[11] or "").strip(),
                "unit":up,"total":round(t/10000) if t else None,"park":(r[23] or "").strip(),
                "term":1 if (len(r)>30 and (r[30] or "").strip()) else 0})
    bl = read_csv_from_zip(z, "h_lvr_land_b_land.csv")
    if bl:
        for r in bl[2:]:
            if len(r) < 8: continue
            eid = (r[0] or "").strip()
            if eid in proj_by_eid:
                proj, dist = proj_by_eid[eid]
                presale_parcel[(dist, (r[1] or "").strip(), (r[7] or "").strip())] = proj

    # ---- 中古/成屋 & 土地 (_a) + 土地子表(_a_land 供地號比對) ----
    al = read_csv_from_zip(z, "h_lvr_land_a_land.csv")
    aparc = defaultdict(set)   # 編號 -> {(地段,地號)}
    if al:
        for r in al[2:]:
            if len(r) >= 8: aparc[(r[0] or "").strip()].add(((r[1] or "").strip(), (r[7] or "").strip()))
    rr = read_csv_from_zip(z, "h_lvr_land_a.csv")
    if rr:
        for r in rr[2:]:
            if len(r) < 28: continue
            target = (r[1] or "").strip()
            u = num(r[22]); up = round(u*PING/10000,2) if u else None
            t = num(r[21]); tw = round(t/10000) if t else None
            if target == "土地":
                area = num(r[3]); area_ping = round(area/PING,1) if area else None
                land.append({"season":season,"d":r[0].strip(),"addr":(r[2] or "").strip(),
                    "date":roc_to_ad(r[7]),"zone":(r[4] or "").strip() or (r[5] or "").strip(),
                    "area":area_ping,"unit":up,"total":tw})
            elif "建物" in target:
                by = roc_year(r[14])
                age = (ad_year - by) if by else None
                d = r[0].strip()
                eid = (r[27] or "").strip()
                keys = {(d, seg, no) for (seg, no) in aparc.get(eid, ())}
                resale.append({"season":season,"d":d,"addr":(r[2] or "").strip(),
                    "date":roc_to_ad(r[7]),"bt":(r[11] or "").strip(),"age":age if (age is not None and 0<=age<80) else None,
                    "rm":int(num(r[16]) or 0),"hl":int(num(r[17]) or 0),"ba":int(num(r[18]) or 0),
                    "fl":(r[9] or "").strip(),"tf":(r[10] or "").strip(),
                    "unit":up,"total":tw,"park":(r[23] or "").strip(),"_keys":keys})

# 用預售地號字典把建案名反貼到中古（交屋後轉售/預售換約後成屋）
resale_hit = Counter()
for r in resale:
    proj = None
    for k in r["_keys"]:
        if k in presale_parcel: proj = presale_parcel[k]; break
    r["proj"] = proj
    del r["_keys"]
    if proj: resale_hit[proj] += 1
print(f"預售 {len(presale)} | 中古 {len(resale)} | 土地 {len(land)} | 中古對到建案名 {sum(resale_hit.values()):,} 筆/{len(resale_hit)} 建案")

# ================= SQLite =================
if os.path.exists(DB): os.remove(DB)
con = sqlite3.connect(DB); cur = con.cursor()
cur.execute("""CREATE TABLE presale(season,district,project,addr,tdate,rooms INT,halls INT,baths INT,
  floor,totfloor,btype,unit_ping REAL,total_wan INT,park,terminated INT)""")
cur.execute("""CREATE TABLE resale(season,district,addr,tdate,btype,age INT,rooms INT,halls INT,baths INT,
  floor,totfloor,unit_ping REAL,total_wan INT,park,project)""")
cur.execute("""CREATE TABLE land(season,district,addr,tdate,zone,area_ping REAL,unit_ping REAL,total_wan INT)""")
cur.executemany("INSERT INTO presale VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
  [(x["season"],x["d"],x["proj"],x["addr"],x["date"],x["rm"],x["hl"],x["ba"],x["fl"],x["tf"],x["bt"],x["unit"],x["total"],x["park"],x["term"]) for x in presale])
cur.executemany("INSERT INTO resale VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
  [(x["season"],x["d"],x["addr"],x["date"],x["bt"],x["age"],x["rm"],x["hl"],x["ba"],x["fl"],x["tf"],x["unit"],x["total"],x["park"],x.get("proj")) for x in resale])
cur.executemany("INSERT INTO land VALUES(?,?,?,?,?,?,?,?)",
  [(x["season"],x["d"],x["addr"],x["date"],x["zone"],x["area"],x["unit"],x["total"]) for x in land])
con.commit()

# ================= 預售：建案聚合 + 逐筆 =================
g = defaultdict(list)
for r in presale: g[(r["d"], r["proj"])].append(r)
pprojects = []
for (d, proj), lst in g.items():
    valid=[x for x in lst if not x["term"]]
    priced=[x["unit"] for x in valid if x["unit"] and x["unit"]>0]
    dates=[x["date"] for x in valid if x["date"]]; tots=[x["total"] for x in valid if x["total"]]
    rc=Counter(x["rm"] for x in valid if x["rm"])
    pprojects.append({"d":d,"n":proj,"cnt":len(lst),"valid":len(valid),"term":sum(x["term"] for x in lst),"resale":resale_hit.get(proj,0),
        "avg":round(statistics.mean(priced),1) if priced else None,"med":round(statistics.median(priced),1) if priced else None,
        "lo":round(min(priced),1) if priced else None,"hi":round(max(priced),1) if priced else None,
        "avgtot":round(statistics.mean(tots)) if tots else None,
        "p1":min(dates) if dates else None,"p2":max(dates) if dates else None,
        "room":(rc.most_common(1)[0][0] if rc else None)})
pprojects.sort(key=lambda x:-x["valid"])
def dump(name,obj): json.dump(obj,open(os.path.join(DATADIR,name),"w",encoding="utf-8"),ensure_ascii=False,separators=(",",":"))
dump("presale_projects.json", pprojects)
dump("presale_tx.json", [[r["d"],r["proj"],r["addr"],r["date"],r["rm"],r["hl"],r["ba"],r["fl"],r["tf"],r["unit"],r["total"],r["park"],r["term"]] for r in presale])

# ================= 中古：逐筆 + 區×型態統計 =================
dump("resale_tx.json", [[r["d"],r["addr"],r["date"],r["bt"],r["age"],r["rm"],r["hl"],r["ba"],r["fl"],r["tf"],r["unit"],r["total"],r.get("proj") or ""] for r in resale])
ga=defaultdict(list)
for r in resale: ga[(r["d"],r["bt"])].append(r)
resale_area=[]
for (d,bt),lst in ga.items():
    pr=[x["unit"] for x in lst if x["unit"] and x["unit"]>0]
    ages=[x["age"] for x in lst if x["age"] is not None]
    resale_area.append({"d":d,"bt":bt,"cnt":len(lst),
        "avg":round(statistics.mean(pr),1) if pr else None,"med":round(statistics.median(pr),1) if pr else None,
        "lo":round(min(pr),1) if pr else None,"hi":round(max(pr),1) if pr else None,
        "age":round(statistics.median(ages)) if ages else None})
resale_area.sort(key=lambda x:-x["cnt"])
dump("resale_area.json", resale_area)

# 中古：棟/社區聚合（門牌截到「號」= 同棟；只收 >=2 筆 = 有二次成交）
import re as _re
def bldg_key(addr):
    if not addr: return None
    m=_re.search("號", addr)
    return addr[:m.end()] if m else addr
gb=defaultdict(list)
for r in resale:
    bk=bldg_key(r["addr"])
    if bk: gb[(r["d"],bk)].append(r)
resale_bldg=[]
for (d,bk),lst in gb.items():
    if len(lst)<2: continue  # 只保留有二次成交的棟
    pr=[x["unit"] for x in lst if x["unit"] and x["unit"]>0]
    dates=[x["date"] for x in lst if x["date"]]
    bt=Counter(x["bt"] for x in lst if x["bt"])
    pj=Counter(x["proj"] for x in lst if x.get("proj"))
    resale_bldg.append({"d":d,"b":bk,"cnt":len(lst),
        "avg":round(statistics.mean(pr),1) if pr else None,"med":round(statistics.median(pr),1) if pr else None,
        "lo":round(min(pr),1) if pr else None,"hi":round(max(pr),1) if pr else None,
        "p1":min(dates) if dates else None,"p2":max(dates) if dates else None,
        "bt":(bt.most_common(1)[0][0] if bt else ""),"proj":(pj.most_common(1)[0][0] if pj else "")})
resale_bldg.sort(key=lambda x:-x["cnt"])
dump("resale_bldg.json", resale_bldg)
print(f"  中古棟聚合(>=2筆): {len(resale_bldg)} 棟")

# ================= 土地：逐筆 + 區×分區統計 =================
dump("land_tx.json", [[r["d"],r["addr"],r["date"],r["zone"],r["area"],r["unit"],r["total"]] for r in land])
gl=defaultdict(list)
for r in land: gl[(r["d"],r["zone"] or "其他")].append(r)
land_area=[]
for (d,z),lst in gl.items():
    pr=[x["unit"] for x in lst if x["unit"] and x["unit"]>0]
    land_area.append({"d":d,"zone":z,"cnt":len(lst),
        "avg":round(statistics.mean(pr),1) if pr else None,"med":round(statistics.median(pr),1) if pr else None,
        "lo":round(min(pr),1) if pr else None,"hi":round(max(pr),1) if pr else None})
land_area.sort(key=lambda x:-x["cnt"])
dump("land_area.json", land_area)

# ================= meta =================
seasons=sorted(set([r["season"] for r in presale]+[r["season"] for r in resale]+[r["season"] for r in land]))
districts=sorted(set([r["d"] for r in presale]+[r["d"] for r in resale]+[r["d"] for r in land]))
def rng(rows):  # 過濾明顯髒日期(<2000)，回傳真實 min/max
    ds=sorted(x["date"] for x in rows if x["date"] and x["date"]>="2000-01-01")
    return (ds[0], ds[-1]) if ds else (None, None)
p_min,p_max=rng(presale); r_min,r_max=rng(resale); l_min,l_max=rng(land)
alldates=[d for d in (p_min,p_max,r_min,r_max,l_min,l_max) if d]
meta={"seasons":seasons,"districts":districts,
    "presale_tx":len(presale),"presale_projects":len(pprojects),"presale_term":sum(r["term"] for r in presale),
    "resale_tx":len(resale),"land_tx":len(land),
    "resale_named":sum(1 for x in resale if x.get("proj")),"resale_named_projects":len(resale_hit),
    "presale_min":p_min,"presale_max":p_max,"resale_min":r_min,"resale_max":r_max,"land_min":l_min,"land_max":l_max,
    "date_min":min(alldates),"date_max":max(alldates)}
json.dump(meta,open(os.path.join(DATADIR,"meta.json"),"w",encoding="utf-8"),ensure_ascii=False,indent=1)
con.close()

print("季別:", seasons[0], "~", seasons[-1], f"({len(seasons)}季)")
print("日期:", meta["date_min"], "~", meta["date_max"])
for f in sorted(os.listdir(DATADIR)):
    p=os.path.join(DATADIR,f); print(f"  {f}: {round(os.path.getsize(p)/1024,1)} KB")
