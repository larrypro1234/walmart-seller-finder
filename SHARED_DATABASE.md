# Shared seller database (Walmart + Amazon finders)

The Walmart Seller Finder and the Amazon seller finder run on the same Mac and share **one SQLite database**, so both always know which sellers we already have, and nobody gets the same lead twice.

This page tells the Amazon app exactly how to use it. Follow it as written. Do not rename tables or columns, and do not delete rows: the Walmart app reads the same data.

## Where it is

```
~/SellerFinderData/numbers.db
```

`Path.home() / "SellerFinderData" / "numbers.db"`. Create the folder if it doesn't exist. Never copy this file somewhere else to work on it: both apps must open this one file.

It doesn't matter which app creates it first: the Walmart app merges its own older database into this one the first time it starts with the shared setup.

## How to connect (every time)

```python
import sqlite3
from pathlib import Path

DB_FILE = Path.home() / "SellerFinderData" / "numbers.db"
DB_FILE.parent.mkdir(exist_ok=True)

def db():
    con = sqlite3.connect(str(DB_FILE), timeout=30)   # wait (don't fail) while the other app writes
    con.execute("PRAGMA journal_mode=WAL")             # both apps can read and write at the same time
    con.execute("CREATE TABLE IF NOT EXISTS numbers (phone TEXT PRIMARY KEY, details TEXT NOT NULL DEFAULT '', "
                "source TEXT NOT NULL DEFAULT '', added TEXT NOT NULL DEFAULT '')")
    con.execute("CREATE TABLE IF NOT EXISTS leads (key TEXT PRIMARY KEY, data TEXT NOT NULL, "
                "status TEXT NOT NULL DEFAULT 'new', found TEXT NOT NULL DEFAULT '', added TEXT NOT NULL DEFAULT '')")
    return con
```

Rules for not fighting:
- Open a connection, do one small thing, commit, close. Never keep a transaction open while scraping or waiting on a web page.
- Use `timeout=30` and WAL mode as above. Don't use exclusive locks.

## Phone numbers are the key

Every phone number is stored as **exactly 10 digits**, US only, no `+1`, no spaces or dashes:

```python
import re

def normalize_phone(p):
    digits = re.sub(r"\D", "", str(p or ""))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) == 10 else ""   # "" means: not a usable US number
```

`(917) 592-1495`, `+1 917-592-1495` and `9175921495` are all `9175921495`.

## Table `numbers`: every number we already have

| column | meaning |
|---|---|
| `phone` | 10 digits, primary key |
| `details` | free text about who it is, e.g. `Seller Name · address · store link` |
| `source` | where it came from, e.g. `Found by Amazon app`, `Rejected by Amazon app`, `Uploaded: file.csv` |
| `added` | `YYYY-MM-DD` |

**Before treating a seller as new, check this table right then** (not a copy loaded at startup — the Walmart app may have added the number a minute ago):

```python
def is_known(phone):
    con = db()
    hit = con.execute("SELECT 1 FROM numbers WHERE phone = ?", (phone,)).fetchone()
    con.close()
    return hit is not None
```

If `is_known(phone)` is true, the seller is **not** a new lead (reason: "Already have this number").

**Saving a number** (never overwrite details someone else saved):

```python
from datetime import datetime

def add_number(phone, details, source):
    con = db()
    with con:
        con.execute(
            "INSERT INTO numbers (phone, details, source, added) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(phone) DO UPDATE SET details = CASE WHEN numbers.details = '' "
            "THEN excluded.details ELSE numbers.details END",
            (phone, details, source, datetime.now().strftime("%Y-%m-%d")))
    con.close()
```

Save a number when:
- a seller passes all your rules (a good lead): `source = "Found by Amazon app"`
- a seller is rejected for a real reason about the seller (not US, sells what we don't buy): `source = "Rejected by Amazon app"`, and start `details` with `Rejected: <reason> · `. This blocks them in both apps from then on.
- **Don't** save a number when the seller was only skipped because a page failed to load.

## Table `leads`: the shared Leads inbox

Good sellers wait here until someone clicks "Added to queue" in the Walmart app (it shows Amazon leads too, tagged Amazon).

| column | meaning |
|---|---|
| `key` | the 10-digit phone; if the seller has no phone, their store link |
| `data` | JSON object, fields below |
| `status` | `new` when you add it. The Walmart app sets `added` when it's handed to a rep. **Never change status yourself.** |
| `found` | `YYYY-MM-DD HH:MM` |
| `added` | leave empty |

Add a lead with `INSERT OR IGNORE`, so a seller that already has a lead (from either app) is not duplicated:

```python
import json

def add_lead(lead):
    key = normalize_phone(lead.get("phone")) or lead.get("seller_url") or lead["seller_name"].lower()
    con = db()
    with con:
        con.execute("INSERT OR IGNORE INTO leads (key, data, found) VALUES (?, ?, ?)",
                    (key, json.dumps(lead), datetime.now().strftime("%Y-%m-%d %H:%M")))
    con.close()
```

`data` fields (use these exact names; leave a field `""` if you don't have it):

```json
{
  "marketplace": "Amazon",
  "status": "Good",
  "seller_name": "Toy Depot LLC",
  "business_name": "Toy Depot Holdings LLC",
  "phone": "(512) 555-0100",
  "email": "sales@example.com",
  "address": "1 Main St, Austin, TX, 78701",
  "rating": "4.7",
  "review_count": "312",
  "seller_url": "https://www.amazon.com/sp?seller=XXXXXXXX",
  "store_mix": "Toys & Games 12 · Health & Beauty 3",
  "found_from": "\"lego\" search: LEGO Classic Bricks",
  "found_from_url": "https://www.amazon.com/dp/XXXXXXXX",
  "keywords": "lego"
}
```

`marketplace` must be `"Amazon"` for every Amazon lead. `seller_url` is the seller's own storefront page.

## Same seller found by both apps: Walmart wins

- If the Walmart app finds the seller first, the Amazon app sees the number in `numbers` and treats it as "Already have this number". Nothing to do.
- If the Amazon app found the seller first and its lead is still `status = 'new'`, the Walmart app **takes the lead over**: it rewrites that lead's `data` with its own (marketplace `Walmart`) and changes the number's `source` to `Found by app (took over from Amazon)`. This is expected. The Amazon app must not undo it or add the seller again (it won't, if it follows this page: `is_known` is true and leads use `INSERT OR IGNORE`).
- If the Amazon lead was already given to a rep (`status = 'added'`), the Walmart app leaves it alone.

## Search history

Don't write to the `searches` table (it's the Walmart app's search history). If the Amazon app keeps a search history, give it its own table, e.g. `amazon_searches`.

## Numbers the Amazon app already has

If the Amazon app kept its own list of numbers or leads before this, import them **once** with `add_number(...)` (`source = "Found by Amazon app"`) and good leads with `add_lead(...)`. After that, use only the shared database.

## Don'ts
- Don't delete or rewrite rows, and don't drop or rename tables or columns.
- Don't store numbers anywhere else as the "real" list.
- Don't put `numbers.db` or any phone numbers in a GitHub repository.
- Don't use ports 8765–8784 for the Amazon app's web page (the Walmart app uses them).
