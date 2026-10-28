# Walmart Seller Finder

A small app that searches Walmart.com and finds third-party Marketplace sellers, checks them against your rules, and saves the good ones to a spreadsheet.

## Rules
- **Only US sellers**: the seller's business address must be in the US (US state + ZIP code).
- **No clothing-only stores**: rejected if 80% or more of the seller's products are clothing.

New rules go in the `RULES` list in `app.py` and show up as checkboxes automatically.

## Running it (Mac)
Double-click the **Walmart Seller Finder** icon on the Desktop. The app opens in your browser.
Click **Quit app** in the top right to turn it off.

## Updates
The app checks this repository for a newer `VERSION` when it starts (and every 30 minutes while open) and updates itself.
To ship a change: edit `app.py`, increase `VERSION`, and push to `main`.
