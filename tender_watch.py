# -*- coding: utf-8 -*-
"""
標案監看系統 tender_watch.py
================================
自動抓「政府電子採購網」指定機關(公路局、參山處等)的招標公告，
依金額/資格篩掉無法承接的案子，產生含倒數計時的 dashboard.html。

用法:
    python tender_watch.py            # 依 config.json 抓取並產生儀表板
    python tender_watch.py --demo    # 用示範資料產生儀表板(不連網，先看畫面用)

排程:
    Windows 工作排程器 或 cron 每天跑一次即可，見 README.md。

資料來源:
    主要: 政府電子採購網 web.pcc.gov.tw (依法所有機關招標公告皆刊登於此)
    引用資料時請註明出處為政府電子採購網。
"""
import argparse
import json
import re
import smtplib
import ssl
import sys
import time
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from pathlib import Path

import requests
from bs4 import BeautifulSoup


from zoneinfo import ZoneInfo

TZ_TW = ZoneInfo("Asia/Taipei")

def now_tw() -> datetime:
    """台灣現在時間(無時區標記)。伺服器在任何時區跑都以台灣時間為準。"""
    return datetime.now(TZ_TW).replace(tzinfo=None)

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
STORE_PATH = BASE_DIR / "tenders.json"      # 歷次抓到的案子(用來判斷哪些是新案)
DASHBOARD_PATH = BASE_DIR / "dashboard.html"

PCC_SEARCH_URL = "https://web.pcc.gov.tw/prkms/tender/common/basic/readTenderBasic"
PCC_BASE = "https://web.pcc.gov.tw"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0 Safari/537.36"),
    "Accept-Language": "zh-TW,zh;q=0.9",
}

# ---------------------------------------------------------------- 工具

def roc_date(d: datetime) -> str:
    """西元 datetime -> 民國日期字串 114/08/05"""
    return f"{d.year - 1911}/{d.month:02d}/{d.day:02d}"


def parse_roc_datetime(text: str):
    """把 '115/08/20 17:00' 或 '115/08/20' 這類民國日期轉成 ISO 字串，失敗回 None"""
    if not text:
        return None
    text = text.strip()
    m = re.search(r"(\d{2,3})/(\d{1,2})/(\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?", text)
    if not m:
        return None
    y, mo, d = int(m.group(1)) + 1911, int(m.group(2)), int(m.group(3))
    hh = int(m.group(4)) if m.group(4) else 17   # 沒寫時間預設 17:00 (常見截止時間)
    mm = int(m.group(5)) if m.group(5) else 0
    try:
        return datetime(y, mo, d, hh, mm).strftime("%Y-%m-%dT%H:%M")
    except ValueError:
        return None


def parse_money(text: str):
    """從文字抓出金額數字，抓不到回 None"""
    if not text:
        return None
    t = text.replace(",", "")
    m = re.search(r"(\d{4,})", t)   # 至少千元等級才當金額
    if m:
        return int(m.group(1))
    return None


def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return default


# ---------------------------------------------------------------- 抓取

def fetch_list_for_org(session: requests.Session, org_kw: str, days: int):
    """依機關名稱查詢招標公告。會依序嘗試多種查詢策略，用成功的那個。"""
    end = now_tw()
    start = end - timedelta(days=days)
    base_params = {
        "firstSearch": "true",
        "searchType": "basic",
        "isBinding": "N",
        "isLogIn": "N",
        "level_1": "on",
        "orgName": org_kw,
        "orgId": "",
        "tenderName": "",
        "tenderId": "",
        "tenderType": "TENDER_DECLARATION",
        "tenderWay": "TENDER_WAY_ALL_DECLARATION",
        "radProctrgCate": "",
        "policyAdvocacy": "",
        "pageSize": "100",
    }
    # 先逛一次查詢首頁拿 session cookie(很多政府網站沒 cookie 會查不到東西)
    try:
        session.get(PCC_BASE + "/prkms/tender/common/basic/indexTenderBasic",
                    headers=HEADERS, timeout=30)
    except Exception as e:
        print(f"    [除錯] 取得首頁cookie失敗(繼續嘗試): {e}")

    strategies = [
        ("等標期內(isSpdt)", "GET",  {**base_params, "dateType": "isSpdt"}),
        ("日期區間GET",      "GET",  {**base_params, "dateType": "isDate",
                                      "tenderStartDate": roc_date(start),
                                      "tenderEndDate": roc_date(end)}),
        ("日期區間POST",     "POST", {**base_params, "dateType": "isDate",
                                      "tenderStartDate": roc_date(start),
                                      "tenderEndDate": roc_date(end)}),
    ]
    debug_dir = BASE_DIR / "debug"
    debug_dir.mkdir(exist_ok=True)
    safe = re.sub(r"[^\w]", "_", org_kw)

    for i, (name, method, params) in enumerate(strategies, 1):
        print(f"  查詢: {org_kw} [策略{i}: {name}]")
        try:
            if method == "GET":
                r = session.get(PCC_SEARCH_URL, params=params, headers=HEADERS, timeout=30)
            else:
                r = session.post(PCC_SEARCH_URL, data=params, headers=HEADERS, timeout=30)
        except Exception as e:
            print(f"    [除錯] 連線失敗: {e}")
            continue
        print(f"    [除錯] HTTP={r.status_code} 長度={len(r.text)}")
        (debug_dir / f"list_{safe}_策略{i}.html").write_text(
            r.text, encoding="utf-8", errors="ignore")
        m = re.search(r"共有\s*(\d+)\s*筆", r.text)
        if m:
            print(f"    [除錯] 網站回報共 {m.group(1)} 筆")
        if r.status_code != 200:
            continue
        items = parse_list_html(r.text, org_kw)
        if items:
            print(f"    -> 策略{i}成功，解析到 {len(items)} 筆")
            if len(items) >= 95:  # 接近每頁100筆上限 -> 可能有下一頁
                seen = {it["url"] for it in items}
                for pg in range(2, 6):
                    pp = dict(params); pp["d-49738-p"] = str(pg)
                    try:
                        r2 = (session.get(PCC_SEARCH_URL, params=pp, headers=HEADERS, timeout=30)
                              if method == "GET" else
                              session.post(PCC_SEARCH_URL, data=pp, headers=HEADERS, timeout=30))
                    except Exception:
                        break
                    more = [x for x in parse_list_html(r2.text, org_kw) if x["url"] not in seen]
                    if not more:
                        break
                    print(f"    -> 第{pg}頁 +{len(more)} 筆")
                    items += more
                    seen |= {x["url"] for x in more}
                    time.sleep(1.5)
            return items
    print(f"    -> 所有策略都是 0 筆")
    return []


def cell_text(td):
    """取表格儲存格文字。政府網站把部分文字藏在 pageCode2Img("...") 的JS裡，優先從那裡取。"""
    m = re.search(r'pageCode2Img\("([^"]*)"', str(td))
    if m and m.group(1).strip():
        return m.group(1).strip()
    return td.get_text(" ", strip=True)


def parse_list_html(html: str, org_kw: str):
    """解析查詢結果列表頁(id=tpam 的表格)。
    欄位順序: 0項次 1機關名稱 2案號+標案名稱 3傳輸次數 4招標方式 5採購性質 6公告日期 7截止投標 8預算金額 9功能選項
    政府網站改版時最可能要調整的就是這個函式。"""
    soup = BeautifulSoup(html, "lxml")
    items = []
    table = soup.find(id="tpam") or soup.find("table", class_=re.compile("tb_"))
    if table is None:
        print("    [除錯] 找不到結果表格(id=tpam)")
        return items
    for tr in table.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 9:
            continue  # 表頭或提示列
        cell = [td.get_text(" ", strip=True) for td in tds]
        if not re.search(r"\d{2,3}/\d{1,2}/\d{1,2}", " ".join(cell)):
            continue
        c2 = tds[2]
        # 標案名稱藏在 pageCode2Img("...") 裡; 案號是 <br> 前的純文字
        m = re.search(r'pageCode2Img\("([^"]*)"', str(c2))
        title = m.group(1).strip() if m else ""
        if not title:
            title = cell[2]
        first_text = c2.find(string=True)
        case_no = first_text.strip() if first_text else ""
        a = c2.find("a", href=True) or tr.find("a", href=True)
        url = ""
        if a:
            href = a["href"]
            url = href if href.startswith("http") else PCC_BASE + href
        item = {
            "org": cell[1] or org_kw,
            "title": title,
            "case_no": case_no,
            "method": cell[4],
            "category": cell[5],
            "publish_date": parse_roc_datetime(cell[6]),
            "deadline": parse_roc_datetime(cell[7]),
            "open_date": None,
            "budget": parse_money(cell[8]) if re.search(r"\d", cell[8]) else None,
            "qualification": "",
            "url": url,
            "source": "政府電子採購網",
        }
        if item["title"]:
            items.append(item)
    print(f"    -> 解析到 {len(items)} 筆")
    return items


DETAIL_FIELD_MAP = {
    "deadline": ["截止投標"],
    "open_date": ["開標時間", "開標日期"],
    "budget_text": ["預算金額", "預算或費用"],
    "qualification": ["廠商資格摘要", "投標廠商資格", "訂有底價"],
}


def fetch_detail(session: requests.Session, item: dict):
    """進標案詳細頁補齊 開標時間/預算/廠商資格。失敗不影響主流程。"""
    if not item.get("url"):
        return
    try:
        r = session.get(item["url"], headers=HEADERS, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        # 詳細頁多為 <th>欄名</th><td>值</td> 或 兩欄 table
        pairs = {}
        for tr in soup.find_all("tr"):
            cells = tr.find_all(["th", "td"])
            if len(cells) >= 2:
                k = cell_text(cells[0])
                v = cell_text(cells[1])
                if k:
                    pairs[k] = v
        def find(keys):
            for pk, pv in pairs.items():
                for key in keys:
                    if key in pk:
                        return pv
            return None
        v = find(DETAIL_FIELD_MAP["deadline"])
        if v:
            item["deadline"] = parse_roc_datetime(v) or item["deadline"]
        v = find(DETAIL_FIELD_MAP["open_date"])
        if v:
            item["open_date"] = parse_roc_datetime(v)
        v = find(DETAIL_FIELD_MAP["budget_text"])
        if v:
            item["budget"] = parse_money(v) or item["budget"]
        v = find(DETAIL_FIELD_MAP["qualification"])
        if v:
            item["qualification"] = v[:500]
    except Exception as e:
        print(f"    (詳細頁抓取失敗，僅用列表資料: {item['title'][:20]}... {e})")



# ---------------------------------------------------------------- 案場定位

# 地標/鄉鎮 (從標案名稱比對, 長詞優先)
GEO_LANDMARKS = {
    "梨山": (24.253, 121.253), "谷關": (24.202, 121.011), "德基": (24.255, 121.170),
    "東澳": (24.517, 121.832), "南澳": (24.465, 121.800), "蘇澳": (24.594, 121.851),
    "頭城": (24.857, 121.823), "五結": (24.685, 121.796), "礁溪": (24.827, 121.773),
    "羅東": (24.677, 121.767), "冬山": (24.634, 121.792), "大同鄉": (24.590, 121.520),
    "南庄": (24.596, 120.995), "獅頭山": (24.605, 120.985), "獅潭": (24.539, 120.918),
    "三義": (24.413, 120.772), "卓蘭": (24.313, 120.823), "大湖": (24.422, 120.865),
    "八卦山": (24.078, 120.565), "大佛": (24.078, 120.565), "松柏嶺": (23.888, 120.617),
    "埔里": (23.966, 120.964), "霧社": (24.023, 121.133), "日月潭": (23.857, 120.916),
    "清境": (24.058, 121.163), "合歡山": (24.143, 121.272), "武嶺": (24.136, 121.276),
    "阿里山": (23.510, 120.803), "塔塔加": (23.489, 120.887), "溪頭": (23.672, 120.796),
    "太魯閣": (24.158, 121.622), "天祥": (24.184, 121.494), "玉里": (23.336, 121.312),
    "池上": (23.124, 121.215), "知本": (22.696, 121.062), "恆春": (22.001, 120.744),
    "墾丁": (21.946, 120.798), "楓港": (22.190, 120.700), "壽卡": (22.211, 120.856),
    "梧棲": (24.254, 120.531), "清水": (24.268, 120.573), "通霄": (24.489, 120.677),
    "苑裡": (24.441, 120.652), "竹南": (24.686, 120.880), "後龍": (24.616, 120.786),
    "峨眉": (24.686, 121.015), "北埔": (24.700, 121.053), "竹東": (24.733, 121.087),
    "尖石": (24.704, 121.198), "橫山": (24.720, 121.116), "寶山": (24.760, 120.855),
    "大溪": (24.881, 121.287), "復興區": (24.820, 121.352), "拉拉山": (24.708, 121.427),
    "坪林": (24.937, 121.711), "石碇": (24.991, 121.658), "瑞芳": (25.109, 121.810),
    "貢寮": (25.022, 121.909), "福隆": (25.016, 121.944), "金山": (25.222, 121.638),
    "淡水": (25.170, 121.440), "三峽": (24.934, 121.369), "烏來": (24.865, 121.550),
    "和平區": (24.250, 121.100), "梨山賓館": (24.254, 121.254),
    # 主要河川(河川分署案名常見, 取流域概略中點)
    "蘭陽溪": (24.65, 121.65), "淡水河": (25.05, 121.45), "基隆河": (25.08, 121.65),
    "大漢溪": (24.90, 121.30), "新店溪": (24.95, 121.53), "景美溪": (24.98, 121.57),
    "頭前溪": (24.78, 121.05), "鳳山溪": (24.85, 121.10), "中港溪": (24.62, 120.90),
    "後龍溪": (24.58, 120.85), "大安溪": (24.35, 120.80), "大甲溪": (24.22, 120.90),
    "烏溪": (24.03, 120.75), "濁水溪": (23.80, 120.65), "清水溪": (23.70, 120.70),
    "北港溪": (23.60, 120.35), "朴子溪": (23.47, 120.30), "八掌溪": (23.42, 120.35),
    "急水溪": (23.30, 120.30), "曾文溪": (23.10, 120.35), "鹽水溪": (23.03, 120.22),
    "二仁溪": (22.90, 120.25), "阿公店溪": (22.80, 120.28), "高屏溪": (22.65, 120.43),
    "荖濃溪": (22.95, 120.65), "旗山溪": (22.90, 120.50), "東港溪": (22.50, 120.50),
    "林邊溪": (22.43, 120.55), "四重溪": (22.08, 120.75), "卑南溪": (22.80, 121.10),
    "秀姑巒溪": (23.40, 121.35), "花蓮溪": (23.85, 121.55), "和平溪": (24.30, 121.75),
    "立霧溪": (24.17, 121.60), "木瓜溪": (23.93, 121.50),
    # ── 臺中市 ──
    "烏日": (24.104, 120.623), "大里": (24.099, 120.678), "太平": (24.126, 120.718),
    "霧峰": (24.061, 120.700), "大甲": (24.349, 120.622), "沙鹿": (24.234, 120.566),
    "梧棲": (24.255, 120.531), "清水": (24.268, 120.560), "大肚": (24.154, 120.541),
    "龍井": (24.192, 120.545), "神岡": (24.257, 120.662), "大雅": (24.229, 120.647),
    "潭子": (24.209, 120.705), "豐原": (24.242, 120.723), "后里": (24.309, 120.711),
    "東勢": (24.258, 120.827), "石岡": (24.275, 120.780), "新社": (24.234, 120.809),
    "和平": (24.220, 121.010), "外埔": (24.332, 120.654), "大安": (24.348, 120.586),
    "太平區": (24.126, 120.718), "北屯": (24.182, 120.686), "西屯": (24.181, 120.616),
    "南屯": (24.138, 120.643),
    # ── 彰化縣 ──
    "彰化市": (24.075, 120.544), "員林": (23.958, 120.574), "和美": (24.111, 120.500),
    "鹿港": (24.057, 120.435), "溪湖": (23.962, 120.480), "田中": (23.861, 120.581),
    "北斗": (23.871, 120.521), "二林": (23.899, 120.374), "線西": (24.128, 120.462),
    "伸港": (24.148, 120.481), "福興": (24.052, 120.430), "秀水": (24.036, 120.505),
    "花壇": (24.024, 120.539), "芬園": (24.014, 120.628), "大村": (23.993, 120.548),
    "埔心": (23.953, 120.539), "永靖": (23.925, 120.546), "社頭": (23.897, 120.583),
    "二水": (23.809, 120.619), "田尾": (23.891, 120.524), "埤頭": (23.892, 120.462),
    "溪州": (23.851, 120.499), "竹塘": (23.867, 120.427), "大城": (23.853, 120.320),
    "芳苑": (23.924, 120.322), "埔鹽": (24.001, 120.463), "彰濱": (24.100, 120.420),
    # ── 苗栗縣 ──
    "苗栗市": (24.560, 120.821), "頭份": (24.688, 120.895), "竹南": (24.686, 120.880),
    "後龍": (24.616, 120.786), "通霄": (24.489, 120.677), "苑裡": (24.441, 120.652),
    "銅鑼": (24.489, 120.788), "三義": (24.413, 120.772), "西湖": (24.545, 120.752),
    "造橋": (24.629, 120.859), "頭屋": (24.578, 120.851), "公館": (24.500, 120.822),
    "大湖": (24.422, 120.865), "泰安": (24.427, 120.905), "卓蘭": (24.313, 120.823),
    "獅潭": (24.539, 120.918), "南庄": (24.596, 120.995), "三灣": (24.653, 120.951),
    # ── 南投縣 ──
    "南投市": (23.910, 120.688), "草屯": (23.973, 120.680), "埔里": (23.966, 120.964),
    "竹山": (23.757, 120.681), "集集": (23.829, 120.785), "名間": (23.838, 120.702),
    "鹿谷": (23.744, 120.751), "中寮": (23.879, 120.766), "水里": (23.812, 120.855),
    "魚池": (23.896, 120.937), "國姓": (24.042, 120.858), "信義": (23.702, 120.857),
    "仁愛": (24.023, 121.133), "日月潭": (23.857, 120.916),
}

GEO_COUNTIES = {
    "基隆": (25.128, 121.742), "臺北": (25.038, 121.564), "台北": (25.038, 121.564),
    "新北": (24.990, 121.470), "桃園": (24.994, 121.301), "新竹": (24.804, 121.010),
    "苗栗": (24.561, 120.821), "臺中": (24.147, 120.674), "台中": (24.147, 120.674),
    "彰化": (24.052, 120.516), "南投": (23.910, 120.850), "雲林": (23.709, 120.431),
    "嘉義": (23.480, 120.449), "臺南": (23.000, 120.227), "台南": (23.000, 120.227),
    "高雄": (22.627, 120.302), "屏東": (22.552, 120.549), "宜蘭": (24.702, 121.738),
    "花蓮": (23.976, 121.605), "臺東": (22.758, 121.144), "台東": (22.758, 121.144),
    "澎湖": (23.571, 119.579), "金門": (24.437, 118.318), "連江": (26.160, 119.951),
    "馬祖": (26.160, 119.951),
}

# 台N線 概略中點 (公路局案名常見)
GEO_HIGHWAYS = {
    "1": (23.90, 120.45), "2": (25.10, 121.80), "3": (24.35, 120.85),
    "7": (24.68, 121.45), "8": (24.20, 121.15), "9": (24.30, 121.75),
    "11": (23.50, 121.45), "13": (24.52, 120.79), "14": (23.99, 121.05),
    "16": (23.83, 120.85), "17": (23.60, 120.25), "18": (23.48, 120.70),
    "19": (23.75, 120.35), "20": (23.15, 120.85), "21": (23.60, 120.90),
    "24": (22.72, 120.65), "26": (21.99, 120.80), "61": (24.40, 120.65),
    "62": (25.09, 121.77), "63": (23.98, 120.69), "64": (25.05, 121.42),
    "66": (24.93, 121.20), "74": (24.16, 120.72), "78": (23.72, 120.35),
    "82": (23.45, 120.30), "84": (23.12, 120.35), "86": (22.94, 120.25),
    "88": (22.60, 120.45),
}


GEO_FREEWAYS = {
    "1": (24.50, 120.90), "2": (25.05, 121.22), "3": (24.40, 120.95),
    "4": (24.25, 120.62), "5": (24.85, 121.75), "6": (23.97, 120.85),
    "8": (23.05, 120.25), "10": (22.73, 120.38),
}

# 機關所在(找不到案場時的約略退路)
GEO_AGENCIES = [
    ("第一河川", (24.702, 121.738), "宜蘭(機關)"),
    ("第二河川", (24.804, 121.010), "新竹(機關)"),
    ("第三河川", (24.147, 120.674), "臺中(機關)"),
    ("第四河川", (24.052, 120.516), "彰化(機關)"),
    ("第五河川", (23.480, 120.449), "嘉義(機關)"),
    ("第六河川", (22.800, 120.298), "高雄岡山(機關)"),
    ("第七河川", (22.552, 120.542), "屏東潮州(機關)"),
    ("第八河川", (22.758, 121.144), "臺東(機關)"),
    ("第九河川", (23.976, 121.605), "花蓮(機關)"),
    ("第十河川", (25.036, 121.450), "新北(機關)"),
    ("北區水資源", (24.865, 121.211), "桃園龍潭(機關)"),
    ("中區水資源", (24.147, 120.674), "臺中(機關)"),
    ("南區水資源", (23.200, 120.550), "臺南楠西(機關)"),
    ("水利署", (24.062, 120.700), "臺中霧峰(機關)"),
    ("臺中市", (24.147, 120.674), "臺中市(機關)"),
    ("彰化縣", (24.075, 120.544), "彰化縣(機關)"),
    ("苗栗縣", (24.560, 120.821), "苗栗縣(機關)"),
    ("南投縣", (23.910, 120.688), "南投縣(機關)"),
    ("蘇花公路", (24.594, 121.851), "宜蘭蘇澳(機關)"),
    ("北區養護", (25.080, 121.520), "臺北(機關)"),
    ("中區養護", (24.150, 120.680), "臺中(機關)"),
    ("南區養護", (22.630, 120.320), "高雄(機關)"),
    ("東區養護", (23.980, 121.600), "花蓮(機關)"),
    ("參山",     (24.539, 120.918), "苗栗獅潭(機關)"),
    ("高速公路", (24.950, 121.200), "泰山(機關)"),
    ("公路局",   (25.040, 121.520), "臺北(機關)"),
]


def locate(item):
    """依標案名稱/機關推定案場座標。site=從案名定位, org=退回機關所在(約略)。"""
    text = f"{item.get('title','')} {item.get('org','')}"
    for name in sorted(GEO_LANDMARKS, key=len, reverse=True):
        if name in item.get("title", ""):
            lat, lng = GEO_LANDMARKS[name]
            item.update(lat=lat, lng=lng, loc=name, loc_src="site")
            return
    for name, (lat, lng) in GEO_COUNTIES.items():
        if name in item.get("title", ""):
            item.update(lat=lat, lng=lng, loc=name, loc_src="site")
            return
    m = re.search(r"台(\d{1,2})線", item.get("title", ""))
    if m and m.group(1) in GEO_HIGHWAYS:
        lat, lng = GEO_HIGHWAYS[m.group(1)]
        item.update(lat=lat, lng=lng, loc=f"台{m.group(1)}線沿線", loc_src="site")
        return
    m = re.search(r"國道\s*(\d{1,2})\s*號", item.get("title", ""))
    if m and m.group(1) in GEO_FREEWAYS:
        lat, lng = GEO_FREEWAYS[m.group(1)]
        item.update(lat=lat, lng=lng, loc=f"國道{m.group(1)}號沿線", loc_src="site")
        return
    for key, (lat, lng), label in GEO_AGENCIES:
        if key in text:
            item.update(lat=lat, lng=lng, loc=label, loc_src="org")
            return
    item.update(lat=None, lng=None, loc="", loc_src="none")


def region_of(lat, lng):
    """依座標粗分台灣四區(示意用): 宜蘭歸北部、雲林歸中部、嘉義歸南部、花東歸東部。"""
    if lat is None:
        return ""
    if (lng >= 121.35 and lat <= 24.35) or (lng >= 121.0 and lat <= 23.35):
        return "東部"
    if lat >= 24.62 or lng >= 121.3:
        return "北部"
    if lat > 23.55:
        return "中部"
    return "南部"


def jitter(items):
    """同座標的案子稍微錯開，避免地圖標記完全疊在一起。"""
    from collections import defaultdict
    groups = defaultdict(list)
    for it in items:
        if it.get("lat") is not None:
            groups[(it["lat"], it["lng"])].append(it)
    for (lat, lng), grp in groups.items():
        if len(grp) > 1:
            import math
            for i, it in enumerate(grp[1:], 1):
                ang = i * 2.399963  # 黃金角, 讓點呈螺旋散開
                r = 0.012 * math.sqrt(i)
                it["lat"] = round(lat + r * math.cos(ang), 5)
                it["lng"] = round(lng + r * math.sin(ang), 5)


# ---------------------------------------------------------------- 篩選

def apply_filters(items, cfg):
    f = cfg.get("篩選", {})
    cap = f.get("預算金額上限", 0) or 0
    bad_qual = f.get("排除資格關鍵字", [])
    bad_title = f.get("標題排除關鍵字", [])
    need_title = f.get("標題必含關鍵字", [])
    kept, dropped = [], []
    for it in items:
        reason = None
        text_all = f"{it.get('title','')} {it.get('qualification','')}"
        if cap and it.get("budget") and it["budget"] > cap:
            reason = f"預算 {it['budget']:,} 超過上限 {cap:,}"
        elif any(k in text_all for k in bad_qual):
            reason = "資格要求含排除關鍵字(甲等綜合營造等)"
        elif any(k and k in it.get("title", "") for k in bad_title):
            reason = "標題含排除關鍵字"
        elif need_title and not any(k in it.get("title", "") for k in need_title):
            reason = "標題不含必含關鍵字"
        if reason:
            it["_dropped"] = reason
            dropped.append(it)
        else:
            kept.append(it)
    return kept, dropped


# ---------------------------------------------------------------- 儀表板

DASHBOARD_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>標案戰情室</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@500;700&family=Noto+Sans+TC:wght@400;500;700;900&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>
  :root{
    /* 美安品牌色 */
    --navy:#0C1E3C;      /* 主色 深藍 */
    --gold:#C9A45C;      /* 輔色 金 */
    --gold-d:#9C7A32;    /* 金(深, 白底文字用) */
    --grayblue:#55637A;  /* 內文 灰藍 */
    --bg:#F7F8FA;        /* 底色 淺灰 */
    --panel:#FFFFFF; --line:#E2E6EE; --line2:#CBD3E0;
    --hot:#D6455A; --warn:#E08A2E; --safe:#2F9E6C; --done:#9AA4B2;
    --mono:"IBM Plex Mono",monospace;
    font-family:"Noto Sans TC","Microsoft JhengHei",system-ui,sans-serif;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  html,body{height:100%}
  body{background:var(--bg);color:var(--navy);display:flex;flex-direction:column;overflow:hidden}

  /* ── 頁首(品牌深藍) ── */
  header{display:flex;align-items:center;gap:18px;padding:12px 18px;
         background:var(--navy);color:#fff;border-bottom:3px solid var(--gold)}
  .brand{display:flex;align-items:center;gap:11px}
  .shield{width:34px;height:38px;background:var(--gold);color:var(--navy);
          clip-path:polygon(50% 0,100% 18%,100% 72%,50% 100%,0 72%,0 18%);
          display:flex;align-items:center;justify-content:center;font:700 15px var(--mono)}
  h1{font-size:17px;font-weight:900;letter-spacing:.14em}
  h1 small{display:block;font:500 10px var(--mono);color:var(--gold);letter-spacing:.24em}
  .stats{margin-left:auto;display:flex;gap:24px;text-align:right}
  .stat b{display:block;font:700 20px var(--mono);line-height:1;color:#fff}
  .stat span{font-size:11px;color:#B9C3D6}
  .stat.hot b{color:#FF8B9A}
  .updated{font:500 12px var(--mono);color:var(--gold)}

  main{flex:1;display:grid;grid-template-columns:470px 1fr;min-height:0}
  aside{display:flex;flex-direction:column;min-height:0;border-right:1px solid var(--line2);
        background:var(--bg)}
  #map{min-height:0}

  /* ── 篩選 ── */
  .filters{padding:12px 14px;display:flex;flex-direction:column;gap:9px;
           background:var(--panel);border-bottom:1px solid var(--line2)}
  input[type=search]{background:var(--bg);border:1px solid var(--line2);color:var(--navy);
        padding:8px 12px;border-radius:6px;font-size:13px;width:100%}
  input[type=search]::placeholder{color:#9AA4B2}
  input[type=search]:focus{outline:2px solid var(--gold);outline-offset:0;border-color:var(--gold)}
  .chiprow{display:flex;flex-wrap:wrap;gap:6px}
  .chiprow .lab{font-size:11px;color:var(--grayblue);align-self:center;margin-right:2px;
                font-family:var(--mono);letter-spacing:.1em}
  .chip{border:1px solid var(--line2);background:#fff;color:var(--grayblue);
        padding:4px 11px;border-radius:4px;font-size:12px;cursor:pointer;user-select:none;
        transition:all .12s}
  .chip:hover{border-color:var(--gold);color:var(--navy)}
  .chip.on{color:var(--navy);background:var(--gold);border-color:var(--gold);font-weight:700}
  .chip .n{font-family:var(--mono);font-size:10px;margin-left:5px;opacity:.85}
  .chip .x{font-style:normal;margin-left:6px;opacity:.7;padding:0 3px}
  .chip.partial{background:#fff;border-color:var(--gold);color:var(--gold-d);font-weight:700}
  .subrow{background:var(--bg);border:1px dashed var(--line2);border-radius:6px;
          padding:6px 8px;margin-left:26px}

  /* ── 清單 ── */
  #list{flex:1;overflow-y:auto;padding:10px 12px;display:flex;flex-direction:column;gap:8px;
        scrollbar-width:thin;scrollbar-color:var(--line2) transparent}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:8px;
        padding:11px 13px 11px 16px;position:relative;cursor:pointer;
        display:grid;grid-template-columns:1fr auto;gap:4px 12px;transition:all .12s;
        box-shadow:0 1px 2px rgba(12,30,60,.05)}
  .card::before{content:"";position:absolute;left:0;top:0;bottom:0;width:5px;
        border-radius:8px 0 0 8px;background:var(--safe)}
  .card.hot::before{background:var(--hot)} .card.warn::before{background:var(--warn)}
  .card.done::before{background:var(--done)}
  .card.done{opacity:.55}
  .card:hover,.card.active{border-color:var(--gold);box-shadow:0 2px 8px rgba(12,30,60,.10)}
  .card.active{box-shadow:0 0 0 2px var(--gold)}
  .caseno{font:700 11px var(--mono);color:var(--gold-d);letter-spacing:.05em}
  .rg{display:inline-block;font-size:10px;padding:1px 6px;border-radius:3px;
      background:var(--navy);color:#fff;margin-left:6px;vertical-align:1px}
  .nb{display:inline-block;font:700 10px var(--mono);padding:1px 6px;border-radius:3px;
      background:var(--gold);color:var(--navy);margin-left:6px;vertical-align:1px;
      letter-spacing:.05em}
  .t{grid-column:1;font-size:14px;font-weight:500;line-height:1.5;color:var(--navy)}
  .meta{grid-column:1/-1;display:flex;flex-wrap:wrap;gap:3px 14px;
        font-size:11.5px;color:var(--grayblue);line-height:1.8}
  .meta .loc{color:var(--safe);font-weight:500}
  .meta .loc.approx{color:var(--grayblue);font-weight:400}
  .meta b{color:var(--navy);font-weight:500;font-family:var(--mono)}
  .cd{grid-row:1/3;grid-column:2;text-align:right;align-self:start}
  .cd .num{font:700 17px var(--mono);white-space:nowrap}
  .cd .lab{font-size:10px;color:var(--grayblue)}
  .hot .cd .num{color:var(--hot)} .warn .cd .num{color:var(--warn)}
  .safe .cd .num{color:var(--safe)} .done .cd .num{color:var(--done)}
  .empty{color:var(--grayblue);text-align:center;padding:50px 0;font-size:14px}
  .listfoot{padding:8px 14px;border-top:1px solid var(--line2);font-size:11px;
            color:var(--grayblue);background:var(--panel)}

  /* ── 地圖 ── */
  .leaflet-container{background:#EAEEF4;font-family:inherit}
  .leaflet-popup-content-wrapper{background:#fff;color:var(--navy);
        border:1px solid var(--gold);border-radius:8px}
  .leaflet-popup-tip{background:var(--gold)}
  .leaflet-popup-content{margin:10px 14px;font-size:12.5px;line-height:1.7}
  .leaflet-popup-content .pt{font-weight:700;font-size:13px}
  .leaflet-popup-content .pc{font:700 11px var(--mono);color:var(--gold-d)}
  .leaflet-popup-content a{color:var(--gold-d);font-weight:700}
  .rglabel{font:900 13px "Noto Sans TC";color:var(--navy);letter-spacing:.3em;
        text-shadow:0 0 6px #fff,0 0 6px #fff;white-space:nowrap;opacity:.75}
  .legend{position:absolute;right:14px;bottom:24px;z-index:900;background:#fff;
        border:1px solid var(--line2);border-radius:8px;padding:10px 14px;font-size:11.5px;
        color:var(--grayblue);line-height:2;box-shadow:0 2px 8px rgba(12,30,60,.10)}
  .legend i{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:7px}
  .legend .o{background:transparent;border:2px dashed var(--grayblue)}

  @media(max-width:900px){
    body{overflow:auto}
    main{grid-template-columns:1fr;grid-template-rows:45vh auto}
    #map{grid-row:1;height:45vh}
    aside{grid-row:2;border-right:none}
    #list{max-height:60vh}
    .stats{display:none}
  }
</style>
</head>
<body>
<header>
  <div class="brand">
    <div class="shield">案</div>
    <h1>標案戰情室<small>TENDER WATCH · TAIWAN</small></h1>
  </div>
  <div class="stats">
    <div class="stat"><b id="stOpen">–</b><span>可投件數</span></div>
    <div class="stat hot"><b id="stHot">–</b><span>3天內截止</span></div>
    <div class="stat"><b class="updated">__UPDATED__</b><span>資料更新</span></div>
  </div>
</header>
<main>
  <aside>
    <div class="filters">
      <input type="search" id="q" placeholder="搜尋標案名稱、案號、地點…">
      <div class="chiprow" id="sysChips"><span class="lab">機關</span></div>
      <div class="chiprow subrow" id="subChips" style="display:none"></div>
      <div class="chiprow" id="rgChips"><span class="lab">區域</span>
        <span class="chip on" data-v="all">全部</span>
        <span class="chip" data-v="北部">北部</span>
        <span class="chip" data-v="中部">中部</span>
        <span class="chip" data-v="南部">南部</span>
        <span class="chip" data-v="東部">東部</span>
      </div>
      <div class="chiprow" id="stChips"><span class="lab">狀態</span>
        <span class="chip on" data-v="all">全部</span>
        <span class="chip" data-v="new">本週新公告</span>
        <span class="chip" data-v="open">投標中</span>
        <span class="chip" data-v="hot">3天內</span>
        <span class="chip" data-v="done">已截止</span>
      </div>
      <div class="chiprow" id="catChips"><span class="lab">性質</span>
        <span class="chip on" data-v="all">全部</span>
        <span class="chip" data-v="工程">工程</span>
        <span class="chip" data-v="財物">財物</span>
        <span class="chip" data-v="勞務">勞務</span>
      </div>
      <div class="chiprow" id="bgChips"><span class="lab">預算</span>
        <span class="chip on" data-v="all">全部</span>
        <span class="chip" data-v="s">&lt;500萬</span>
        <span class="chip" data-v="m">500萬–2,700萬</span>
        <span class="chip" data-v="l">2,700萬–9,000萬</span>
        <span class="chip" data-v="na">未公告</span>
      </div>
      <div class="chiprow" id="sortChips"><span class="lab">排序</span>
        <span class="chip on" data-v="urgent">最急先</span>
        <span class="chip" data-v="relaxed">最寬鬆先</span>
        <span class="chip" data-v="budget_hi">預算高→低</span>
        <span class="chip" data-v="budget_lo">預算低→高</span>
        <span class="chip" data-v="newest">最新公告</span>
      </div>
    </div>
    <div id="list"></div>
    <div class="listfoot" id="foot"></div>
  </aside>
  <div id="map"></div>
</main>
<div class="legend">
  <i style="background:var(--hot)"></i>3天內截止<br>
  <i style="background:var(--warn)"></i>7天內截止<br>
  <i style="background:var(--safe)"></i>時間充裕<br>
  <i class="o"></i>約略位置(依機關)<br>
  <span style="color:#9AA4B2">─ ─ 分區界線為概略示意</span>
</div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const DATA = __DATA__;
const listEl = document.getElementById('list');

function stateOf(d, now){
  if(!d.deadline) return {cls:'safe', ms:Infinity};
  const ms = new Date(d.deadline) - now;
  if(ms<=0) return {cls:'done', ms};
  return {cls: ms<3*864e5?'hot': ms<7*864e5?'warn':'safe', ms};
}
function cdText(d, now){
  if(!d.deadline) return '—';
  const ms = new Date(d.deadline)-now;
  if(ms<=0) return '已截止';
  const dd=Math.floor(ms/864e5), h=Math.floor(ms%864e5/36e5),
        m=Math.floor(ms%36e5/6e4), s=Math.floor(ms%6e4/1e3);
  return dd>0? dd+' 天 '+h+' 時' : String(h).padStart(2,'0')+':'+String(m).padStart(2,'0')+':'+String(s).padStart(2,'0');
}
function fmtD(x){ if(!x) return '—'; const d=new Date(x);
  return (d.getMonth()+1)+'/'+d.getDate()+' '+String(d.getHours()).padStart(2,'0')+':'+String(d.getMinutes()).padStart(2,'0');}
function money(n){ if(!n) return '未公告';
  if(n>=1e8) return (n/1e8).toFixed(2).replace(/\.?0+$/,'')+'億';
  if(n>=1e4) return Math.round(n/1e4).toLocaleString()+'萬';
  return n.toLocaleString();}
function daysSincePub(d, now){
  if(!d.publish_date) return 999;
  return (now - new Date(d.publish_date)) / 864e5;
}
function orgShort(o){return o.replace('交通部觀光署','').replace('交通部','').replace('經濟部','').replace('國家風景區','').replace('政府',''); }

let F={q:'', orgs:new Set(), rg:'all', st:'all', cat:'all', bg:'all', sort:'urgent'};
function chipify(elId, key){
  document.getElementById(elId).addEventListener('click', e=>{
    const c=e.target.closest('.chip'); if(!c) return;
    [...c.parentElement.querySelectorAll('.chip')].forEach(x=>x.classList.remove('on'));
    c.classList.add('on'); F[key]=c.dataset.v; render();
  });
}
chipify('rgChips','rg'); chipify('stChips','st'); chipify('catChips','cat'); chipify('bgChips','bg'); chipify('sortChips','sort');
document.getElementById('q').addEventListener('input', e=>{F.q=e.target.value.trim(); render();});

/* ── 機關兩層式篩選: 母系統(帶件數) -> ▾ 展開分支 ── */
const SYS_ORDER=['公路局','水利署','高速公路局','參山處','地方縣市','其他'];
function sysOf(o){
  if(o.includes('水利署'))return'水利署';
  if(o.includes('高速公路'))return'高速公路局';
  if(o.includes('參山'))return'參山處';
  if(o.includes('公路'))return'公路局';
  if(/臺中|台中|彰化|苗栗|南投/.test(o))return'地方縣市';
  return'其他';
}
function branchName(o){
  const n=o.replace('經濟部水利署','').replace('交通部高速公路局','')
           .replace('交通部公路局','').replace('交通部觀光署','').replace('國家風景區','').replace('政府','');
  return n||'署(局)本部';
}
const SYS={};
[...new Set(DATA.map(d=>d.org))].forEach(o=>{
  F.orgs.add(o);
  const s=sysOf(o); (SYS[s]=SYS[s]||[]).push(o);
});
const CNT={}; DATA.forEach(d=>{const s=sysOf(d.org); CNT[s]=(CNT[s]||0)+1;});
const sysBox=document.getElementById('sysChips'), subBox=document.getElementById('subChips');
let expanded=null;
SYS_ORDER.filter(s=>SYS[s]).forEach(s=>{
  const c=document.createElement('span'); c.className='chip on sys'; c.dataset.sys=s;
  c.innerHTML=s+' <b class="n">'+(CNT[s]||0)+'</b>'+(SYS[s].length>1?'<i class="x">▾</i>':'');
  sysBox.appendChild(c);
});
function sysState(s){
  const on=SYS[s].filter(o=>F.orgs.has(o)).length;
  return on===0?'off': on===SYS[s].length?'on':'partial';
}
function refreshChips(){
  sysBox.querySelectorAll('.sys').forEach(c=>{
    const st=sysState(c.dataset.sys);
    c.classList.toggle('on',st==='on'); c.classList.toggle('partial',st==='partial');
  });
  if(expanded){
    subBox.style.display='flex'; subBox.innerHTML='';
    SYS[expanded].forEach(o=>{
      const c=document.createElement('span');
      c.className='chip'+(F.orgs.has(o)?' on':''); c.dataset.org=o;
      c.textContent=branchName(o);
      subBox.appendChild(c);
    });
  } else subBox.style.display='none';
}
sysBox.addEventListener('click',e=>{
  const c=e.target.closest('.sys'); if(!c) return;
  const s=c.dataset.sys;
  if(e.target.closest('.x')){ expanded = expanded===s? null : s; refreshChips(); return; }
  if(sysState(s)==='on'){
    if([...F.orgs].filter(o=>sysOf(o)!==s).length===0) return; /* 至少留一個系統 */
    SYS[s].forEach(o=>F.orgs.delete(o));
  } else {
    SYS[s].forEach(o=>F.orgs.add(o));
  }
  refreshChips(); render();
});
subBox.addEventListener('click',e=>{
  const c=e.target.closest('.chip'); if(!c) return;
  const o=c.dataset.org;
  if(F.orgs.has(o)){ if(F.orgs.size>1) F.orgs.delete(o); }
  else F.orgs.add(o);
  refreshChips(); render();
});
refreshChips();

function passes(d, st){
  if(!F.orgs.has(d.org)) return false;
  if(F.q && !((d.title||'')+(d.case_no||'')+(d.loc||'')).includes(F.q)) return false;
  if(F.rg!=='all' && d.region!==F.rg) return false;
  if(F.st==='new'&&daysSincePub(d,new Date())>7) return false;
  if(F.st==='open'&&st.cls==='done') return false;
  if(F.st==='hot'&&st.cls!=='hot') return false;
  if(F.st==='done'&&st.cls!=='done') return false;
  if(F.cat!=='all'&&!(d.category||'').includes(F.cat)) return false;
  if(F.bg!=='all'){
    const b=d.budget;
    if(F.bg==='na'&&b) return false;
    if(F.bg==='s'&&!(b&&b<5e6)) return false;
    if(F.bg==='m'&&!(b&&b>=5e6&&b<2.7e7)) return false;
    if(F.bg==='l'&&!(b&&b>=2.7e7&&b<=9e7)) return false;
  }
  return true;
}

/* ── 地圖(淺色底圖) ── */
const map=L.map('map').setView([23.75,121.0],8);
L.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png',
  {attribution:'&copy; OpenStreetMap &copy; CARTO', maxZoom:18}).addTo(map);

/* 四區示意圖層: 淡色底 + 灰藍虛線界線 + 區名 */
const REGIONS=[
  {name:'北部', color:'#0C1E3C', label:[25.02,121.30],
   poly:[[25.45,120.60],[25.45,122.05],[24.35,122.05],[24.35,121.35],[24.62,121.30],[24.62,120.60]]},
  {name:'中部', color:'#C9A45C', label:[24.05,120.70],
   poly:[[24.62,119.95],[24.62,121.30],[24.35,121.35],[23.55,121.35],[23.55,119.95]]},
  {name:'南部', color:'#2F9E6C', label:[22.90,120.35],
   poly:[[23.55,119.90],[23.55,121.35],[23.35,121.35],[23.35,121.00],[21.80,121.00],[21.80,119.90]]},
  {name:'東部', color:'#55637A', label:[23.30,121.42],
   poly:[[24.35,121.35],[24.35,122.05],[21.80,122.05],[21.80,121.00],[23.35,121.00],[23.35,121.35]]},
];
REGIONS.forEach(r=>{
  L.polygon(r.poly,{color:'#55637A',weight:1.6,dashArray:'6 6',
    fillColor:r.color,fillOpacity:.06,interactive:false}).addTo(map);
  L.marker(r.label,{interactive:false,icon:L.divIcon({className:'rglabel',html:r.name,iconSize:null})}).addTo(map);
});

const layer=L.layerGroup().addTo(map);
const COLORS={hot:'#D6455A',warn:'#E08A2E',safe:'#2F9E6C',done:'#9AA4B2'};
let markers={};
function popupHtml(d){
  return '<div class="pc">'+(d.case_no||'')+'</div>'
    +'<div class="pt">'+d.title+'</div>'
    +orgShort(d.org)+(d.region?' ・ '+d.region:'')+'<br>截止 '+fmtD(d.deadline)+' ・ 預算 '+money(d.budget)
    +(d.loc?'<br>📍 '+d.loc+(d.loc_src==='org'?'(約略)':''):'')
    +(d.url?'<br><a href="'+d.url+'" target="_blank" rel="noopener">開啟採購網公告 ↗</a>':'');
}

function render(){
  const now=new Date();
  const rows=DATA.map((d,i)=>({d,i,st:stateOf(d,now)}))
    .filter(r=>passes(r.d,r.st))
    .sort((a,b)=>{
      // 已截止一律沉底
      const ad=a.st.cls==='done', bd=b.st.cls==='done';
      if(ad!==bd) return ad?1:-1;
      const bA=a.d.budget, bB=b.d.budget;
      switch(F.sort){
        case 'relaxed':   return b.st.ms-a.st.ms;                       // 寬鬆先(剩越多越前)
        case 'budget_hi': return (bB??-1)-(bA??-1);                     // 預算高先(未公告墊底)
        case 'budget_lo': return (bA??Infinity)-(bB??Infinity);        // 預算低先(未公告墊底)
        case 'newest': {                                               // 公告日最新先
          const pa=a.d.publish_date?new Date(a.d.publish_date):0;
          const pb=b.d.publish_date?new Date(b.d.publish_date):0;
          return pb-pa;
        }
        default:          return a.st.ms-b.st.ms;                       // urgent 最急先
      }
    });

  listEl.innerHTML = rows.length? rows.map(r=>{
    const d=r.d;
    return '<div class="card '+r.st.cls+'" data-i="'+r.i+'">'
     +'<div><span class="caseno">'+(d.case_no||'—')+'</span>'
     +(d.region?'<span class="rg">'+d.region+'</span>':'')
     +(daysSincePub(d,now)<=3?'<span class="nb">NEW</span>':'')+'</div>'
     +'<div class="cd"><div class="num" data-cd="'+r.i+'">'+cdText(d,now)+'</div><div class="lab">距截止投標</div></div>'
     +'<div class="t">'+d.title+'</div>'
     +'<div class="meta">'
       +'<span>'+orgShort(d.org)+'</span>'
       +(d.loc?'<span class="loc'+(d.loc_src==='org'?' approx':'')+'">📍'+d.loc+'</span>':'')
       +(d.publish_date?'<span>公告 <b>'+fmtD(d.publish_date).split(' ')[0]+'</b></span>':'')
       +'<span>預算 <b>'+money(d.budget)+'</b></span>'
       +'<span>截止 <b>'+fmtD(d.deadline)+'</b></span>'
       +(d.open_date?'<span>開標 <b>'+fmtD(d.open_date)+'</b></span>':'')
     +'</div></div>';
  }).join('') : '<div class="empty">沒有符合條件的標案</div>';

  layer.clearLayers(); markers={};
  rows.forEach(r=>{
    const d=r.d;
    if(d.lat==null) return;
    const approx = d.loc_src==='org';
    const mk=L.circleMarker([d.lat,d.lng],{
      radius: r.st.cls==='hot'?9:7,
      color:'#fff', weight:2,
      fillColor: COLORS[r.st.cls], fillOpacity: approx?0.35:0.95,
      dashArray: approx?'2 3':null,
    }).bindPopup(popupHtml(d));
    mk.setStyle({color: approx?COLORS[r.st.cls]:'#fff'});
    mk.on('click', ()=>highlight(r.i, false));
    mk.addTo(layer); markers[r.i]=mk;
  });

  const open=DATA.filter(d=>stateOf(d,now).cls!=='done').length;
  const hot=DATA.filter(d=>stateOf(d,now).cls==='hot').length;
  document.getElementById('stOpen').textContent=open;
  document.getElementById('stHot').textContent=hot;
  const located=rows.filter(r=>r.d.lat!=null).length;
  document.getElementById('foot').textContent=
    '顯示 '+rows.length+' 件・地圖已定位 '+located+' 件'
    +(rows.length-located?'（'+(rows.length-located)+' 件無法判定案場位置）':'')
    +'・點卡片可在地圖上定位';
}

function highlight(i, fly){
  [...listEl.querySelectorAll('.card')].forEach(c=>c.classList.toggle('active', c.dataset.i==i));
  const card=listEl.querySelector('.card[data-i="'+i+'"]');
  if(card) card.scrollIntoView({block:'nearest',behavior:'smooth'});
  const mk=markers[i];
  if(mk){ if(fly) map.flyTo(mk.getLatLng(), Math.max(map.getZoom(),10), {duration:.6}); mk.openPopup(); }
}
listEl.addEventListener('click', e=>{
  const c=e.target.closest('.card'); if(c) highlight(+c.dataset.i, true);
});

setInterval(()=>{
  const now=new Date();
  listEl.querySelectorAll('[data-cd]').forEach(el=>{
    el.textContent=cdText(DATA[+el.dataset.cd], now);
  });
}, 1000);
setInterval(render, 60000);
render();
</script>
</body>
</html>
"""


def build_dashboard(items, dropped_count):
    html = (DASHBOARD_TEMPLATE
            .replace("__DATA__", json.dumps(items, ensure_ascii=False))
            .replace("__UPDATED__", now_tw().strftime("%Y/%m/%d %H:%M"))
            .replace("__COUNT__", str(len(items)))
            .replace("__DROPPED__", str(dropped_count)))
    DASHBOARD_PATH.write_text(html, encoding="utf-8")
    print(f"\n儀表板已產生: {DASHBOARD_PATH}")


# ---------------------------------------------------------------- 通知

def notify(cfg, new_items, hot_items):
    e = cfg.get("email通知", {})
    if not e.get("啟用") or not (new_items or hot_items):
        return
    lines = []
    if new_items:
        lines.append(f"◆ 新公告 {len(new_items)} 件:")
        lines += [f"  - {i['title']} (截止 {i.get('deadline','?')}) {i.get('url','')}" for i in new_items]
    if hot_items:
        lines.append(f"\n◆ 3天內截止 {len(hot_items)} 件:")
        lines += [f"  - {i['title']} (截止 {i.get('deadline','?')})" for i in hot_items]
    msg = MIMEText("\n".join(lines), "plain", "utf-8")
    msg["Subject"] = f"[標案監看] 新案 {len(new_items)} 件 / 急件 {len(hot_items)} 件"
    msg["From"] = e["帳號"]
    msg["To"] = ", ".join(e["收件人"])
    try:
        with smtplib.SMTP(e["smtp主機"], e["smtp埠"]) as s:
            s.starttls(context=ssl.create_default_context())
            s.login(e["帳號"], e["應用程式密碼"])
            s.send_message(msg)
        print("Email 通知已寄出")
    except Exception as ex:
        print(f"Email 寄送失敗: {ex}")


# ---------------------------------------------------------------- 主流程

DEMO_ITEMS = [
    {"org": "交通部公路局中區養護工程分局", "title": "台8線梨山段邊坡防護及排水改善工程", "case_no": "115-XZ-15",
     "method": "公開招標", "category": "工程", "budget": 24800000,
     "publish_date": None, "deadline": None, "open_date": None,
     "qualification": "丙等以上綜合營造業", "url": "https://web.pcc.gov.tw/", "source": "示範資料"},
    {"org": "交通部觀光署參山國家風景區管理處", "title": "獅頭山風景區步道設施整修工程", "case_no": "115-TS-08",
     "method": "公開招標", "category": "工程", "budget": 8600000,
     "publish_date": None, "deadline": None, "open_date": None,
     "qualification": "土木包工業或丙等以上綜合營造業", "url": "https://web.pcc.gov.tw/", "source": "示範資料"},
    {"org": "交通部觀光署參山國家風景區管理處", "title": "八卦山大佛風景區公廁改善工程", "case_no": "115-TS-11",
     "method": "公開取得報價單", "category": "工程", "budget": 4200000,
     "publish_date": None, "deadline": None, "open_date": None,
     "qualification": "", "url": "https://web.pcc.gov.tw/", "source": "示範資料"},
    {"org": "交通部公路局中區養護工程分局", "title": "台14線人行環境改善工程(已截止示意)", "case_no": "115-XZ-02",
     "method": "公開招標", "category": "工程", "budget": 15000000,
     "publish_date": None, "deadline": None, "open_date": None,
     "qualification": "", "url": "https://web.pcc.gov.tw/", "source": "示範資料"},
]


def run_demo():
    now = now_tw()
    offsets = [2.2, 5.5, 12, -1]          # 天數: 紅 / 黃 / 綠 / 已截止
    for it, off in zip(DEMO_ITEMS, offsets):
        dl = now + timedelta(days=off)
        it["deadline"] = dl.strftime("%Y-%m-%dT%H:%M")
        it["open_date"] = (dl + timedelta(hours=18)).strftime("%Y-%m-%dT%H:%M")
        it["publish_date"] = (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M")
    for it in DEMO_ITEMS:
        locate(it)
    jitter(DEMO_ITEMS)
    for it in DEMO_ITEMS:
        it["region"] = region_of(it.get("lat"), it.get("lng"))
    build_dashboard(DEMO_ITEMS, dropped_count=2)
    print("(示範模式：以上為假資料，僅供預覽畫面)")


def run():
    cfg = load_json(CONFIG_PATH, {})
    if not cfg:
        sys.exit(f"找不到或無法讀取 {CONFIG_PATH}")
    session = requests.Session()
    all_items = []
    for org in cfg.get("目標機關關鍵字", []):
        try:
            all_items += fetch_list_for_org(session, org, cfg.get("查詢天數", 30))
        except Exception as e:
            print(f"  !! {org} 抓取失敗: {e}")
        time.sleep(2)

    # 去重(同網址或同 機關+標題)
    seen, uniq = set(), []
    for it in all_items:
        key = it.get("url") or (it["org"] + it["title"])
        if key not in seen:
            seen.add(key)
            uniq.append(it)

    # 先用列表頁資料篩一輪(金額/標題)，再只對保留的案子抓詳細頁，省時且對網站更禮貌
    kept, dropped = apply_filters(uniq, cfg)
    if cfg.get("抓詳細頁", True):
        print(f"\n抓取 {len(kept)} 件詳細頁(每件間隔2秒)…")
        for it in kept:
            fetch_detail(session, it)
            time.sleep(2)
        kept, dropped2 = apply_filters(kept, cfg)  # 詳細頁的資格/金額再篩一次
        dropped += dropped2
    for d in dropped:
        print(f"  剔除: {d['title'][:30]} <- {d['_dropped']}")

    old = load_json(STORE_PATH, {})
    old_keys = set(old.get("keys", []))
    new_items = [i for i in kept if (i.get("url") or i["org"] + i["title"]) not in old_keys]
    STORE_PATH.write_text(json.dumps(
        {"updated": now_tw().isoformat(),
         "keys": list(old_keys | {i.get("url") or i["org"] + i["title"] for i in kept}),
         "items": kept},
        ensure_ascii=False, indent=1), encoding="utf-8")

    now = now_tw()
    hot = [i for i in kept if i.get("deadline")
           and 0 < (datetime.fromisoformat(i["deadline"]) - now).total_seconds() < 3 * 86400]

    for it in kept:
        locate(it)
    jitter(kept)
    for it in kept:
        it["region"] = region_of(it.get("lat"), it.get("lng"))
    build_dashboard(kept, len(dropped))
    print(f"共 {len(kept)} 件符合條件 (新案 {len(new_items)} 件、3天內截止 {len(hot)} 件)")
    notify(cfg, new_items, hot)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="用示範資料產生儀表板(不連網)")
    args = ap.parse_args()
    run_demo() if args.demo else run()
