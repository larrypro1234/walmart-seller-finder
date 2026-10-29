# Walmart Seller Finder

A small app that searches Walmart.com and finds third-party Marketplace sellers, checks them against your rules, and saves the good ones to a spreadsheet.

## Rules
- **Only US sellers**: the seller's business address must be in the US (US state + ZIP code).
- **Only stores that sell what we buy**: each product is sorted into a buy category (Toys & Games, Health & Beauty, OTC, Personal Care, Household Essentials, Baby, Supplements, Apparel basics), a don't-buy category (Clothing, Shoes, Automotive, Outdoor & Camping, Books, Movies & Music, Bedding & Linens, Furniture, Home décor, Costumes, Party supplies, Medical equipment, Used/Refurbished, Computers, Phone cases, Security cameras, Jewelry, Precious metals, Alcohol), or neutral (Grocery, Electronics). A store is rejected only when 60%+ of its sorted products are don't-buy and it has fewer than 3 (and under 25%) buy products. Stores with fewer than 3 sorted products are kept.
- **Skip numbers we already have**: rejected if the seller's phone is in your numbers list, or another seller in the same search already has it.

## Numbers we already have
Click **Upload numbers** in the app and pick an Excel (.xlsx), CSV or text file. Every phone number in it is saved to `numbers.db` (a small database next to `app.py`) together with the rest of its spreadsheet row, the file it came from, and the date. Good sellers the app finds are added automatically.

Use **Look up a number or name** in the app to search it: any phone format, part of a number, or words like a company or setter name.

This database stays on your computer and is never uploaded to GitHub.

## Running it (Mac)
Double-click the **Walmart Seller Finder** icon on the Desktop. The app opens in your browser.
Click **Quit app** in the top right to turn it off.

## Updates
The app checks this repository for a newer `VERSION` when it starts (and every 30 minutes while open) and updates itself.
To ship a change: edit `app.py`, increase `VERSION`, and push to `main`.
