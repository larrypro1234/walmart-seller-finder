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
import sqlite3
import subprocess
import sys
import threading
import time
import webbrowser
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, urlparse

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
VERSION = 20
UPDATE_URL = "https://raw.githubusercontent.com/larrypro1234/walmart-seller-finder/main/app.py"
BASE = "https://www.walmart.com"
MAX_PAGES = 25  # Walmart doesn't show more than this many result pages for one search
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


# What a store sells. Each product is sorted into the first category whose words match.
# Order matters: used/refurbished first, then what we BUY (so "LEGO car" is Toys and
# "baby blanket" is Baby), then what we DON'T buy, then neutral categories.

def _words(*words):
    return re.compile(r"\b(" + "|".join(words) + r")\b", re.I)


USED = _words(r"refurbished", r"renewed", r"restored", r"pre-?owned", r"open[- ]box", r"\(used\)", r"used -")

BUY = [
    ("Apparel basics", _words(r"socks?", r"boxers?", r"boxer briefs", r"briefs", r"underwear", r"undershirts?",
                              r"boy ?shorts", r"panties", r"trunks")),
    ("Toys & Games", _words(r"toys?", r"lego", r"hasbro", r"mattel", r"barbie", r"hot wheels", r"fisher[- ]price",
                            r"nerf", r"play-?doh", r"monopoly", r"puzzles?", r"board games?", r"card games?",
                            r"dolls?", r"action figures?", r"plush", r"stuffed animals?", r"building (set|blocks|kit)",
                            r"playsets?", r"funko", r"uno", r"jenga", r"crayola", r"melissa (&|and) doug",
                            r"pok[eé]mon", r"squishmallows?", r"magna-?tiles", r"vtech", r"little tikes", r"games? for kids")),
    ("OTC", _words(r"tylenol", r"advil", r"motrin", r"aleve", r"ibuprofen", r"acetaminophen", r"naproxen", r"allergy",
                   r"antihistamine", r"claritin", r"zyrtec", r"allegra", r"benadryl", r"cold (&|and) flu", r"antacid",
                   r"tums", r"pepto", r"cough", r"nasal spray", r"mucinex", r"dayquil", r"nyquil", r"pain relie(f|ver)",
                   r"first aid", r"band-?aid", r"hydrocortisone", r"neosporin", r"laxative", r"eye drops")),
    ("Supplements", _words(r"vitamins?", r"multivitamins?", r"supplements?", r"protein powder", r"probiotics?",
                           r"fish oil", r"omega-?3", r"collagen", r"melatonin", r"garden of life", r"nature made",
                           r"centrum", r"one a day", r"electrolytes?", r"biotin", r"magnesium", r"zinc", r"elderberry")),
    ("Baby", _words(r"diapers?", r"baby wipes", r"pampers", r"huggies", r"luvs", r"infant formula", r"baby formula",
                    r"similac", r"enfamil", r"baby bottles?", r"pacifiers?", r"sippy", r"baby", r"teethers?", r"newborn")),
    ("Health & Beauty", _words(r"makeup", r"cosmetics?", r"mascara", r"lipstick", r"lip gloss", r"foundation",
                               r"eyeliner", r"eyeshadow", r"concealer", r"nail polish", r"revlon", r"l'?or[eé]al",
                               r"maybelline", r"cerave", r"neutrogena", r"olay", r"cetaphil", r"aveeno", r"la roche-posay",
                               r"skin ?care", r"moisturi[sz]er", r"serum", r"face wash", r"cleanser", r"sunscreen",
                               r"lotion", r"perfume", r"fragrance", r"cologne", r"eau de")),
    ("Personal Care", _words(r"shampoo", r"conditioner", r"body wash", r"soap", r"deodorant", r"antiperspirant",
                             r"toothpaste", r"toothbrush(es)?", r"razors?", r"shaving", r"shave", r"dove", r"hair care",
                             r"hair (oil|gel|spray|mask)", r"floss", r"mouthwash", r"tampons?", r"feminine", r"(period|maxi|menstrual|panty) (pads|liners)",
                             r"crest", r"colgate", r"gillette", r"old spice", r"dry shampoo")),
    ("Household Essentials", _words(r"tide", r"dawn", r"lysol", r"clorox", r"detergent", r"dish ?soap", r"cleaner",
                                    r"cleaning", r"disinfect(ant|ing)", r"paper towels?", r"toilet paper", r"bath tissue",
                                    r"bounty", r"charmin", r"trash bags?", r"garbage bags?", r"glad", r"febreze",
                                    r"swiffer", r"laundry", r"dryer sheets", r"sponges?", r"ziploc", r"bleach",
                                    r"air freshener", r"downy", r"gain", r"cascade", r"mr\.? clean", r"scrub")),
]

DONT_BUY = [
    ("Computers", _words(r"laptops?", r"notebook computer", r"chromebooks?", r"macbook", r"desktop (computer|pc)",
                         r"gaming pc", r"pc tower", r"all-in-one (pc|computer)", r"mini pc")),
    ("Phone cases", _words(r"phone cases?", r"(iphone|galaxy|pixel|samsung|motorola)[\w\s+-]{0,25}\bcases?", r"case for (iphone|samsung|galaxy|pixel)",
                           r"screen protectors?", r"phone covers?")),
    ("Security cameras", _words(r"security cameras?", r"surveillance", r"cctv", r"doorbell cameras?", r"ip cameras?",
                                r"nvr", r"dvr", r"wireless camera system", r"spy cam(era)?")),
    ("Jewelry", _words(r"jewelry", r"jewellery", r"necklaces?", r"bracelets?", r"earrings?", r"pendants?", r"anklets?",
                       r"engagement rings?", r"wedding bands?", r"sterling silver", r"1[04]k gold", r"diamond rings?")),
    ("Precious metals", _words(r"bullion", r"gold bars?", r"silver bars?", r"silver coins?", r"gold coins?", r"troy oz",
                               r"silver rounds?", r"proof coins?", r"morgan dollar", r"silver eagle", r"numismatic")),
    ("Alcohol", _words(r"wine", r"beer", r"vodka", r"whiske?y", r"bourbon", r"tequila", r"rum", r"liquor", r"champagne",
                       r"sake", r"hard seltzer", r"gin", r"brandy", r"cognac")),
    ("Shoes", _words(r"shoes?", r"sneakers?", r"boots?", r"sandals?", r"slippers?", r"heels", r"loafers?", r"cleats",
                     r"flip[- ]flops?", r"clogs?", r"footwear")),
    ("Costumes", _words(r"costumes?", r"wigs?", r"cosplay", r"halloween mask")),
    ("Clothing", re.compile(CLOTHING_WORDS.pattern + r"|\b(hats?|caps|beanies?|scarf|scarves|gloves|belts?|handbags?|purses?)\b", re.I)),
    ("Automotive", _words(r"car", r"cars", r"truck", r"tires?", r"brake", r"headlights?", r"tail ?lights?", r"bumper",
                          r"motor oil", r"spark plugs?", r"wiper blades?", r"auto parts?", r"automotive", r"vehicle",
                          r"seat covers?", r"floor mats?", r"radiator", r"alternator", r"oem", r"jeep", r"ford", r"chevy",
                          r"toyota", r"honda", r"dodge", r"silverado")),
    ("Outdoor & Camping", _words(r"tents?", r"sleeping bags?", r"camping", r"hiking", r"backpacking", r"trekking poles?",
                                 r"camp stove", r"hammocks?", r"fishing", r"hunting")),
    ("Books", _words(r"books?", r"paperback", r"hardcover", r"novel", r"textbook", r"isbn", r"workbook")),
    ("Movies & Music", _words(r"dvd", r"blu-?ray", r"cd", r"vinyl", r"lp", r"soundtrack", r"album", r"4k uhd")),
    ("Bedding & Linens", _words(r"sheet sets?", r"bed sheets?", r"pillows?", r"pillowcases?", r"comforters?", r"duvet",
                                r"blankets?", r"quilts?", r"mattress (topper|pad|protector)", r"bedspreads?", r"linens?",
                                r"throws?", r"bath towels?", r"towel sets?", r"curtains?")),
    ("Furniture", _words(r"sofas?", r"couch(es)?", r"desks?", r"tables?", r"chairs?", r"dressers?", r"bookshel(f|ves)",
                         r"cabinets?", r"bed frames?", r"mattress(es)?", r"nightstands?", r"ottomans?", r"recliners?",
                         r"futons?", r"bar stools?", r"tv stands?", r"headboards?", r"furniture")),
    ("Home décor", _words(r"wall art", r"canvas prints?", r"posters?", r"ornaments?", r"figurines?", r"artificial (flowers?|plants?)",
                          r"wreaths?", r"vases?", r"d[eé]cor", r"wall decals?", r"picture frames?", r"tapestry")),
    ("Party supplies", _words(r"balloons?", r"party favors?", r"party supplies", r"party decorations?", r"banners?",
                              r"confetti", r"pi[nñ]atas?", r"cake toppers?", r"paper plates", r"party plates", r"streamers")),
    ("Medical equipment", _words(r"wheelchairs?", r"walkers?", r"rollators?", r"canes?", r"crutch(es)?", r"hospital beds?",
                                 r"blood pressure monitors?", r"oximeters?", r"nebulizers?", r"cpap", r"mobility scooters?",
                                 r"commodes?", r"shower chairs?", r"knee scooters?", r"stethoscopes?")),
]

# Neither buy nor don't-buy (yet): they don't count for or against a store.
NEUTRAL = [
    ("Grocery", _words(r"snacks?", r"chips", r"cereal", r"candy", r"chocolate", r"coffee", r"tea", r"cookies?",
                       r"crackers?", r"gum", r"soda", r"juice", r"sauce", r"seasoning", r"spices?", r"pasta", r"jerky")),
    ("Electronics", _words(r"headphones?", r"earbuds?", r"chargers?", r"charging", r"cables?", r"speakers?", r"bluetooth",
                           r"power banks?", r"smart ?watch", r"tablets?", r"tv", r"television", r"keyboard", r"mouse")),
]

BUY_NAMES = {n for n, _ in BUY}
DONT_BUY_NAMES = {n for n, _ in DONT_BUY}


def classify(text):
    if USED.search(text or ""):
        return "Used/Refurbished"
    for name, rx in BUY + DONT_BUY + NEUTRAL:
        if rx.search(text or ""):
            return name
    return None


def store_mix(s):
    counts = {}
    for t in s.get("products", {}).values():
        c = classify(t)
        if c:
            counts[c] = counts.get(c, 0) + 1
    return counts


def describe_mix(counts):
    return " · ".join(f"{k} {v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1]))


def rule_store_categories(s):
    """Reject only stores whose main merchandise is what we don't buy.
    A store with a meaningful amount of what we buy (3+ products or 25%+) is kept."""
    counts = store_mix(s)
    buy = sum(v for k, v in counts.items() if k in BUY_NAMES)
    dont = {k: v for k, v in counts.items() if k in DONT_BUY_NAMES or k == "Used/Refurbished"}
    classified = sum(counts.values())
    if classified < 3 or buy >= 3 or buy / classified >= 0.25:
        return None
    if sum(dont.values()) / classified >= 0.6:
        top = ", ".join(k for k, _ in sorted(dont.items(), key=lambda kv: -kv[1])[:2])
        return f"Mostly sells what we don't buy: {top} ({sum(dont.values())} of {classified} products)"
    return None


# Numbers we already have. Kept only on this computer in numbers.db (a small
# database next to app.py), never on GitHub. Each number remembers the rest of the
# spreadsheet row it came from, which file, and when. Good sellers found by the app
# are added automatically.
DB_FILE = HERE / "numbers.db"
OLD_KNOWN_FILE = HERE / "known_numbers.txt"  # versions 7-8 kept bare numbers here
PHONE_RE = re.compile(r"(?<!\d)(?:\+?1[\s.\-]?)?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}(?!\d)")
DB_LOCK = threading.Lock()


def normalize_phone(p):
    digits = re.sub(r"\D", "", str(p or ""))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) == 10 else ""


def pretty_phone(p):
    return f"({p[:3]}) {p[3:6]}-{p[6:]}" if len(p) == 10 else p


def db():
    con = sqlite3.connect(str(DB_FILE))
    con.execute("CREATE TABLE IF NOT EXISTS numbers (phone TEXT PRIMARY KEY, details TEXT NOT NULL DEFAULT '', "
                "source TEXT NOT NULL DEFAULT '', added TEXT NOT NULL DEFAULT '')")
    return con


def add_known(entries, source):
    """entries: (phone, details) pairs. Returns how many numbers were new."""
    today = datetime.now().strftime("%Y-%m-%d")
    rows = {}
    for phone, details in entries:
        if phone and (phone not in rows or not rows[phone]):
            rows[phone] = details or ""
    new = [p for p in rows if p not in KNOWN]
    with DB_LOCK:
        con = db()
        with con:
            con.executemany(
                "INSERT INTO numbers (phone, details, source, added) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(phone) DO UPDATE SET details = CASE WHEN numbers.details = '' "
                "THEN excluded.details ELSE numbers.details END",
                [(p, d, source, today) for p, d in rows.items()])
        con.close()
        KNOWN.update(rows)
    return len(new)


def load_known():
    con = db()
    known = {r[0] for r in con.execute("SELECT phone FROM numbers")}
    con.close()
    return known


KNOWN = load_known()
RUN_PHONES = {}  # phone -> seller name, for good sellers in the current search



# Rejections that say something real about the seller. Their numbers are saved and
# blocked from future searches. Others (like a seller page that didn't load) are not.
BLOCKING_REASONS = ("Not a US seller", "Mostly sells what we don't buy", "Clothing-only store")


def blocking_reason(reason):
    return any(part.strip().startswith(BLOCKING_REASONS) for part in (reason or "").split(";"))


def recover_numbers():
    """Make sure nothing found earlier is missing from the database: numbers versions 7-8
    kept in known_numbers.txt, and good and rejected sellers in every saved results spreadsheet."""
    for f in sorted(HERE.glob("walmart_sellers_*.csv")):
        try:
            rows = list(csv.DictReader(io.StringIO(f.read_text(encoding="utf-8", errors="ignore"))))
        except Exception:
            continue
        found, rejected = [], []
        for r in rows:
            phone = normalize_phone(r.get("phone"))
            if not phone or phone in KNOWN:
                continue
            details = " · ".join(x for x in (r.get("seller_name"), r.get("address"), r.get("seller_url")) if x)
            if r.get("status") == "Good":
                found.append((phone, details))
            elif r.get("status") == "Rejected" and blocking_reason(r.get("reason")):
                rejected.append((phone, f"Rejected: {r['reason']} · {details}"))
        if found:
            add_known(found, f"Found by app ({f.name})")
        if rejected:
            add_known(rejected, f"Rejected by app ({f.name})")
    if OLD_KNOWN_FILE.exists():
        old = [l.strip() for l in OLD_KNOWN_FILE.read_text().splitlines() if l.strip()]
        add_known([(p, "") for p in old if p not in KNOWN], "Earlier upload")


recover_numbers()


# ========================================================== NEW LEADS INBOX
# Good sellers wait here across searches and restarts until marked "added to queue".

def leads_db():
    con = db()
    con.execute("CREATE TABLE IF NOT EXISTS leads (key TEXT PRIMARY KEY, data TEXT NOT NULL, "
                "status TEXT NOT NULL DEFAULT 'new', found TEXT NOT NULL DEFAULT '', added TEXT NOT NULL DEFAULT '')")
    return con


def lead_key(r):
    return normalize_phone(r.get("phone")) or r.get("seller_url") or (r.get("seller_name") or "").lower()


def add_lead(r, found=None):
    with DB_LOCK:
        con = leads_db()
        with con:
            con.execute("INSERT OR IGNORE INTO leads (key, data, found) VALUES (?, ?, ?)",
                        (lead_key(r), json.dumps(r), found or datetime.now().strftime("%Y-%m-%d %H:%M")))
        con.close()


def get_leads(status):
    con = leads_db()
    rows = con.execute("SELECT key, data, found, added FROM leads WHERE status = ? ORDER BY found DESC, key",
                       (status,)).fetchall()
    con.close()
    out = []
    for key, data, found, added in rows:
        r = json.loads(data)
        r.update(key=key, found_on=found, added_on=added)
        out.append(r)
    return out


def mark_leads(keys, status):
    when = datetime.now().strftime("%Y-%m-%d %H:%M") if status == "added" else ""
    with DB_LOCK:
        con = leads_db()
        with con:
            con.executemany("UPDATE leads SET status = ?, added = ? WHERE key = ?", [(status, when, k) for k in keys])
        con.close()


def lead_counts():
    con = leads_db()
    counts = dict(con.execute("SELECT status, COUNT(*) FROM leads GROUP BY status").fetchall())
    con.close()
    return counts


def seed_leads():
    """First time the inbox exists: fill it with the good sellers from the latest search."""
    con = db()
    exists = con.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'leads'").fetchone()
    con.close()
    if exists:
        return
    leads_db().close()
    files = sorted(HERE.glob("walmart_sellers_*.csv"), key=lambda f: f.stat().st_mtime)
    if not files:
        return
    found = datetime.fromtimestamp(files[-1].stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    for r in csv.DictReader(io.StringIO(files[-1].read_text(encoding="utf-8", errors="ignore"))):
        if r.get("status") == "Good":
            add_lead(r, found)


seed_leads()


# =========================================================== SEARCH HISTORY
# One row per search word: when it was last searched, how it went, and when to check again.

def history_db():
    con = db()
    con.execute("CREATE TABLE IF NOT EXISTS searches (keyword TEXT PRIMARY KEY, last_run TEXT NOT NULL, "
                "runs INTEGER NOT NULL DEFAULT 0, pages INTEGER NOT NULL DEFAULT 0, sellers INTEGER NOT NULL DEFAULT 0, "
                "new_leads INTEGER NOT NULL DEFAULT 0, rejected INTEGER NOT NULL DEFAULT 0, "
                "already_have INTEGER NOT NULL DEFAULT 0, total_new_leads INTEGER NOT NULL DEFAULT 0)")
    return con


def check_again_days(new_leads):
    return 7 if new_leads >= 5 else 14 if new_leads >= 1 else 30


def keyword_stats(rows, kw):
    """rows: seller rows (dicts) from one search. Counts the ones this search word found."""
    mine = [r for r in rows if kw in [k.strip().lower() for k in
                                      (r["keywords"] if isinstance(r["keywords"], (set, list)) else
                                       str(r.get("keywords") or "").split(";"))]]
    already = sum(1 for r in mine if "Already have this number" in (r.get("reason") or ""))
    good = sum(1 for r in mine if r.get("status") == "Good")
    rejected = sum(1 for r in mine if r.get("status") == "Rejected") - already
    return len(mine), good, rejected, already


def record_search(kw, when, pages, rows):
    kw = kw.strip().lower()
    sellers, good, rejected, already = keyword_stats(rows, kw)
    with DB_LOCK:
        con = history_db()
        with con:
            con.execute(
                "INSERT INTO searches (keyword, last_run, runs, pages, sellers, new_leads, rejected, already_have, total_new_leads) "
                "VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?) ON CONFLICT(keyword) DO UPDATE SET last_run = excluded.last_run, "
                "runs = runs + 1, pages = excluded.pages, sellers = excluded.sellers, new_leads = excluded.new_leads, "
                "rejected = excluded.rejected, already_have = excluded.already_have, "
                "total_new_leads = total_new_leads + excluded.new_leads",
                (kw, when, pages, sellers, good, rejected, already, good))
        con.close()


def get_history():
    con = history_db()
    rows = con.execute("SELECT keyword, last_run, runs, pages, sellers, new_leads, rejected, already_have, total_new_leads "
                       "FROM searches").fetchall()
    con.close()
    out = []
    for kw, last, runs, pages, sellers, good, rejected, already, total in rows:
        days = check_again_days(good)
        due = datetime.strptime(last[:10], "%Y-%m-%d").toordinal() + days
        out.append({"keyword": kw, "last_run": last, "runs": runs, "pages": pages, "sellers": sellers, "new_leads": good,
                    "rejected": rejected, "already_have": already, "total_new_leads": total, "check_every": days,
                    "check_again": datetime.fromordinal(due).strftime("%Y-%m-%d"),
                    "due": due <= datetime.now().toordinal()})
    out.sort(key=lambda h: (not h["due"], h["check_again"]))
    return out


def seed_history():
    """First time the history exists: fill it in from earlier searches' spreadsheets."""
    con = db()
    exists = con.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'searches'").fetchone()
    con.close()
    if exists:
        return
    history_db().close()
    for f in sorted(HERE.glob("walmart_sellers_*.csv"), key=lambda f: f.stat().st_mtime):
        try:
            rows = list(csv.DictReader(io.StringIO(f.read_text(encoding="utf-8", errors="ignore"))))
        except Exception:
            continue
        if not rows or "status" not in rows[0]:
            continue
        when = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        for kw in sorted({k.strip().lower() for r in rows for k in str(r.get("keywords") or "").split(";") if k.strip()}):
            record_search(kw, when, 0, rows)


seed_history()


def lookup(q, limit=50):
    q = (q or "").strip()
    digits = re.sub(r"\D", "", q)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) >= 3 and not re.search(r"[A-Za-z]", q):
        where, arg = "phone LIKE ?", f"%{digits}%"
    elif len(q) >= 2:
        where, arg = "details LIKE ? OR source LIKE ?", f"%{q}%"
    else:
        return {"total": 0, "rows": []}
    con = db()
    total = con.execute(f"SELECT COUNT(*) FROM numbers WHERE {where}", (arg,) * where.count("?")).fetchone()[0]
    rows = con.execute(f"SELECT phone, details, source, added FROM numbers WHERE {where} ORDER BY phone LIMIT {limit}",
                       (arg,) * where.count("?")).fetchall()
    con.close()
    return {"total": total, "rows": [{"phone": pretty_phone(p), "details": d, "source": s, "added": a}
                                     for p, d, s, a in rows]}


XL = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def xlsx_rows(raw):
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        names = z.namelist()
        shared = []
        if "xl/sharedStrings.xml" in names:
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(XL + "si"):
                shared.append("".join(t.text or "" for t in si.iter(XL + "t")))
        for n in sorted(n for n in names if n.startswith("xl/worksheets/sheet")):
            for r in ET.fromstring(z.read(n)).iter(XL + "row"):
                cells = []
                for c in r.iter(XL + "c"):
                    v = c.find(XL + "v")
                    if c.get("t") == "s" and v is not None:
                        cells.append(shared[int(v.text)])
                    elif c.get("t") == "inlineStr":
                        cells.append("".join(t.text or "" for t in c.iter(XL + "t")))
                    elif v is not None and v.text:
                        val = v.text
                        if re.fullmatch(r"[\d.]+(E\+?\d+)?", val, re.I):  # 9175921495.0 or 9.175921495E9
                            try:
                                f = float(val)
                                val = str(int(f)) if f == int(f) else val
                            except ValueError:
                                pass
                        cells.append(val)
                yield cells


def phones_in_file(name, raw):
    """(phone, details) for every phone number in an uploaded .xlsx, .csv or .txt file,
    whatever column it's in. details is the rest of that row, labelled by the header row."""
    if name.lower().endswith(".xlsx"):
        rows = xlsx_rows(raw)
    elif name.lower().endswith(".csv"):
        rows = csv.reader(io.StringIO(raw.decode("utf-8-sig", "ignore")))
    else:
        rows = ([line] for line in raw.decode("utf-8", "ignore").splitlines())
    found, header = [], None
    for i, cells in enumerate(rows):
        cells = [str(c).strip() for c in cells]
        phones = [normalize_phone(m) for c in cells for m in PHONE_RE.findall(c)]
        phones = [p for p in phones if p]
        if not phones:
            if i == 0:
                header = cells
            continue
        parts = []
        for j, c in enumerate(cells):
            if c and not PHONE_RE.fullmatch(c):
                label = header[j] if header and j < len(header) and header[j] else ""
                parts.append(f"{label}: {c}" if label else c)
        details = " · ".join(parts)[:400]
        found.extend((p, details) for p in phones)
    return found


def rule_no_duplicate_phone(s):
    p = normalize_phone(s.get("phone"))
    if not p:
        return None
    other = RUN_PHONES.get(p)
    if other and other != s["seller_name"]:
        return f"Same phone as {other}"
    if p in KNOWN and other != s["seller_name"]:
        return "Already have this number"
    return None


RULES = [
    {"id": "us_only", "label": "Only US sellers", "check": rule_us_only},
    {"id": "store_categories", "label": "Only stores that sell what we buy", "check": rule_store_categories},
    {"id": "no_duplicate_phone", "label": "Skip numbers we already have", "check": rule_no_duplicate_phone},
]


# Brand ideas shown in the app's Search tab, grouped by the categories we buy.
BRAND_IDEAS = {
    "Toys & Games": ["LEGO", "Hasbro", "Mattel", "Barbie", "Hot Wheels", "Fisher-Price", "Nerf", "Play-Doh",
                     "Melissa & Doug", "Crayola", "Funko", "Squishmallows", "VTech", "Little Tikes", "Magna-Tiles",
                     "Board games", "Puzzles"],
    "Health & Beauty": ["CeraVe", "Neutrogena", "L'Oreal", "Maybelline", "Revlon", "Olay", "Cetaphil", "Aveeno",
                        "e.l.f.", "La Roche-Posay", "Burt's Bees", "NYX"],
    "OTC": ["Tylenol", "Advil", "Motrin", "Aleve", "Zyrtec", "Claritin", "Allegra", "Benadryl", "Mucinex",
            "Pepto-Bismol", "Tums", "Flonase"],
    "Personal Care": ["Dove", "Crest", "Colgate", "Oral-B", "Gillette", "Old Spice", "Head & Shoulders", "Pantene",
                      "Degree", "Secret", "Listerine", "Schick"],
    "Household Essentials": ["Tide", "Dawn", "Lysol", "Clorox", "Bounty", "Charmin", "Cascade", "Downy", "Gain",
                             "Febreze", "Swiffer", "Glad", "Ziploc", "Mr. Clean"],
    "Baby": ["Pampers", "Huggies", "Similac", "Enfamil", "Johnson's Baby", "Gerber", "Dr. Brown's", "Philips Avent",
             "Desitin", "Aquaphor Baby"],
    "Supplements": ["Nature Made", "Garden of Life", "Centrum", "One A Day", "Vitafusion", "Olly", "Nature's Bounty",
                    "Optimum Nutrition", "Emergen-C", "Airborne"],
    "Apparel basics": ["Hanes", "Fruit of the Loom", "Jockey", "Gildan socks"],
}


# "Search every corner": Walmart shows at most MAX_PAGES pages per search, so a big
# search is split into price ranges (each gets its own pages) plus related terms.
PRICE_BANDS = [(0, 10), (10, 20), (20, 35), (35, 60), (60, 100), (100, 200), (200, 100000)]
RELATED_TERMS = {
    "lego": ["lego star wars", "lego technic", "lego city", "lego friends", "lego duplo", "lego harry potter",
             "lego minecraft", "lego creator", "lego marvel", "lego ninjago", "lego disney", "lego icons",
             "lego classic", "lego speed champions", "lego architecture", "lego botanical"],
    "barbie": ["barbie dolls", "barbie dreamhouse", "barbie accessories", "barbie playset", "barbie car", "barbie clothes"],
    "hot wheels": ["hot wheels cars", "hot wheels track", "hot wheels monster trucks", "hot wheels 20 pack", "hot wheels premium"],
    "nerf": ["nerf blaster", "nerf darts", "nerf elite", "nerf fortnite", "nerf rival"],
    "funko": ["funko pop", "funko pop marvel", "funko pop disney", "funko pop anime", "funko soda"],
    "pokemon": ["pokemon cards", "pokemon booster box", "pokemon elite trainer box", "pokemon plush", "pokemon figures"],
    "hasbro": ["hasbro games", "monopoly", "play-doh", "transformers", "my little pony", "marvel legends", "peppa pig toys"],
    "mattel": ["mattel games", "uno", "polly pocket", "matchbox", "mega bloks", "jurassic world toys", "masters of the universe"],
    "fisher-price": ["fisher-price baby toys", "fisher-price little people", "fisher-price laugh and learn", "fisher-price imaginext"],
    "melissa & doug": ["melissa and doug puzzles", "melissa and doug wooden toys", "melissa and doug pretend play"],
    "squishmallows": ["squishmallows 8 inch", "squishmallows 12 inch", "squishmallows 16 inch", "squishmallows 24 inch"],
    "board games": ["family board games", "strategy board games", "kids board games", "card games", "party games"],
    "puzzles": ["1000 piece puzzle", "500 piece puzzle", "kids puzzles", "jigsaw puzzles"],
}


def search_url(term):
    return f"{BASE}/search?q={quote_plus(term)}"


def money(x):
    return f"${x:,.2f}".replace(".00", "")


def band_entry(kw, lo, hi):
    label = f"{kw} ({money(lo)}-{money(hi)})" if hi < 100000 else f"{kw} ({money(lo)}+)"
    return {"label": label, "url": f"{search_url(kw)}&min_price={lo:g}&max_price={hi:g}", "kw": kw, "band": (lo, hi)}


def split_band(lo, hi):
    """Two smaller price ranges, or None when the range is already under a dollar wide."""
    if hi - lo < 1:
        return None
    mid = round((lo * hi) ** 0.5, 2) if lo > 0 and hi > 4 * lo else round((lo + hi) / 2, 2)
    return (lo, mid), (mid, hi)


def search_plan(kw, every_corner):
    """The searches to run for one search word: {label, url, kw, band}."""
    if not every_corner:
        return [{"label": kw, "url": search_url(kw), "kw": kw, "band": None}]
    plan = [band_entry(kw, lo, hi) for lo, hi in PRICE_BANDS]
    plan += [{"label": t, "url": search_url(t), "kw": t, "band": None} for t in RELATED_TERMS.get(kw.strip().lower(), [])]
    return plan


# "Find hidden sellers": search results show one seller per product, but many products are
# sold by several sellers. Each product's "all sellers" page lists the rest.
OFFER_COUNT_KEY = re.compile(r"^(additional)?offers?count$|^numberofoffers$|^sellercount$|^totaloffers$", re.I)


def offer_signal(d):
    """How many sellers a search result says the product has, or None if it doesn't say."""
    for k, v in d.items():
        if OFFER_COUNT_KEY.match(k) and isinstance(v, (int, float)):
            return int(v) + (1 if k.lower().startswith("additional") else 0)
    return None


def parse_offers(data, pid):
    """Third-party sellers of product pid on its product or all-sellers page. Entries that belong
    to a different product (like "similar items" on the page) are skipped."""
    found = {}
    for d in walk(data):
        if d.get("usItemId") and str(d["usItemId"]) != pid:
            continue
        name = d.get("sellerName") or d.get("sellerDisplayName")
        sid = str(d.get("catalogSellerId") or d.get("sellerId") or "")
        if not name or not sid or name.strip().lower() in WALMART_FIRST_PARTY or sid in WALMART_FIRST_PARTY_IDS:
            continue
        found[name.strip().lower()] = {"seller_name": name.strip(), "catalog_seller_id": str(d.get("catalogSellerId") or "")}
    return list(found.values())


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
            "product": (d.get("name") or "") + (" (sponsored)" if d.get("isSponsoredFlag") or d.get("sponsoredProduct") else ""),
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


UPDATE_CHECK = {"last": time.time(), "busy": False}


def check_for_update():
    UPDATE_CHECK.update(last=time.time(), busy=True)
    try:
        text = fetch_latest()
        if text:
            STATE["update_available"] = re.search(r"^VERSION = (\d+)", text, re.M).group(1)
    finally:
        UPDATE_CHECK["busy"] = False


def maybe_check_for_update():
    """Called whenever the app page asks for its status: look for a new version at most every 5 minutes,
    so the Update now bar shows up soon after opening the app instead of up to 30 minutes later."""
    if not UPDATE_CHECK["busy"] and not STATE["update_available"] and time.time() - UPDATE_CHECK["last"] > 5 * 60:
        threading.Thread(target=check_for_update, daemon=True).start()


def watch_for_updates():
    while True:
        time.sleep(30 * 60)
        check_for_update()


# ============================================================== SCRAPER

STATE = {"running": False, "stop": False, "log": [], "sellers": [], "saved_file": "", "update_available": "", "summary": ""}
LOCK = threading.Lock()


def log(msg):
    with LOCK:
        STATE["log"].append(datetime.now().strftime("%H:%M:%S ") + msg)
        STATE["log"] = STATE["log"][-1000:]
        try:
            with open(HERE / "search_log.txt", "a", encoding="utf-8") as f:
                f.write(datetime.now().strftime("%Y-%m-%d ") + STATE["log"][-1] + "\n")
        except OSError:
            pass


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


def add_found_seller(sellers, name, catalog_id, kw, found_from, found_from_url, item_id, item_text):
    """Add a seller to this search's list (or note another product of theirs). Returns 1 if new."""
    key = name.lower()
    new = key not in sellers
    if new:
        sellers[key] = {"seller_name": name, "catalog_seller_id": "", "keywords": set(), "products": {},
                        "sample_product_url": found_from_url, "status": "Checking...",
                        "found_from": found_from, "found_from_url": found_from_url}
    rec = sellers[key]
    if str(catalog_id).isdigit():
        rec["catalog_seller_id"] = str(catalog_id)
    rec["keywords"].add(kw)
    if item_id:
        rec["products"][item_id] = item_text
    return int(new)


def find_hidden_sellers(br, products, sellers, stats):
    with_signal = [pid for pid, p in products.items() if p["signal"] is not None]
    if with_signal:  # Walmart says which products have more than one seller: only open those
        todo = [pid for pid in with_signal if products[pid]["signal"] > 1]
        log(f"Finding hidden sellers: {len(todo)} of {len(products)} products have more than one seller")
    else:
        todo = list(products)
        log(f"Finding hidden sellers: checking all {len(todo)} products")
    found_any = False
    stats["hidden_to_check"] = len(todo)
    for n, pid in enumerate(todo, 1):
        if STATE["stop"]:
            break
        p = products[pid]
        log(f"More sellers {n}/{len(todo)}: {p['name'][:60]}")
        html = br.get(f"{BASE}/product/{pid}/sellers")
        data = next_data(html) if html else None
        if not data:
            html = br.get(p["url"])
            data = next_data(html) if html else None
        offers = parse_offers(data, pid) if data else []
        found_any = found_any or bool(offers)
        new = sum(add_found_seller(sellers, o["seller_name"], o["catalog_seller_id"], p["kw"],
                                   f'"{p["label"]}" more sellers on: {p["name"]}', p["url"], pid, p["name"])
                  for o in offers)
        stats["hidden_checked"] = n
        stats["hidden_found"] += new
        if new:
            log(f"  found {new} hidden sellers ({len(sellers)} total)")
            publish(sellers)
        if n == 10 and not found_any:
            log("  Couldn't find Walmart's all-sellers lists on these pages, so skipping hidden sellers")
            stats["hidden_note"] = "Walmart's more-sellers lists couldn't be read, so this step was skipped"
            break


def run_scrape(keywords, pages, active_rules, delay, every_corner=False, hidden_sellers=False):
    sellers = {}
    RUN_PHONES.clear()
    searched = []  # search words that actually ran, for the search history
    started = datetime.now().strftime("%Y-%m-%d %H:%M")
    br = None
    try:
        br = Browser(delay)
        # Step 1: find sellers in search results.
        products = {}  # every product seen: id -> {name, url, kw, label, sellers}
        stats = {"searches": 0, "pages": 0, "splits": 0, "price_filter": "not used", "hidden_to_check": 0,
                 "hidden_checked": 0, "hidden_found": 0, "hidden_note": "", "blocked_pages": 0}
        for kw in keywords:
            if STATE["stop"]:
                break
            searched.append(kw)
            plan = search_plan(kw, every_corner)
            if every_corner:
                log(f'Searching every corner of "{kw}": {len(plan)} searches to start with')
            first_ids = None  # first-page products of the first price range, to spot an ignored price filter
            skip_bands = False
            i = 0
            while i < len(plan) and not STATE["stop"]:
                entry = plan[i]
                i += 1
                label, band = entry["label"], entry["band"]
                if band and skip_bands:
                    continue
                pages_with_results, last_page = 0, None
                stats["searches"] += 1
                for page in range(1, pages + 1):
                    if STATE["stop"]:
                        break
                    log(f'Searching "{label}" - page {page}')
                    html = br.get(f"{entry['url']}&page={page}")
                    data = next_data(html) if html else None
                    # Stop when Walmart runs out of products (not just out of third-party sellers:
                    # a page can be all Walmart's own items and the next page still have sellers).
                    items = [d for d in walk(data) if d.get("usItemId") and d.get("name")] if data else []
                    ids = {str(d["usItemId"]) for d in items}
                    if not ids:
                        log(f'  no more results for "{label}"' if html else f"  couldn't load page {page}, moving on")
                        stats["blocked_pages"] += 0 if html else 1
                        break
                    stats["pages"] += 1
                    if band and page == 1:
                        if first_ids is not None and ids == first_ids:
                            log("  Walmart ignored the price filter, so skipping the other price ranges")
                            stats["price_filter"] = "ignored by Walmart"
                            skip_bands = True
                            break
                        if first_ids is not None and stats["price_filter"] == "not used":
                            stats["price_filter"] = "worked"
                        first_ids = first_ids if first_ids is not None else ids
                    pages_with_results += 1
                    last_page = first(data, "maxPage")
                    for d in items:
                        products.setdefault(str(d["usItemId"]), {
                            "name": d.get("name") or "", "kw": kw, "label": label, "signal": offer_signal(d),
                            "url": BASE + d["canonicalUrl"] if d.get("canonicalUrl") else f"{BASE}/ip/{d['usItemId']}"})
                    new = 0
                    for h in parse_search(data):
                        new += add_found_seller(sellers, h["seller_name"], h["catalog_seller_id"], kw,
                                                f'"{label}" search: {h["product"]}', h["product_url"],
                                                h["item_id"], h["item_text"])
                    log(f"  found {new} new sellers ({len(sellers)} total)")
                    publish(sellers)
                    if str(last_page).isdigit() and page >= int(last_page):
                        log(f'  that was the last page for "{label}"')
                        break
                # Hit Walmart's page limit inside a price range? Split it so nothing is cut off.
                capped = pages == MAX_PAGES and pages_with_results == MAX_PAGES and not (
                    str(last_page).isdigit() and int(last_page) < MAX_PAGES)
                if band and capped and not skip_bands:
                    halves = split_band(*band)
                    if halves:
                        log(f'  "{label}" has more than {MAX_PAGES} pages, so splitting it into smaller price ranges')
                        plan[i:i] = [band_entry(entry["kw"], lo, hi) for lo, hi in halves]
                        stats["splits"] += 1

        # Step 1b: open each product's all-sellers list to find sellers search doesn't show.
        from_search = len(sellers)
        if hidden_sellers and products and not STATE["stop"]:
            find_hidden_sellers(br, products, sellers, stats)
        parts = [f"{stats['searches']} searches", f"{stats['pages']} pages", f"{len(products):,} products seen",
                 f"{from_search} sellers from search results"]
        if every_corner:
            parts.append(f"price search {stats['price_filter']}")
            parts.append(f"{stats['splits']} price ranges split")
        if hidden_sellers:
            parts.append(stats["hidden_note"] or f"hidden sellers: checked {stats['hidden_checked']} of "
                         f"{stats['hidden_to_check']} products, found {stats['hidden_found']} more sellers")
        if stats["blocked_pages"]:
            parts.append(f"{stats['blocked_pages']} pages didn't load")
        STATE["summary"] = " · ".join(parts)
        log("Summary: " + STATE["summary"])

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
            rec["store_mix"] = describe_mix(store_mix(rec))
            reasons = [r["check"](rec) for r in RULES if r["id"] in active_rules]
            reasons = [r for r in reasons if r]
            rec["status"] = "Rejected" if reasons else "Good"
            rec["reason"] = "; ".join(reasons)
            if rec["status"] == "Good":
                add_lead(row(rec))
            phone = normalize_phone(rec.get("phone"))
            if rec["status"] == "Good" and phone:
                RUN_PHONES[phone] = rec["seller_name"]
                add_known([(phone, f"{rec['seller_name']} · {rec.get('address', '')} · {rec.get('seller_url', '')}")],
                          "Found by app")
            elif phone and phone not in KNOWN and blocking_reason(rec["reason"]):
                add_known([(phone, f"Rejected: {rec['reason']} · {rec['seller_name']} · {rec.get('address', '')} · "
                                   f"{rec.get('seller_url', '')}")], "Rejected by app")
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
        try:
            for kw in searched:
                record_search(kw, started, pages, list(sellers.values()))
        except Exception as e:
            log(f"Couldn't save search history: {e}")
        with LOCK:
            STATE["running"] = False
            STATE["stop"] = False


COLUMNS = ["status", "reason", "seller_name", "business_name", "phone", "email", "address", "country",
           "rating", "review_count", "seller_url", "catalog_seller_id", "products_checked", "store_mix", "keywords",
           "found_from", "found_from_url"]


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
    w = csv.DictWriter(buf, fieldnames=COLUMNS, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


def load_last_results():
    """Show the most recent search's results again after the app restarts."""
    files = sorted(HERE.glob("walmart_sellers_*.csv"), key=lambda f: f.stat().st_mtime)
    if not files:
        return
    try:
        rows = list(csv.DictReader(io.StringIO(files[-1].read_text(encoding="utf-8", errors="ignore"))))
    except Exception:
        return
    if rows and "status" in rows[0]:
        STATE["sellers"] = rows
        STATE["saved_file"] = files[-1].name


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
:root{--bg:#f4f6f9;--card:#fff;--text:#1b2230;--muted:#667085;--line:#e4e7ec;--soft:#f2f4f7;
  --blue:#0071dc;--blue-soft:#e8f2fd;--green:#12805c;--green-soft:#e7f6ef;--red:#c4320a;--red-soft:#fdeee9;--amber:#b54708;--amber-soft:#fef4e6}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
.wrap{max-width:1100px;margin:0 auto;padding:20px 16px 60px}
header{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:16px}
header h1{font-size:22px;margin:0}header h1 small{font-size:13px;color:var(--muted);font-weight:500;margin-left:6px}
button{font:inherit;font-weight:600;border:0;border-radius:10px;padding:9px 16px;cursor:pointer;background:var(--soft);color:var(--text)}
button:hover{filter:brightness(.97)}button:disabled{opacity:.45;cursor:default}
.primary{background:var(--blue);color:#fff}.big{font-size:17px;padding:14px 28px}
.ok{background:var(--green-soft);color:var(--green)}.small{font-size:13px;padding:6px 12px}
a{color:var(--blue)}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:20px;margin-bottom:14px}
h2{font-size:19px;margin:0 0 2px}h3{font-size:15px;margin:0 0 8px}
.sub{color:var(--muted);font-size:14px;margin:0 0 14px}
.muted{color:var(--muted);font-size:13px}
nav{display:flex;gap:6px;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:6px;margin-bottom:14px;overflow-x:auto}
nav button{flex:1;background:none;padding:11px 10px;white-space:nowrap;color:var(--muted)}
nav button.on{background:var(--text);color:#fff}
.badge{display:inline-block;min-width:22px;padding:1px 7px;border-radius:99px;font-size:12px;background:var(--soft);color:var(--text);margin-left:6px}
nav button.on .badge{background:rgba(255,255,255,.2);color:#fff}
.badge.hot{background:var(--blue);color:#fff}
.section{display:none}.section.on{display:block}
.pills{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:14px}
.pill{background:var(--soft);padding:7px 14px;font-size:14px}.pill.on{background:var(--blue-soft);color:var(--blue)}
.bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:12px}.bar .grow{flex:1}
.tablewrap{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:14px}
th{color:var(--muted);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.02em;text-align:left;padding:8px 10px;border-bottom:1px solid var(--line)}
td{padding:12px 10px;border-bottom:1px solid var(--line);vertical-align:top}
tr:last-child td{border-bottom:0}
.name{font-weight:650;font-size:15px}
.tag{white-space:nowrap;display:inline-block;font-size:12px;padding:2px 8px;border-radius:99px;background:var(--soft);color:var(--muted);margin:3px 4px 0 0}
.why{color:var(--red);font-size:13px}
.empty{color:var(--muted);text-align:center;padding:36px 10px}
textarea,input[type=search],select{width:100%;padding:11px 12px;border:1px solid var(--line);border-radius:10px;font:inherit;background:#fff}
textarea{height:120px;resize:vertical}
.cols{display:grid;grid-template-columns:3fr 2fr;gap:18px}@media(max-width:820px){.cols{grid-template-columns:1fr}}
.check{display:flex;gap:9px;align-items:center;margin:8px 0}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin:6px 0 14px}
.chip{background:#fff;border:1px solid var(--line);padding:5px 11px;font-size:13px;font-weight:500;border-radius:99px}
.chip.picked{background:var(--blue);border-color:var(--blue);color:#fff}
.chip.done{color:var(--muted)}.chip.done::after{content:" ✓"}
.chip.due{border-color:var(--amber);color:var(--amber)}.chip.due::after{content:" · due"}
.cat{font-size:12px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.03em}
.banner{display:none;align-items:center;gap:14px;flex-wrap:wrap;border-radius:14px;padding:14px 18px;margin-bottom:14px}
.banner.run{background:var(--blue-soft);border:1px solid #b9d7f7}.banner.upd{background:var(--green-soft);border:1px solid #b5e3cd}
.progress{height:8px;background:#fff;border-radius:99px;overflow:hidden;margin-top:6px;width:100%}
.progress div{height:100%;background:var(--blue);width:0;transition:width .4s}
.answer{border-radius:12px;padding:14px 16px;margin-top:12px;font-weight:600}
.answer.have{background:var(--red-soft);color:var(--red)}.answer.new{background:var(--green-soft);color:var(--green)}
.stat{font-size:28px;font-weight:700}
details summary{cursor:pointer;color:var(--muted);font-size:14px;margin-top:10px}
#log{background:#11151c;color:#cfd6e0;font:12px/1.5 ui-monospace,Menlo,monospace;height:160px;overflow:auto;border-radius:10px;padding:10px;white-space:pre-wrap;margin-top:8px}
.due{color:var(--amber);font-weight:700}
</style></head><body><div class="wrap">

<header><h1>Walmart Seller Finder<small>version __VERSION__</small></h1><button id="quit" class="small" style="white-space:nowrap">Quit app</button></header>

<div id="update" class="banner upd"><b style="flex:1">A new version of the app is ready.</b><button id="updbtn" class="primary">Update now</button></div>

<div id="running" class="banner run">
  <div style="flex:1;min-width:220px">
    <b id="runTitle">Searching Walmart...</b>
    <div class="muted" id="runDetail"></div>
    <div class="progress"><div id="runBar"></div></div>
    <div class="muted" style="margin-top:6px">Chrome works minimized in your Dock. If it pops up asking you to "Press &amp; Hold", hold the button and it carries on.</div>
  </div>
  <button id="stop">Stop</button>
</div>

<nav>
  <button data-s="leads" class="on">Leads<span class="badge" id="nLeadsNav">0</span></button>
  <button data-s="search">Search</button>
  <button data-s="history">History<span class="badge" id="nDueNav" style="display:none">0</span></button>
  <button data-s="numbers">Numbers<span class="badge" id="nKnownNav">0</span></button>
</nav>

<!-- ============ LEADS ============ -->
<section id="s-leads" class="section on"><div class="card">
  <h2>Leads</h2>
  <p class="sub">Good sellers from all your searches. They stay here until you mark them as added to a sales rep's queue.</p>
  <div class="bar">
    <div class="pills" style="margin:0">
      <button class="pill on" data-l="new">New <span id="nLeads">0</span></button>
      <button class="pill" data-l="added">Added to queue <span id="nAdded">0</span></button>
    </div>
    <span class="grow"></span>
    <button id="markAll" class="small">Mark all as added</button>
    <button class="small" onclick="location='/download?which=leads'" id="dlLeads">Download new leads</button>
  </div>
  <div class="tablewrap"><table><thead><tr><th>Seller</th><th>Contact</th><th>Location</th><th>Rating</th><th></th></tr></thead>
    <tbody id="leadRows"></tbody></table></div>
</div></section>

<!-- ============ SEARCH ============ -->
<section id="s-search" class="section">
<div class="card">
  <h2>New search</h2>
  <p class="sub">Type what to search, or click brands below. The app finds the sellers and checks them against your rules.</p>
  <div class="cols">
    <div>
      <h3>1. What to search</h3>
      <textarea id="kw" placeholder="One per line, for example:&#10;lego&#10;cerave&#10;tide pods"></textarea>
      <div class="muted" style="margin-top:4px">Tip: brands you've already searched show a ✓. Brands due for another look show "due".</div>
    </div>
    <div>
      <h3>2. How deep</h3>
      <select id="pages">
        <option value="1">Quick: 1 page per search (~40 products)</option>
        <option value="3" selected>Normal: 3 pages per search</option>
        <option value="5">Deep: 5 pages per search</option>
        <option value="10">Very deep: 10 pages per search</option>
        <option value="25">All pages: keep going until Walmart runs out (up to 25)</option>
      </select>
      <label class="check" style="align-items:flex-start;margin-top:12px"><input type="checkbox" id="everyCorner" style="margin-top:4px">
        <span><b>Search every corner</b><br><span class="muted">Splits each search by price and also searches related terms (like "lego star wars"), so it finds far more sellers. Takes much longer: use it with "All pages".</span></span></label>
      <label class="check" style="align-items:flex-start"><input type="checkbox" id="hiddenSellers" style="margin-top:4px">
        <span><b>Find hidden sellers</b><br><span class="muted">Many products are sold by several sellers, but search only shows one. This opens each product's "more sellers" list. Finds many more sellers, but much slower.</span></span></label>
      <h3 style="margin-top:16px">3. Rules</h3>
      <div id="rules"></div>
    </div>
  </div>
  <div style="margin-top:16px"><button id="start" class="primary big">Start search</button></div>
</div>
<div class="card">
  <h2>Brand ideas</h2>
  <p class="sub">Brands from the categories you buy. Click one to add it to your search.</p>
  <div id="brands"></div>
</div>
<div class="card">
  <h2>Latest search results</h2>
  <p class="sub" id="lastInfo">Good sellers from here are also added to Leads automatically.</p>
  <div id="summary" class="answer" style="display:none;background:var(--blue-soft);color:var(--text);font-weight:500;margin:0 0 14px"></div>
  <div class="pills">
    <button class="pill on" data-r="Good">Good <span id="nGood">0</span></button>
    <button class="pill" data-r="Rejected">Rejected <span id="nRejected">0</span></button>
    <button class="pill" data-r="Checking...">Still checking <span id="nChecking">0</span></button>
    <span style="flex:1"></span>
    <button class="small" onclick="location='/download?which=all'">Download results</button>
  </div>
  <div class="tablewrap"><table><thead><tr><th>Seller</th><th>Contact</th><th>Location</th><th>Rating</th><th id="resLast"></th></tr></thead>
    <tbody id="resRows"></tbody></table></div>
  <details><summary>Show what the app is doing (details)</summary><div id="log"></div></details>
</div>
</section>

<!-- ============ HISTORY ============ -->
<section id="s-history" class="section"><div class="card">
  <h2>Search history</h2>
  <p class="sub">When to search a word again: <b>1 week</b> if it found 5+ new leads last time, <b>2 weeks</b> if 1–4, <b>1 month</b> if none.</p>
  <div class="bar"><span class="grow muted" id="dueInfo"></span><button id="searchDue" class="primary small">Search everything that's due</button></div>
  <div class="tablewrap"><table><thead><tr><th>Search word</th><th>Last searched</th><th>New leads last time</th><th>New leads ever</th><th>Search again</th><th></th></tr></thead>
    <tbody id="history"></tbody></table></div>
</div></section>

<!-- ============ NUMBERS ============ -->
<section id="s-numbers" class="section">
<div class="card">
  <h2>Look up a number</h2>
  <p class="sub">Type a phone number (any format) or a company name to see if we already have it.</p>
  <input id="lookup" type="search" placeholder="e.g. (917) 592-1495 or Toy Depot" autocomplete="off">
  <div id="lookupOut"></div>
</div>
<div class="card">
  <h2>Numbers we already have</h2>
  <div class="stat" id="nKnown">0</div>
  <p class="sub">Sellers with these numbers are skipped. Good and rejected sellers the app finds are added automatically. This list stays on this computer only.</p>
  <input type="file" id="knownFile" accept=".csv,.txt,.xlsx" style="display:none">
  <button class="primary" onclick="document.getElementById('knownFile').click()">Upload more numbers</button>
  <span id="knownMsg" class="muted" style="margin-left:10px"></span>
  <div class="muted" style="margin-top:8px">Excel (.xlsx), CSV or text files. Every phone number in the file is added, together with the rest of its row.</div>
</div>
</section>

</div>
<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
let data={sellers:[],log:[]}, leads={new:[],added:[]}, history=[], brands={}, leadView="new", resView="Good", wasRunning=false;

// ---- navigation
function show(s){
  document.querySelectorAll("nav button").forEach(b=>b.classList.toggle("on",b.dataset.s===s));
  document.querySelectorAll(".section").forEach(x=>x.classList.toggle("on",x.id==="s-"+s));
  window.scrollTo(0,0);
}
document.querySelectorAll("nav button").forEach(b=>b.onclick=()=>show(b.dataset.s));
document.querySelectorAll("[data-l]").forEach(b=>b.onclick=()=>{leadView=b.dataset.l;
  document.querySelectorAll("[data-l]").forEach(x=>x.classList.toggle("on",x===b));drawLeads();});
document.querySelectorAll("[data-r]").forEach(b=>b.onclick=()=>{resView=b.dataset.r;
  document.querySelectorAll("[data-r]").forEach(x=>x.classList.toggle("on",x===b));drawResults();});

// ---- shared row pieces
function sellerCell(x){
  return `<div class="name">${esc(x.seller_name)}</div>`+
    (x.business_name&&x.business_name!==x.seller_name?`<div class="muted">${esc(x.business_name)}</div>`:"")+
    (x.store_mix?`<div>${x.store_mix.split(" · ").map(t=>`<span class="tag">${esc(t)}</span>`).join("")}</div>`:"")+
    (x.found_from?`<div class="muted" style="margin-top:4px">Found from <a href="${esc(x.found_from_url)}" target="_blank">${esc(x.found_from)}</a></div>`:"");
}
const fmtPhone=p=>{const d=String(p||"").replace(/\D/g,"").replace(/^1(?=\d{10}$)/,"");return d.length===10?`(${d.slice(0,3)}) ${d.slice(3,6)}-${d.slice(6)}`:p;};
const contactCell=x=>`<div style="white-space:nowrap"><b>${esc(fmtPhone(x.phone))||'<span class="muted">No phone</span>'}</b></div><div class="muted">${esc(x.email)}</div>`;
const placeCell=x=>`<div style="max-width:220px">${esc(x.address)}</div>`;
const ratingCell=x=>x.rating?`<b>${esc((+x.rating).toFixed?.(1)??x.rating)}</b> ★${x.review_count?`<div class="muted">${esc(x.review_count)} reviews</div>`:""}`:`<span class="muted">–</span>`;
const storeBtn=x=>x.seller_url?`<a href="${esc(x.seller_url)}" target="_blank"><button class="small">Open store</button></a>`:"";

// ---- leads
async function loadLeads(){
  const [n,a]=await Promise.all(["new","added"].map(st=>fetch("/leads?status="+st).then(r=>r.json())));
  leads={new:n,added:a};drawLeads();
}
async function mark(keys,status){await fetch("/leads/mark",{method:"POST",body:JSON.stringify({keys,status})});await loadLeads();}
function drawLeads(){
  $("nLeads").textContent=leads.new.length;$("nAdded").textContent=leads.added.length;
  $("nLeadsNav").textContent=leads.new.length;$("nLeadsNav").classList.toggle("hot",leads.new.length>0);
  $("markAll").style.display=$("dlLeads").style.display=leadView==="new"&&leads.new.length?"":"none";
  const list=leads[leadView];
  $("leadRows").innerHTML=list.length?list.map(x=>`<tr>
    <td>${sellerCell(x)}</td><td>${contactCell(x)}</td><td>${placeCell(x)}</td><td>${ratingCell(x)}</td>
    <td style="white-space:nowrap;text-align:right">${storeBtn(x)}
      ${leadView==="new"?` <button class="ok small" data-key="${esc(x.key)}" data-to="added">Added to queue ✓</button><div class="muted" style="margin-top:6px">Found ${esc(x.found_on)}</div>`
        :` <button class="small" data-key="${esc(x.key)}" data-to="new">Undo</button><div class="muted" style="margin-top:6px">Added ${esc(x.added_on)}</div>`}</td></tr>`).join("")
    :`<tr><td colspan="5" class="empty">${leadView==="new"?"No new leads right now. Go to <b>Search</b> to find more.":"Nothing added to a queue yet."}</td></tr>`;
}
$("leadRows").onclick=e=>{const b=e.target.closest("button[data-key]");if(b)mark([b.dataset.key],b.dataset.to);};
$("markAll").onclick=()=>{if(leads.new.length&&confirm(`Mark all ${leads.new.length} new leads as added to the queue?`))mark(leads.new.map(x=>x.key),"added");};

// ---- search
fetch("/rules").then(r=>r.json()).then(rules=>{
  $("rules").innerHTML=rules.map(r=>`<label class="check"><input type="checkbox" value="${r.id}" checked> ${esc(r.label)}</label>`).join("");
});
const words=()=>$("kw").value.split("\n").map(s=>s.trim()).filter(Boolean);
function setWords(w){$("kw").value=w.join("\n");drawBrands();}
$("kw").oninput=()=>drawBrands();
$("start").onclick=async()=>{
  const keywords=words();
  if(!keywords.length){alert("Type at least one thing to search for, or click a brand.");return;}
  const rules=[...document.querySelectorAll("#rules input:checked")].map(i=>i.value);
  await fetch("/start",{method:"POST",body:JSON.stringify({keywords,rules,pages:+$("pages").value,every_corner:$("everyCorner").checked,hidden_sellers:$("hiddenSellers").checked})});
  resView="Good";document.querySelectorAll("[data-r]").forEach(x=>x.classList.toggle("on",x.dataset.r==="Good"));
  poll();
};
$("stop").onclick=()=>fetch("/stop",{method:"POST"});
function drawResults(){
  const s=data.sellers;
  for(const [k,id] of [["Good","nGood"],["Rejected","nRejected"],["Checking...","nChecking"]])$(id).textContent=s.filter(x=>x.status===k).length;
  $("resLast").textContent=resView==="Rejected"?"Why rejected":"";
  const list=s.filter(x=>x.status===resView);
  $("resRows").innerHTML=list.length?list.map(x=>`<tr>
    <td>${sellerCell(x)}</td><td>${contactCell(x)}</td><td>${placeCell(x)}</td><td>${ratingCell(x)}</td>
    <td style="text-align:right">${resView==="Rejected"?`<div class="why" style="text-align:left;max-width:240px">${esc(x.reason)}</div>`:storeBtn(x)}</td></tr>`).join("")
    :`<tr><td colspan="5" class="empty">Nothing here.</td></tr>`;
  $("summary").style.display=data.summary?"":"none";$("summary").textContent=data.summary?"Last search: "+data.summary:"";
  $("lastInfo").textContent=data.saved_file?`Saved to ${data.saved_file} in your walmart folder. Good sellers are also added to Leads.`:"Good sellers from here are also added to Leads automatically.";
}

// ---- brand ideas
fetch("/brands").then(r=>r.json()).then(b=>{brands=b;drawBrands();});
function drawBrands(){
  const picked=new Set(words().map(w=>w.toLowerCase())), hist={};
  history.forEach(h=>hist[h.keyword]=h);
  $("brands").innerHTML=Object.entries(brands).map(([cat,list])=>`<div class="cat">${esc(cat)}</div><div class="chips">`+
    list.map(b=>{const k=b.toLowerCase(),h=hist[k];
      const cls=picked.has(k)?"picked":h?(h.due?"due":"done"):"";
      return `<button class="chip ${cls}" data-b="${esc(b)}" title="${h?`Last searched ${esc(h.last_run)}`:"Not searched yet"}">${esc(b)}</button>`;}).join("")+`</div>`).join("");
}
$("brands").onclick=e=>{
  const b=e.target.closest("[data-b]");if(!b)return;
  const w=words(),k=b.dataset.b.toLowerCase(),i=w.findIndex(x=>x.toLowerCase()===k);
  if(i>=0)w.splice(i,1);else w.push(b.dataset.b);setWords(w);
};

// ---- history
async function loadHistory(){
  history=await (await fetch("/history")).json();
  const due=history.filter(h=>h.due);
  $("nDueNav").textContent=due.length;$("nDueNav").style.display=due.length?"":"none";$("nDueNav").classList.add("hot");
  $("dueInfo").textContent=due.length?`${due.length} search word${due.length>1?"s are":" is"} due for another look.`:"Nothing is due right now.";
  $("searchDue").style.display=due.length?"":"none";
  $("history").innerHTML=history.length?history.map(h=>`<tr>
    <td class="name">${esc(h.keyword)}</td>
    <td style="white-space:nowrap">${esc(h.last_run.slice(0,10))}<div class="muted">searched ${h.runs} time${h.runs>1?"s":""}</div></td>
    <td><b>${h.new_leads}</b> <span class="muted">of ${h.sellers} sellers</span></td>
    <td>${h.total_new_leads}</td>
    <td style="white-space:nowrap">${h.due?`<span class="due">Due now</span>`:esc(h.check_again)}</td>
    <td style="text-align:right"><button class="small" data-kw="${esc(h.keyword)}">Search again</button></td></tr>`).join("")
    :`<tr><td colspan="6" class="empty">No searches yet.</td></tr>`;
  drawBrands();
}
function addAndGo(list){const w=words();list.forEach(k=>{if(!w.some(x=>x.toLowerCase()===k))w.push(k);});setWords(w);show("search");}
$("history").onclick=e=>{const b=e.target.closest("button[data-kw]");if(b)addAndGo([b.dataset.kw]);};
$("searchDue").onclick=()=>addAndGo(history.filter(h=>h.due).map(h=>h.keyword));

// ---- numbers
let lookupTimer;
$("lookup").oninput=e=>{
  clearTimeout(lookupTimer);
  lookupTimer=setTimeout(async()=>{
    const q=e.target.value.trim(),out=$("lookupOut");
    if(q.length<2){out.innerHTML="";return;}
    const r=await (await fetch("/lookup?q="+encodeURIComponent(q))).json();
    if(!r.total){out.innerHTML=`<div class="answer new">✓ Not in our numbers. We don't have this one yet.</div>`;return;}
    out.innerHTML=`<div class="answer have">We already have ${r.total===1?"this number":r.total+" matches"}${r.total>r.rows.length?` (showing first ${r.rows.length})`:""}</div>
      <div class="tablewrap"><table style="margin-top:8px"><thead><tr><th>Phone</th><th>Details</th><th>Where it came from</th><th>Added</th></tr></thead><tbody>`+
      r.rows.map(x=>`<tr><td style="white-space:nowrap"><b>${esc(x.phone)}</b></td><td>${esc(x.details)}</td><td>${esc(x.source)}</td><td style="white-space:nowrap">${esc(x.added)}</td></tr>`).join("")+
      `</tbody></table></div>`;
  },250);
};
$("knownFile").onchange=async e=>{
  const f=e.target.files[0];if(!f)return;
  $("knownMsg").textContent="Reading "+f.name+"...";
  const r=await (await fetch("/known/upload?name="+encodeURIComponent(f.name),{method:"POST",body:await f.arrayBuffer()})).json();
  $("knownMsg").textContent=r.error||`Found ${r.found.toLocaleString()} numbers in ${f.name}. ${r.added.toLocaleString()} were new.`;
  if(r.total!==undefined){$("nKnown").textContent=r.total.toLocaleString();$("nKnownNav").textContent=r.total.toLocaleString();}
  e.target.value="";
};

// ---- app controls
$("updbtn").onclick=async()=>{
  if(data.running){alert("Wait until the search is done (or press Stop) before updating.");return;}
  await fetch("/update",{method:"POST"});
  $("update").innerHTML="<b>Updating... the page will reload by itself.</b>";
  const wait=async()=>{try{await fetch("/status");location.reload();}catch(e){setTimeout(wait,1500);}};
  setTimeout(wait,3000);
};
$("quit").onclick=async()=>{
  if(!confirm("Turn the app off?"))return;
  await fetch("/quit",{method:"POST"});
  document.body.innerHTML='<div class="wrap"><h1>The app is off.</h1><p>You can close this tab. Double-click the Walmart Seller Finder icon to start it again.</p></div>';
};

// ---- live status
async function poll(){
  data=await (await fetch("/status")).json();
  $("start").disabled=data.running;$("start").textContent=data.running?"Search running...":"Start search";
  $("running").style.display=data.running?"flex":"none";
  if(data.running){
    const s=data.sellers,left=s.filter(x=>x.status==="Checking...").length,done=s.length-left,last=data.log[data.log.length-1]||"";
    const checking=/Checking seller/.test(last)||done>0;
    $("runTitle").textContent=checking?`Checking sellers: ${done} of ${s.length} done`:`Searching Walmart... ${s.length} sellers found so far`;
    $("runDetail").textContent=last.replace(/^\d\d:\d\d:\d\d /,"");
    $("runBar").style.width=(checking&&s.length?Math.round(100*done/s.length):5)+"%";
  }
  const log=$("log"),atBottom=log.scrollTop+log.clientHeight>=log.scrollHeight-5;
  log.textContent=data.log.join("\n");if(atBottom)log.scrollTop=log.scrollHeight;
  if(wasRunning&&!data.running)loadHistory();
  wasRunning=data.running;
  if(data.leads_new!==leads.new.length||data.leads_added!==leads.added.length)loadLeads();
  const k=(data.known_count||0).toLocaleString();$("nKnown").textContent=k;$("nKnownNav").textContent=k;
  $("update").style.display=data.update_available?"flex":"none";
  drawResults();
  setTimeout(poll,data.running?2000:30000);
}
loadLeads();loadHistory();poll();
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
            maybe_check_for_update()
            with LOCK:
                status = {k: STATE[k] for k in ("running", "log", "sellers", "saved_file", "update_available", "summary")}
                status["known_count"] = len(KNOWN)
                counts = lead_counts()
                status["leads_new"], status["leads_added"] = counts.get("new", 0), counts.get("added", 0)
                self.send(json.dumps(status, default=str))
        elif url.path == "/lookup":
            self.send(json.dumps(lookup(parse_qs(url.query).get("q", [""])[0])))
        elif url.path == "/brands":
            self.send(json.dumps(BRAND_IDEAS))
        elif url.path == "/history":
            self.send(json.dumps(get_history()))
        elif url.path == "/leads":
            self.send(json.dumps(get_leads(parse_qs(url.query).get("status", ["new"])[0]), default=str))
        elif url.path == "/download":
            which = parse_qs(url.query).get("which", ["good"])[0]
            if which == "leads":
                rows = get_leads("new")
            else:
                with LOCK:
                    rows = [s for s in STATE["sellers"] if which == "all" or s["status"] == "Good"]
            name = f"walmart_sellers_{which}_{datetime.now():%Y-%m-%d}.csv"
            self.send(to_csv(rows), "text/csv; charset=utf-8",
                      {"Content-Disposition": f'attachment; filename="{name}"'})
        else:
            self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        url = urlparse(self.path)
        if url.path == "/known/upload":
            name = parse_qs(url.query).get("name", [""])[0]
            if not name.lower().endswith((".xlsx", ".csv", ".txt")):
                return self.send(json.dumps({"error": "Please save the file as Excel (.xlsx) or CSV first, then upload that."}))
            try:
                found = phones_in_file(name, raw)
            except Exception:
                return self.send(json.dumps({"error": "Couldn't read that file. Try saving it as CSV and uploading again."}))
            added = add_known(found, f"Uploaded: {name}")
            unique = len({p for p, _ in found})
            log(f"Uploaded {name}: {unique} numbers found, {added} new")
            return self.send(json.dumps({"found": unique, "added": added, "total": len(KNOWN)}))
        body = json.loads(raw or b"{}")
        if self.path == "/start":
            with LOCK:
                if STATE["running"]:
                    return self.send('{"ok":false}')
                STATE.update(running=True, stop=False, log=[], sellers=[], saved_file="", summary="")
            pages = max(1, min(MAX_PAGES, int(body.get("pages") or 3)))
            threading.Thread(target=run_scrape, daemon=True,
                             args=(body["keywords"], pages, set(body.get("rules", [])), 2.0, bool(body.get("every_corner")),
                                   bool(body.get("hidden_sellers")))).start()
            self.send('{"ok":true}')
        elif self.path == "/leads/mark":
            mark_leads(body.get("keys", []), "added" if body.get("status") == "added" else "new")
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
    load_last_results()
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
