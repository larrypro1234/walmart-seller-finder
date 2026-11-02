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
import unicodedata
import shutil
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
VERSION = 39
UPDATE_URL = "https://raw.githubusercontent.com/larrypro1234/walmart-seller-finder/main/app.py"
BASE = "https://www.walmart.com"
MAX_PAGES = 25  # never ask for more than this many result pages for one search
FULL_SEARCH_PAGES = 9  # Walmart stops at about 10-11 pages, so a search this long was probably cut off
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
    ("Furniture", _words(r"sofas?", r"couch(es)?", r"loveseats?", r"sectionals?", r"desks?", r"tables?", r"chairs?",
                         r"dressers?", r"chests? of drawers", r"bookshel(f|ves)", r"bookcases?", r"shel(f|ves)", r"shelving",
                         r"cabinets?", r"cubb(y|ies)", r"wardrobes?", r"armoires?", r"sideboards?", r"credenzas?",
                         r"kitchen (island|cart)s?", r"bar carts?", r"benches", r"bench", r"stools?", r"vanit(y|ies)",
                         r"(toddler|bunk|loft|platform|floor|kids|twin|full|queen|king|murphy|canopy|storage|trundle|day) beds?",
                         r"daybeds?", r"bed frames?", r"mattress(es)?", r"cribs?", r"changing tables?", r"nightstands?",
                         r"ottomans?", r"recliners?", r"futons?", r"bar stools?", r"tv stands?", r"media consoles?",
                         r"headboards?", r"(drying|clothes|garment|coat|shoe|wine) racks?", r"hall trees?", r"room dividers?",
                         r"closet organizers?", r"(toy|dress up|kids) storage", r"storage (cabinet|organizer|unit|shelf)s?",
                         r"furniture")),
    ("Clothing", re.compile(CLOTHING_WORDS.pattern + r"|\b(hats?|caps|beanies?|scarf|scarves|gloves|belts?|handbags?|purses?)\b", re.I)),
    ("Industrial", _words(r"industrial", r"machinist", r"mro", r"pneumatic", r"\d+ ?cfm", r"\d+ ?psi", r"shims?",
                          r"feeler gauges?", r"(cutting|grinding|tapping) (fluid|oil)", r"coolant", r"lifting magnets?",
                          r"magnetic lifters?", r"abrasive", r"sanding (discs?|belts?)", r"flap discs?", r"carbide",
                          r"end mills?", r"lathe", r"milling", r"weld(ing|er|ers)?", r"hydraulic", r"valves?",
                          r"pipe fittings?", r"(npt|bsp) fittings?", r"flanges?", r"bearings?", r"bushings?", r"gaskets?",
                          r"o-?rings?", r"fasteners?", r"threaded rods?", r"(hex|anchor|carriage) bolts?", r"conveyor",
                          r"forklift", r"pallet jacks?", r"(chain|electric) hoists?", r"rigging", r"shackles?",
                          r"wire rope", r"solenoids?", r"contactors?", r"circuit breakers?", r"conduit", r"drum pumps?",
                          r"spill kits?", r"safety cabinets?", r"osha", r"ansi z\d+")),
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
    ("Home décor", _words(r"wall art", r"canvas prints?", r"posters?", r"ornaments?", r"figurines?", r"artificial (flowers?|plants?)",
                          r"wreaths?", r"vases?", r"d[eé]cor", r"wall decals?", r"picture frames?", r"tapestry",
                          r"(wall|floor|full length) mirrors?")),
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


SURELY_FURNITURE = _words(r"vanity (desk|table|set|stool)s?", r"makeup vanit(y|ies)", r"(toddler|bunk|loft|kids) beds?",
                          r"bed frames?", r"bookcases?", r"bookshel(f|ves)", r"wardrobes?", r"armoires?", r"dressers?",
                          r"kitchen islands?", r"(coffee|console|dining|end) tables?", r"tv stands?", r"storage cabinets?",
                          r"(drying|garment|clothes|shoe) racks?", r"(baby|convertible) cribs?", r"changing tables?")


def classify(text):
    if USED.search(text or ""):
        return "Used/Refurbished"
    if SURELY_FURNITURE.search(text or ""):
        return "Furniture"
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
# The database lives in a shared folder so the other seller finder on this computer
# (the Amazon one) uses the same numbers and leads. See SHARED_DATABASE.md.
DATA_DIR = Path.home() / "SellerFinderData"
DB_FILE = DATA_DIR / "numbers.db"
OLD_DB_FILE = HERE / "numbers.db"  # versions 9-25 kept it next to app.py
MARKETPLACE = "Walmart"


def move_database():
    """One time: move this app's database into the shared folder, keeping the old file as a backup.
    If the other app already made the shared database, merge ours into it instead (nothing is overwritten)."""
    DATA_DIR.mkdir(exist_ok=True)
    if not OLD_DB_FILE.exists():
        return
    if not DB_FILE.exists():
        shutil.copy2(OLD_DB_FILE, DB_FILE)
    else:
        con = sqlite3.connect(str(DB_FILE), timeout=30)
        con.execute("ATTACH DATABASE ? AS old", (str(OLD_DB_FILE),))
        tables = {r[0] for r in con.execute("SELECT name FROM old.sqlite_master WHERE type = 'table'")}
        main_tables = {r[0] for r in con.execute("SELECT name FROM main.sqlite_master WHERE type = 'table'")}
        with con:
            for t in tables:
                if t in main_tables:
                    con.execute(f"INSERT OR IGNORE INTO main.{t} SELECT * FROM old.{t}")
                else:
                    sql = con.execute("SELECT sql FROM old.sqlite_master WHERE name = ?", (t,)).fetchone()[0]
                    con.execute(sql)
                    con.execute(f"INSERT INTO main.{t} SELECT * FROM old.{t}")
        con.execute("DETACH DATABASE old")
        con.close()
    OLD_DB_FILE.rename(HERE / "numbers-backup-before-move.db")


move_database()
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
    con = sqlite3.connect(str(DB_FILE), timeout=30)  # if the other app is writing, wait for it
    con.execute("PRAGMA journal_mode=WAL")  # lets both apps read and write at the same time
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
BLOCKING_REASONS = ("Not a US seller", "Mostly sells what we don't buy", "Clothing-only store", "Private label",
                    "Mostly no-name products", "Industrial store")


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
        if r.get("marketplace", MARKETPLACE) != MARKETPLACE:
            continue  # the other app's leads stay in the shared database, but aren't shown here
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


def remove_lead(r, reason, source, key=None, phone=None):
    """Take a lead out of Leads and block its number for good."""
    key = key or lead_key(r)
    phone = phone or normalize_phone(r.get("phone"))
    with DB_LOCK:
        con = leads_db()
        with con:
            con.execute("UPDATE leads SET status = 'rejected', added = ? WHERE key = ?",
                        (datetime.now().strftime("%Y-%m-%d %H:%M"), key))
            if phone:
                details = f"{reason} · {r.get('seller_name', '')} · {r.get('address', '')} · {r.get('seller_url', '')}"
                con.execute("INSERT INTO numbers (phone, details, source, added) VALUES (?, ?, ?, ?) "
                            "ON CONFLICT(phone) DO UPDATE SET details = excluded.details, source = excluded.source",
                            (phone, details, source, datetime.now().strftime("%Y-%m-%d")))
        con.close()
    if phone:
        KNOWN.add(phone)
    known_seller_ids().update(re.findall(r"/seller/(\d+)", str(r.get("seller_url") or "")))


def reject_seller(r, why):
    """You rejected a seller by hand: take them out of Leads and block their number for good."""
    key = lead_key(r)
    reason = "Rejected by you" + (f": {why}" if why else "")
    phone = normalize_phone(r.get("phone"))
    with DB_LOCK:
        con = leads_db()
        with con:
            con.execute("UPDATE leads SET status = 'rejected', added = ? WHERE key = ?",
                        (datetime.now().strftime("%Y-%m-%d %H:%M"), key))
            if phone:
                details = f"{reason} · {r.get('seller_name', '')} · {r.get('address', '')} · {r.get('seller_url', '')}"
                con.execute("INSERT INTO numbers (phone, details, source, added) VALUES (?, ?, 'Rejected by you', ?) "
                            "ON CONFLICT(phone) DO UPDATE SET details = excluded.details, source = excluded.source",
                            (phone, details, datetime.now().strftime("%Y-%m-%d")))
        con.close()
    if phone:
        KNOWN.add(phone)
    known_seller_ids().update(re.findall(r"/seller/(\d+)", str(r.get("seller_url") or "")))
    # show it as rejected in this search's results too (and in its saved spreadsheet)
    with LOCK:
        for row_ in STATE["sellers"]:
            if lead_key(row_) == key:
                row_["status"], row_["reason"] = "Rejected", reason
        rows = list(STATE["sellers"])
    if STATE.get("saved_file") and (HERE / STATE["saved_file"]).exists():
        (HERE / STATE["saved_file"]).write_text(to_csv(rows), encoding="utf-8")
    log(f"You rejected {r.get('seller_name', 'a seller')}" + (f" ({why})" if why else ""))
    return reason


def lead_counts():
    return {"new": len(get_leads("new")), "added": len(get_leads("added"))}


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


def refresh_search(kw, rows):
    """Update a search word's latest numbers after Finish checking (without counting a new search)."""
    kw = kw.strip().lower()
    sellers, good, rejected, already = keyword_stats(rows, kw)
    with DB_LOCK:
        con = history_db()
        with con:
            old = con.execute("SELECT new_leads FROM searches WHERE keyword = ?", (kw,)).fetchone()
            if old:
                con.execute("UPDATE searches SET sellers = ?, new_leads = ?, rejected = ?, already_have = ?, "
                            "total_new_leads = total_new_leads - ? + ? WHERE keyword = ?",
                            (sellers, good, rejected, already, old[0], good, kw))
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


def is_known(phone):
    """True if the number is in the shared database. Checks the database itself, not just this
    app's memory, because the other app may have added it a moment ago."""
    if phone in KNOWN:
        return True
    con = db()
    hit = con.execute("SELECT 1 FROM numbers WHERE phone = ?", (phone,)).fetchone()
    con.close()
    if hit:
        KNOWN.add(phone)
    return bool(hit)


def known_count():
    con = db()
    n = con.execute("SELECT COUNT(*) FROM numbers").fetchone()[0]
    con.close()
    return n


def claim_number(phone, details):
    """Save a newly found number in one step. Returns False if it's already there (the other
    app found it first, even just now), so the same number is never added twice."""
    with DB_LOCK:
        con = db()
        with con:
            added = con.execute("INSERT OR IGNORE INTO numbers (phone, details, source, added) VALUES (?, ?, ?, ?)",
                                (phone, details, "Found by app", datetime.now().strftime("%Y-%m-%d"))).rowcount
        con.close()
    if added:
        KNOWN.add(phone)
    return bool(added)


def rule_no_duplicate_phone(s):
    p = normalize_phone(s.get("phone"))
    if not p:
        return None
    other = RUN_PHONES.get(p)
    if other and other != s["seller_name"]:
        return f"Same phone as {other}"
    if other != s["seller_name"] and is_known(p):
        return "Already have this number"
    return None


# Private label: a store whose products mostly carry the store's own name as the brand
# (e.g. store "Shiny Leaf" selling only "Shiny Leaf ..." products). Stores named after an
# established brand, or with generic names, are never flagged.
COMPANY_WORDS = set("""llc inc co corp corporation company store stores shop shoppe official the ltd us usa
    trading group enterprises enterprise holdings supply supplies distribution distributors distributor wholesale
    outlet direct goods mart market online international intl and of""".split())
GENERIC_WORDS = set("""toy toys kid kids baby babies home beauty health pet pets game games gift gifts deal deals
    discount discounts best top value smart big little happy family world life essentials essential products
    product brands brand things stuff daily everyday plus pro prime express quick fast depot center hub zone house
    land city bargain bargains savings shopping retail sales seller sellers new great good more one all star""".split())


def norm(text):
    text = (text or "").replace("\u2019", "'").replace("\u2018", "'")  # Hyland’s -> Hyland's
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()  # L'Oréal -> L'Oreal
    text = text.lower().replace("'", "")  # O'Keeffe's = Okeeffes, Burt's = Burts
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text)).strip()


def seller_core(name):
    """The distinctive part of a seller's name: 'Shiny Leaf LLC' -> 'shiny leaf'."""
    return " ".join(w for w in norm(name).split() if w not in COMPANY_WORDS)


def established_brands():
    names = {norm(b) for group in BRAND_IDEAS.values() for b in group} | {norm(k) for k in RELATED_TERMS}
    return names | {n.split()[0] for n in names if len(n.split()[0]) >= 4}


def own_brand(product_text, core):
    t = norm(product_text)
    squeeze = lambda x: x.replace(" ", "")
    return (t.startswith(core + " ") or t == core or squeeze(t).startswith(squeeze(core))
            or f"brand {core}" in t)


def rule_private_label(s):
    texts = list(s.get("products", {}).values())
    if len(texts) < 3:
        return None
    words = seller_core(s.get("seller_name")).split()
    known = established_brands()
    # The brand can be the whole store name ("Shiny Leaf") or its start
    # ("Life Extension" sold by "Life Extension Buyers Club").
    for n in range(len(words), 0, -1):
        brand = " ".join(words[:n])
        if len(brand) < 4 or set(brand.split()) <= GENERIC_WORDS or brand in known:
            continue
        own = sum(1 for t in texts if own_brand(t, brand))
        if own / len(texts) >= 0.5:  # half or more of what they sell is their own brand
            return f"Private label: sells only its own brand ({brand.title()}, {own} of {len(texts)} products)"
    return None


# No-name stores: most products have no real brand ("Sodium Citrate Powder 10 oz...").
# A product counts as branded if Walmart lists a real brand for it, if it names an
# established brand, or if its first word isn't a plain English word (CeraVe, Hasbro...).
DICT_FILE = Path("/usr/share/dict/words")  # comes with every Mac
_ENGLISH = []
# Real brands that are plain English words, so they don't look like no-name products.
WORD_BRANDS = set("""suave dial axe dove secret degree tide gain glad era cheer joy bounce all scott cottonelle
    kleenex charmin bounty dawn crest scope always tampax playtex simple method native herbal clean gold bond
    caress zest coast shield irish spring ivory olay aussie raid off glade pledge windex shout resolve finish
    cascade palmolive ajax comet arm hammer oxiclean purex snuggle downy bold persil sun tylenol excedrin
    mucinex vicks theraflu robitussin zicam emergen airborne centrum nature made spring valley one day
    vitafusion olly kirkland equate great value parent choice up&up member mark hanes champion jockey
    puffs angel soft quilted northern hefty ziploc solo dixie reynolds glad hot wheels nerf fisher price
    little tikes step crayola elmer sharpie bic paper mate expo post-it scotch duck gorilla
    first years summer munchkin boppy graco evenflo chicco nuby tommee tippee playskool""".split())
GENERIC_PRODUCT_WORDS = set("""allulose gummies gummy keto collagen probiotic probiotics electrolyte electrolytes
    bluetooth usb led lcd hdmi iphone ipad android airpods magsafe wifi rgb vegan organic""".split())
NO_BRAND = {"", "unbranded", "generic", "no brand", "none", "n a", "na", "null", "unknown", "other"}


def english_words():
    if not _ENGLISH:
        try:
            _ENGLISH.append({w.strip() for w in DICT_FILE.read_text(errors="ignore").splitlines() if w.strip().islower()})
        except OSError:
            _ENGLISH.append(set())
    return _ENGLISH[0]


def full_brand_names():
    """Whole brand names from Brand ideas ("nature made", "lego"), minus the plain category ideas."""
    return {norm(b) for group in BRAND_IDEAS.values() for b in group} - {"board games", "puzzles", "gildan socks"}


def plain_word(w):
    words = english_words()
    w = w.lower().replace("'s", "").replace("'", "")
    return w in words or (w.endswith("s") and w[:-1] in words) or (w.endswith("es") and w[:-2] in words)


def is_branded(product_text, seller_words):
    m = re.search(r"\| brand: (.+)$", product_text or "")
    if m:
        brand = norm(m.group(1))
        if brand not in NO_BRAND and not set(brand.split()) <= seller_words:
            return True
    title = norm(re.sub(r"\| brand: .+$", "", product_text or ""))
    padded = f" {title} "
    if any(f" {b} " in padded for b in full_brand_names()):  # e.g. "2 pack lego city ..."
        return True
    if any(title == b or title.startswith(b + " ") for b in WORD_BRANDS):  # "suave shampoo", not "all purpose flour"
        return True
    first = next((w for w in title.split() if w.isalpha()), "")
    if not first or first in seller_words or first in GENERIC_PRODUCT_WORDS or len(first) <= 2:
        return False  # no brand, or the seller's own name
    return not plain_word(first)


# Our brands (from Larry): one per line, as given. Words after the brand name like
# "coffee" or "deodorant" are fine: the app also matches the name without them.
YOUR_BRANDS = """
303 Products
3M Mask
5-hour Energy
AbilityOne
Acnefree
ADAMS
Advil
Airborne
Alaffia
Alba Botanica
Aleve
Algenist
Alka-Seltzer
ALLMAX Nutrition
Almay
Alteril
Altoids
American Crew
amiibo
Amope
Andalou
Andis
Aquaphor
Aqueon
Arches & Halos
Arm & Hammer
Asmodee
Aspercreme
At-a-Glance
Audio-Technica
Aveeno
Avia socks
AVON
Axe and Sledge
AXIS-Y
Babyganics
Balmex
Band-Aid
Bandai hobby
Barbasol
Barbie
Barney
basis
Bath and Body Works
Batiste
Bayer
Benadryl
Benefiber
Berkley
Berkley jensen
Betco
BIC Pens
Biofreeze
BioGuard
Biore
Biotene
Bissell
Black Diamond
Black Radiance
Blistex
Blue Devil
Blue Lizard
Bluebonnet Nutrition
BodyArmor IV
BodyTech
Boiron
Botanics Organic
Boudreauxs
Bratz
Braun
Brio World
Brita pitcher
brother printer
Brut
BSN
Bucked Up
Buffalo Games
Bulldog mens
Burt's Bees
Cafe Bustelo coffee
Calico Critters
Caltrate
CamelBak
Canon Printer
Cantu
Carbona
Caribou Coffee
Carlson Vitamin
Carmex
Casabella
Cascade Platinum
Case Pack
casio watch
CELSIUS On The Go
Centrum
Ceramedx
CeraVe
Cetaphil
Champion Socks
Channellock
Chapstick
Chefman
Chore Boy
Cif
Cirkul
Citrucel
Claritin
Clean & Clear
Clearblue test
Clif Bar
CLIF BLOKS
Clif Builders
Clorox
Coffeemate
Colgate
Coloplast
Combat Max
Comfort Zone
Conair
Copper Fit
Coralife
Cortizone
COSRX
Covergirl
cramer
Crayola
Creative Kids
Cremo
CRESCENT Tool
Crest Teeth
Cuccio
Curad
Curt
CVS
d-CON
Dallmayr coffee
Dawn
Degree deodorant
Delsym
DenTek
Dentemp
Depend
Derma B
Derma E
Dermasil
Desert Essence
Design Essentials
DeWalt
Diablo Blade
Difeel
Differin
Digestive Advantage
Dirt Devil
Disney Princess
Dixon markers
Doctor Squish
doTERRA
Dove
Downy
Dr Bronner
Dr Scholl's
Dr. Beckmann
Dr. Dan's
Dr. Squatch
Dr. Teal's
Dragon Ball
Dreamworks
dreo heater
Dunkin coffee
Duracell
Duraflame
DYMO
E45
E6000
Earth Breeze laundry
Earth Mama
Earth Therapeutics
Ecover
Eight O'Clock coffee
elf
Emergen-C
Eminence
Encanto
Energizer Batteries
Eos
Essie
Estwing
Eternabond
Eucerin
Everlast
EVERPRO
Excedrin
Exploding Kittens
Expo Dry Erase
Farberware
Febreze
Fellowes
FENTY BEAUTY
Fingerlings
Finish Quantum
Finishing Touch Flawless
First Alert
Fisher-Price
Fit-rx
Fixodent
flamingo
Flex Paste
Flex Tape
Flintstones
Flonase
Florajen
Florastor
fluidmaster
Folgers coffee
Force Factor
Fox Racing
Franklin Sports
Freeman
Fruit of the Loom
Funko
Furby
G.I. Joe
Gain detergent
Galison
Game Face
Gamewright
Garden of Life
Garnier
Gas-X
GAT Sport
Gen See
Genepro
Germ-X
Gevalia coffee
Ghost supplements
Gildan Pack
Gillette
Glad
Glade PlugIns
Glymed
GNC
Gojo Sanitizer
Gold Bond
Goli
Goo Gone
Gorilla glue
Got2B
Green Mountain coffee
Guardsman
HALLS
Hamilton Beach
Hanes
Harney and Sons
Harrys
Hasbro
HASK
Hawaiian Tropic
Heal & Rescue
Hefty
Hello
Hempz
HEX BOTS
Hills Bros.
Hiya Toys
Homax
Honeywell fan
Hot Tools
Hot Wheels
Hothands
Howard Leight
Howard Products
Husky (tools)
Hyland’s
HyperX
Icy Hot
IIT
Ingenuity Baby
Irish Spring
Irwin Naturals
iSatori
Isopure
Italia Deluxe
Its a 10
IZOD
J.R. Watkins
Jason
Jason Coconut
Jellycat
Jockey
John Frieda
Jovan musk
Kendra Scott
Kervan gummy
Keurig
Kirkland
Kiss salon
Kiwi
Klein tools
Kopari
KOS
Krud Kutter
KVD
Kwikset
L'Oréal
LA Colors
La Roche-Posay
Lactaid
Lady Speed Stick
Larabar
Lavazza
Lego
Libman
Liftmaster
Lil Critters
Limbsaver
Little Live Pets
Little Tikes
Logitech
Lotrimin
Lumax
Luna Bar
Lysol
Mac Makeup
Magnetoe jurassic
Magpul
Mally beauty
Martha Stewart
MaryRuth's
Master Lock
Masters of the Universe
Maui Moisture
Maybelline
Mckesson hand sanitizer
MECHANIX WEAR
MEGA BLOKS
MegaFood
Meguiar’s
MERIT
Metamucil
Method
MGA’s Miniverse
Micro-Scientific
Microban
Mielle
milani
Milk Bone
Milwaukee
Minwax
Mitchum deodorant
Moen
Moira Beauty
Molotow
Monster High
Motortrend
Mr. Coffee
Mr. Heater
Mrs. Meyer's
Mucinex
Muscle Milk
MuscleTech
My Arcade
Nair
Nakd Bars
Native
Natrol
Nature's Bounty
Nature’s Truth
Nature's Way
Natureplex
NeilMed
Nerf
Neuriva
New Chapter
Nexium
NGK
Nickelodeon
Nicorette Gum
Nordic Naturals
NOW Foods
Nu Finish
NYX
o.b.
O’keeffes
O2COOL
Officemate
Olay
Olly
One A Day
Ontel
Optase
Optimum Nutrition
Oral-B
Orly Nail
Osteo
OXO Good Grips
Ozium
Palmer's
Paper Mate
Parker & Bailey
Parodontax
Paw Patrol
Peppa Pig
Peptiva
Permatex
pH-D Feminine
Physicians Formula
PlayMonster
Poise
Polident
Poligrip
Power Rangers
Power Stick deodorant
PowerA
Preparation H
Pressman
Prevacid
Procell
ProClearz
PROcure
Pronamel
PROVON
PUR gum
Purell
Quartet
QUIXX scratch
Qunol
Radians
Raid
Rain-X
Rare Beauty
Ravensburger
Raw Elements
Raw Nutrition
Real Techniques
Redcon1
REF shampoo
RELAXIUM
Remington
Renew Life
RepHresh
replenza
Restoration Games
Revlon
Revolution Beauty
Rid-X
Ridgid
Right Guard
Rimmel
Rite Aid
RITUALS Sakura
Robitussin
Rolda
Rubbermaid
Ryse
Sally Hansen
SanDisk
Schlage
Schmidt's
Schwarzkopf
Scinic
Scrubbing Bubbles
Scunci
Secret deodorant
Sensodyne
Seventh Generation
Sharpie
SheaMoisture
Siemens
Skil
Skin1004
Sky and Sol
Slim Jim jerky
SmartyPants
Snark guitar tuners
Solgar
Spenco
Spring Valley
Square D Schneider
Squish-a-longs
Sqwincher
Stanley Utility Knife
Star Wars
Stem Insect
Streamlight Flashlight
Summer's Eve
Sylvania
Systane
Thayers
The Original Donut Shop
Theraflu
Thermacell
Think Fun
Thinkkids
TIGI
TimeMist
TITEBOND
Titleist Golf
Tonies
Transformers
Tree Hut
Treva
TropiClean
Truvia
TUMS
Turtle Beach
Turtle Wax
U by Kotex
Under Armour
Under armour socks
Unicorn Academy
University Games
Vagisil
Vanicream
VELCRO Brand
Vera Wang perfume
Versachem
Vicks
Victoria's Secret perfume
Victorinox
Vitafusion
Vital Proteins
Viviscal
Voltaren
VTech
Wahl
Warner Bros
Westcott
Western Digital
Wet ones
Wilkinson Sword
Yale
Yankee Candle
Zebra pen
ZEISS
Zicam
zuru
Tylenol
Motrin
Zyrtec
Allegra
Xyzal
Sudafed
Mylanta
Maalox
Pepcid
Prilosec OTC
Tagamet HB
Imodium
Dulcolax
MiraLAX
Senokot
Colace
Kaopectate
Pepto-Bismol
Dramamine
Bonine
Unisom
ZzzQuil
NyQuil
DayQuil
Coricidin HBP
Afrin
Nasacort
Rhinocort
Astepro
Pataday
Zaditor
Visine
Clear Eyes
Refresh
Blink Tears
TheraTears
Rohto
Opti-Free
Biotrue
Renu
Bausch + Lomb
Similasan
Polysporin
Neosporin
Hibiclens
Betadine
Bactine
Solarcaine
Orajel
Abreva
Nature Made
Life Extension
Thorne
Pure Encapsulations
Jarrow Formulas
Doctor's Best
Sports Research
Solaray
Swanson
21st Century Vitamins
Country Life
Rainbow Light
Healthy Origins
Nutricost
BulkSupplements
Horbäach
Bronson
Carlyle
Puritan's Pride
HUM Nutrition
Ritual
Seed
Culturelle
Align
BioGaia
AZO
Metagenics
Ortho Molecular Products
Gaia Herbs
Herb Pharm
Traditional Medicinals
Yogi Tea
Zipfizz
Liquid I.V.
Pedialyte
DripDrop
Nuun
Ultima Replenisher
LMNT
Propel
Gatorade
Powerade
Ensure
Boost
Premier Protein
Orgain
Dymatize
Cellucor
Rule One Proteins
MusclePharm
Neutrogena
Vaseline
Nivea
Lubriderm
Jergens
Suave
St. Ives
Pond's
Simple Skincare
Banana Boat
Coppertone
Sun Bum
Supergoop!
EltaMD
Curél
AmLactin
Bioderma
Embryolisse
First Aid Beauty
Paula's Choice
The Ordinary
The INKEY List
Drunk Elephant
Glow Recipe
Sunday Riley
Tatcha
Youth To The People
Farmacy
Laneige
Innisfree
belif
Beauty of Joseon
Good Molecules
Byoma
Versed
Bubble Skincare
Cocokind
Hero Cosmetics
PanOxyl
Kate Somerville
Kiehl's
Clinique
Estée Lauder
Dermalogica
Murad
Peter Thomas Roth
Philosophy
Fresh
Origins
Mario Badescu
Wet n Wild
Anastasia Beverly Hills
Tarte
Too Faced
Urban Decay
Benefit Cosmetics
NARS
IT Cosmetics
bareMinerals
Bobbi Brown
Laura Mercier
Charlotte Tilbury
Huda Beauty
Pat McGrath Labs
ColourPop
Morphe
Juvia's Place
essence cosmetics
Catrice
Old Spice
Inglot
Palladio Beauty
No7
Pixi Beauty
IMAN Cosmetics
Black Opal
Ardell
DUO Lash Adhesive
Lilly Lashes
Velour Lashes
Redken
Matrix
Biolage
Paul Mitchell
Joico
CHI Haircare
Kenra Professional
OGX
Pantene
TRESemmé
Head & Shoulders
Aussie
Herbal Essences
Nioxin
Moroccanoil
Olaplex
K18 Hair
Living Proof
Pureology
Not Your Mother's
Tide
Swiffer
Mr. Clean
Bounty
Charmin
Kleenex
Puffs
Scott
Cottonelle
Viva
Windex
Pine-Sol
Fabuloso
Ajax
Comet
Bar Keepers Friend
Soft Scrub
OxiClean
Shout
Spray 'n Wash
Zout
Woolite
Persil
all Laundry
Bounce
Snuggle
Purex
Dreft
Foca
Zote
OdoBan
Simple Green
Zep
CLR
Kaboom
Easy-Off
Weiman
Pledge
Endust
Murphy Oil Soap
Ziploc
Reynolds Wrap
Saran Wrap
Scotch-Brite
Brillo
S.O.S. Pads
Scrub Daddy
Lestoil
ECOS
The Pink Stuff
Huggies
Pampers
Luvs
Pull-Ups
Goodnites
WaterWipes
The Honest Company
Hello Bello
Mustela
Tubby Todd
Pipette
Johnson's Baby
Desitin
A+D
Triple Paste
Boogie Wipes
Frida Baby
Dr. Brown's
Philips Avent
NUK
MAM Baby
Tommee Tippee
Evenflo
Munchkin
Skip Hop
Graco
Chicco
Safety 1st
The First Years
Comotomo
Lansinoh
Medela
Haakaa
Elvie
Boppy
Summer Infant
Baby Brezza
Ubbi
Playtex Baby
Listerine
ACT Mouthwash
TheraBreath
Tom's of Maine
Close-Up
Aim Toothpaste
Aquafresh
GUM Oral Care
Plackers
REACH Dental
Dr. Tung's
Pokémon
Squishmallows
Bluey
Melissa & Doug
Play-Doh
MAGNA-TILES
PicassoTiles
K'NEX
Lincoln Logs
Tinkertoy
Lite-Brite
Etch A Sketch
Spirograph
Perler
Rainbow Loom
L.O.L. Surprise!
Baby Alive
My Little Pony
Care Bears
Cabbage Patch Kids
American Girl
Polly Pocket
Littlest Pet Shop
Schleich
Safari Ltd.
Papo
Beyblade
Bakugan
Magic: The Gathering
Yu-Gi-Oh!
Dungeons & Dragons
Warhammer
Hatchimals
Pillow Pets
Bop It!
Simon
Connect 4
Jenga
Monopoly
Clue
Operation
Sorry!
Guess Who?
UNO
Skip-Bo
Phase 10
Apples to Apples
Cards Against Humanity
Sequence
Coca-Cola
Pepsi
Sprite
Fanta
Dr Pepper
7UP
Canada Dry
Schweppes
A&W Root Beer
Mountain Dew
Ocean Spray
Welch's
Mott's
Hawaiian Punch
Capri Sun
Kool-Aid
Crystal Light
Tang
Country Time
Minute Maid
Simply
Tropicana
Florida's Natural
Naked Juice
V8
Campbell's
Progresso
Hormel
SPAM
Chef Boyardee
Ragu
Prego
Barilla
Rao's
Bertolli
Hunt's
Heinz
French's
Hellmann's
Duke's Mayonnaise
Hidden Valley
Kraft
Velveeta
Philadelphia Cream Cheese
Jif
Skippy
Smucker's
Nutella
Hershey's
Reese's
Apple
Samsung
Sony
Microsoft
Dell
HP
Lenovo
ASUS
Acer
Anker
Belkin
JBL
Bose
Beats
Skullcandy
JLab
Sennheiser
Razer
SteelSeries
Corsair
Elgato
Nintendo
PlayStation
Xbox
Seagate
Kingston
Lexar
TP-Link
NETGEAR
Linksys
Epson
Zojirushi
Ninja
Cuisinart
KitchenAid
Oster
Black+Decker
Breville
Vitamix
NutriBullet
Instant Pot
Crock-Pot
Thermos
Contigo
Hydro Flask
YETI
Pyrex
Corelle
Lodge
T-fal
Calphalon
Purina
Pedigree
Blue Buffalo
IAMS
Royal Canin
Hill's Science Diet
Temptations
Greenies
KONG
Nylabone
PetSafe
Armor All
WD-40
Lucas Oil
"""
NOT_BRANDS = {"case pack"}
# Our brands that are also everyday words at the start of product names ("Fresh Cut Flowers",
# "Simply Delicious..."): they only count when Walmart lists them as the product's brand.
EVERYDAY_WORD_BRANDS = {"fresh", "simply", "hello", "seed", "origins", "sequence", "matrix", "sorry", "clue",
                        "operation", "ritual", "philosophy", "refresh", "boost", "propel", "align"}
DESCRIPTOR_WORDS = set("""coffee pitcher printer watch deodorant batteries detergent pack fan tools tool utility knife
    flashlight golf socks perfume pen pens heater laundry test teeth dry erase hand sanitizer gum vitamin vitamins
    blade mens musk salon nail hobby jerky insect makeup scratch shampoo mouthwash toothpaste oral care skincare
    cosmetics professional haircare lash adhesive beauty proteins products mayonnaise cream cheese root beer wrap
    pads otc hb hbp markers glue supplements nutrition baby on the go platinum quantum plugins bar bars brand
    feminine guitar tuners hair dental""".split())

# Well-known brands (Claude's list, kept alongside ours).
WELL_KNOWN_BRANDS = [
    "a+d", "abreva", "act", "acure", "adidas", "advil", "afrin", "air wick",
    "airborne", "ajax", "aleve", "align", "alka-seltzer", "all", "allegra", "almay",
    "always", "amazonbasics", "american girl", "angel soft", "anker", "annie's", "apple", "aquafresh",
    "aquaphor", "aquaphor baby", "arm & hammer", "aussie", "australian gold", "aveeno", "aveeno baby", "axe",
    "azo", "baby einstein", "bakugan", "banana boat", "band-aid", "barbie", "barbie dreamhouse", "batman",
    "bausch", "bayer", "beanie babies", "beano", "beats", "beech-nut", "belkin", "benadryl",
    "benefiber", "bengay", "bic", "biofreeze", "biore", "black+decker", "bliss", "blistex",
    "blue buffalo", "bluey", "bombas", "boost", "boppy", "bose", "boudreaux's", "bounce",
    "bounty", "bratz", "brawny", "brio", "brita", "bufferin", "burt's bees", "campbell's",
    "cantu", "cards against humanity", "carefree", "carhartt", "carmex", "cascade", "celestial seasonings", "centrum",
    "cepacol", "cerave", "cetaphil", "champion", "chapstick", "charmin", "cheer", "cheetos",
    "cheez-it", "chicco", "children's motrin", "chloraseptic", "claritin", "clean & clear", "clear eyes", "clif",
    "clinique", "clorox", "clue", "coca-cola", "cocomelon", "colace", "cold-eeze", "colgate",
    "connect 4", "contigo", "coppertone", "cortizone-10", "cosrx", "cottonelle", "covergirl", "crayola",
    "crest", "cuisinart", "culturelle", "curad", "cutter", "d-con", "dawn", "dayquil",
    "degree", "delsym", "depend", "dermalogica", "desitin", "dial", "dickies", "differin",
    "disney", "dollar shave club", "doritos", "dove", "dove men", "downy", "dr. brown's", "dramamine",
    "dulcolax", "dunkin", "duracell", "e.l.f.", "earth's best", "elmer's", "emergen-c", "energizer",
    "enfamil", "ensure", "equate", "era", "essie", "estee lauder", "eucerin", "evenflo",
    "excedrin", "exploding kittens", "expo", "febreze", "finish", "first aid beauty", "fisher-price", "five star",
    "flonase", "florastor", "folgers", "freeman", "friskies", "frito-lay", "fruit of the loom", "funko",
    "gain", "garden of life", "garnier", "garnier fructis", "gas-x", "gatorade", "ge", "general mills",
    "gerber", "gi joe", "gildan", "gillette", "glad", "glade", "glide", "gold bond",
    "goldfish", "graco", "greenies", "halls", "hamilton beach", "hanes", "happy baby", "haribo",
    "harry's", "hasbro", "hatchimals", "hawaiian tropic", "head & shoulders", "hefty", "heinz", "herbal essences",
    "hershey's", "hot wheels", "hot wheels monster trucks", "huggies", "hydro flask", "iams", "icy hot", "imodium",
    "instant pot", "irish spring", "isopure", "jakks", "jbl", "jenga", "jergens", "jif",
    "jockey", "john frieda", "johnson's", "joy", "jurassic world", "k'nex", "kellogg's", "keurig",
    "kiehl's", "kind", "kirkland", "kit kat", "kleenex", "kotex", "kraft", "l'oreal",
    "l.o.l. surprise", "la roche-posay", "lactaid", "lamisil", "lancome", "lay's", "leapfrog", "lego",
    "lever 2000", "libman", "lincoln logs", "lipton", "liquid i.v.", "listerine", "little remedies", "little tikes",
    "logitech", "lotrimin", "lubriderm", "luvs", "lysol", "m&m's", "magna-tiles", "mam",
    "mario badescu", "marvel legends", "matchbox", "mattel", "maxwell house", "maybelline", "mead", "mega bloks",
    "melissa & doug", "melissa and doug", "metamucil", "method", "mga", "milani", "milk-bone", "minecraft",
    "miralax", "monistat", "monopoly", "monster", "mophie", "motrin", "move free", "mr. clean",
    "mrs. meyer's", "mucinex", "munchkin", "muscletech", "my little pony", "mykind", "nabisco", "nasacort",
    "native", "natrol", "nature made", "nature valley", "nature's bounty", "nature's truth", "nature's way", "neosporin",
    "nerf", "nerf rival", "neutrogena", "new chapter", "nexcare", "nexium", "nexxus", "nike",
    "ninja", "nivea", "nordic naturals", "now foods", "noxzema", "nuby", "nuk", "nutella",
    "nyquil", "nyx", "o-cedar", "ocean", "off!", "ogx", "olay", "old spice",
    "olly", "one a day", "opcon", "operation", "opi", "optimum nutrition", "oral-b", "oreo",
    "orgain", "osteo bi-flex", "otterbox", "oxiclean", "oxo", "palmolive", "pampers", "pantene",
    "paper mate", "paula's choice", "paw patrol", "pedialyte", "pedigree", "pepcid", "peppa pig", "pepsi",
    "pepto-bismol", "persil", "philips", "philips avent", "physicians formula", "pine-sol", "plackers", "planters",
    "play-doh", "playmobil", "playtex", "playtex baby", "poise", "pokemon", "polly pocket", "polysporin",
    "pond's", "pop-tarts", "post-it", "power rangers", "premier protein", "preparation h", "prevail", "prilosec",
    "pringles", "progresso", "puffs", "puma", "purex", "purina", "pyrex", "q-tips",
    "quaker", "quilted northern", "qunol", "radio flyer", "raid", "rainbow high", "rainbow light", "ravensburger",
    "razor", "red bull", "redken", "reese's", "refresh", "renuzit", "resolve", "revlon",
    "reynolds", "ricola", "rimmel", "ritz", "robitussin", "roblox", "rolaids", "rubbermaid",
    "sally hansen", "salonpas", "samsung", "sandisk", "saran", "schick", "scope", "scotch",
    "scotch-brite", "scott", "scrabble", "scrubbing bubbles", "secret", "sensodyne", "sesame street", "seventh generation",
    "seventh generation baby", "sharpie", "shea moisture", "shout", "similac", "simple", "simply saline", "skippy",
    "skittles", "skullcandy", "smucker's", "snickers", "snuggle", "solgar", "sony", "spider-man",
    "spigen", "spin master", "spring valley", "squishmallows", "st. ives", "stanley", "star wars", "starbucks",
    "starburst", "stayfree", "step2", "sterilite", "suave", "sudafed", "summer's eve", "sun bum",
    "sundown", "supergoop", "swiffer", "systane", "tampax", "tena", "thayers", "the first years",
    "the ordinary", "theraflu", "thermacare", "thermos", "thomas & friends", "tide", "tiger balm", "tinactin",
    "tom's of maine", "tommee tippee", "tonka", "transformers", "tresemme", "tums", "twister", "ty",
    "tylenol", "u by kotex", "under armour", "unisom", "uno", "vagisil", "vaseline", "venus",
    "vicks", "visine", "vitafusion", "vital proteins", "viva", "vtech", "waterpik", "welch's",
    "wet n wild", "windex", "xyzal", "yes to", "yeti", "zantac", "zarbee's", "zicam",
    "ziploc", "zojirushi", "zuru", "zyrtec", "zzzquil",
]
PACK_WORDS = {"pack", "packs", "of", "set", "bundle", "count", "ct", "x", "pk", "lot", "case", "box", "value", "bulk"}


_BRAND_INDEX = []


def known_brand_index():
    if not _BRAND_INDEX:
        _BRAND_INDEX.append(build_brand_index())
    return _BRAND_INDEX[0]


def our_brand_names():
    names = set()
    for line in YOUR_BRANDS.splitlines():
        name = norm(re.sub(r"\(.*?\)", "", line))  # "Husky (tools)" -> "husky"
        if not name or name in NOT_BRANDS:
            continue
        names.add(name)
        words = name.split()
        while len(words) > 1 and words[-1] in DESCRIPTOR_WORDS:
            words = words[:-1]  # "folgers coffee" -> "folgers"
        core = " ".join(words)
        if core != name and (" " in core or (len(core) >= 4 and not plain_word(core))):
            names.add(core)  # only if what's left is still clearly a brand ("gain" alone is not)
    return names


def build_brand_index():
    """Brand names split into ones that can match anywhere in a product name, and ones that are plain
    English words ("tide", "all") or very short ("hp") that only count at the start of the name."""
    names = our_brand_names() | {norm(b) for b in WELL_KNOWN_BRANDS} | full_brand_names() | {norm(b) for b in WORD_BRANDS}
    names = {n for n in names if n and not n.isdigit()}
    start_only = {n for n in names if " " not in n and (len(n) <= 3 or plain_word(n))} - EVERYDAY_WORD_BRANDS
    return names - start_only - EVERYDAY_WORD_BRANDS, start_only, max(len(n.split()) for n in names)


def names_known_brand(product_text):
    m = re.search(r"\| brand: (.+)$", product_text or "")
    anywhere, start_only, longest = known_brand_index()
    if m and norm(m.group(1)) in anywhere | start_only | EVERYDAY_WORD_BRANDS:
        return True
    words = norm(re.sub(r"\| brand: .+$", "", product_text or "")).split()
    while words and (words[0].isdigit() or words[0] in PACK_WORDS):
        words = words[1:]  # "2 pack tide pods" -> "tide pods"
    for n in range(1, longest + 1):
        if " ".join(words[:n]) in start_only:
            return True
        if any(" ".join(words[i:i + n]) in anywhere for i in range(len(words) - n + 1)):
            return True
    return False


INDUSTRIAL_NAME = re.compile(r"\b(industrial|industries|machinery|machine shop|hydraulics?|welding|fasteners?|"
                             r"bearings?|mro|equipment supply|industrial supply)\b", re.I)


def rule_no_industrial(s):
    """Industrial stores: named like one ("A&M Industrial") or selling mostly industrial supplies.
    Kept only if they clearly sell what we buy (3+ products)."""
    counts = store_mix(s)
    buy = sum(v for k, v in counts.items() if k in BUY_NAMES)
    if buy >= 3:
        return None
    if INDUSTRIAL_NAME.search(s.get("seller_name") or ""):
        return "Industrial store"
    classified = sum(counts.values())
    if counts.get("Industrial", 0) >= 3 and counts["Industrial"] / max(classified, 1) >= 0.4:
        return f"Industrial store ({counts['Industrial']} industrial products)"
    return None


def rule_known_brands(s):
    texts = list(s.get("products", {}).values())
    if not texts:
        return None  # no products seen at all
    if any(names_known_brand(t) for t in texts):
        return None  # carries at least one of our brands
    return f"None of our brands ({len(texts)} product{'' if len(texts) == 1 else 's'} checked)"


def rule_no_name_products(s):
    texts = list(s.get("products", {}).values())
    if len(texts) < 5 or not english_words():
        return None  # too few products to judge, or no word list on this computer
    seller_words = set(seller_core(s.get("seller_name")).split())
    no_name = sum(1 for t in texts if not is_branded(t, seller_words))
    if no_name / len(texts) >= 0.8:
        return f"Mostly no-name products ({no_name} of {len(texts)} have no real brand)"
    return None


RULES = [
    {"id": "us_only", "label": "Only US sellers", "check": rule_us_only},
    {"id": "store_categories", "label": "Only stores that sell what we buy", "check": rule_store_categories},
    {"id": "no_private_label", "label": "No private-label stores (own brand only)", "check": rule_private_label},
    {"id": "no_no_name", "label": "No stores selling mostly no-name products", "check": rule_no_name_products},
    {"id": "known_brands", "label": "Only stores that carry at least 1 of our brands", "check": rule_known_brands},
    {"id": "no_industrial", "label": "No industrial stores", "check": rule_no_industrial},
    {"id": "no_duplicate_phone", "label": "Skip numbers we already have", "check": rule_no_duplicate_phone},
]


# Brand ideas shown in the app's Search tab, grouped by the categories we buy.
# Category ideas: plain product words for the categories we buy. Searching these finds
# sellers of any brand (the rules still decide who's a good lead). "Find random sellers"
# picks from these and the brand ideas.
CATEGORY_IDEAS = {
    "Health & Beauty": ["face moisturizer", "facial cleanser", "sunscreen", "mascara", "lipstick", "foundation makeup",
                        "nail polish", "makeup remover", "eye cream", "body lotion", "lip balm", "hair dye"],
    "OTC": ["allergy relief", "pain reliever", "cold and flu medicine", "cough syrup", "antacid", "sleep aid",
            "nasal spray", "eye drops", "first aid kit", "bandages", "hydrocortisone cream", "heartburn relief"],
    "Personal Care": ["shampoo", "conditioner", "body wash", "bar soap", "deodorant", "toothpaste", "toothbrush",
                      "mouthwash", "razors", "shaving cream", "hair styling gel", "dental floss", "feminine care"],
    "Household Essentials": ["laundry detergent", "dish soap", "disinfecting wipes", "all purpose cleaner", "paper towels",
                             "toilet paper", "trash bags", "fabric softener", "air freshener", "sponges",
                             "dishwasher pods", "bleach", "food storage bags"],
    "Baby": ["diapers", "baby wipes", "baby formula", "baby bottles", "pacifiers", "baby shampoo", "diaper rash cream",
             "baby food", "teething toys", "sippy cups"],
    "Supplements": ["vitamins", "multivitamin", "vitamin d3", "fish oil", "probiotics", "protein powder", "melatonin",
                    "vitamin c", "magnesium", "collagen", "elderberry", "electrolyte powder"],
    "Toys & Games": ["board games", "card games", "action figures", "dolls", "building blocks", "puzzles", "plush toys",
                     "toy cars", "play dough", "remote control car", "outdoor toys", "educational toys",
                     "trading cards", "arts and crafts kit"],
    "General Merchandise": ["phone charger", "headphones", "batteries", "light bulbs", "storage bins", "kitchen gadgets",
                            "water bottles", "school supplies", "pet treats", "party games", "flashlight", "extension cord"],
    "Apparel basics": ["socks", "boxer briefs", "undershirts", "underwear multipack", "kids socks"],
}

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
    """Product name plus any category/department/brand info, used to classify the product."""
    parts = [d.get("name") or ""]
    if isinstance(d.get("brand"), str) and d["brand"].strip():
        parts.append(f"| brand: {d['brand'].strip()}")
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

STATE = {"running": False, "stop": False, "skip_search": False, "log": [], "sellers": [], "saved_file": "",
         "update_available": "", "summary": ""}


def still_searching():
    """False once Stop or "Stop searching & check now" was pressed."""
    return not STATE["stop"] and not STATE["skip_search"]
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
        self.loads = 0
        self._open()

    def _open(self):
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

    def _closed(self):
        try:
            return self.page.is_closed() or not self.ctx.pages
        except Exception:
            return True

    def reopen(self, why="Chrome was closed, so opening it again"):
        log(why)
        try:
            self.ctx.close()
        except Exception:
            pass
        self._open()

    def get(self, url):
        time.sleep(self.delay + random.uniform(0, self.delay))
        self.loads += 1
        if self.loads % 300 == 0:  # Chrome slowly uses more memory on long searches and can crash
            try:
                self.reopen("Restarting Chrome to keep it running smoothly")
            except Exception as e:
                log(f"Couldn't restart Chrome: {e}")
        for attempt in (1, 2):
            try:
                if self._closed():
                    self.reopen()
                self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
                break
            except Exception as e:
                msg = str(e).lower()
                if attempt == 1 and ("closed" in msg or "crash" in msg or self._closed()):
                    try:  # Chrome was closed or crashed: open it again and try once more
                        self.reopen("Chrome crashed, so opening it again" if "crash" in msg else
                                    "Chrome was closed, so opening it again")
                    except Exception as e2:
                        log(f"Couldn't open Chrome again: {e2}")
                        return None
                    continue
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
        if not still_searching():
            break
        p = products[pid]
        log(f"More sellers {n}/{len(todo)}: {p['name'][:60]}")
        html = br.get(f"{BASE}/product/{pid}/sellers")
        data = next_data(html) if html else None
        if not data:
            html = br.get(p["url"])
            data = next_data(html) if html else None
        offers = parse_offers(data, pid) if data else []
        new = sum(add_found_seller(sellers, o["seller_name"], o["catalog_seller_id"], p["kw"],
                                   f'"{p["label"]}" more sellers on: {p["name"]}', p["url"], pid, p["name"])
                  for o in offers)
        # Only count it as working if a page really lists more than one seller (the main seller
        # alone is just the one search already showed).
        found_any = found_any or len(offers) > 1 or new > 0
        stats["hidden_checked"] = n
        stats["hidden_found"] += new
        if new:
            log(f"  found {new} hidden sellers ({len(sellers)} total)")
            publish(sellers)
        if n == 25 and not found_any:
            log("  No extra sellers on the first 25 products' seller lists, so skipping hidden sellers")
            stats["hidden_note"] = "Walmart's more-sellers lists couldn't be read, so this step was skipped"
            break


NOT_LOADED = "Walmart page didn't load. Press Finish checking to try again."


def load_seller(br, rec, skip_seller=None, skip_number=None):
    """Open one seller's Walmart page and fill in their details and products.
    Returns False if Walmart's page didn't load. skip_seller(store number) and skip_number(rec)
    can stop early (rec["_skipped"] says why) so known sellers don't cost extra pages."""
    cid = rec["catalog_seller_id"]
    if not cid.isdigit():
        html = br.get(rec["sample_product_url"])
        if not html:
            return False
        cid = str(first(next_data(html), "catalogSellerId") or "") if next_data(html) else ""
        rec["catalog_seller_id"] = cid
    if cid.isdigit():
        rec["seller_url"] = f"{BASE}/seller/{cid}"
        if skip_seller and skip_seller(cid):
            rec["_skipped"] = "seller"
            return True
        html = br.get(rec["seller_url"])
        data = next_data(html) if html else None
        if not data:
            return False
        info = parse_seller_page(data, html)
        rec["products"].update(info.pop("store_products"))
        rec.update(info)
        if skip_number and skip_number(rec):
            rec["_skipped"] = "number"
            return True
        if len(rec["products"]) < 10:
            # The seller page often shows only a few products. Their "Shop all" page lists the
            # rest, so the brand rules see what the store really sells.
            html = br.get(f"{BASE}/seller/{cid}/cp/shopall")
            data = next_data(html) if html else None
            if data:
                rec["products"].update({str(d["usItemId"]): item_text(d)
                                        for d in walk(data) if d.get("usItemId") and d.get("name")})
    rec["store_mix"] = describe_mix(store_mix(rec))
    return True


_SELLER_IDS = []


def known_seller_ids():
    """Walmart store numbers we already have, from the store links saved with numbers and leads."""
    if not _SELLER_IDS:
        ids = set()
        con = db()
        for (details,) in con.execute("SELECT details FROM numbers WHERE details LIKE '%/seller/%'"):
            ids.update(re.findall(r"/seller/(\d+)", details))
        con.close()
        con = leads_db()
        for (data,) in con.execute("SELECT data FROM leads"):
            ids.update(re.findall(r"/seller/(\d+)", data))
        con.close()
        _SELLER_IDS.append(ids)
    return _SELLER_IDS[0]


def number_already_known(rec):
    p = normalize_phone(rec.get("phone"))
    if not p:
        return False
    other = RUN_PHONES.get(p)
    return bool(other and other != rec["seller_name"]) or is_known(p)


def check_seller(br, rec, active_rules):
    """Open one seller's page, fill in their details and apply the rules.
    Returns False if Walmart's page didn't load (the seller stays unchecked).
    With "Skip numbers we already have" on, sellers we already have are skipped as early as
    possible: by their store number before opening anything, then by their phone number
    before loading their full product list."""
    skip_dupes = "no_duplicate_phone" in active_rules
    if not load_seller(br, rec,
                       skip_seller=(lambda cid: cid in known_seller_ids()) if skip_dupes else None,
                       skip_number=number_already_known if skip_dupes else None):
        return False
    skipped = rec.pop("_skipped", "")
    if skipped:
        rec["status"] = "Rejected"
        rec["reason"] = "Already have this seller" if skipped == "seller" else "Already have this number"
        return True
    reasons = [r["check"](rec) for r in RULES if r["id"] in active_rules]
    reasons = [r for r in reasons if r]
    rec["status"] = "Rejected" if reasons else "Good"
    rec["reason"] = "; ".join(reasons)
    phone = normalize_phone(rec.get("phone"))
    if rec["status"] == "Good" and phone and not claim_number(
            phone, f"{rec['seller_name']} · {rec.get('address', '')} · {rec.get('seller_url', '')}"):
        # the other app saved this number a moment ago: whoever found it first keeps it
        rec["status"], rec["reason"] = "Rejected", "Already have this number"
    if rec["status"] == "Good":
        add_lead(row(rec))
    if rec["status"] == "Good" or blocking_reason(rec["reason"]):
        if rec["catalog_seller_id"].isdigit():
            known_seller_ids().add(rec["catalog_seller_id"])
    if rec["status"] == "Good" and phone:
        RUN_PHONES[phone] = rec["seller_name"]
    elif phone and not is_known(phone) and blocking_reason(rec["reason"]):
        add_known([(phone, f"Rejected: {rec['reason']} · {rec['seller_name']} · {rec.get('address', '')} · "
                           f"{rec.get('seller_url', '')}")], "Rejected by app")
    return True


def check_sellers(br, sellers, active_rules):
    todo = [r for r in sellers.values() if r["status"] == "Checking..."]
    for i, rec in enumerate(todo, 1):
        if STATE["stop"]:
            break
        log(f"Checking seller {i}/{len(todo)}: {rec['seller_name']}")
        if not check_seller(br, rec, active_rules):
            rec["reason"] = NOT_LOADED
            log("  couldn't load this seller's page, leaving it for Finish checking")
        publish(sellers)


def run_recheck(active_rules, delay):
    """Open every waiting lead's store again and apply today's rules. Leads that fail are
    removed and blocked; leads whose page doesn't load are kept as they are."""
    rules = set(active_rules) - {"no_duplicate_phone"}  # their number is in the database because they're our lead
    waiting = get_leads("new")
    sellers = {}
    for r in waiting:
        rec = dict(r)
        rec["keywords"] = {k.strip() for k in str(r.get("keywords") or "").split(";") if k.strip()}
        found = re.split(r" search: | more sellers on: ", str(r.get("found_from") or ""), maxsplit=1)
        rec["products"] = {"found": found[1]} if len(found) == 2 and found[1] else {}
        rec["sample_product_url"] = r.get("found_from_url") or r.get("seller_url") or ""
        rec["catalog_seller_id"] = str(r.get("catalog_seller_id") or "")
        rec["status"], rec["reason"] = "Checking...", ""
        rec["lead_key"], rec["lead_phone"] = r["key"], normalize_phone(r.get("phone"))  # as saved, before re-loading
        sellers[r["key"]] = rec
    log(f"Re-checking {len(sellers)} waiting leads with today's rules")
    br, removed, unchecked = None, 0, 0
    try:
        br = Browser(delay)
        publish(sellers)
        for i, rec in enumerate(list(sellers.values()), 1):
            if STATE["stop"]:
                break
            log(f"Re-checking lead {i}/{len(sellers)}: {rec['seller_name']}")
            if not load_seller(br, rec):
                rec["status"], rec["reason"] = "Good", "Kept: Walmart page didn't load, so it couldn't be re-checked"
                unchecked += 1
            else:
                reasons = [r for r in (x["check"](rec) for x in RULES if x["id"] in rules) if r]
                if reasons:
                    reason = "; ".join(reasons)
                    rec["status"], rec["reason"] = "Rejected", f"Re-check: {reason}"
                    remove_lead(rec, f"Rejected on re-check: {reason}", "Rejected by app",
                                key=rec["lead_key"], phone=rec["lead_phone"])
                    removed += 1
                    log(f"  removed: {reason}")
                else:
                    rec["status"], rec["reason"] = "Good", ""
            publish(sellers)
        for rec in sellers.values():  # stopped early: the rest stay as leads
            if rec["status"] == "Checking...":
                rec["status"], rec["reason"] = "Good", "Kept: not re-checked (stopped early)"
        save_csv(sellers)
        log(f"Done! Re-checked {len(sellers)} leads: {removed} removed, {len(sellers) - removed} kept"
            + (f" ({unchecked} couldn't be opened, kept as they were)" if unchecked else "") + ".")
    except Exception as e:
        log(f"Something went wrong: {e}")
        save_csv(sellers)
    finally:
        if br:
            br.close()
        with LOCK:
            STATE["running"] = False
            STATE["stop"] = False
            STATE["skip_search"] = False


def run_finish(active_rules, delay):
    """Finish checking the last search's sellers that weren't checked (stopped early, or pages didn't load)."""
    with LOCK:
        rows = [dict(r) for r in STATE["sellers"]]
    sellers = {}
    for r in rows:
        rec = dict(r)
        rec["keywords"] = {k.strip() for k in str(r.get("keywords") or "").split(";") if k.strip()}
        rec["products"] = {}
        rec["sample_product_url"] = r.get("found_from_url") or r.get("seller_url") or ""
        rec["catalog_seller_id"] = str(r.get("catalog_seller_id") or "")
        if r.get("status") == "Rejected" and r.get("reason") == "Country not shown on seller page":
            rec["status"] = "Checking..."  # older versions marked pages that didn't load this way
        sellers[rec["seller_name"].lower()] = rec
    RUN_PHONES.clear()
    RUN_PHONES.update({normalize_phone(r.get("phone")): r["seller_name"] for r in rows
                       if r.get("status") == "Good" and normalize_phone(r.get("phone"))})
    todo = sum(1 for r in sellers.values() if r["status"] == "Checking...")
    log(f"Finishing the last search: {todo} sellers still to check")
    br = None
    try:
        br = Browser(delay)
        publish(sellers)
        check_sellers(br, sellers, active_rules)
        save_csv(sellers)
        good = sum(1 for s in sellers.values() if s["status"] == "Good")
        left = sum(1 for s in sellers.values() if s["status"] == "Checking...")
        log(f"Done! {good} good sellers in this search" + (f", {left} still not checked" if left else "") +
            f". Saved to {STATE['saved_file']}")
        for kw in {k.strip().lower() for r in sellers.values() for k in r["keywords"]}:
            refresh_search(kw, list(sellers.values()))
    except Exception as e:
        log(f"Something went wrong: {e}")
        save_csv(sellers)
    finally:
        if br:
            br.close()
        with LOCK:
            STATE["running"] = False
            STATE["stop"] = False
            STATE["skip_search"] = False


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
            if not still_searching():
                break
            searched.append(kw)
            plan = search_plan(kw, every_corner)
            if every_corner:
                log(f'Searching every corner of "{kw}": {len(plan)} searches to start with')
            first_ids = None  # first-page products of the first price range, to spot an ignored price filter
            skip_bands = False
            i = 0
            while i < len(plan) and still_searching():
                entry = plan[i]
                i += 1
                label, band = entry["label"], entry["band"]
                if band and skip_bands:
                    continue
                pages_with_results, last_page = 0, None
                stats["searches"] += 1
                for page in range(1, pages + 1):
                    if not still_searching():
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
                # Walmart only shows about 11 pages per search, so a search that long was probably
                # cut off. Split it into smaller price ranges so the rest gets seen too.
                capped = every_corner and pages == MAX_PAGES and pages_with_results >= FULL_SEARCH_PAGES
                if capped and not skip_bands:
                    if band:
                        halves = split_band(*band)
                        if halves:
                            log(f'  "{label}" was cut off by Walmart, so splitting it into smaller price ranges')
                            plan[i:i] = [band_entry(entry["kw"], lo, hi) for lo, hi in halves]
                            stats["splits"] += 1
                    else:
                        log(f'  "{label}" was cut off by Walmart, so searching it by price range too')
                        plan[i:i] = [band_entry(entry["kw"], lo, hi) for lo, hi in PRICE_BANDS]
                        stats["splits"] += 1

        # Step 1b: open each product's all-sellers list to find sellers search doesn't show.
        from_search = len(sellers)
        if hidden_sellers and products and still_searching():
            find_hidden_sellers(br, products, sellers, stats)
        if STATE["skip_search"] and not STATE["stop"]:
            log(f"Stopped searching early, now checking the {len(sellers)} sellers found so far")
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
        check_sellers(br, sellers, active_rules)

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
            STATE["skip_search"] = False


COLUMNS = ["status", "reason", "marketplace", "seller_name", "business_name", "phone", "email", "address", "country",
           "rating", "review_count", "seller_url", "catalog_seller_id", "products_checked", "store_mix", "keywords",
           "found_from", "found_from_url"]


def row(rec):
    r = {k: rec.get(k, "") for k in COLUMNS}
    r["marketplace"] = MARKETPLACE
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
.ok{background:var(--green-soft);color:var(--green)}.rej{background:var(--red-soft);color:var(--red)}.small{font-size:13px;padding:6px 12px}
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
  <button id="skip" class="primary" style="display:none">Stop searching &amp; check now</button>
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
    <button id="recheck" class="small">Re-check with current rules</button>
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
  <div style="margin-top:16px;display:flex;gap:10px;flex-wrap:wrap;align-items:center">
    <button id="start" class="primary big">Start search</button>
    <button id="random" class="big">🎲 Find random sellers</button>
    <span class="muted">Picks 10 random category or brand words (ones you haven't searched first) and starts.</span>
  </div>
</div>
<div class="card">
  <h2>Category ideas</h2>
  <p class="sub">Plain product words for the categories you buy. They find sellers of any brand. Click one to add it to your search.</p>
  <div id="cats"></div>
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
  <div id="breakdown" class="chips" style="margin:0 0 14px"></div>
  <div id="spotBar" style="display:none;margin:0 0 14px"><button id="spotBtn" class="primary small">Spot check 10</button>
    <span class="muted" style="margin-left:8px">Shows 10 random sellers from this search (5 accepted, 5 rejected) so you can check the app's choices.</span></div>
  <div id="spot" style="display:none" class="card"></div>
  <div class="pills">
    <button class="pill on" data-r="Good">Good <span id="nGood">0</span></button>
    <button class="pill" data-r="Rejected">Rejected <span id="nRejected">0</span></button>
    <button class="pill" data-r="Checking..."><span id="checkLabel">Still checking</span> <span id="nChecking">0</span></button>
    <span style="flex:1"></span>
    <button class="primary small" id="finish" style="display:none">Finish checking</button>
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
  return `<div class="name">${esc(x.seller_name)}${x.marketplace?` <span class="tag" style="vertical-align:middle">${esc(x.marketplace)}</span>`:""}</div>`+
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
  $("markAll").style.display=$("dlLeads").style.display=$("recheck").style.display=leadView==="new"&&leads.new.length?"":"none";
  const list=leads[leadView];
  $("leadRows").innerHTML=list.length?list.map(x=>`<tr>
    <td>${sellerCell(x)}</td><td>${contactCell(x)}</td><td>${placeCell(x)}</td><td>${ratingCell(x)}</td>
    <td style="white-space:nowrap;text-align:right">${storeBtn(x)}
      ${leadView==="new"?` <button class="ok small" data-key="${esc(x.key)}" data-to="added">Added to queue ✓</button> <button class="small rej" data-rej="${esc(x.key)}">Reject</button><div class="muted" style="margin-top:6px">Found ${esc(x.found_on)}</div>`
        :` <button class="small" data-key="${esc(x.key)}" data-to="new">Undo</button><div class="muted" style="margin-top:6px">Added ${esc(x.added_on)}</div>`}</td></tr>`).join("")
    :`<tr><td colspan="5" class="empty">${leadView==="new"?"No new leads right now. Go to <b>Search</b> to find more.":"Nothing added to a queue yet."}</td></tr>`;
}
$("leadRows").onclick=e=>{
  const r=e.target.closest("button[data-rej]");
  if(r){const x=leads.new.find(l=>l.key===r.dataset.rej);if(x)rejectSeller(x).then(()=>loadLeads());return;}
  const b=e.target.closest("button[data-key]");if(b)mark([b.dataset.key],b.dataset.to);
};
// Reject a seller by hand: they leave Leads and their number is blocked for good.
async function rejectSeller(x){
  const why=prompt(`Reject ${x.seller_name}?\n\nThey'll leave your Leads and won't come back.\nWhy? (optional, e.g. "private label")`,"");
  if(why===null)return false;
  const r=await (await fetch("/reject",{method:"POST",body:JSON.stringify({seller:x,why})})).json();
  data.sellers.forEach(s=>{if(s.seller_name===x.seller_name&&(s.phone||"")===(x.phone||"")){s.status="Rejected";s.reason=r.reason;}});
  drawResults();return true;
}
$("recheck").onclick=async()=>{
  if(data.running){alert("Wait until the app is done with what it's doing.");return;}
  const n=leads.new.length,mins=Math.max(1,Math.round(n*7/60));
  if(!n||!confirm(`Re-check all ${n} waiting leads with today's rules?\n\nThe app opens each store on Walmart again (about ${mins>90?Math.round(mins/60)+" hours":mins+" minutes"}). Leads that fail a rule are removed and blocked. The rest stay.`))return;
  await fetch("/recheck",{method:"POST"});poll();
};
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
$("skip").onclick=()=>{fetch("/skip",{method:"POST"});$("skip").style.display="none";};
$("finish").onclick=async()=>{
  const rules=[...document.querySelectorAll("#rules input:checked")].map(i=>i.value);
  await fetch("/finish",{method:"POST",body:JSON.stringify({rules})});poll();
};
// Why sellers in the latest search didn't become leads, grouped into a few plain reasons.
function reasonGroup(x){
  if(x.status==="Good")return "New leads";
  if(x.status==="Checking...")return "Not checked yet";
  const r=x.reason||"";
  if(/Already have this (number|seller)|Same phone as/.test(r))return "Already have";
  if(/Not a US seller|Country not shown/.test(r))return "Not US / no US address";
  if(/Mostly sells what we don't buy|Clothing-only/.test(r))return "Sells what we don't buy";
  if(/Private label/.test(r))return "Private label";
  if(/no-name products/.test(r))return "No-name products";
  if(/None of our brands/.test(r))return "None of our brands";
  if(/Industrial store/.test(r))return "Industrial store";
  if(/Rejected by you/.test(r))return "Rejected by you";
  return "Other";
}
// ---- spot check: 10 random sellers from the latest search, half accepted and half rejected
function pickRandom(list,n){const a=[...list];for(let i=a.length-1;i>0;i--){const j=Math.floor(Math.random()*(i+1));[a[i],a[j]]=[a[j],a[i]];}return a.slice(0,n);}
function spotCheck(){
  const good=data.sellers.filter(x=>x.status==="Good");
  const bad=data.sellers.filter(x=>x.status==="Rejected"&&!/Already have this (number|seller)|Same phone as/.test(x.reason||""));
  let g=pickRandom(good,5),b=pickRandom(bad,5);
  if(g.length<5)b=pickRandom(bad,10-g.length);if(b.length<5)g=pickRandom(good,10-b.length);
  const picks=[...g,...b];
  $("spot").style.display="";
  $("spot").innerHTML=`<div class="bar"><h3 class="grow" style="margin:0">Spot check: ${g.length} accepted, ${b.length} rejected</h3>
      <button class="small" id="spotMore">Pick 10 more</button><button class="small" id="spotClose">Close</button></div>
    <div class="muted" style="margin-bottom:8px">Open a few stores. If one looks wrong, send a screenshot to Claude. "Already have this number" rejections are left out, since those are just repeats.</div>
    <div class="tablewrap"><table><thead><tr><th>Decision</th><th>Seller</th><th>Location</th><th></th></tr></thead><tbody>`+
    (picks.length?picks.map(x=>`<tr>
      <td style="width:230px">${x.status==="Good"?'<b style="color:var(--green);white-space:nowrap">✓ Accepted</b>':'<b style="color:var(--red);white-space:nowrap">✗ Rejected</b>'}
        ${x.status!=="Good"?`<div class="why" style="margin-top:4px">${esc(x.reason)}</div>`:""}</td>
      <td>${sellerCell(x)}</td><td>${placeCell(x)}</td><td style="text-align:right;white-space:nowrap">${storeBtn(x)}${x.status==="Good"?` <button class="small rej" data-spot="${picks.indexOf(x)}">Reject</button>`:""}</td></tr>`).join("")
      :`<tr><td colspan="4" class="empty">No checked sellers in this search yet.</td></tr>`)+`</tbody></table></div>`;
  $("spotMore").onclick=spotCheck;$("spotClose").onclick=()=>{$("spot").style.display="none";};
  $("spot").querySelectorAll("button[data-spot]").forEach(b=>b.onclick=async()=>{
    const x=picks[+b.dataset.spot];
    if(await rejectSeller(x)){const td=b.closest("tr").firstElementChild;
      td.innerHTML=`<b style="color:var(--red)">✗ Rejected by you</b>`;b.remove();loadLeads();}
  });
  $("spot").scrollIntoView({behavior:"smooth",block:"start"});
}
$("spotBtn").onclick=spotCheck;
function drawBreakdown(){
  $("spotBar").style.display=!data.running&&data.sellers.some(x=>x.status==="Good"||x.status==="Rejected")?"":"none";
  const counts={};data.sellers.forEach(x=>{const g=reasonGroup(x);counts[g]=(counts[g]||0)+1;});
  const order=["New leads","Already have","Not US / no US address","Sells what we don't buy","Private label","No-name products","None of our brands","Industrial store","Rejected by you","Not checked yet","Other"];
  $("breakdown").innerHTML=data.sellers.length?order.filter(k=>counts[k]).map(k=>
    `<span class="chip" style="cursor:default${k==="New leads"?";background:var(--green-soft);border-color:#b5e3cd;color:var(--green)":""}"><b>${counts[k]}</b>&nbsp;${esc(k)}</span>`).join(""):"";
}
function drawResults(){
  const s=data.sellers;
  drawBreakdown();
  for(const [k,id] of [["Good","nGood"],["Rejected","nRejected"],["Checking...","nChecking"]])$(id).textContent=s.filter(x=>x.status===k).length;
  const unchecked=s.filter(x=>x.status==="Checking...").length+s.filter(x=>x.status==="Rejected"&&x.reason==="Country not shown on seller page").length;
  $("checkLabel").textContent=data.running?"Still checking":"Not checked";
  $("finish").style.display=!data.running&&unchecked?"":"none";
  $("finish").textContent=`Finish checking ${unchecked} sellers`;
  $("resLast").textContent=resView==="Rejected"?"Why rejected":resView==="Checking..."?"Why not checked":"";
  const list=s.filter(x=>x.status===resView);
  $("resRows").innerHTML=list.length?list.map(x=>`<tr>
    <td>${sellerCell(x)}</td><td>${contactCell(x)}</td><td>${placeCell(x)}</td><td>${ratingCell(x)}</td>
    <td style="text-align:right">${resView!=="Good"&&x.reason?`<div class="why" style="text-align:left;max-width:240px">${esc(x.reason)}</div>`:storeBtn(x)}</td></tr>`).join("")
    :`<tr><td colspan="5" class="empty">Nothing here.</td></tr>`;
  $("summary").style.display=data.summary?"":"none";$("summary").textContent=data.summary?"Last search: "+data.summary:"";
  $("lastInfo").textContent=data.saved_file?`Saved to ${data.saved_file} in your walmart folder. Good sellers are also added to Leads.`:"Good sellers from here are also added to Leads automatically.";
}

// ---- brand ideas
let cats={};
fetch("/brands").then(r=>r.json()).then(b=>{brands=b;drawBrands();});
fetch("/categories").then(r=>r.json()).then(c=>{cats=c;drawBrands();});
function drawBrands(){drawIdeas("brands",brands);drawIdeas("cats",cats);}
function drawIdeas(id,ideas){
  const picked=new Set(words().map(w=>w.toLowerCase())), hist={};
  history.forEach(h=>hist[h.keyword]=h);
  $(id).innerHTML=Object.entries(ideas).map(([cat,list])=>`<div class="cat">${esc(cat)}</div><div class="chips">`+
    list.map(b=>{const k=b.toLowerCase(),h=hist[k];
      const cls=picked.has(k)?"picked":h?(h.due?"due":"done"):"";
      return `<button class="chip ${cls}" data-b="${esc(b)}" title="${h?`Last searched ${esc(h.last_run)}`:"Not searched yet"}">${esc(b)}</button>`;}).join("")+`</div>`).join("");
}
// ---- random search: 10 words, never-searched first, then ones that are due, then the rest
$("random").onclick=()=>{
  if(data.running){alert("Wait until the app is done with what it's doing.");return;}
  const hist={};history.forEach(h=>hist[h.keyword]=h);
  const seen=new Set(),all=[...Object.values(cats).flat(),...Object.values(brands).flat()].filter(w=>!seen.has(w.toLowerCase())&&seen.add(w.toLowerCase()));
  const shuffle=a=>{for(let i=a.length-1;i>0;i--){const j=Math.floor(Math.random()*(i+1));[a[i],a[j]]=[a[j],a[i]];}return a;};
  const fresh=shuffle(all.filter(w=>!hist[w.toLowerCase()])),due=shuffle(all.filter(w=>hist[w.toLowerCase()]?.due)),
        rest=shuffle(all.filter(w=>hist[w.toLowerCase()]&&!hist[w.toLowerCase()].due));
  setWords([...fresh,...due,...rest].slice(0,10));
  $("start").click();
};
$("cats").onclick=e=>$("brands").onclick(e);
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
    $("skip").style.display=checking?"none":"";
    const rechecking=/Re-checking/.test(data.log[0]||"");
    if(rechecking){$("skip").style.display="none";$("runTitle").textContent=`Re-checking leads: ${done} of ${s.length} done`;}
    else $("runTitle").textContent=checking?`Checking sellers: ${done} of ${s.length} done`:`Searching Walmart... ${s.length} sellers found so far`;
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
                status["known_count"] = known_count()
                counts = lead_counts()
                status["leads_new"], status["leads_added"] = counts.get("new", 0), counts.get("added", 0)
                self.send(json.dumps(status, default=str))
        elif url.path == "/lookup":
            self.send(json.dumps(lookup(parse_qs(url.query).get("q", [""])[0])))
        elif url.path == "/brands":
            self.send(json.dumps(BRAND_IDEAS))
        elif url.path == "/categories":
            self.send(json.dumps(CATEGORY_IDEAS))
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
            return self.send(json.dumps({"found": unique, "added": added, "total": known_count()}))
        body = json.loads(raw or b"{}")
        if self.path == "/start":
            with LOCK:
                if STATE["running"]:
                    return self.send('{"ok":false}')
                STATE.update(running=True, stop=False, skip_search=False, log=[], sellers=[], saved_file="", summary="")
            pages = max(1, min(MAX_PAGES, int(body.get("pages") or 3)))
            threading.Thread(target=run_scrape, daemon=True,
                             args=(body["keywords"], pages, set(body.get("rules", [])), 2.0, bool(body.get("every_corner")),
                                   bool(body.get("hidden_sellers")))).start()
            self.send('{"ok":true}')
        elif self.path == "/reject":
            self.send(json.dumps({"reason": reject_seller(body.get("seller") or {}, (body.get("why") or "").strip()[:200])}))
        elif self.path == "/leads/mark":
            mark_leads(body.get("keys", []), "added" if body.get("status") == "added" else "new")
            self.send('{"ok":true}')
        elif self.path == "/recheck":
            with LOCK:
                if STATE["running"]:
                    return self.send('{"ok":false}')
                STATE.update(running=True, stop=False, skip_search=False, log=[], summary="")
            threading.Thread(target=run_recheck, daemon=True,
                             args=({r["id"] for r in RULES}, 2.0)).start()
            self.send('{"ok":true}')
        elif self.path == "/finish":
            with LOCK:
                if STATE["running"]:
                    return self.send('{"ok":false}')
                STATE.update(running=True, stop=False, log=[], summary="")
            threading.Thread(target=run_finish, daemon=True, args=(set(body.get("rules", [])), 2.0)).start()
            self.send('{"ok":true}')
        elif self.path == "/skip":
            if STATE["running"]:
                STATE["skip_search"] = True
                log("Stopping the search after this page, then checking the sellers found so far...")
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
