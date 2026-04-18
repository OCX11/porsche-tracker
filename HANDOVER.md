# PTOX11 — Porsche Market Intelligence Platform
*Last updated: April 18, 2026 (post-redesign)*

---

## 1. Project Overview

Autonomous Porsche market intelligence platform on a Mac Mini M4. Scrapes 10 sources every 12 minutes, scores every listing against FMV using 6,010 BaT sold comps, and fires iMessage alerts the moment a new listing enters the DB.

**Repo:** https://github.com/OCX11/PTOX11  
**Dashboard:** https://ocx11.github.io/PTOX11/  
**Auctions:** https://ocx11.github.io/PTOX11/auctions.html  
**Machine:** Mac Mini M4, user: claw, 24/7

### Business Context
Small performance car dealership. All purchases are investments. Core range $70K–$150K, GT/collector no ceiling.

---

## 2. Target Vehicles

### Tier 1 — GT / Collector (alert immediately on any new listing)
- 911: GT3, GT3 RS, GT2, GT2 RS, R, Speedster, Sport Classic, Touring (996/997/991/992)
- 911: All air-cooled — 930, 964, 993 (pre-1998)
- Cayman: GT4, GT4 RS, Spyder, R (987/981/718)
- Boxster: Spyder (987/981/718)
- Any Turbo S variant · 356, 914-6

### Tier 2 — Standard (alert on any new listing)
- 911: Carrera, S, 4S, GTS, Targa (996/997/991/992)
- Cayman: S, GTS (987/981/718) · Boxster: Base, S, GTS (987/981/718)

### Never
- Cayenne, Panamera, Macan, Taycan — excluded at scrape level
- Year: 1986–2024 | Mileage: <100k | Price: <$5,000 (non-auction)
- **⚠️ HARD RULE — YEAR_MAX=2024:** Locked until Jan 1 2027. Owner decision required.

---

## 3. Active Sources (April 18, 2026)

| Source | Active | Method | Images |
|---|---|---|---|
| DuPont Registry | ~889 | Direct API (api.dupontregistry.com POST) | ✅ 100% |
| eBay Motors | ~662 | Browse API OAuth2, cache+incremental+seller sweep | ✅ 100% |
| cars.com | ~269 | curl_cffi, 5 model slugs, VIN-stop incremental | ✅ 99% |
| Bring a Trailer | 42 | Playwright | ✅ 100% |
| PCA Mart | 32 | Playwright cookie-auth | ✅ CDN URLs stored |
| Cars and Bids | 11 | Playwright scroll | ✅ 100% |
| Built for Backroads | 11 | curl_cffi | ✅ 100% |
| pcarmarket | 10 | Playwright | ✅ 100% |
| AutoTrader | 8 | curl_cffi + headless PW fallback | ⚠️ 80% |
| Rennlist | 5 | curl_cffi (Cloudflare bypass) | ✅ 100% |

**Total active: ~1,939 listings. All local — zero Distill dependency.**

### DuPont Registry — key notes
- API: POST api.dupontregistry.com/api/v1/en_US/car/list, filter carBrand=[14], currentPage pagination
- URL format: /autos/listing/{year}/porsche/{model-alias}/{id} — CRITICAL, old format redirects to /dealers
- upsert_listing matches DuPont by car ID tail (last /\d+) to survive URL changes
- model inferred from carModel.name (e.g. "Carrera 4S" → model=911, trim=Carrera 4S)

### eBay Motors — key notes
- holtmotorsports seller sweep runs every cycle (owner's own listings)
- Dedup by listing_url first (not year/model) — fixes relist timestamp problem
- 20-min alert window guard prevents bulk re-ingestion storms

---

## 4. System Architecture

### Schedules (launchd)
- `com.porschetracker.scrape` — `run_daily.sh` every 720s (12 min)
- `com.porschetracker.gitpush` — `git_push_dashboard.sh` every 120s (2 min)
- `com.porschetracker.archive-capture` — HTML/screenshot archive every 10 min

### Key Files
```
~/porsche-tracker/
├── scraper.py              # BaT, PCA Mart, pcarmarket
├── scraper_autotrader.py   # AutoTrader curl_cffi + headless PW
├── scraper_carscom.py      # cars.com curl_cffi, 5 slugs, VIN-stop
├── scraper_ebay.py         # eBay Browse API OAuth2 + holtmotorsports sweep
├── scraper_rennlist.py     # Rennlist curl_cffi
├── scraper_cnb.py          # Cars & Bids Playwright
├── scraper_bfb.py          # Built for Backroads curl_cffi
├── scraper_dupont.py       # DuPont Registry direct API
├── db.py                   # DB layer, upsert_listing, tier classification
├── fmv.py                  # FMV engine — score_active_listings()
├── main.py                 # Entry point — scrape + dashboards + alerts
├── notify_imessage.py      # iMessage alerts (standardized format all 10 sources)
├── new_dashboard.py        # Primary dashboard → docs/index.html
├── auction_dashboard.py    # Auction watcher → docs/auctions.html
├── live_feed.py            # DEPRECATED — will be deleted during redesign
├── comp_scraper.py         # Daily BaT comp scrape + 24mo auto-expiry
├── decode_vin_generation.py # VIN → generation column
└── data/
    ├── inventory.db              # SQLite — all tables
    ├── imessage_config.json      # {"recipient": "6108361111"}
    ├── seen_alerts_imessage.json # Alert dedup store
    ├── proxy_config.json         # DataImpulse proxy
    ├── ebay_api_config.json      # eBay OAuth credentials
    └── carscom_state.json        # {"bootstrapped": true}
```

---

## 5. Database

### Tables
- **listings** — active + sold. Key columns: `dealer`, `year`, `make`, `model`, `trim`, `mileage`, `price`, `vin`, `listing_url`, `image_url`, `image_url_cdn`, `source_category`, `tier`, `created_at`, `date_first_seen`, `date_last_seen`, `auction_ends_at`, `status`, `feed_type`
- **price_history** — every price change per listing (silent tracking, no alerts)
- **sold_comps** — 6,010 records, 84% with generation filled. Auto-expires >24mo on each comp scrape run.
- **bat_reserve_not_met** — BaT auctions that didn't meet reserve (price floor signal)
- **snapshots** — daily raw snapshots per dealer

### upsert_listing dedup priority
1. VIN match (most reliable)
2. listing_url match (catches eBay/DuPont correctly)
3. DuPont fallback: car ID tail match (survives URL format changes)
4. year/make/model fallback (non-eBay, non-DuPont only)

### FMV Engine
- Source: BaT sold comps (weight 1.0), recency decay ≤6 months full → 0.3 at 24 months
- Groups by: generation + trim family
- Confidence: HIGH (10+ comps) / MEDIUM (4-9) / LOW (1-3) / NONE (0)
- Current: 78% HIGH, 22% MEDIUM, <1% LOW

---

## 6. Alert System

### Current State (April 18)
| Alert type | Status | Notes |
|---|---|---|
| New-listing iMessage | ✅ ACTIVE | Every new listing → one iMessage + thumbnail. 20-min window guard prevents storms. |
| Auction-ending iMessage | ✅ ACTIVE | TIER1 <3hr, TIER2 <1hr |
| Deal/watch iMessage | ❌ DISABLED | Uncomment notify_imessage.main() in main.py ~line 323 |
| Price-drop alerts | ❌ REMOVED | Parked — revisit when system is stable |

### iMessage Format (standardized April 18)
```
🆕 2022 Porsche 911 GT3
💰 $274,998
🛣️  8,200 mi
📍 DuPont · RETAIL · GT/Collector 🔥
🔗 https://www.dupontregistry.com/autos/listing/...
[thumbnail as second message]
```
Source labels: BaT, C&B, BfB, DuPont, eBay, Cars.com, PCA Mart, AutoTrader, Rennlist, pcarmarket

---

## 7. Dashboard

**URL:** https://ocx11.github.io/PTOX11/  
Built by `new_dashboard.py` → `docs/index.html`, pushed every 2 min.  
Auctions: `auction_dashboard.py` → `docs/auctions.html`

**⚠️ REDESIGN COMPLETE** — Full visual overhaul shipped April 18. Commit `cac351d44`.
- Design system: PTOX logo, `--red #D6293E`, Syne + DM Mono + DM Sans fonts, `#0A0A0C` bg
- Nav: Listings · Auctions · Comps · Market · Search (no Live tab — deleted)
- Chip filters for Generation + Source (replaced dropdowns)
- FMV progress bar on all listing cards (replaced emoji circles 🟢🟡🔴)
- Image overlays: gen badge top-left, deal % badge top-right (≥10% below FMV)
- Joined stats strip: Active / New Today / Auctions (yellow) / Comps / Deals (green)
- Auction cards: horizontal layout, urgency red bar, DM Mono timer
- Search: Spotlight-style frosted glass input, expands on focus
- Year filter: 1986–2024 enforced in `_keep()` in new_dashboard.py
- `docs/live_feed.html` deleted, Quick Links sidebar removed

---

## 8. Known Issues / Watch List

| Issue | Severity | Notes |
|---|---|---|
| Deal/watch alerts disabled | Medium | Intentional — re-enable when ready |
| AutoTrader count fluctuates 8-49 | Low | Akamai blocks intermittent |
| AutoTrader images 80% | Low | Some listings missing image_url |
| Rennlist only 5-6 listings | Low | Low-volume source, scraper working correctly |
| live_feed.html | — | Deleted during redesign ✅ |

---

## 9. Open Items / Roadmap

### Next up (approved)
1. **PWA push notifications** — replace iMessage with native iOS push, tap → listing URL

### Claude can do solo (queue)
2. FMV audit — fix known-bad estimates (GT2RS comp crossing, 1987 Carrera undervalued)
3. Deal/watch alerts re-enable — 1 line in main.py when ready
4. AutoTrader image coverage improvement (~80% currently)
5. Sold comp backfill (more BaT history depth)

### Needs owner input
7. New scrapers — owner researching which dealer-heavy sources are worth building
8. BMW M / Alpina support — scope TBD
9. Interactive pricing graph — discuss in Lead Dev chat
10. Manual FMV calculator — discuss in Lead Dev chat

---

## 10. Proxy & Infrastructure

- **DataImpulse** rotating residential `gw.dataimpulse.com:823`
- Mandatory for AutoTrader + eBay. Never falls back to bare IP.
- cars.com, Rennlist, BfB, DuPont: direct curl_cffi (no proxy needed)
- BaT, pcarmarket, C&B, PCA Mart: direct Playwright (no proxy needed)

---

## 11. Session Log

### April 18, 2026 (evening)
- Dashboard redesign shipped — full visual overhaul (commit cac351d44)
  - PTOX logo, new color system, Syne/DM Mono/DM Sans fonts
  - Chip filters, FMV progress bars, image overlays, horizontal auction cards
  - Spotlight-style search, joined stats strip, year range 1986–2024
  - live_feed.html deleted, Quick Links removed
- Next: PWA push notifications

### April 18, 2026 (daytime)
- DuPont Registry scraper built (scraper_dupont.py) — direct API, ~889 listings, 100% images
- DuPont URL format fixed (/autos/listing/{year}/porsche/{alias}/{id}), 900 DB records backfilled
- DuPont upsert dedup hardened — car ID tail match survives future URL changes
- Rennlist trim field fixed — stops at comma, strips color/mileage/gen artifacts
- Sold comp auto-expiry added to comp_scraper.py (prunes >24mo on each run)
- AutoTrader junk bootstrap record cleaned
- iMessage format standardized across all 10 sources — title dedup fix, source labels, AUCTION/RETAIL tag
- Pending scrapers (Hagerty, CarGurus, Hemmings, Porsche NA) moved to low priority — owner researching
- Price-drop badge parked — revisit when system is stable
- Dashboard redesign approved — next major task

### April 17, 2026
- Full investigation session: eBay dedup bug fixed (URL-first lookup), iMessage storm fixed (20-min guard)
- auction_dashboard.py built → docs/auctions.html (4 sections, live countdown)
- YEAR_MAX 2024 enforced in eBay + AutoTrader scrapers
- C&B auction_ends_at NULL fixed
- eBay holtmotorsports seller sweep added

### March 26 – April 16, 2026
- Full platform build: all scrapers, FMV engine, iMessage alerts, dashboard, GitHub Pages
- BaT comp backfill: 6,010 comps
- DataImpulse proxy, launchd scheduling, archive capture
- cars.com: 260 listings via curl_cffi direct

---

## 12. VIN Decoder Reference

**Position key:** 1-3=WMI (WP0=Porsche), 4-6=series, 10=model year, 11=plant

| Series | Model | Generation logic |
|---|---|---|
| AA2/AB2/AC2 | 911 Carrera RWD | ≤2004=996, ≤2008=997.1, ≤2012=997.2, ≤2015=991.1, ≤2019=991.2, 2019+=992 |
| AD2 | 911 Turbo | same splits |
| AF2 | GT3/GT3RS/GT2RS | same splits |
| CA2/CB2/CC2 | Boxster/Cayman/718 | ≤2004=986, ≤2011=987, ≤2016=981, 2017+=718 |
| AA0/AB0 | 964/993 | ≤1993=964, 1994+=993 |
| JA0/JB0 | 930 Turbo | ≤1989=930 |
