#!/usr/bin/env python3
"""
Walmart Seller Finder - a simple app that runs on your computer.

Start it with:   python3 app.py
A page opens in your browser. Type search words, pick your rules, press Start.

Adding a rule later: write a small function in the RULES section below and add
it to the RULES list. It shows up as a checkbox in the app automatically.

Updates: the app checks GitHub for a newer version when it starts (and every 30
minutes while open) and installs it by itself. Bump VERSION when publishing a change.
"""

import csv
import io
import json
import os
import random
import re
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, urlparse

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
VERSION = 6
UPDATE_URL = "https://raw.githubusercontent.com/larrypro1234/walmart-seller-finder/main/app.py"
BASE = "https://www.walmart.com"
WALMART_FIRST_PARTY = {"walmart.com", "walmart"}
WALMART_FIRST_PARTY_IDS = {"F55CDC31AB754BB68FE0B39041159D63", "0"}


# =============================================================== RULES
# Each rule gets one seller (a dict) and returns None if the seller passes,
# or a short reason string if the seller should be rejected.

def rule_us_only(s):
    if s.get("country") == "US":
        return None
    if s.get("country"):
        return f"Not a US seller ({s['country']})"
    return "Country not shown on seller page"


CLOTHING_WORDS = re.compile(
    r"\b(clothing|apparel|shirts?|t-shirts?|tees?|blouses?|dress(es)?|skirts?|pants|jeans|shorts|"
    r"leggings|hoodies?|sweatshirts?|sweaters?|cardigans?|jackets?|coats?|vests?|rompers?|"
    r"jumpsuits?|pajamas?|pyjamas?|sleepwear|lingerie|bras?|bralettes?|panties|underwear|boxers?|"
    r"briefs|socks|swimsuits?|swimwear|bikinis?|tank tops?|polos?|tops|joggers|sweatpants|"
    r"overalls|tunics?|kimonos?|onesies?|bodysuits?|outfits?)\b",
    re.I,
)


def is_clothing(text):
    return bool(CLOTHING_WORDS.search(text or ""))


def rule_no_clothing_only(s):
    total = len(s.get("products", {}))
    clothing = sum(1 for t in s.get("products", {}).values() if is_clothing(t))
    if total >= 3 and clothing / total >= 0.8:
        return f"Clothing-only store ({clothing} of {total} products are clothing)"
    return None


RULES = [
    {"id": "us_only", "label": "Only US sellers", "check": rule_us_only},
    {"id": "no_clothing_only", "label": "No clothing-only stores", "check": rule_no_clothing_only},
]


# ========================================================= PAGE PARSING

def next_data(html):
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html or "", re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def walk(obj):
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            yield cur
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)


def first(obj, *keys):
    for d in walk(obj):
        for k in keys:
            v = d.get(k)
            if v not in (None, "", [], {}):
                return v
    return None


def item_text(d):
    """Product name plus any category/department info, used to classify the product."""
    parts = [d.get("name") or ""]
    for k, v in d.items():
        if "categor" in k.lower() or "department" in k.lower():
            parts.append(v if isinstance(v, str) else json.dumps(v))
    return " ".join(parts)


def parse_search(data):
    found = []
    for d in walk(data):
        name = d.get("sellerName")
        if not name or "usItemId" not in d:
            continue
        seller_id = str(d.get("catalogSellerId") or d.get("sellerId") or "")
        if name.strip().lower() in WALMART_FIRST_PARTY or seller_id in WALMART_FIRST_PARTY_IDS:
            continue
        found.append({
            "seller_name": name.strip(),
            "catalog_seller_id": str(d.get("catalogSellerId") or ""),
            "item_id": str(d["usItemId"]),
            "item_text": item_text(d),
            "product_url": BASE + d["canonicalUrl"] if d.get("canonicalUrl") else f"{BASE}/ip/{d['usItemId']}",
        })
    return found


US_STATES = set(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ "
    "NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC PR".split()
)
FOREIGN = re.compile(
    r"\b(China|Hong Kong|Taiwan|Canada|United Kingdom|England|Germany|France|India|Vietnam|Japan|"
    r"Korea|Mexico|Australia|Singapore|Malaysia|Turkey|Pakistan|Israel|Thailand|Indonesia|"
    r"Philippines|Netherlands|Italy|Spain|Poland)\b",
    re.I,
)


STATE_KEYS = ("state", "stateCode", "stateOrProvinceCode", "stateOrProvince", "province")
ZIP_KEYS = ("postalCode", "zipCode", "zip")


def find_address(data):
    """Walmart keeps the seller's address as a dict with address1/city/postalCode fields."""
    for d in walk(data):
        if d.get("address1") and ("city" in d or any(k in d for k in ZIP_KEYS)):
            return d
    return first(data, "sellerAddress", "businessAddress")


def detect_country(addr_dict, addr_text):
    if isinstance(addr_dict, dict):
        # Only trust a real country name/code; Walmart's "countryCode" is often the phone prefix like "+1".
        c = str(addr_dict.get("country") or addr_dict.get("countryCode") or "").strip()
        if c.isalpha() and len(c) >= 2:
            return "US" if c.upper() in ("US", "USA", "UNITED STATES", "UNITED STATES OF AMERICA") else c
        state = str(next((addr_dict[k] for k in STATE_KEYS if addr_dict.get(k)), "")).strip().upper()
        zipc = str(next((addr_dict[k] for k in ZIP_KEYS if addr_dict.get(k)), "")).strip()
        us_zip = re.fullmatch(r"\d{5}(-\d{4})?", zipc)
        if state in US_STATES and (us_zip or not zipc):
            return "US"
        if us_zip and c in ("+1", "1") and not state:
            return "US"  # US-style ZIP code + US phone prefix, no state given
        if c.startswith("+") and c != "+1":
            return f"phone code {c}"
    text = addr_text or ""
    m = FOREIGN.search(text)
    if m:
        return m.group(1).title()
    if re.search(r"\b(United States|USA)\b", text, re.I):
        return "US"
    m = re.search(r"\b([A-Z]{2})[ ,]+\d{5}(?:-\d{4})?\b", text)
    if m and m.group(1) in US_STATES:
        return "US"
    return ""


def parse_seller_page(data, html):
    info = {
        "display_name": first(data, "sellerDisplayName", "sellerName", "displayName") or "",
        "rating": first(data, "averageOverallRating", "sellerRating", "averageRating") or "",
        "review_count": first(data, "totalReviewCount", "numberOfReviews", "reviewCount") or "",
        "phone": first(data, "sellerPhone", "phone", "phoneNumber") or "",
        "email": first(data, "sellerEmail", "email", "emailAddress") or "",
        "business_name": first(data, "sellerLegalName", "businessName", "legalName") or "",
        "address": "",
    }
    addr = find_address(data)
    if isinstance(addr, dict):
        keys = ("address1", "addressLineOne", "street", "address2", "city") + STATE_KEYS + ZIP_KEYS + ("country",)
        info["address"] = ", ".join(str(addr[k]) for k in keys if addr.get(k))
    elif isinstance(addr, str):
        info["address"] = addr

    # Visible page text only (script blocks removed so raw JSON doesn't leak in).
    visible = re.sub(r"<(script|style)\b.*?</\1>", " ", html or "", flags=re.S | re.I)
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", visible))
    if not info["address"]:
        m = re.search(r"address:?\s*(.{10,160}?\d{5}(?:-\d{4})?(?:,? ?(?:US|USA|United States))?)", text, re.I)
        if m:
            info["address"] = m.group(1).strip()
    if not info["email"]:
        m = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", text)
        if m and "walmart" not in m.group(0).lower():
            info["email"] = m.group(0)
    if not info["phone"]:
        m = re.search(r"(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}", text)
        if m:
            info["phone"] = m.group(0).strip()
    info["country"] = detect_country(addr, info["address"])

    # Products shown on the storefront, used by the clothing rule.
    info["store_products"] = {str(d["usItemId"]): item_text(d)
                              for d in walk(data) if d.get("usItemId") and d.get("name")}
    return info


# ============================================================== UPDATES

def fetch_latest():
    """Return the newest app.py from GitHub if it's a higher version that compiles, else None."""
    try:
        text = subprocess.run(["curl", "-fsSL", "--max-time", "10", f"{UPDATE_URL}?t={int(time.time())}"],
                              capture_output=True, text=True, check=True).stdout
        m = re.search(r"^VERSION = (\d+)", text, re.M)
        if not m or int(m.group(1)) <= VERSION:
            return None
        compile(text, "app.py", "exec")
        return text
    except Exception:
        return None


def install_and_restart(text, open_browser=True):
    me = Path(__file__).resolve()
    tmp = me.with_name("app.py.new")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, me)
    os.execv(sys.executable, [sys.executable, str(me)] + ([] if open_browser else ["--no-open"]))


def watch_for_updates():
    while True:
        time.sleep(30 * 60)
        text = fetch_latest()
        if text:
            STATE["update_available"] = re.search(r"^VERSION = (\d+)", text, re.M).group(1)


# ============================================================== SCRAPER

STATE = {"running": False, "stop": False, "log": [], "sellers": [], "saved_file": "", "update_available": ""}
LOCK = threading.Lock()


def log(msg):
    with LOCK:
        STATE["log"].append(datetime.now().strftime("%H:%M:%S ") + msg)
        STATE["log"] = STATE["log"][-300:]


class Browser:
    def __init__(self, delay):
        self.delay = delay
        self._pw = sync_playwright().start()
        self.ctx = self._pw.chromium.launch_persistent_context(
            str(HERE / "browser-profile"),
            headless=False,
            viewport={"width": 1366, "height": 900},
            locale="en-US",
            args=["--disable-blink-features=AutomationControlled",
                  # keep pages loading normally while the window is minimized
                  "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
                  "--disable-backgrounding-occluded-windows"],
        )
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        # The window stays minimized in the Dock and only comes up for a robot check.
        self.window_id = None
        try:
            self.cdp = self.ctx.new_cdp_session(self.page)
            self.window_id = self.cdp.send("Browser.getWindowForTarget")["windowId"]
        except Exception:
            pass
        self.hide()

    def _set_state(self, state):
        if self.window_id is None:
            return
        try:
            self.cdp.send("Browser.setWindowBounds", {"windowId": self.window_id, "bounds": {"windowState": state}})
        except Exception:
            pass

    def hide(self):
        self._set_state("minimized")

    def show(self):
        self._set_state("normal")
        self.page.bring_to_front()

    def get(self, url):
        time.sleep(self.delay + random.uniform(0, self.delay))
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            log(f"Could not load page: {e}")
            return None
        warned = False
        for _ in range(120):
            if STATE["stop"]:
                return None
            try:
                html = self.page.content()
            except Exception:  # page is mid-navigation
                time.sleep(1)
                continue
            if "__NEXT_DATA__" in html:
                if warned:
                    self.hide()
                return html
            if not warned and re.search(r"robot or human|press\s*&\s*hold|px-captcha", html, re.I):
                log("Walmart is asking if you're a robot - press & hold the button in the Chrome window that just popped up")
                self.show()
                warned = True
            time.sleep(1)
        log("Gave up on a page that wouldn't load")
        return None

    def close(self):
        try:
            self.ctx.close()
            self._pw.stop()
        except Exception:
            pass


def run_scrape(keywords, pages, active_rules, delay):
    sellers = {}
    br = None
    try:
        br = Browser(delay)
        # Step 1: find sellers in search results.
        for kw in keywords:
            for page in range(1, pages + 1):
                if STATE["stop"]:
                    break
                log(f'Searching "{kw}" - page {page}')
                html = br.get(f"{BASE}/search?q={quote_plus(kw)}&page={page}")
                hits = parse_search(next_data(html)) if html else []
                if not hits:
                    break
                new = 0
                for h in hits:
                    key = h["seller_name"].lower()
                    if key not in sellers:
                        new += 1
                        sellers[key] = {
                            "seller_name": h["seller_name"], "catalog_seller_id": "",
                            "keywords": set(), "products": {},
                            "sample_product_url": h["product_url"], "status": "Checking...",
                        }
                    rec = sellers[key]
                    if h["catalog_seller_id"].isdigit():
                        rec["catalog_seller_id"] = h["catalog_seller_id"]
                    rec["keywords"].add(kw)
                    rec["products"][h["item_id"]] = h["item_text"]
                log(f"  found {new} new sellers ({len(sellers)} total)")
                publish(sellers)

        # Step 2: open each seller's page, get their details, apply the rules.
        for i, rec in enumerate(list(sellers.values()), 1):
            if STATE["stop"]:
                break
            log(f"Checking seller {i}/{len(sellers)}: {rec['seller_name']}")
            cid = rec["catalog_seller_id"]
            if not cid.isdigit():
                html = br.get(rec["sample_product_url"])
                cid = str(first(next_data(html), "catalogSellerId") or "") if html and next_data(html) else ""
                rec["catalog_seller_id"] = cid
            if cid.isdigit():
                rec["seller_url"] = f"{BASE}/seller/{cid}"
                html = br.get(rec["seller_url"])
                data = next_data(html) if html else None
                if data:
                    info = parse_seller_page(data, html)
                    rec["products"].update(info.pop("store_products"))
                    rec.update(info)
            reasons = [r["check"](rec) for r in RULES if r["id"] in active_rules]
            reasons = [r for r in reasons if r]
            rec["status"] = "Rejected" if reasons else "Good"
            rec["reason"] = "; ".join(reasons)
            publish(sellers)

        save_csv(sellers)
        good = sum(1 for s in sellers.values() if s["status"] == "Good")
        log(f"Done! {good} good sellers, {len(sellers) - good} rejected. Saved to {STATE['saved_file']}")
    except Exception as e:
        log(f"Something went wrong: {e}")
        save_csv(sellers)
    finally:
        if br:
            br.close()
        with LOCK:
            STATE["running"] = False
            STATE["stop"] = False


COLUMNS = ["status", "reason", "seller_name", "business_name", "phone", "email", "address", "country",
           "rating", "review_count", "seller_url", "catalog_seller_id", "products_checked", "keywords"]


def row(rec):
    r = {k: rec.get(k, "") for k in COLUMNS}
    r["products_checked"] = len(rec.get("products", {}))
    r["keywords"] = "; ".join(sorted(rec.get("keywords", [])))
    return r


def publish(sellers):
    with LOCK:
        STATE["sellers"] = [row(s) for s in sellers.values()]


def to_csv(rows):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUMNS)
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


def save_csv(sellers):
    if not sellers:
        return
    publish(sellers)
    name = f"walmart_sellers_{datetime.now():%Y-%m-%d_%H-%M}.csv"
    (HERE / name).write_text(to_csv(STATE["sellers"]), encoding="utf-8")
    STATE["saved_file"] = name


# ============================================================ WEB PAGE

PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>Walmart Seller Finder</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{--bg:#f6f7f9;--card:#fff;--text:#1d2330;--muted:#6b7280;--line:#e3e6eb;--blue:#0071dc;--green:#1a7f37;--red:#c62828}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
.wrap{max-width:1200px;margin:0 auto;padding:24px 16px}
h1{font-size:24px;margin:0 0 16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:20px;margin-bottom:16px}
.grid{display:grid;grid-template-columns:2fr 1fr 1fr;gap:20px}@media(max-width:800px){.grid{grid-template-columns:1fr}}
label.h{display:block;font-weight:600;margin-bottom:6px}
textarea,input[type=number]{width:100%;padding:10px;border:1px solid var(--line);border-radius:8px;font:inherit}
textarea{height:110px;resize:vertical}
.rule{display:flex;gap:8px;align-items:center;margin:6px 0}
.hint{color:var(--muted);font-size:13px;margin-top:4px}
button{font:inherit;font-weight:600;border:0;border-radius:8px;padding:12px 22px;cursor:pointer}
#start{background:var(--blue);color:#fff}#stop{background:#eceff3;color:var(--text)}
button:disabled{opacity:.45;cursor:default}
.dl{background:#eceff3;color:var(--text);padding:8px 14px;font-size:14px}
#status{margin-left:12px;color:var(--muted)}
#log{background:#11151c;color:#cfd6e0;font:12px/1.5 ui-monospace,Menlo,monospace;height:150px;overflow:auto;border-radius:8px;padding:10px;white-space:pre-wrap}
.tabs{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:12px}
.tab{background:#eceff3;color:var(--text);padding:8px 14px}.tab.on{background:var(--text);color:#fff}
.tablewrap{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{text-align:left;padding:8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:12px;text-transform:uppercase}
.good{color:var(--green);font-weight:600}.bad{color:var(--red);font-weight:600}
a{color:var(--blue)}
.empty{color:var(--muted);padding:20px;text-align:center}
</style></head><body><div class="wrap">
<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px">
  <h1 style="margin:0">Walmart Seller Finder <span style="font-size:14px;color:var(--muted);font-weight:500">version __VERSION__</span></h1><button id="quit" class="dl">Quit app</button></div>
<div id="update" class="card" style="display:none;border-color:var(--blue);display:none;align-items:center;gap:12px;flex-wrap:wrap">
  <b>A new version of the app is ready.</b><button id="updbtn" class="dl" style="background:var(--blue);color:#fff">Update now</button></div>
<div class="card"><div class="grid">
  <div><label class="h">What to search for</label>
    <textarea id="kw" placeholder="One search per line, for example:&#10;lego&#10;shampoo&#10;board games"></textarea></div>
  <div><label class="h">Pages per search</label>
    <input id="pages" type="number" min="1" max="25" value="3">
    <div class="hint">Each page has about 40 products. More pages = more sellers, but slower.</div></div>
  <div><label class="h">Rules</label><div id="rules"></div></div>
</div>
<div style="margin-top:18px;display:flex;align-items:center;gap:10px;flex-wrap:wrap">
  <button id="start">Start</button><button id="stop" disabled>Stop</button><span id="status"></span>
</div></div>
<div class="card"><label class="h">What it's doing</label><div id="log"></div></div>
<div class="card">
  <div class="tabs">
    <button class="tab on" data-t="Good">Good sellers (<span id="nGood">0</span>)</button>
    <button class="tab" data-t="Rejected">Rejected (<span id="nRejected">0</span>)</button>
    <button class="tab" data-t="Checking...">Still checking (<span id="nChecking">0</span>)</button>
    <span style="flex:1"></span>
    <button class="dl" onclick="location='/download?which=good'">Download good sellers</button>
    <button class="dl" onclick="location='/download?which=all'">Download all</button>
  </div>
  <div class="tablewrap"><table><thead><tr>
    <th>Seller</th><th>Phone</th><th>Email</th><th>Address</th><th>Rating</th><th>Store</th><th>Why rejected</th>
  </tr></thead><tbody id="rows"></tbody></table></div>
</div></div>
<script>
let tab="Good", data={sellers:[]};
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
fetch("/rules").then(r=>r.json()).then(rules=>{
  document.getElementById("rules").innerHTML=rules.map(r=>
    `<label class="rule"><input type="checkbox" value="${r.id}" checked> ${esc(r.label)}</label>`).join("");
});
document.querySelectorAll(".tab").forEach(b=>b.onclick=()=>{
  document.querySelectorAll(".tab").forEach(x=>x.classList.remove("on"));b.classList.add("on");tab=b.dataset.t;draw();});
document.getElementById("start").onclick=async()=>{
  const keywords=document.getElementById("kw").value.split("\n").map(s=>s.trim()).filter(Boolean);
  if(!keywords.length){alert("Type at least one thing to search for.");return;}
  const rules=[...document.querySelectorAll("#rules input:checked")].map(i=>i.value);
  const pages=+document.getElementById("pages").value||3;
  await fetch("/start",{method:"POST",body:JSON.stringify({keywords,rules,pages})});poll();
};
document.getElementById("stop").onclick=()=>fetch("/stop",{method:"POST"});
document.getElementById("updbtn").onclick=async()=>{
  if(data.running){alert("Wait until the search is done (or press Stop) before updating.");return;}
  await fetch("/update",{method:"POST"});
  document.getElementById("update").innerHTML="<b>Updating... the page will reload by itself.</b>";
  const wait=async()=>{try{await fetch("/status");location.reload();}catch(e){setTimeout(wait,1500);}};
  setTimeout(wait,3000);
};
document.getElementById("quit").onclick=async()=>{
  if(!confirm("Turn the app off?"))return;
  await fetch("/quit",{method:"POST"});
  document.body.innerHTML='<div class="wrap"><h1>The app is off.</h1><p>You can close this tab. Double-click the Walmart Seller Finder icon to start it again.</p></div>';
};
function draw(){
  const s=data.sellers;
  for(const [k,id] of [["Good","nGood"],["Rejected","nRejected"],["Checking...","nChecking"]])
    document.getElementById(id).textContent=s.filter(x=>x.status===k).length;
  const list=s.filter(x=>x.status===tab);
  document.getElementById("rows").innerHTML=list.length?list.map(x=>`<tr>
    <td><b>${esc(x.seller_name)}</b>${x.business_name&&x.business_name!==x.seller_name?`<br><span class="hint">${esc(x.business_name)}</span>`:""}</td>
    <td>${esc(x.phone)}</td><td>${esc(x.email)}</td><td>${esc(x.address)}</td>
    <td>${x.rating?esc((+x.rating).toFixed?.(1)??x.rating):""}${x.review_count?` <span class="hint">(${esc(x.review_count)})</span>`:""}</td>
    <td>${x.seller_url?`<a href="${esc(x.seller_url)}" target="_blank">Open</a>`:""}</td>
    <td class="${x.status==="Rejected"?"bad":""}">${esc(x.reason)}</td></tr>`).join("")
    :`<tr><td colspan="7" class="empty">Nothing here yet.</td></tr>`;
}
async function poll(){
  data=await (await fetch("/status")).json();
  document.getElementById("start").disabled=data.running;
  document.getElementById("stop").disabled=!data.running;
  document.getElementById("status").textContent=data.running?"Running... (Chrome works minimized in the Dock and only pops up if Walmart needs you to press & hold)":(data.saved_file?"Last results saved to "+data.saved_file:"");
  const log=document.getElementById("log"),atBottom=log.scrollTop+log.clientHeight>=log.scrollHeight-5;
  log.textContent=data.log.join("\n");if(atBottom)log.scrollTop=log.scrollHeight;
  document.getElementById("update").style.display=data.update_available?"flex":"none";
  draw();
  setTimeout(poll,data.running?2000:30000);
}
poll();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, body, ctype="application/json", extra=None):
        body = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            self.send(PAGE.replace("__VERSION__", str(VERSION)), "text/html; charset=utf-8")
        elif url.path == "/rules":
            self.send(json.dumps([{"id": r["id"], "label": r["label"]} for r in RULES]))
        elif url.path == "/status":
            with LOCK:
                self.send(json.dumps({k: STATE[k] for k in ("running", "log", "sellers", "saved_file", "update_available")}, default=str))
        elif url.path == "/download":
            which = parse_qs(url.query).get("which", ["good"])[0]
            with LOCK:
                rows = [s for s in STATE["sellers"] if which == "all" or s["status"] == "Good"]
            name = f"walmart_sellers_{which}_{datetime.now():%Y-%m-%d}.csv"
            self.send(to_csv(rows), "text/csv; charset=utf-8",
                      {"Content-Disposition": f'attachment; filename="{name}"'})
        else:
            self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/start":
            with LOCK:
                if STATE["running"]:
                    return self.send('{"ok":false}')
                STATE.update(running=True, stop=False, log=[], sellers=[], saved_file="")
            pages = max(1, min(25, int(body.get("pages") or 3)))
            threading.Thread(target=run_scrape, daemon=True,
                             args=(body["keywords"], pages, set(body.get("rules", [])), 2.0)).start()
            self.send('{"ok":true}')
        elif self.path == "/stop":
            STATE["stop"] = True
            log("Stopping after the current page...")
            self.send('{"ok":true}')
        elif self.path == "/update":
            if STATE["running"]:
                return self.send('{"ok":false}')
            self.send('{"ok":true}')

            def update():
                time.sleep(0.5)
                text = fetch_latest()
                if text:
                    install_and_restart(text, open_browser=False)
            threading.Thread(target=update, daemon=True).start()
        elif self.path == "/quit":
            STATE["stop"] = True
            self.send('{"ok":true}')

            def shutdown():
                for _ in range(30):  # let a running search save its results first
                    if not STATE["running"]:
                        break
                    time.sleep(1)
                self.server.shutdown()
            threading.Thread(target=shutdown, daemon=True).start()
        else:
            self.send_error(404)


def main():
    text = fetch_latest()
    if text:
        print("Installing a newer version of the app...")
        install_and_restart(text, open_browser="--no-open" not in sys.argv)
    threading.Thread(target=watch_for_updates, daemon=True).start()
    for port in range(8765, 8785):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    url = f"http://localhost:{port}"
    print(f"\nWalmart Seller Finder is running at {url}")
    print("Keep this Terminal window open. Press Control + C here to quit.\n")
    if "--no-open" not in sys.argv:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    server.serve_forever()
    print("App turned off.")


if __name__ == "__main__":
    main()
