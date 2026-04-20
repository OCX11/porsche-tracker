# PTOX11 — Porsche Market Intelligence Platform
*Last updated: April 19, 2026*

---

## 1. Project Overview

Autonomous Porsche market intelligence platform on a Mac Mini M4. Scrapes 10 sources every 12 minutes, scores every listing against FMV using 6,024 BaT sold comps, and fires iOS push notifications the moment a new listing enters the DB.

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

## 3. Active Sources (April 19, 2026)

| Source | Count | Method | Images |
|---|---|---|---|
| DuPont Registry | ~922 | Direct API (api.dupontregistry.com POST) | ✅ 100% |
| eBay Motors | ~729 | Browse API OAuth2, cache+incremental+seller sweep | ✅ 100% |
| cars.com | ~240 | curl_cffi, 5 model slugs, VIN-stop incremental | ✅ 99% |
| AutoTrader | ~135 | curl_cffi + headless PW fallback | ⚠️ ~80% |
| PCA Mart | ~53 | Playwright cookie-auth | ✅ CDN URLs |
| Bring a Trailer | ~33 | Playwright | ✅ 100% |
| Cars and Bids | ~12 | Playwright scroll | ✅ 100% |
| Built for Backroads | ~11 | curl_cffi | ✅ 100% |
| Rennlist | ~10 | curl_cffi (Cloudflare bypass) | ✅ 100% |
| pcarmarket | ~7 | Playwright | ✅ 100% |

**Total active: ~2,152 listings. All local — zero Distill dependency.**

---

## 4. System Architecture

### Schedules (launchd)
- `com.porschetracker.scrape` — `run_daily.sh` every 720s (12 min)
- `com.porschetracker.gitpush` — `git_push_dashboard.sh` every 120s (2 min)
- `com.porschetracker.archive-capture` — HTML/screenshot archive every 10 min
- `com.ptox11.pushserver` — push_server.py on localhost:5055
- `com.ptox11.cloudflared` — Cloudflare tunnel to push server
- `com.ptox11.update-tunnel-url` — keeps Worker URL current

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
├── notify_push.py          # iOS push alerts (new listings + auction ending)
├── push_server.py          # Flask push server on localhost:5055
├── health_monitor.py       # Scraper health checks → push alerts
├── new_dashboard.py        # Primary dashboard → docs/index.html
├── auction_dashboard.py    # Auction watcher → docs/auctions.html
├── comp_scraper.py         # Daily BaT comp scrape + 24mo auto-expiry
├── decode_vin_generation.py # VIN → generation column
└── data/
    ├── inventory.db              # SQLite — all tables
    ├── push_subscriptions.json   # Active push subscribers
    ├── vapid_keys.json           # VAPID keys for Web Push
    ├── seen_alerts_imessage.json # Alert dedup store
    ├── proxy_config.json         # DataImpulse proxy
    ├── ebay_api_config.json      # eBay OAuth credentials
    └── carscom_state.json        # {"bootstrapped": true}
```

### Deleted / Archived (April 19)
- `notify_imessage.py` — replaced by notify_push.py
- `notify_gunther.py` — Telegram, never wired, deleted
- `live_feed.py` + `docs/live_feed.html` — deprecated, deleted
- `distill_poller.py`, `distill_watcher.py`, `distill_receiver.py` — Distill gone, deleted
- 3 Distill launchd plists — unloaded and removed

---

## 5. Database

### Tables
- **listings** — active + sold. Key columns: `dealer`, `year`, `make`, `model`, `trim`, `mileage`, `price`, `vin`, `listing_url`, `image_url`, `image_url_cdn`, `source_category`, `tier`, `created_at`, `date_first_seen`, `date_last_seen`, `auction_ends_at`, `status`, `feed_type`
- **price_history** — every price change per listing (silent tracking, no alerts)
- **sold_comps** — 6,024 records, 84% with generation filled. Auto-expires >24mo on each comp scrape run.
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
- **⚠️ KNOWN ISSUE:** Some estimates are significantly off. Full audit + rebuild is 🔴 High Priority next task. Approach: owner walks through known-bad examples → trace comps → fix logic in fmv.py.

---

## 6. Alert System

### Current State (April 19)
| Alert type | Status | Notes |
|---|---|---|
| New-listing push | ✅ ACTIVE | Every new listing → iOS push. 20-min window guard. |
| Auction-ending push | ✅ ACTIVE | Tier1 <3hr, Tier2 <1hr |
| Scraper health push | ✅ ACTIVE | 3 consecutive zero-run cycles → push alert |
| Scheduler stuck push | ✅ ACTIVE | Log not updated in 30min → push alert |
| Deal/watch alerts | ❌ DROPPED | New-listing push covers it |
| Price-drop alerts | ❌ DROPPED | Too noisy. Silent price_history tracking only. |

### Push Stack
- **Subscriber page:** https://ocx11.github.io/PTOX11/notify.html
- **Cloudflare Worker (permanent URL):** https://ptox11-push.openclawx1.workers.dev
- **Local push server:** localhost:5055 (push_server.py via launchd)
- **VAPID sub claim:** https://ocx11.github.io/PTOX11/ (Apple requires https URL, not mailto:)
- **pywebpush:** 1.14.1 — do NOT upgrade, 2.x has Apple JWT bug

### Push Format
```
🆕 2022 Porsche 911 GT3
💰 $274,998
🛣️  8,200 mi
📍 DuPont · RETAIL · GT/Collector 🔥
[tap → opens listing URL in Safari]
```

---

## 7. Dashboard

**URL:** https://ocx11.github.io/PTOX11/  
Built by `new_dashboard.py` → `docs/index.html`, pushed every 2 min.  
Auctions: `auction_dashboard.py` → `docs/auctions.html`

### Features (as of April 19)
- Data-driven rendering — JSON array, not DOM nodes. No lag.
- Mobile filter drawer — 92vh slide-up, 2x tap targets
- Air-cooled / Water-cooled filter chips
- Days-on-market chip on each card (📅 Nd) + "Longest Listed" sort
- Bell icon in nav → notify.html
- Nav horizontally scrollable on mobile
- Pull-to-refresh — swipe down from top of listings triggers smart data refresh, red→green progress bar indicator

---

## 8. Known Issues / Watch List

| Issue | Severity | Notes |
|---|---|---|
| FMV estimates off on some models | HIGH | Full audit + rebuild next priority |
| AutoTrader images ~80% | Low | Some listings missing image_url |
| AutoTrader count fluctuates 8-135 | Low | Akamai blocks intermittent |
| Rennlist only 5-10 listings | Low | Low-volume source, scraper working correctly |

---

## 9. Open Items / Roadmap

### High Priority
1. **FMV engine audit + rebuild** — owner to walk through known-bad examples, trace comps, fix logic

### Queue (ready to build)
2. Days-on-market chip ✅ DONE — commit 789a7dd00
3. Auction result auto-capture ✅ DONE — commit f5145ec09
4. AutoTrader image coverage — medium priority, monitor

### Needs Owner Input
5. Auto-fix health monitor — scope safe-fix scenarios
6. Interactive pricing graph — active + sold comps, hoverable
7. Manual FMV calculator — off-market car valuation tool
8. Watchlist alerts by spec — e.g. "991.2 GT3 Touring manual only"
9. Seller intelligence — flag repeat/disguised dealers
10. New scrapers — Hagerty, Porsche NA CPO, CarGurus, Hemmings
11. Manheim API — low priority, wholesale data

---

## 10. Proxy & Infrastructure

- **DataImpulse** rotating residential `gw.dataimpulse.com:823`
- Mandatory for AutoTrader + eBay. Never falls back to bare IP.
- cars.com, Rennlist, BfB, DuPont: direct curl_cffi (no proxy needed)
- BaT, pcarmarket, C&B, PCA Mart: direct Playwright (no proxy needed)

---

## 11. Session Log

### April 19, 2026
- PWA push notifications built end-to-end and fixed (Apple BadJwtToken → sub must be https URL not mailto:)
- VAPID keys regenerated, manual JWT signing replacing pywebpush JWT (pywebpush 1.14.1 kept for encryption only)
- health_monitor.py migrated from iMessage → push
- Deleted dead files: live_feed.py, live_feed.html, notify_imessage.py, notify_gunther.py, all 3 distill files
- 3 Distill launchd services unloaded and removed
- Dashboard: data-driven rendering, mobile drawer, air/water-cooled chips, days-on-market chip, nav scroll
- Days-on-market chip + Longest Listed sort added to dashboard
- Auction result auto-capture: final hammer price → sold_comps on auction close
- FMV audit v2 P1/P2/P3 cleared (GT2 RS 992 fixed, Singer excluded, body-style scoring)
- Full codebase cleanup — main.py dead comments removed
- Pull-to-refresh added to PWA — swipe down from top triggers smart refresh without page reload
- git_push_dashboard.sh fixed — was crashing every 2min due to deleted live_feed.html reference; now refreshes gh token on every run so credential expiry cannot freeze the dashboard again
- FMV engine flagged as high priority — some estimates significantly off, full audit + rebuild queued
- Tiered scrape cadence spec confirmed: DuPont/eBay/cars.com page 1 every 10min + 3-page deep pull every 4hr; AutoTrader page 1 every 15min + 3-page deep every 4hr

### April 18, 2026
- DuPont Registry scraper built — direct API, ~922 listings, 100% images
- Rennlist trim field fixed
- Sold comp auto-expiry added to comp_scraper.py
- iMessage format standardized across all 10 sources
- Full visual dashboard redesign

### April 17, 2026
- eBay dedup bug fixed, iMessage storm fixed (20-min guard)
- auction_dashboard.py built
- YEAR_MAX 2024 enforced in eBay + AutoTrader
- eBay holtmotorsports seller sweep added

### March 26 – April 16, 2026
- Full platform build: all scrapers, FMV engine, iMessage alerts, dashboard, GitHub Pages
- BaT comp backfill: 6,024 comps
- DataImpulse proxy, launchd scheduling, archive capture

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
