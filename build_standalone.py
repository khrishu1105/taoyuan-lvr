# -*- coding: utf-8 -*-
"""把 index.html + data/*.json 打包成單一自帶資料的 HTML（免伺服器/免Python，雙擊即開）。
預設「lite」：只內嵌預售屋(輕量~10MB，最適合傳給別人)。full：內嵌全品項(~40MB)。
用法：python3 build_standalone.py [lite|full]"""
import json, os, sys
BASE = os.path.dirname(os.path.abspath(__file__))
mode = sys.argv[1] if len(sys.argv) > 1 else "lite"
html = open(os.path.join(BASE, "index.html"), encoding="utf-8").read()

def j(name):
    return json.load(open(os.path.join(BASE, "data", name + ".json"), encoding="utf-8"))

# lite：預售屋完整 + 中古/土地區域統計(不含逐筆)；full：全部逐筆
files = ["meta", "presale_projects", "presale_tx", "resale_area", "land_area"]
if mode == "full":
    files += ["resale_tx", "land_tx"]

embed = {("meta" if f=="meta" else f): j(f) for f in files}
emb_js = "<script>window.__EMBED__=" + json.dumps(embed, ensure_ascii=False, separators=(",",":")) + ";" \
         + f"window.__EMBED_MODE__={json.dumps(mode)};</script>"

html = html.replace("<script>\nlet META", emb_js + "\n<script>\nlet META", 1)

out = os.path.join(BASE, f"桃園實登查詢_單機版_{mode}.html")
open(out, "w", encoding="utf-8").write(html)
print(f"OK [{mode}] ->", os.path.basename(out), f"({round(os.path.getsize(out)/1024/1024,1)} MB)")
if mode == "lite":
    print("  lite 版：預售屋完整可查；中古/土地分頁只有區域統計、逐筆需用伺服器版。")
