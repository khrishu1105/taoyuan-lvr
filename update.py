# -*- coding: utf-8 -*-
"""自動更新：重抓最近數季內政部開放資料 → 重建 SQLite/JSON → 重產單機版。
排程每旬(1/11/21後)執行，資料永遠新。內政部當季資料每旬累積、前一兩季可能修訂，故重抓最近3季。"""
import os, sys, subprocess, urllib.request, datetime, ssl

BASE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(BASE, "raw")
os.makedirs(RAW, exist_ok=True)
URL = "https://plvr.land.moi.gov.tw/DownloadSeason?season={s}&type=zip&fileName=lvr_landcsv.zip"
LOG = os.path.join(BASE, "update.log")

def log(msg):
    line = f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line)
    with open(LOG, "a", encoding="utf-8") as f: f.write(line + "\n")

def season_of(d):
    roc = d.year - 1911
    q = (d.month - 1)//3 + 1
    return roc, q

def prev_season(roc, q):
    return (roc, q-1) if q > 1 else (roc-1, 4)

def recent_seasons(n=3):
    d = datetime.date.today()
    roc, q = season_of(d)
    out = []
    for _ in range(n):
        out.append(f"{roc}S{q}")
        roc, q = prev_season(roc, q)
    return out

def download(season):
    url = URL.format(s=season)
    dst = os.path.join(RAW, season + ".zip")
    try:
        ctx = ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
        req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=120, context=ctx) as r:
            data = r.read()
        # 內政部對尚未發布的季別可能回極小檔或錯誤頁；過小視為無效
        if len(data) < 100000:
            log(f"  {season}: 回傳過小({len(data)}B)，可能該季尚未發布，略過"); return False
        with open(dst, "wb") as f: f.write(data)
        log(f"  {season}: 已更新 {len(data)//1024} KB"); return True
    except Exception as e:
        log(f"  {season}: 下載失敗 {e}"); return False

def main():
    log("=== 開始自動更新 ===")
    seasons = recent_seasons(3)
    log(f"重抓最近季別: {seasons}")
    got = sum(download(s) for s in seasons)
    log(f"成功更新 {got}/{len(seasons)} 季")
    log("重建資料庫 (etl.py)…")
    subprocess.run([sys.executable, os.path.join(BASE, "etl.py")], check=True)
    log("重產單機版 (build_standalone.py)…")
    subprocess.run([sys.executable, os.path.join(BASE, "build_standalone.py")], check=True)
    log("=== 更新完成 ===\n")

if __name__ == "__main__":
    main()
