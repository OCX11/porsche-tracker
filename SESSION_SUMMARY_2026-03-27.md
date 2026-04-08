# Project Summary & Decision Log – March 27, 2026

## Current Project Goal
Build a Porsche-focused FMV engine backed by a deep historical sold-comp database from BaT, with VIN-decoded generation tags and full listing detail enrichment (engine, drivetrain, color, options) to power accurate deal scoring.

---

## Key Decisions Made

- **Switched BaT backfill from HTML scraping to JSON API** — `wp-json/bringatrailer/1.0/data/listings-filter` returns pre-rendered sold data including price/title/URL; the old HTML scrape required JS execution and was unreliable
- **Hard allowlist filter: 911/Cayman/Boxster/718 only** — removed Cayenne/Macan/Panamera/Taycan at the API query level to cut irrelevant data and speed up the backfill (~15 filtered per page)
- **Deleted all 5,738 previous BaT comps and restarted fresh** — prior data was collected without the model filter; cleaner to start over than patch
- **VINs scraped from individual listing pages, not the index API** — BaT's JSON API doesn't return VINs; only available on each listing's HTML page under `.essentials li` → "Chassis: WP0..."
- **Expanded enrichment from VIN-only to full listing detail extraction** — since we're visiting each page anyway, pulling engine, drivetrain, color, and options JSON at the same time costs nothing and is far more valuable for FMV
- **Added `generation` column decoded from VIN positions 4-6 + model year** — maps VIN series codes to human labels (992, 991, 997, 996, 993, 964, 930, 987, 986, 981, 718/982, Classic)
- **Notifications disabled during backfill** — `NOTIFICATIONS_ENABLED = False` flag in `notify_gunther.py` to prevent false alerts while data is incomplete

---

## Major Changes & Iterations

- **v1 Backfill**: Scraped `/auctions/results/` HTML with BeautifulSoup → returned all makes, only ~17 Porsches per page, no sold price, JS-rendered content missing
- **v2 Backfill**: Switched to BaT JSON API → correct paginated results, sold price included, ~24 items/page
- **v1 Filter**: Blocklist approach (drop Cayenne/Macan/etc.) → missed edge cases, still pulled unwanted cars
- **v2 Filter**: Allowlist (911/Cayman/Boxster/718 keyword match on title) → clean, fast, no ambiguity
- **v1 Enrich**: VIN-only scrape from `.listing-essentials li` with "Chassis: X" label matching → failed because BaT uses free-text format ("Chassis: WP0..."), not a structured label
- **v2 Enrich**: Regex on raw HTML for WP0 pattern → VINs working, mileage/transmission still missing (no fallback)
- **v3 Enrich**: Fixed parser to match BaT's actual free-text `<li>` format ("51k Miles Shown", "Five-Speed Manual Transaxle") → mileage and transmission now populating
- **v4 Enrich**: Expanded to capture ALL listing detail fields per page visit — engine, drivetrain, color, and options (JSON list of remaining bullet points like wheels, interior, PCCB, LSD, etc.)
- **Added `decode_vin_generation.py`**: Standalone script that reads all VINs from sold_comps and writes decoded generation to new `generation` column — idempotent, safe to re-run

---

## Current Status

### Done
- BaT backfill complete: **5,652 comps** in DB (pages 1–416, June 2024 → March 2026)
- DB schema extended: `generation`, `engine`, `drivetrain`, `options` columns added
- `decode_vin_generation.py` built and tested — correctly decodes 964/993/996/997/991/992/986/987/981/718/930/Classic from VIN
- Enrich script v4 running — pulling VIN, mileage, transmission, engine, drivetrain, color, options in one page visit per listing

### In Progress
- **`enrich_bat_vins.py` running overnight** — ~5,435 listings remaining at ~7s each (~10-11 hours). Will be done by morning.
- After enrich completes, re-run `decode_vin_generation.py` to decode all newly populated VINs

### Blocked / Known Issues
- `drivetrain` field showing 0 populated — BaT phrases it as "All-Wheel Drive" inline in an `<li>` that may also match mileage/transmission first; regex priority may need tuning
- Some listings show `miles=48000` across multiple cars — suspected generic page element being hit by fallback regex; individual listing parser (`.essentials li`) should override this but needs verification
- Pre-1980 cars have short/non-standard VINs — decoder returns "Classic" or None for these; acceptable
- Mileage not listed on some BaT listings (genuinely optional on BaT) — these stay NULL, that's correct

---

## Open Questions / Next Steps

1. **Morning: verify enrich completion** — `tail -5 ~/porsche-tracker/logs/enrich.log` and check DB counts for engine/drivetrain/options
2. **Re-run `decode_vin_generation.py`** after enrich finishes to decode all 5,600+ VINs
3. **Audit drivetrain field** — check if AWD/RWD is populating correctly; may need to loosen regex or lower its priority in the `<li>` parsing loop
4. **Build FMV engine** — now that we have generation, mileage, transmission, engine, color, and options per comp, we can build meaningful per-generation price bands with option adjustments
5. **Wire generation into dashboard** — filter/group comps by generation (e.g. "show me all 991.2 GT3 comps")
6. **Differentiate 997.1 vs 997.2 and 991.1 vs 991.2** — currently decoder lumps both into "997" and "991"; can split by model year (997.1: 2005-2008, 997.2: 2009-2012; 991.1: 2012-2015, 991.2: 2016-2019)
7. **Add 3rd VIN decoder URL** — user had a 3rd resource (sent as HEIC image, couldn't be read); add when available
8. **Re-enable Günther alerts** once FMV engine is live and deal scores are calculated

---

*Enrich script PID: running in Terminal on Mac Mini. Log: `~/porsche-tracker/logs/enrich.log`. DB: `~/porsche-tracker/data/inventory.db`.*
