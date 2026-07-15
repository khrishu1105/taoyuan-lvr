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

def adj_unit_price(total, car_price, barea, carea):
    """去車位單價(萬/坪)=(總價-車位價)/(建物面積-車位面積)。避免車位灌入拉低單價。"""
    if not total or not barea: return None
    net_area = barea - (carea or 0)
    net_price = total - (car_price or 0)
    if net_area <= 0 or net_price <= 0: return None
    return round(net_price / net_area * PING / 10000, 2)

# 特殊交易/非市場行情關鍵字(備註)：這些會造成異常低價，需排除於行情統計與檢便宜
SPECIAL_NOTE = ("親友","員工","共有人","特殊關係","關係人","含土地增值","增建","持分","權利範圍",
    "債務","債權","瑕疵","分算","急買","急賣","畸零","毛胚","未辦保存","未登記","含裝潢","facilities",
    "含車位交易總價","坪數含","二親等","公同共有","抵繳","拍賣","法拍","協議")
def is_special(note): return any(k in (note or "") for k in SPECIAL_NOTE)
def is_resi(use): return "住" in (use or "")   # 住家用/住商用/國民住宅=住宅；商業/辦公/工業/其他=非住宅
# 政策性住宅(區段徵收安置宅/抵價地/合宜宅)非市場行情，須排除；價格受管制偏低會拉低均價
POLICY_KW = ("航空城", "合宜", "安置住宅", "區段徵收", "抵價地", "社會住宅")
POLICY_PROJ = {"成家大璽"}   # 明確政策宅但案名無關鍵字者
def is_policy(proj):
    p = proj or ""
    return any(k in p for k in POLICY_KW) or p in POLICY_PROJ
def clean_ok(r): return r.get("resi") and not r.get("special") and not r.get("policy")   # 純市場住宅

presale, resale, land = [], [], []
presale_parcel = {}   # (區,地段,地號) -> 建案名；用來把建案名反貼到中古(交屋後轉售)
bseg = {}   # 預售編號 -> 地段

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
            t = num(r[21])
            up = adj_unit_price(t, num(r[25]), num(r[15]), num(r[24]))  # 去車位單價
            use = (r[12] or "").strip()
            proj = (r[28] or "").strip() or "（未填建案名）"
            eid = (r[27] or "").strip() if len(r) > 27 else ""
            if eid and proj != "（未填建案名）": proj_by_eid[eid] = (proj, r[0].strip())
            presale.append({"season":season,"d":r[0].strip(),"proj":proj,"eid":eid,
                "addr":(r[2] or "").strip(),"date":roc_to_ad(r[7]),"use":use,
                "rm":int(num(r[16]) or 0),"hl":int(num(r[17]) or 0),"ba":int(num(r[18]) or 0),
                "fl":(r[9] or "").strip(),"tf":(r[10] or "").strip(),"bt":(r[11] or "").strip(),
                "unit":up,"total":round(t/10000) if t else None,"park":(r[23] or "").strip(),
                "resi":is_resi(use),"special":is_special(r[26] or ""),"policy":is_policy(proj),
                "term":1 if (len(r)>30 and (r[30] or "").strip()) else 0})
    bl = read_csv_from_zip(z, "h_lvr_land_b_land.csv")
    if bl:
        for r in bl[2:]:
            if len(r) < 8: continue
            eid = (r[0] or "").strip()
            bseg.setdefault(eid, (r[1] or "").strip())
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
                up_adj = adj_unit_price(t, num(r[25]), num(r[15]), num(r[24]))  # 去車位單價
                use = (r[12] or "").strip(); note=(r[26] or "").strip()
                segs=[s for (_,s,_) in keys]; seg=Counter(segs).most_common(1)[0][0] if segs else ""
                resale.append({"season":season,"d":d,"addr":(r[2] or "").strip(),
                    "date":roc_to_ad(r[7]),"bt":(r[11] or "").strip(),"age":age if (age is not None and 0<=age<80) else None,
                    "rm":int(num(r[16]) or 0),"hl":int(num(r[17]) or 0),"ba":int(num(r[18]) or 0),
                    "fl":(r[9] or "").strip(),"tf":(r[10] or "").strip(),"use":use,"seg":seg,
                    "unit":up_adj,"total":tw,"park":(r[23] or "").strip(),"note":note,
                    "resi":is_resi(use),"special":is_special(note),"_keys":keys})

# 用預售地號字典把建案名反貼到中古(標示該筆屬於哪個建案)
named_cnt = Counter()
for r in resale:
    proj = None
    for k in r["_keys"]:
        if k in presale_parcel: proj = presale_parcel[k]; break
    r["proj"] = proj
    r["policy"] = is_policy(proj)
    del r["_keys"]
    if proj: named_cnt[proj] += 1

# 真二次轉售判定：同一完整門牌(含樓/之X)在成屋檔出現>=2次，第2次起才算轉售。
# (剛交屋建案的成屋紀錄多為建商成屋/交屋首購=第一手，不能算轉售)
addr_groups = defaultdict(list)
for r in resale:
    if r["addr"]: addr_groups[(r["d"], r["addr"])].append(r)
resale_hit = Counter()   # 建案 -> 真二次轉售筆數
for (d, addr), lst in addr_groups.items():
    extra = len(lst) - 1   # 第一次為一手，其餘為轉售
    if extra <= 0: continue
    projs = [x["proj"] for x in lst if x.get("proj")]
    if projs:
        resale_hit[Counter(projs).most_common(1)[0][0]] += extra
for r in presale: r["seg"] = bseg.get(r["eid"], "")
# 近3年基準(動態)：最新成交日往前推3年。全期中位會被早年低價拉低，另計近3年中位才貼近現況
_alld = sorted(x["date"] for x in presale+resale+land if x["date"] and x["date"]>="2000-01-01")
_maxd = _alld[-1] if _alld else "2026-01-01"
RECENT_CUT = f"{int(_maxd[:4])-3}{_maxd[4:]}"   # e.g. 2023-05-10
def med3(rows):  # 近3年中位單價(rows需已含unit/date)
    pr=[x["unit"] for x in rows if x["unit"] and x["unit"]>0 and x["date"] and x["date"]>=RECENT_CUT]
    return round(statistics.median(pr),1) if pr else None
print(f"預售 {len(presale)} | 中古 {len(resale)} | 土地 {len(land)} | 對到建案名 {sum(named_cnt.values()):,}筆/{len(named_cnt)}建案 | 真二次轉售 {sum(resale_hit.values()):,}筆/{len(resale_hit)}建案 | 近3年基準 {RECENT_CUT}起")

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
    clean=[x for x in valid if clean_ok(x)]  # 純市場住宅(排除特殊/政策宅)
    priced=[x["unit"] for x in clean if x["unit"] and x["unit"]>0]
    dates=[x["date"] for x in valid if x["date"]]; tots=[x["total"] for x in clean if x["total"]]
    rc=Counter(x["rm"] for x in clean if x["rm"])
    pprojects.append({"d":d,"n":proj,"cnt":len(lst),"valid":len(valid),"term":sum(x["term"] for x in lst),"resale":resale_hit.get(proj,0),
        "med3":med3(clean),
        "avg":round(statistics.mean(priced),1) if priced else None,"med":round(statistics.median(priced),1) if priced else None,
        "lo":round(min(priced),1) if priced else None,"hi":round(max(priced),1) if priced else None,
        "avgtot":round(statistics.mean(tots)) if tots else None,
        "p1":min(dates) if dates else None,"p2":max(dates) if dates else None,
        "room":(rc.most_common(1)[0][0] if rc else None)})
pprojects.sort(key=lambda x:-x["valid"])
def dump(name,obj): json.dump(obj,open(os.path.join(DATADIR,name),"w",encoding="utf-8"),ensure_ascii=False,separators=(",",":"))
dump("presale_projects.json", pprojects)
dump("presale_tx.json", [[r["d"],r["proj"],r["addr"],r["date"],r["rm"],r["hl"],r["ba"],r["fl"],r["tf"],r["unit"],r["total"],r["park"],r["term"]] for r in presale])

# ================= 中古：精簡(仿trueway不做舊屋) + 棟聚合 + 檢便宜 =================
import re as _re
def bldg_key(addr):
    if not addr: return None
    m=_re.search("號", addr)
    return addr[:m.end()] if m else addr
AGE_CUT = 20   # 只保留「有社區名」或「屋齡<=20」的中古(砍老舊無識別)
RESALE_KEEP = [r for r in resale if r.get("proj") or (r["age"] is not None and r["age"] <= AGE_CUT)]

# 棟/社區聚合(門牌截到「號」=同棟；>=2筆=有二次成交) + 每棟中位數(供檢便宜基準)
gb=defaultdict(list)
for r in RESALE_KEEP:
    bk=bldg_key(r["addr"]); r["_bk"]=bk
    if bk: gb[(r["d"],bk)].append(r)
bldg_med={}; resale_bldg=[]
for (d,bk),lst in gb.items():
    pr=[x["unit"] for x in lst if x["unit"] and x["unit"]>0 and clean_ok(x)]
    if pr: bldg_med[(d,bk)]=statistics.median(pr)
    if len(lst)<2: continue
    dates=[x["date"] for x in lst if x["date"]]
    bt=Counter(x["bt"] for x in lst if x["bt"]); pj=Counter(x["proj"] for x in lst if x.get("proj"))
    # 真轉售=同一門牌出現>=2次的超出部分(第一次為一手)
    ac=Counter(x["addr"] for x in lst if x["addr"]); resold=sum(c-1 for c in ac.values() if c>=2)
    clean3=[x for x in lst if clean_ok(x)]
    resale_bldg.append({"d":d,"b":bk,"cnt":len(lst),"resold":resold,"med3":med3(clean3),
        "avg":round(statistics.mean(pr),1) if pr else None,"med":round(statistics.median(pr),1) if pr else None,
        "lo":round(min(pr),1) if pr else None,"hi":round(max(pr),1) if pr else None,
        "p1":min(dates) if dates else None,"p2":max(dates) if dates else None,
        "bt":(bt.most_common(1)[0][0] if bt else ""),"proj":(pj.most_common(1)[0][0] if pj else "")})
resale_bldg.sort(key=lambda x:-x["cnt"])
dump("resale_bldg.json", resale_bldg)

# 區×型態統計 + 中位數(檢便宜次要基準)
ga=defaultdict(list)
for r in RESALE_KEEP: ga[(r["d"],r["bt"])].append(r)
area_med={}; resale_area=[]
for (d,bt),lst in ga.items():
    pr=[x["unit"] for x in lst if x["unit"] and x["unit"]>0 and clean_ok(x)]
    ages=[x["age"] for x in lst if x["age"] is not None]
    if pr: area_med[(d,bt)]=statistics.median(pr)
    resale_area.append({"d":d,"bt":bt,"cnt":len(lst),
        "avg":round(statistics.mean(pr),1) if pr else None,"med":round(statistics.median(pr),1) if pr else None,
        "lo":round(min(pr),1) if pr else None,"hi":round(max(pr),1) if pr else None,
        "age":round(statistics.median(ages)) if ages else None})
resale_area.sort(key=lambda x:-x["cnt"])
dump("resale_area.json", resale_area)

# 檢便宜：每筆相對「同棟中位數(優先)或同區同型態中位數」的價差%
def gap_of(r):
    u=r["unit"]
    if not u or u<=0: return None
    base=bldg_med.get((r["d"],r.get("_bk"))) or area_med.get((r["d"],r["bt"]))
    if not base: return None
    return round((u-base)/base*100)
# resale_tx: 加 gap(13)、用途(14)、住宅(15,1/0)、特殊交易(16,1/0)，供前端顯示與過濾
dump("resale_tx.json", [[r["d"],r["addr"],r["date"],r["bt"],r["age"],r["rm"],r["hl"],r["ba"],r["fl"],r["tf"],r["unit"],r["total"],r.get("proj") or "",gap_of(r),r.get("use",""),1 if r.get("resi") else 0,1 if r.get("special") else 0] for r in RESALE_KEEP])
# 檢便宜：住宅、非特殊、有社區名、單價>=15、合理便宜區間(-35%~-8%)
bargains=[]
for r in RESALE_KEEP:
    if not (clean_ok(r) and r.get("proj") and r["unit"] and r["unit"]>=15): continue
    g=gap_of(r)
    if g is not None and -35<=g<=-8:
        bargains.append([r["d"],r["addr"],r["date"],r.get("proj"),r["bt"],r["age"],r["unit"],r["total"],g])
bargains.sort(key=lambda x:x[8])
dump("resale_bargains.json", bargains[:2000])
resi_keep=sum(1 for r in RESALE_KEEP if clean_ok(r))
print(f"  中古精簡 {len(RESALE_KEEP):,}/{len(resale):,} (住宅純淨{resi_keep:,}) | 棟聚合 {len(resale_bldg):,} | 檢便宜 {len(bargains):,}筆")

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

# ================= 地圖：各行政區彙總(每品項 成交量/中位單價/均總價) =================
def district_agg(rows, clean=False):
    g=defaultdict(list)
    for r in rows:
        if clean and not clean_ok(r): continue
        g[r["d"]].append(r)
    out={}
    for d,lst in g.items():
        pr=[x["unit"] for x in lst if x["unit"] and x["unit"]>0]
        tots=[x["total"] for x in lst if x["total"]]
        out[d]={"n":len(lst),"med":round(statistics.median(pr),1) if pr else None,"med3":med3(lst),
                "avgtot":round(statistics.mean(tots)) if tots else None}
    return out
map_agg={"presale":district_agg([x for x in presale if not x["term"]],clean=True),
         "resale":district_agg(RESALE_KEEP,clean=True),
         "land":district_agg(land)}
dump("map_agg.json", map_agg)

# ================= 地圖：重劃區(依地段歸類，可校正) =================
# 對照表：重劃區 -> {中心座標, 對應地段清單}。歸類把握度高者優先，Khris(都計+代銷)可校正。
# 地段歸類經「代表建案反查地段」實測校正；座標經 OSM 地理編碼(部分機捷站用已知站點座標)。
REDEV_ZONES = {
 "青埔A18高鐵站":   {"c":[25.0137,121.2141],"segs":["青昇段","青平段","青埔段","青山段"]},
 "青埔A19體育園區":  {"c":[25.0017,121.2033],"segs":["青芝段","青塘段"]},
 "青埔A17領航站":   {"c":[25.0241,121.2211],"segs":["青溪段"]},
 "中路重劃區":       {"c":[24.9919,121.2863],"segs":["中路段","中路二段","中路三段"]},
 "小檜溪重劃區":     {"c":[25.0014,121.3064],"segs":["三民段"]},
 "經國重劃區":       {"c":[25.0275,121.2875],"segs":["水汴頭段","龍祥段"]},
 "藝文特區":         {"c":[25.0172,121.2996],"segs":["同安段","中埔段"]},
 "大有/龍安":        {"c":[25.0044,121.3227],"segs":["大有段","忠義段"]},
 "桃園站前":         {"c":[24.9893,121.3136],"segs":["東門段"]},
 "航空城客運園區":   {"c":[25.0480,121.2030],"segs":["客運一段","客運二段","客運三段"]},
 "中壢站前A22":      {"c":[24.9533,121.2262],"segs":["豐興段"]},
 "龜山機捷A7":       {"c":[25.0338,121.3898],"segs":["善捷段","樂捷段"]},
 "蘆竹南崁":         {"c":[25.0535,121.2849],"segs":["上興段","新鼻段","大新段"]},
 "八德擴大重劃":     {"c":[24.9287,121.2969],"segs":["興仁段","明智段"]},
 "龍潭中正":         {"c":[24.8604,121.2183],"segs":["石門段"]},
 "楊梅":             {"c":[24.9344,121.0832],"segs":["二重溪段","頭重溪段","大金山下段大金山下小段"]},
}
seg2zone={}
for z,info in REDEV_ZONES.items():
    for s in info["segs"]: seg2zone[s]=z
def zone_agg(rows, clean=False):
    g=defaultdict(list)
    for r in rows:
        z=seg2zone.get(r.get("seg",""))
        if not z: continue
        if clean and not clean_ok(r): continue
        g[z].append(r)
    out={}
    for z,lst in g.items():
        pr=[x["unit"] for x in lst if x["unit"] and x["unit"]>0]
        tots=[x["total"] for x in lst if x["total"]]
        out[z]={"n":len(lst),"med":round(statistics.median(pr),1) if pr else None,"med3":med3(lst),
                "avgtot":round(statistics.mean(tots)) if tots else None}
    return out
map_zones={"zones":{z:info["c"] for z,info in REDEV_ZONES.items()},
           "presale":zone_agg([x for x in presale if not x["term"]],clean=True),
           "resale":zone_agg(RESALE_KEEP,clean=True)}
dump("map_zones.json", map_zones)
print(f"  重劃區地圖: {len(REDEV_ZONES)}區, 預售命中 {sum(v['n'] for v in map_zones['presale'].values()):,}筆")

# ================= 趨勢：各季成交量 + 中位單價 =================
def quarter(d):
    if not d or d<"2000-01-01": return None
    return f"{d[:4]}Q{(int(d[5:7])-1)//3+1}"
def trend_agg(rows, clean=False):
    g=defaultdict(list)
    for r in rows:
        if clean and not clean_ok(r): continue
        q=quarter(r["date"])
        if q and int(q[:4])>=2018: g[q].append(r)
    out=[]
    for q in sorted(g):
        lst=g[q]; pr=[x["unit"] for x in lst if x["unit"] and x["unit"]>0]
        out.append({"q":q,"n":len(lst),"med":round(statistics.median(pr),1) if pr else None})
    return out
trends={"presale":trend_agg([x for x in presale if not x["term"]],clean=True),
        "resale":trend_agg(RESALE_KEEP,clean=True),
        "land":trend_agg(land)}
dump("trends.json", trends)

# 各重劃區的季趨勢(供點擊重劃區看成交量/價格)
def zone_trend(rows, clean=False):
    g=defaultdict(lambda: defaultdict(list))
    for r in rows:
        z=seg2zone.get(r.get("seg",""))
        if not z: continue
        if clean and not clean_ok(r): continue
        q=quarter(r["date"])
        if q and int(q[:4])>=2019: g[z][q].append(r)
    out={}
    for z,qs in g.items():
        out[z]=[{"q":q,"n":len(qs[q]),
                 "med":round(statistics.median([x["unit"] for x in qs[q] if x["unit"] and x["unit"]>0]),1) if [x for x in qs[q] if x["unit"] and x["unit"]>0] else None}
                for q in sorted(qs)]
    return out
zone_trends={"presale":zone_trend([x for x in presale if not x["term"]],clean=True),
             "resale":zone_trend(RESALE_KEEP,clean=True)}
dump("zone_trends.json", zone_trends)

# 各重劃區「成交量由哪些建案組成」明細(供點擊稽核) [案名, 筆數, 中位單價, 近3年中位]
def zone_breakdown(rows):
    g=defaultdict(lambda: defaultdict(list))
    for r in rows:
        z=seg2zone.get(r.get("seg",""))
        if not z or not clean_ok(r): continue
        g[z][r.get("proj") or "（未對到建案名）"].append(r)
    out={}
    for z,projs in g.items():
        lst=[]
        for proj,rs in projs.items():
            pr=[x["unit"] for x in rs if x["unit"] and x["unit"]>0]
            p1=min([x["date"] for x in rs if x["date"]] or [""]); p2=max([x["date"] for x in rs if x["date"]] or [""])
            lst.append([proj,len(rs),round(statistics.median(pr),1) if pr else None,med3(rs),p1[:7],p2[:7]])
        lst.sort(key=lambda x:-x[1])
        out[z]=lst
    return out
zone_breakdown_data={"presale":zone_breakdown([x for x in presale if not x["term"]]),
                     "resale":zone_breakdown(RESALE_KEEP)}
dump("zone_breakdown.json", zone_breakdown_data)

# ================= meta =================
seasons=sorted(set([r["season"] for r in presale]+[r["season"] for r in resale]+[r["season"] for r in land]))
districts=sorted(set([r["d"] for r in presale]+[r["d"] for r in resale]+[r["d"] for r in land]))
def rng(rows):  # 過濾明顯髒日期(<2000)，回傳真實 min/max
    ds=sorted(x["date"] for x in rows if x["date"] and x["date"]>="2000-01-01")
    return (ds[0], ds[-1]) if ds else (None, None)
p_min,p_max=rng(presale); r_min,r_max=rng(RESALE_KEEP); l_min,l_max=rng(land)
alldates=[d for d in (p_min,p_max,r_min,r_max,l_min,l_max) if d]
meta={"seasons":seasons,"districts":districts,
    "presale_tx":len(presale),"presale_projects":len(pprojects),"presale_term":sum(r["term"] for r in presale),
    "resale_tx":len(RESALE_KEEP),"resale_tx_all":len(resale),"land_tx":len(land),
    "resale_named":sum(1 for x in RESALE_KEEP if x.get("proj")),"resale_named_projects":len(named_cnt),
    "resale_resold":sum(resale_hit.values()),
    "resale_bargains":len(bargains[:2000]),"recent_cut":RECENT_CUT,
    "presale_min":p_min,"presale_max":p_max,"resale_min":r_min,"resale_max":r_max,"land_min":l_min,"land_max":l_max,
    "date_min":min(alldates),"date_max":max(alldates)}
json.dump(meta,open(os.path.join(DATADIR,"meta.json"),"w",encoding="utf-8"),ensure_ascii=False,indent=1)
con.close()

print("季別:", seasons[0], "~", seasons[-1], f"({len(seasons)}季)")
print("日期:", meta["date_min"], "~", meta["date_max"])
for f in sorted(os.listdir(DATADIR)):
    p=os.path.join(DATADIR,f); print(f"  {f}: {round(os.path.getsize(p)/1024,1)} KB")
