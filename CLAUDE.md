# PTOX11 / RennMarkt — Project Bible for Claude
*Last updated: May 5, 2026*

---

## 1. Project Overview

Two-product Porsche market intelligence platform on a Mac Mini M4.

| Product | URL | Purpose |
|---|---|---|
| **RennMarkt** | rennmarkt.net | Retail listing aggregator — 4,100+ listings, FMV scoring, deal alerts |
| **RennAuktion** | rennauktion.com | Auction watcher — BaT / C&B / pcarmarket live auctions, comp graphs |

**Repo:** https://github.com/OCX11/rennmarkt  
**Machine:** Mac Mini M4, user: claw, 24/7  
**DB:** ~/porsche-tracker/data/inventory.db (SQLite, WAL mode)  
**Logs:** ~/porsche-tracker/logs/

### Business Context
Small performance car dealership. All purchases are investments. Core range $70K–$150K, GT/collector no ceiling. Owner has ~40 years high-end automotive inspection background.

---

## 2. Hard Rules (never override without owner confirmation)

- **YEAR_MAX=2024** — locked until Jan 1 2027. Owner decision required to change.
- **Never alert on:** Cayenne, Panamera, Macan, Taycan — excluded at scrape level.
- **pywebpush: stay on 1.14.1** — 2.x has Apple JWT bug (BadJwtToken on Apple push).
- **VAPID sub claim must be https URL** — not mailto: (Apple requirement).
- **GitHub PAT: no expiry** — confirmed April 25, 2026.
- **No proxy fallback** — AutoTrader uses Decodo API, cars.com uses Scrapling. No bare IP fallback ever.
- **TASK BOARD — ONE NOTE ONLY:** Exactly one note: "🏎 PTOX11 / RennMarkt — To Do List". NEVER create a new one. Use Apple Notes MCP directly (apple-notes-mcp v1.4.1 is working). note_writer.py is RETIRED.
- **Session start:** Read this file + task board note before any code work.

---

## 3. Repo Structure (post-split May 2026)

```
~/porsche-tracker/
├── rennmarkt/                  # Retail platform
│   ├── main.py                 # Entry point — scrape + FMV + dashboard + alerts
│   ├── build_dashboard.py      # Generates docs/dashboard.html
│   ├── notify_push.py          # Push alerts (new listings, DOM, watchlist)
│   └── scrapers/
│       ├── autotrader.py       # Decodo Web Scraping API
│       ├── carscom.py          # Scrapling
│       ├── dupont.py           # Direct API
│       ├── ebay.py             # Browse API OAuth2
│       ├── bfb.py              # curl_cffi
│       ├── rennlist.py         # curl_cffi
│       └── pca_mart.py         # Playwright
├── rennauktion/                # Auction platform
│   ├── main.py                 # Entry point — scrape + comps + dashboard
│   ├── build_dashboard.py      # Generates docs/auctions.html + auction_comps.json
│   ├── comp_scraper.py         # Daily BaT + C&B sold comp scrape
│   ├── notify_push.py          # Auction-ending alerts
│   └── scrapers/
│       ├── bat.py              # BaT Playwright
│       ├── cnb.py              # C&B Playwright
│       └── pcarmarket.py       # pcarmarket Playwright
├── core/                       # Shared business logic
│   ├── db.py                   # DB layer — init, upsert_listing, mark_sold, FMV columns
│   ├── fmv.py                  # FMV engine — get_fmv(), score_and_persist()
│   ├── vin_decoder.py          # VIN → generation decode
│   └── config.py               # Shared config
├── shared/
│   └── scraper_utils.py        # _is_valid_listing(), shared scraper helpers
├── docs/                       # GitHub Pages output
│   ├── index.html              # RennMarkt splash/waitlist
│   ├── dashboard.html          # RennMarkt main dashboard (~94 KB)
│   ├── auctions.html           # RennAuktion dashboard (~790 KB)
│   ├── retail_comps.json       # Comp dots for retail card graphs (~7 MB, lazy-loaded)
│   ├── auction_comps.json      # Comp dots for auction cards (~635 KB, lazy-loaded)
│   └── search_data.json        # Search index with FMV fields (~1.5 MB)
├── data/
│   ├── inventory.db            # SQLite — all tables
│   ├── push_subscriptions.json # 2 active Apple push subscriptions
│   ├── seen_alerts_push.json   # Alert dedup (30-day prune, ~3K entries)
│   ├── decodo_config.json      # Decodo API credentials (AutoTrader)
│   └── ebay_api_config.json    # eBay OAuth credentials
├── archive/
│   └── pre_split_2026-05-02/   # Old monolith scrapers (RETIRED)
├── main.py                     # ROOT — RETIRED. Prints error and exits.
└── CLAUDE.md                   # This file
```

---

## 4. Active Sources (May 2026)

| Source | Active | Method | Images |
|---|---|---|---|
| eBay Motors | ~1,825 | Browse API OAuth2, incremental | ✅ 100% |
| AutoTrader | ~958 | Decodo Web Scraping API | ✅ 100% |
| DuPont Registry | ~916 | Direct API (POST) | ✅ ~97% |
| cars.com | ~290 | Scrapling | ✅ 99% |
| PCA Mart | ~43 | Playwright cookie-auth | ✅ CDN URLs |
| Bring a Trailer | ~38 | Playwright | ✅ 100% |
| Rennlist | ~28 | curl_cffi (CF bypass) | ✅ 100% |
| Cars and Bids | ~12 | Playwright scroll | ✅ 100% |
| pcarmarket | ~7 | Playwright | ✅ 100% |
| Built for Backroads | ~5 | curl_cffi | ✅ 100% |

**Total active: ~4,122 listings. Zero Distill dependency.**

---

## 5. Schedules (launchd)

| Job | Label | Cadence |
|---|---|---|
| RennMarkt scrape | com.rennmarkt.scrape | Every 720s (12 min) |
| RennAuktion scrape | com.rennauktion.scrape | Every 300s (5 min) |
| Git push dashboard | com.rennmarkt.gitpush | Every 120s (2 min) |
| Comp scraper | com.rennauktion.comps | Daily |
| Push server | com.rennmarkt.pushserver | Persistent daemon |

---

## 6. Database

### Tables
- **listings** — active + sold. FMV columns: `fmv_value, fmv_confidence, fmv_comp_count, fmv_low, fmv_high, fmv_pct, fmv_updated_at`
- **sold_comps** — 6,656 records (BaT: 6,049, C&B: 489, pcarmarket: 34). Auto-expires >24mo.
- **price_history** — every price change per listing
- **vin_history** — cross-source VIN tracking (5,636 rows: listed/price_change/cross_source/relisted)
- **bat_reserve_not_met** — historical BaT reserve signals
- **snapshots** — daily raw snapshots per dealer
- **vin_nhtsa_cache** — NHTSA VIN decode cache

### upsert_listing dedup priority (core/db.py)
1. VIN match (dealer + vin UNIQUE index)
2. listing_url match (dealer + listing_url)
3. DuPont fallback: car ID tail match
4. year/make/model fallback (non-eBay, non-DuPont only)

### FMV Engine (core/fmv.py)
- Source: BaT sold comps (weight 1.0), recency decay ≤6mo full → 0.3 at 24mo
- Groups by: generation + trim family
- Confidence: HIGH (10+ comps) / MEDIUM (4-9) / LOW (1-3) / NONE (0)
- Current: 90% HIGH, 10% MEDIUM
- `score_and_persist()` runs every scrape cycle — writes to DB, not computed at dashboard time

---

## 7. Scraper Notes

### AutoTrader
- Uses Decodo Web Scraping API (`scraper-api.decodo.com/v2/scrape`)
- Config: `data/decodo_config.json`
- No proxy needed — Decodo handles Akamai bypass
- Gate: checks `_DECODO_TOKEN` (not `_PROXY_URL` — that was the old DataImpulse guard, now removed)

### eBay Motors
- Browse API OAuth2. Config: `data/ebay_api_config.json`
- URLs normalised to canonical `https://www.ebay.com/itm/{id}` (no tracking params)
- VIN coverage: ~17% (eBay doesn't reliably expose VIN via API)

### cars.com
- Scrapling library (replaced curl_cffi + DataImpulse)
- State: `data/carscom_state.json` — `{"bootstrapped": true}`

### DataImpulse / Webshare
- **RETIRED** — both cancelled May 2026. No proxy config files remain active.
- Do not add any `_PROXY_URL` or `proxy_config.json` references.

---

## 8. Alert System

| Alert type | Status | File |
|---|---|---|
| New-listing push | ✅ ACTIVE | rennmarkt/notify_push.py |
| Auction-ending push | ✅ ACTIVE | rennauktion/notify_push.py |
| DOM (days-on-market) push | ✅ ACTIVE | rennmarkt/notify_push.py |
| Watchlist push | ✅ ACTIVE | rennmarkt/notify_push.py |

**Push infra:** Cloudflare Worker proxy `ptox11-push.openclawx1.workers.dev`, launchd push server (com.rennmarkt.pushserver), 2 active Apple push subscriptions.

---

## 9. Auth / RennAuktion

- **Supabase project:** shared between rennmarkt.net + rennauktion.com
- **Auth methods:** email+password, magic link (Resend SMTP), Google OAuth
- **⚠️ Google OAuth in TESTING mode** — publish app in Google Cloud Console before public launch
- **⚠️ Resend DNS** — authorize DKIM/SPF records in Resend dashboard
- **Tables:** profiles, saved_auctions, saved_listings, alert_prefs (RLS live)
- **Cloudflare Pages:** rennauktion.pages.dev + rennauktion.com

---

## 10. Key Patterns & Gotchas

- **curl_cffi proxies:** require explicit `proxies={"http": url, "https": url}` — session config alone is not enough. (Now moot — no active proxy.)
- **SQLite WAL mode:** enabled. Both rennmarkt and rennauktion write concurrently — WAL handles it. Watch logs for `SQLITE_BUSY`.
- **eBay URL normalisation:** strip `?_skw=...&hash=...` before DB upsert AND before building seen_alerts keys.
- **Partial scrape guard:** fires when scraped count < threshold vs active count. If DB grows faster than scrape depth, guard trips permanently. Use `cleanup_stale_retail_listings(conn, days=14)` in core/db.py as safety net.
- **Auction comp promotion:** `promote_auction_comps.py` has `sold_date > today` guard at line 80. Never promotes live auctions.
- **VIN decoder series codes:** AE2/AE3 = 718 Cayman GT4/GT4 RS, CE2/CE3 = 718 Boxster Spyder RS, CC2/CB3/CD2 = standard 718. AA2 in HANDOVER was never correct for Caymans — actual series is AE2/CE2 for GT variants.
- **comp dots JSON:** `retail_comps.json` uses short keys `{p, d, t, mi, yr}` (not `price, date, trim`). `auction_comps.json` uses full keys.
- **root main.py:** RETIRED — exits with error message. Use `rennmarkt/main.py` or `rennauktion/main.py`.

---

## 11. Session Log (recent)

### May 5, 2026
- Audit v4 complete. 3 P1s identified: AutoTrader crash, zombie listings, dashboard.html size.
- AutoTrader `_PROXY_URL` NameError fixed (rennmarkt/scrapers/autotrader.py).
- `cleanup_stale_retail_listings(conn, days=14)` added to core/db.py + rennmarkt/main.py. 1,524 zombies cleared.
- `auctions.html`: 1,644KB → 790KB. Comp dots moved to auction_comps.json (lazy-loaded).
- Cayman AE2/CE2 VIN decode added to core/vin_decoder.py. 6 listings backfilled.
- 3 duplicate eBay URL groups deleted (split-day artifact, not ongoing bug).
- Root scrapers archived to archive/pre_split_2026-05-02/. Root main.py now exits with error.
- `fmv_low, fmv_high, fmv_pct` added to search_data.json SELECT.
- Retail card hover comp graph added (expand panel + _drawRetailGraph JS + retail_comps.json).
- Zero-comp fallback added to both dashboard graph renderers.
- All uncommitted HTML + new files committed. Working tree clean.
- seen_alerts_push.json: 219 eBay param keys normalised, 26 stale entries pruned.
- CLAUDE.md fully updated to reflect post-split architecture.

### May 2–4, 2026
- Repo split: rennmarkt/ + rennauktion/ + core/ + shared/ live.
- DataImpulse retired. AutoTrader → Decodo. cars.com → Scrapling.
- C&B comp backfill: 487 comps. FMV HIGH: 78% → 90%. Generation fill: 55% → 98%.
- RennAuktion live: Cloudflare Pages, Supabase auth (email+pw, magic link, Google OAuth).
- scrape-deep race condition fixed. Dashboard auto-refresh fixed. Health monitor ghost alerts fixed.

### April 22, 2026 (Audit v3)
- FMV persisted to DB (7 columns). eBay URL normalisation. Future-dated comps deleted.
- VIN history table live. C&B + pcarmarket + BaT scraper issues resolved.
