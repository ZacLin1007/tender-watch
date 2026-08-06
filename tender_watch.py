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
    end = datetime.now()
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
            return items
    print(f"    -> 所有策略都是 0 筆")
    return []


def parse_list_html(html: str, org_kw: str):
    """解析查詢結果列表頁。政府網站改版時最可能要調整的就是這個函式。"""
    soup = BeautifulSoup(html, "lxml")
    items = []
    table = soup.find(id="tpam") or soup.find("table", class_=re.compile("tb_"))
    rows = table.find_all("tr") if table else soup.find_all("tr")
    for tr in rows:
        tds = tr.find_all("td")
        if len(tds) < 5:
            continue
        link = tr.find("a", href=True)
        row_text = [td.get_text(" ", strip=True) for td in tds]
        joined = " | ".join(row_text)
        if not any(re.search(r"\d{2,3}/\d{1,2}/\d{1,2}", t) for t in row_text):
            continue  # 沒日期的列(表頭等)跳過
        # 常見欄位順序: 項次 | 機關名稱 | 標案案號/標案名稱 | 傳輸次數 | 招標方式 | 採購性質 | 公告日期 | 截止投標 | 預算金額
        item = {
            "org": "", "title": "", "case_no": "",
            "method": "", "category": "",
            "publish_date": None, "deadline": None,
            "open_date": None, "budget": None,
            "qualification": "", "url": "",
            "source": "政府電子採購網",
        }
        if link:
            href = link["href"]
            item["url"] = href if href.startswith("http") else PCC_BASE + href
            item["title"] = link.get_text(" ", strip=True)
        # 逐欄猜測內容
        dates = []
        for t in row_text:
            if org_kw.split("局")[0][:4] in t or "管理處" in t or "分局" in t:
                if not item["org"]:
                    item["org"] = t
            iso = parse_roc_datetime(t)
            if iso and re.fullmatch(r"[\d/\s:]+", t.strip()):
                dates.append(iso)
            if re.search(r"公開招標|限制性招標|選擇性招標|公開取得", t):
                item["method"] = t
            if re.fullmatch(r"工程類?|財物類?|勞務類?", t.strip()):
                item["category"] = t.strip()
            money = parse_money(t) if re.search(r"^[\d,]+$", t.strip()) else None
            if money and money > 10000:
                item["budget"] = money
        if dates:
            item["publish_date"] = dates[0]
            if len(dates) >= 2:
                item["deadline"] = dates[1]
        # 案號通常和標題同一格: "114A123 某某工程"
        m = re.match(r"([A-Za-z0-9\-]+)\s+(.+)", item["title"])
        if m and len(m.group(1)) >= 5:
            item["case_no"], item["title"] = m.group(1), m.group(2)
        if not item["org"]:
            item["org"] = org_kw
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
                k = cells[0].get_text(" ", strip=True)
                v = cells[1].get_text(" ", strip=True)
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
<title>標案監看儀表板</title>
<style>
  :root{
    --bg:#10141a; --panel:#1a2029; --panel2:#212936; --line:#2e3947;
    --ink:#e8edf4; --dim:#8b98a9;
    --safe:#3ecf8e; --warn:#f5b83d; --hot:#ff5d5d; --done:#5b6675;
    --accent:#4da3ff;
    font-family:"Noto Sans TC","Microsoft JhengHei",system-ui,sans-serif;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--ink);min-height:100vh;padding:24px 20px 60px}
  header{max-width:1100px;margin:0 auto 20px;display:flex;flex-wrap:wrap;gap:12px;align-items:baseline;justify-content:space-between}
  h1{font-size:22px;letter-spacing:.08em;font-weight:700}
  h1 .mono{color:var(--accent)}
  .meta{color:var(--dim);font-size:13px}
  .bar{max-width:1100px;margin:0 auto 16px;display:flex;flex-wrap:wrap;gap:8px;align-items:center}
  .chip{border:1px solid var(--line);background:var(--panel);color:var(--dim);
        padding:6px 14px;border-radius:999px;font-size:13px;cursor:pointer;user-select:none}
  .chip.on{color:var(--ink);border-color:var(--accent);background:rgba(77,163,255,.12)}
  input[type=search]{flex:1;min-width:180px;background:var(--panel);border:1px solid var(--line);
        color:var(--ink);padding:8px 14px;border-radius:8px;font-size:14px}
  input[type=search]:focus{outline:2px solid var(--accent);outline-offset:0}
  main{max-width:1100px;margin:0 auto;display:grid;gap:12px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:12px;
        padding:16px 18px;display:grid;gap:10px;
        grid-template-columns:minmax(0,1fr) 200px}
  .card.hot{border-left:4px solid var(--hot)}
  .card.warn{border-left:4px solid var(--warn)}
  .card.safe{border-left:4px solid var(--safe)}
  .card.done{border-left:4px solid var(--done);opacity:.55}
  .t{font-size:16px;font-weight:600;line-height:1.45}
  .t a{color:var(--ink);text-decoration:none}
  .t a:hover{color:var(--accent);text-decoration:underline}
  .sub{color:var(--dim);font-size:13px;display:flex;flex-wrap:wrap;gap:6px 16px;line-height:1.7}
  .sub b{color:var(--ink);font-weight:500}
  .cd{display:flex;flex-direction:column;justify-content:center;align-items:flex-end;gap:2px}
  .cd .num{font-size:22px;font-weight:700;font-variant-numeric:tabular-nums}
  .cd .lab{font-size:12px;color:var(--dim)}
  .hot  .cd .num{color:var(--hot)}
  .warn .cd .num{color:var(--warn)}
  .safe .cd .num{color:var(--safe)}
  .done .cd .num{color:var(--done)}
  .empty{color:var(--dim);text-align:center;padding:60px 0;font-size:15px}
  footer{max-width:1100px;margin:28px auto 0;color:var(--dim);font-size:12px;line-height:1.8}
  @media(max-width:640px){.card{grid-template-columns:1fr}.cd{align-items:flex-start}}
</style>
</head>
<body>
<header>
  <h1>標案監看 <span class="mono">TENDER WATCH</span></h1>
  <div class="meta">資料更新：__UPDATED__ ｜ 符合條件 __COUNT__ 件（已剔除 __DROPPED__ 件）</div>
</header>
<div class="bar" id="orgChips"></div>
<div class="bar">
  <span class="chip on" data-f="all">全部</span>
  <span class="chip" data-f="open">投標中</span>
  <span class="chip" data-f="hot">3天內截止</span>
  <input type="search" id="q" placeholder="搜尋標案名稱 / 案號…">
</div>
<main id="list"></main>
<footer>
  倒數以「截止投標」時間計算，每秒更新。紅色＝3天內、黃色＝7天內、綠色＝7天以上、灰色＝已截止。<br>
  資料來源：政府電子採購網（web.pcc.gov.tw），本頁僅供內部參考，投標前請以採購網原始公告為準。
</footer>
<script>
const DATA = __DATA__;
const list = document.getElementById('list');
let orgFilter = new Set(), mode = 'all', q = '';

function fmt(d){ if(!d) return '—';
  const dt = new Date(d);
  return (dt.getFullYear())+'/'+String(dt.getMonth()+1).padStart(2,'0')+'/'+String(dt.getDate()).padStart(2,'0')
   +' '+String(dt.getHours()).padStart(2,'0')+':'+String(dt.getMinutes()).padStart(2,'0');
}
function money(n){ if(!n) return '未公告';
  if(n>=1e8) return (n/1e8).toFixed(2).replace(/\.?0+$/,'')+' 億';
  if(n>=1e4) return Math.round(n/1e4).toLocaleString()+' 萬';
  return n.toLocaleString()+' 元';
}
function state(item, now){
  if(!item.deadline) return {cls:'safe', num:'—', lab:'無截止日資料'};
  const ms = new Date(item.deadline) - now;
  if(ms <= 0) return {cls:'done', num:'已截止', lab:fmt(item.deadline)};
  const d = Math.floor(ms/86400000), h = Math.floor(ms%86400000/3600000),
        m = Math.floor(ms%3600000/60000), s = Math.floor(ms%60000/1000);
  const num = d>0 ? `${d} 天 ${h} 時` : `${h}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;
  const cls = ms < 3*86400000 ? 'hot' : ms < 7*86400000 ? 'warn' : 'safe';
  return {cls, num, lab:'距截止投標'};
}
function orgShort(o){ return o.replace('交通部觀光署','').replace('交通部','').slice(0,14); }

function buildChips(){
  const box = document.getElementById('orgChips');
  const orgs = [...new Set(DATA.map(d=>d.org))];
  box.innerHTML = orgs.map(o=>`<span class="chip on" data-org="${o}">${orgShort(o)}</span>`).join('');
  orgs.forEach(o=>orgFilter.add(o));
  box.querySelectorAll('.chip').forEach(c=>c.onclick=()=>{
    const o = c.dataset.org;
    if(orgFilter.has(o)&&orgFilter.size>1){orgFilter.delete(o);c.classList.remove('on');}
    else{orgFilter.add(o);c.classList.add('on');}
    render();
  });
}
document.querySelectorAll('[data-f]').forEach(c=>c.onclick=()=>{
  document.querySelectorAll('[data-f]').forEach(x=>x.classList.remove('on'));
  c.classList.add('on'); mode=c.dataset.f; render();
});
document.getElementById('q').oninput=e=>{q=e.target.value.trim();render();};

function render(){
  const now = new Date();
  let rows = DATA.filter(d=>orgFilter.has(d.org));
  if(q) rows = rows.filter(d=>(d.title+(d.case_no||'')).includes(q));
  rows = rows.map(d=>({...d, st:state(d,now)}));
  if(mode==='open') rows = rows.filter(d=>d.st.cls!=='done');
  if(mode==='hot')  rows = rows.filter(d=>d.st.cls==='hot');
  rows.sort((a,b)=>{
    const ax=a.deadline?new Date(a.deadline):Infinity, bx=b.deadline?new Date(b.deadline):Infinity;
    const ad=a.st.cls==='done', bd=b.st.cls==='done';
    if(ad!==bd) return ad?1:-1;
    return ax-bx;
  });
  list.innerHTML = rows.length ? rows.map(d=>`
    <div class="card ${d.st.cls}">
      <div>
        <div class="t">${d.url?`<a href="${d.url}" target="_blank" rel="noopener">${d.title}</a>`:d.title}</div>
        <div class="sub">
          <span>${orgShort(d.org)}</span>
          ${d.case_no?`<span>案號 <b>${d.case_no}</b></span>`:''}
          <span>預算 <b>${money(d.budget)}</b></span>
          <span>截止投標 <b>${fmt(d.deadline)}</b></span>
          <span>開標 <b>${fmt(d.open_date)}</b></span>
          ${d.method?`<span>${d.method}</span>`:''}
        </div>
      </div>
      <div class="cd"><div class="num">${d.st.num}</div><div class="lab">${d.st.lab}</div></div>
    </div>`).join('')
    : '<div class="empty">沒有符合條件的標案</div>';
}
buildChips(); render(); setInterval(render, 1000);
</script>
</body>
</html>
"""


def build_dashboard(items, dropped_count):
    html = (DASHBOARD_TEMPLATE
            .replace("__DATA__", json.dumps(items, ensure_ascii=False))
            .replace("__UPDATED__", datetime.now().strftime("%Y/%m/%d %H:%M"))
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
    now = datetime.now()
    offsets = [2.2, 5.5, 12, -1]          # 天數: 紅 / 黃 / 綠 / 已截止
    for it, off in zip(DEMO_ITEMS, offsets):
        dl = now + timedelta(days=off)
        it["deadline"] = dl.strftime("%Y-%m-%dT%H:%M")
        it["open_date"] = (dl + timedelta(hours=18)).strftime("%Y-%m-%dT%H:%M")
        it["publish_date"] = (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M")
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

    if cfg.get("抓詳細頁", True):
        print(f"\n抓取 {len(uniq)} 件詳細頁(每件間隔2秒)…")
        for it in uniq:
            fetch_detail(session, it)
            time.sleep(2)

    kept, dropped = apply_filters(uniq, cfg)
    for d in dropped:
        print(f"  剔除: {d['title'][:30]} <- {d['_dropped']}")

    old = load_json(STORE_PATH, {})
    old_keys = set(old.get("keys", []))
    new_items = [i for i in kept if (i.get("url") or i["org"] + i["title"]) not in old_keys]
    STORE_PATH.write_text(json.dumps(
        {"updated": datetime.now().isoformat(),
         "keys": list(old_keys | {i.get("url") or i["org"] + i["title"] for i in kept}),
         "items": kept},
        ensure_ascii=False, indent=1), encoding="utf-8")

    now = datetime.now()
    hot = [i for i in kept if i.get("deadline")
           and 0 < (datetime.fromisoformat(i["deadline"]) - now).total_seconds() < 3 * 86400]

    build_dashboard(kept, len(dropped))
    print(f"共 {len(kept)} 件符合條件 (新案 {len(new_items)} 件、3天內截止 {len(hot)} 件)")
    notify(cfg, new_items, hot)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="用示範資料產生儀表板(不連網)")
    args = ap.parse_args()
    run_demo() if args.demo else run()
