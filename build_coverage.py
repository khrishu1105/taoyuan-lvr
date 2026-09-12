# -*- coding: utf-8 -*-
"""覆蓋清單(coverage manifest):解析 raw/*.zip 的 build_time.xml 宣告窗口，
建立「哪些官方發布窗口本機已保存」的可稽核事實，再據此判定每個交易月份的狀態。

關鍵原則(依交接任務書)：月份狀態由來源覆蓋窗口決定，不是由資料量多寡決定。
- 買賣(中古)以「登記日期」為基準；預售以「交易日期」為基準。
- 登記日期會落後交易日期，故買賣某交易月要到登記覆蓋到月底+緩衝才算完整。

輸出：data/coverage.json
狀態 enum：complete / local_source_gap / official_pending / mixed / unpublished
"""
import os, re, json, glob, hashlib, zipfile, datetime

BASE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(BASE, "raw")
OUT = os.path.join(BASE, "data", "coverage.json")
RETRIEVED = datetime.date.today()
REG_BUFFER = 30  # 買賣：交易月底再加 30 天，作為登記落後的完整性緩衝

def roc(y, m, d): return datetime.date(int(y) + 1911, int(m), int(d))
def parse_dates(seg):
    ds = re.findall(r"(\d{2,3})年\s*(\d{1,2})月\s*(\d{1,2})", seg)
    return [roc(*t) for t in ds]

def windows_from_xml(text):
    """回傳 {'resale': (start,end)登記, 'presale': (start,end)交易}"""
    out = {}
    mbuy = re.search(r"登記日期[^，]*?之買賣案件", text)
    if mbuy:
        d = parse_dates(mbuy.group(0))
        if len(d) >= 2: out["resale"] = (d[0], d[1])
    mpre = re.search(r"交易日期[^，]*?之預售屋案件", text)
    if mpre:
        d = parse_dates(mpre.group(0))
        if len(d) >= 2: out["presale"] = (d[0], d[1])
    return out

def batch_kind(name):
    return "current_static" if re.search(r"Z\d{8}", name) else "season_static"

# ── 掃描所有批次 ──
manifest = []
win = {"resale": [], "presale": []}   # 各市場已覆蓋窗口(declared)
for zp in sorted(glob.glob(os.path.join(RAW, "*.zip"))):
    name = os.path.basename(zp)
    try:
        with zipfile.ZipFile(zp) as z:
            xml = z.read("build_time.xml").decode("utf-8", "ignore")
    except Exception:
        continue
    ws = windows_from_xml(xml)
    sha = hashlib.sha256(open(zp, "rb").read()).hexdigest()[:16]
    build_date = None
    try:
        with zipfile.ZipFile(zp) as z:
            build_date = "%04d-%02d-%02d" % z.getinfo("build_time.xml").date_time[:3]
    except Exception:
        pass
    for market, (s, e) in ws.items():
        basis = "registration_date" if market == "resale" else "transaction_date"
        win[market].append((s, e))
        manifest.append({
            "source_file": name, "source_kind": batch_kind(name), "market": market,
            "coverage_basis": basis, "coverage_start": s.isoformat(), "coverage_end": e.isoformat(),
            "coverage_type": "declared", "build_date": build_date, "checksum": "sha256:" + sha,
        })

# ── 合併覆蓋窗口、算連續覆蓋邊界與官方最新可得邊界 ──
def merge(ranges):
    rs = sorted(ranges); out = []
    for s, e in rs:
        if out and s <= out[-1][1] + datetime.timedelta(days=1):
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out

cov = {}
for market in ("resale", "presale"):
    merged = merge(win[market])
    continuous_through = merged[0][1] if merged else None   # 首段(歷史連續段)結束日
    official_through = max((e for _, e in win[market]), default=None)  # 官方最新已發布日
    cov[market] = {"merged": merged, "continuous_through": continuous_through, "official_through": official_through}

# ── 逐月狀態 ──
def month_range(m):
    y, mo = int(m[:4]), int(m[5:7])
    first = datetime.date(y, mo, 1)
    last = datetime.date(y + (mo == 12), (mo % 12) + 1, 1) - datetime.timedelta(days=1)
    return first, last
def covered_days(first, last, merged):
    days = 0; cur = first
    while cur <= last:
        if any(s <= cur <= e for s, e in merged): days += 1
        cur += datetime.timedelta(days=1)
    return days

def status_presale(m, c):
    first, last = month_range(m)
    total = (last - first).days + 1
    cov_d = covered_days(first, last, c["merged"])
    if cov_d >= total: return "complete"
    # 未覆蓋日切成「已缺口(官方已可得)」與「待發布」
    gap = pend = 0; cur = first
    while cur <= last:
        if not any(s <= cur <= e for s, e in c["merged"]):
            if cur <= c["official_through"]: gap += 1
            else: pend += 1
        cur += datetime.timedelta(days=1)
    if gap and pend: return "mixed"
    if gap: return "local_source_gap"
    if pend and cov_d > 0: return "official_pending"   # 已覆蓋+其餘待發布
    if pend: return "official_pending"
    return "local_source_gap"

def status_resale(m, c):
    first, last = month_range(m)
    need = last + datetime.timedelta(days=REG_BUFFER)   # 交易月完整所需的登記覆蓋日
    ct, ot = c["continuous_through"], c["official_through"]
    if need <= ct: return "complete"
    if need <= ot: return "local_source_gap"            # 官方已發布所需登記窗口、本機沒存
    if last <= ot: return "mixed"                       # 部分登記已發布、部分尚未
    if first <= ot: return "mixed"
    return "official_pending"                           # 整月登記皆尚未發布

months = []
y0, m0 = 2010, 1
cur = datetime.date(y0, m0, 1)
end = datetime.date(RETRIEVED.year, RETRIEVED.month, 1)
while cur <= end:
    months.append("%04d-%02d" % (cur.year, cur.month))
    cur = datetime.date(cur.year + (cur.month == 12), (cur.month % 12) + 1, 1)

status = {"resale": {}, "presale": {}}
for m in months:
    status["resale"][m] = status_resale(m, cov["resale"])
    status["presale"][m] = status_presale(m, cov["presale"])

result = {
    "retrieved_at": RETRIEVED.isoformat(),
    "note": "月份狀態由官方發布窗口(build_time.xml)決定，非由資料量。買賣以登記日期為基準(交易月完整需登記覆蓋到月底+30天)，預售以交易日期為基準。",
    "reg_buffer_days": REG_BUFFER,
    "markets": {
        market: {
            "basis": "registration_date" if market == "resale" else "transaction_date",
            "continuous_through": cov[market]["continuous_through"].isoformat() if cov[market]["continuous_through"] else None,
            "official_through": cov[market]["official_through"].isoformat() if cov[market]["official_through"] else None,
            "covered_windows": [[s.isoformat(), e.isoformat()] for s, e in cov[market]["merged"]],
            "month_status": status[market],
        } for market in ("resale", "presale")
    },
    "manifest": manifest,
}
json.dump(result, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))

# 摘要
for market in ("resale", "presale"):
    c = cov[market]
    print(f"【{market}】連續覆蓋至 {c['continuous_through']}、官方最新至 {c['official_through']}、窗口段數 {len(c['merged'])}")
    recent = [m for m in months if m >= "2026-01"]
    print("   近月狀態:", "　".join(f"{m}:{status[market][m]}" for m in recent))
print(f"批次 {len(set(x['source_file'] for x in manifest))} 個、manifest {len(manifest)} 筆 → {OUT}")
