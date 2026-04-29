# PTOX11 — Porsche Market Intelligence Platform

*Project bible for Claude. Read at session start. Updated as project evolves.Last updated: April 23, 2026*

---

## 1. Project Overview

Autonomous Porsche market intelligence platform on a Mac Mini M4. Scrapes 10 sources every 12 minutes, scores every listing against FMV using 6,024 BaT sold comps, and fires iOS push notifications the moment a new listing enters the DB.

**Repo:** <https://github.com/OCX11/rennmarkt>**Dashboard:** <https://ocx11.github.io/PTOX11/>**Auctions:** <https://ocx11.github.io/PTOX11/auctions.html>**Machine:** Mac Mini M4, user: claw, 24/7 **DB:** \~/porsche-tracker/data/inventory.db (SQLite) **Logs:** \~/porsche-tracker/logs/

### Business Context

Small performance car dealership. All purchases are investments. Core range $70K–$150K, GT/collector no ceiling. Owner has \~40 years high-end automotive inspection background.

---

## 2. Hard Rules (never override without owner confirmation)

- **YEAR_MAX=2024** — locked until Jan 1 2027. Owner decision required to change.
- **Never alert on:** Cayenne, Panamera, Macan, Taycan — excluded at scrape level
- **pywebpush: stay on 1.14.1** — 2.x has Apple JWT bug (BadJwtToken on Apple push)
- **VAPID sub claim must be https URL** — not mailto: (Apple requirement)
- **GitHub PAT: no expiry** — confirmed April 25 2026
- **DataImpulse proxy mandatory** for AutoTrader + eBay — never fall back to bare IP
- **TASK BOARD — ONE NOTE ONLY:** There is exactly one task board note. NEVER create a new one. ALWAYS edit the existing note titled "🏎 PTOX11 / RennMarkt — Task Board". ALWAYS use proper HTML formatting via osascript (h1/h2/ul/li). Using Apple Notes MCP to write the task board is FORBIDDEN — it destroys formatting.

---

## 3. Target Vehicles

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

- Cayenne, Panamera, Macan, Taycan
- Year: 1986–2024 | Mileage: &lt;100k | Price: &lt;$5,000 (non-auction)

---

## 4. Active Sources (April 2026)

SourceCountMethodImagesDuPont Registry\~922Direct API ([api.dupontregistry.com](http://api.dupontregistry.com) POST)✅ 100%eBay Motors\~729Browse API OAuth2, cache+incremental+seller sweep✅ 100%[cars.com](http://cars.com)\~240curl_cffi, 5 model slugs, VIN-stop incremental✅ 99%AutoTrader\~135curl_cffi + headless PW fallback⚠️ \~80%PCA Mart\~53Playwright cookie-auth✅ CDN URLsBring a Trailer\~33Playwright✅ 100%Cars and Bids\~12Playwright scroll✅ 100%Built for Backroads\~11curl_cffi✅ 100%Rennlist\~10curl_cffi (Cloudflare bypass)✅ 100%pcarmarket\~7Playwright✅ 100%

**Total active: \~2,152 listings. Zero Distill dependency.**

---

## 5. System Architecture

### Schedules (launchd)

- `com.porschetracker.scrape` — `run_daily.sh` every 720s (12 min)
- `com.porschetracker.gitpush` — `git_push_dashboard.sh` every 120s (2 min)
- `com.porschetracker.archive-capture` — HTML/screenshot archive every 10 min
- `com.ptox11.pushserver` — push_server.py on localhost:5055
- `com.ptox11.cloudflared` — Cloudflare tunnel to push server
- `com.ptox11.update-tunnel-url` — keeps Worker URL current

### Key Files

```
main.py                  # Entry point — scrape + dashboards + alerts
scraper.py               # BaT, PCA Mart, pcarmarket
scraper_autotrader.py    # AutoTrader curl_cffi + headless PW
scraper_carscom.py       # cars.com curl_cffi, 5 slugs, VIN-stop
scraper_ebay.py          # eBay Browse API OAuth2 + holtmotorsports sweep
scraper_rennlist.py      # Rennlist curl_cffi
scraper_cnb.py           # Cars & Bids Playwright
scraper_bfb.py           # Built for Backroads curl_cffi
scraper_dupont.py        # DuPont Registry direct API
db.py                    # DB layer, upsert_listing, tier classification
fmv.py                   # FMV engine — score_active_listings()
notify_push.py           # iOS push alerts (new listings + auction ending)
push_server.py           # Flask push server on localhost:5055
health_monitor.py        # Scraper health checks → push alerts
new_dashboard.py         # Primary dashboard → docs/index.html
auction_dashboard.py     # Auction watcher → docs/auctions.html
comp_scraper.py          # Daily BaT comp scrape + 24mo auto-expiry
decode_vin_generation.py # VIN → generation column
```

### Data Files

```
data/inventory.db              # SQLite — all tables
data/push_subscriptions.json   # Active push subscribers
data/vapid_keys.json           # VAPID keys for Web Push
data/seen_alerts_imessage.json # Alert dedup store
data/proxy_config.json         # DataImpulse proxy
data/ebay_api_config.json      # eBay OAuth credentials
data/carscom_state.json        # {"bootstrapped": true}
```

---

## 6. Database

### Tables

- **listings** — active + sold. Key columns: `dealer`, `year`, `make`, `model`, `trim`, `mileage`, `price`, `vin`, `listing_url`, `image_url`, `image_url_cdn`, `source_category`, `tier`, `created_at`, `date_first_seen`, `date_last_seen`, `auction_ends_at`, `status`, `feed_type`
- **price_history** — every price change per listing (silent tracking, no alerts)
- **sold_comps** — 6,024 records, 84% with generation filled. Auto-expires &gt;24mo on each comp scrape run.
- **bat_reserve_not_met** — BaT auctions that didn't meet reserve (price floor signal)
- **snapshots** — daily raw snapshots per dealer

### upsert_listing Dedup Priority

1. VIN match (most reliable)
2. listing_url match (catches eBay/DuPont correctly)
3. DuPont fallback: car ID tail match (survives URL format changes)
4. year/make/model fallback (non-eBay, non-DuPont only)

### FMV Engine

- Source: BaT sold comps (weight 1.0), recency decay ≤6 months full → 0.3 at 24 months
- Groups by: generation + trim family
- Confidence: HIGH (10+ comps) / MEDIUM (4-9) / LOW (1-3) / NONE (0)
- Current: 78% HIGH, 22% MEDIUM, &lt;1% LOW
- **⚠️ KNOWN ISSUE:** Some estimates significantly off. Full audit + rebuild is 🔴 High Priority. Approach: owner walks through known-bad examples → trace comps → fix logic in [fmv.py](http://fmv.py).

---

## 7. Alert System

### Current State

Alert typeStatusNotesNew-listing push✅ ACTIVEEvery new listing → iOS push. 20-min window guard.Auction-ending push✅ ACTIVETier1 &lt;3hr, Tier2 &lt;1hrScraper health push✅ ACTIVE3 consecutive zero-run cycles → push alertScheduler stuck push✅ ACTIVELog not updated in 30min → push alertDeal/watch alerts❌ DROPPEDNew-listing push covers itPrice-drop alerts❌ DROPPEDToo noisy. Silent price_history tracking only.

### Push Stack

- **Subscriber page:** <https://www.rennmarkt.net/notify.html>
- **Cloudflare Worker (permanent URL):** <https://rennmarkt-push.openclawx1.workers.dev>
- **Local push server:** localhost:5055 (push_server.py via launchd)
- **VAPID sub claim:** <https://www.rennmarkt.net/> (Apple requires https URL, not mailto:)
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

## 8. Dashboard

**URL:** <https://ocx11.github.io/PTOX11/>Built by `new_dashboard.py` → `docs/index.html`, pushed every 2 min. Auctions: `auction_dashboard.py` → `docs/auctions.html`

### Features

- Data-driven rendering — JSON array, not DOM nodes. No lag.
- Mobile filter drawer — 92vh slide-up, 2x tap targets
- Air-cooled / Water-cooled filter chips
- Days-on-market chip on each card (📅 Nd) + "Longest Listed" sort
- Bell icon in nav → notify.html
- Nav horizontally scrollable on mobile
- Pull-to-refresh — swipe down triggers smart refresh, red→green progress bar

---

## 9. Known Issues

IssueSeverityNotesFMV estimates off on some models🔴 HIGHFull audit + rebuild is next priorityAutoTrader images \~80%LowSome listings missing image_urlAutoTrader count fluctuates 8–135LowAkamai blocks intermittentRennlist only 5–10 listingsLowLow-volume source, scraper working correctly

---

## 10. Active Priorities

1. **FMV engine audit + rebuild** — owner walks through known-bad examples, trace comps, fix [fmv.py](http://fmv.py)
2. **Commit uncommitted HTML changes** — dashboard, market_report, weekly reports

---

## 11. Roadmap (not started)

- Interactive pricing graph (active + sold comps, hoverable)
- Manual FMV calculator (off-market valuation)
- Watchlist alerts by spec (e.g. "991.2 GT3 Touring manual only")
- Seller intelligence (flag repeat/disguised dealers)
- New scrapers: Hagerty, Porsche NA CPO, CarGurus, Hemmings
- Manheim API (low priority, wholesale data)
- Site-facing chat assistant for user car questions (long-term, separate project)

---

## 12. Proxy & Infrastructure

- **DataImpulse** rotating residential `gw.dataimpulse.com:823`
- Mandatory for AutoTrader + eBay. Never falls back to bare IP.
- [cars.com](http://cars.com), Rennlist, BfB, DuPont: direct curl_cffi (no proxy needed)
- BaT, pcarmarket, C&B, PCA Mart: direct Playwright (no proxy needed)

---

## 13. VIN Decoder Reference

**Position key:** 1-3=WMI (WP0=Porsche), 4-6=series, 10=model year, 11=plant

SeriesModelGeneration logicAA2/AB2/AC2911 Carrera RWD≤2004=996, ≤2008=997.1, ≤2012=997.2, ≤2015=991.1, ≤2019=991.2, 2019+=992AD2911 Turbosame splitsAF2GT3/GT3RS/GT2RSsame splitsCA2/CB2/CC2Boxster/Cayman/718≤2004=986, ≤2011=987, ≤2016=981, 2017+=718AA0/AB0964/993≤1993=964, 1994+=993JA0/JB0930 Turbo≤1989=930

---

## 14. Housekeeping Rules

- Run `git worktree prune` after every code session — idle worktrees clog the system
- Close Terminal windows opened for tasks as soon as done
- Close browser tabs opened for debugging/testing
- Update this file's Session Log at end of every session

---

## 15. Session Log

### 2026-04-23

- [CLAUDE.md](http://CLAUDE.md) created — merged [HANDOVER.md](http://HANDOVER.md) + NEXT_STEPS.md into single project bible
- [HANDOVER.md](http://HANDOVER.md) and NEXT_STEPS.md deleted
- .claude_context.md restructured with SESSION PROTOCOL header
- Profile preferences updated with memory protocol enforcement
- Memory system build in progress (Steps 4–5 remain: write template + test)

### 2026-04-19

- PWA push notifications built end-to-end (Apple BadJwtToken fixed — sub must be https URL)
- VAPID keys regenerated, manual JWT signing added, pywebpush 1.14.1 kept
- health_monitor.py migrated from iMessage → push
- Deleted: live_feed.py, live_feed.html, notify_imessage.py, notify_gunther.py, all 3 Distill files
- Dashboard: data-driven rendering, mobile drawer, air/water-cooled chips, days-on-market, pull-to-refresh
- Auction result auto-capture: final hammer price → sold_comps on close
- git_push_dashboard.sh fixed — was crashing on deleted live_feed.html reference

### 2026-04-18

- DuPont Registry scraper built — direct API, \~922 listings, 100% images
- Sold comp auto-expiry added to comp_scraper.py
- Full visual dashboard redesign

### 2026-04-17

- eBay dedup bug fixed, iMessage storm fixed (20-min guard)
- auction_dashboard.py built
- YEAR_MAX 2024 enforced in eBay + AutoTrader
- eBay holtmotorsports seller sweep added

### March 26 – April 16, 2026

- Full platform build: all scrapers, FMV engine, push alerts, dashboard, GitHub Pages
- BaT comp backfill: 6,024 comps
- DataImpulse proxy, launchd scheduling, archive capture

### 2026-04-26

- CRITICAL BUG FIXED: [www.rennmarkt.net](http://www.rennmarkt.net) showing no car cards since April 25 gen-badge commit
- Root cause: JS syntax error in renderCard f-string — \\' (Python escape) rendered as bare '' in JS output (adjacent string literals = syntax error), killing entire script before any card rendered
- Fix: replaced \\' with \\x27 (JS hex escape for single quote) in new_dashboard.py renderCard openGenEditor string
- GUARD ADDED: git_push_dashboard.sh now extracts main