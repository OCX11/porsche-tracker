# PTOX11 — Porsche Market Intelligence Platform
*Last updated: April 17, 2026*

---

## 1. Project Overview

Autonomous Porsche market intelligence platform on a Mac Mini M4. Scrapes 9 sources every 12 minutes, scores every listing against FMV using 6,010 BaT sold comps, and fires iMessage alerts the moment a new listing enters the DB.

**Repo:** https://github.com/OCX11/PTOX11  
**Dashboard:** https://ocx11.github.io/PTOX11/  
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

## 3. Active Sources (April 17, 2026)

| Source | Active | Method | Images |
|---|---|---|---|
| cars.com | ~260 | curl_cffi, 5 model slugs, VIN-stop incremental | ✅ 99% |
| eBay Motors | 82 | Browse API OAuth2, cache+incremental | ✅ 100% |
| PCA Mart | 54 | Playwright cookie-auth | ✅ CDN URLs stored |
| Bring a Trailer | 38 | Playwright | ✅ 100% |
| Cars and Bids | 13 | Playwright scroll | ✅ 100% |
| Built for Backroads | 10 | curl_cffi | ✅ 100% |
| AutoTrader | 10 | curl_cffi + headless PW fallback (no headed) | ⚠️ 80% |
| pcarmarket | 8 | Playwright | ✅ 100% |
| Rennlist | 6 | curl_cffi (Cloudflare bypass) | ✅ 100% |

**All local — zero Distill dependency (cancelled April 15).**

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
├── scraper_ebay.py         # eBay Browse API OAuth2
├── scraper_rennlist.py     # Rennlist curl_cffi
├── scraper_cnb.py          # Cars & Bids Playwright
├── scraper_bfb.py          # Built for Backroads curl_cffi
├── db.py                   # DB layer, upsert_listing, tier classification
├── fmv.py                  # FMV engine — score_active_listings()
├── main.py                 # Entry point — scrape + dashboards + alerts
├── notify_imessage.py      # iMessage alerts
├── new_dashboard.py        # Primary dashboard → docs/index.html
├── live_feed.py            # Live feed view
├── comp_scraper.py         # Daily BaT comp scrape
├── decode_vin_generation.py # VIN → generation column
├── enrich_listings.py      # Fill missing price/mileage
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
- **sold_comps** — 6,010 records, 84% with generation filled
- **bat_reserve_not_met** — BaT auctions that didn't meet reserve (price floor signal)
- **snapshots** — daily raw snapshots per dealer

### FMV Engine
- Source: BaT sold comps (weight 1.0), recency decay ≤6 months full → 0.3 at 24 months
- Groups by: generation + trim family
- Confidence: HIGH (10+ comps) / MEDIUM (4-9) / LOW (1-3) / NONE (0)
- Current: 78% HIGH, 22% MEDIUM, <1% LOW

---

## 6. Alert System

### Current State (April 17)
| Alert type | Status | Notes |
|---|---|---|
| New-listing iMessage | ✅ ACTIVE | Every new VIN entering DB → one iMessage + thumbnail |
| Deal/watch iMessage | ❌ DISABLED | `notify_imessage.main()` commented out in main.py — re-enable when ready |
| Price-drop iMessage | ❌ REMOVED | Deleted — caused spam during bootstrap. See Apple Note "Price Drops — Future Feature Design" |

**Re-enable deal alerts:** uncomment `notify_imessage.main()` in main.py (~line 323). Wait a few days for cars.com bootstrap to normalise first.

### iMessage Format (new listing)
```
🆕 NEW: 2022 Porsche 911 GT3
💰 $189,900
🛣️  4,200 mi
📍 Bring a Trailer  [GT/Collector]
🔗 https://bringatrailer.com/listing/...
[thumbnail as second message]
```

### Auction Countdown
`auction_ends_at` TEXT column on listings. BaT: `data-timestamp_end` Unix epoch. C&B: `span.ticking` HH:MM:SS. Dashboard shows live JS countdown on auction cards.

---

## 7. Dashboard

**URL:** https://ocx11.github.io/PTOX11/  
Built by `new_dashboard.py` → `docs/index.html`, pushed every 2 min.

**Sort order:** `created_at DESC` — exact timestamp, newest listing at top.  
**Age badge:** reads `created_at` (full ISO timestamp) for accurate "12m ago" labels.  
**Cards:** image, tier badge, price, FMV delta, mileage, source badge, auction countdown.

---

## 8. Known Issues / Watch List

| Issue | Severity | Notes |
|---|---|---|
| Deal/watch alerts disabled | Medium | Intentional — re-enable when ready (1 line in main.py) |
| cars.com batching on dashboard | Low | Bootstrap artifact — new listings come in mixed order, settles naturally |
| AutoTrader count fluctuates 8-49 | Low | Akamai blocks intermittent — curl_cffi usually recovers same cycle |
| eBay newest listing April 13 | Low | Normal — private eBay sellers don't list daily |
| 933 sold comps NULL generation | Low | VINs without decodeable series codes — diminishing returns |

---

## 9. Open Items

### Re-enable when ready
1. **Deal/watch alerts** — uncomment `notify_imessage.main()` in main.py. Tune thresholds after re-enable.
2. **Price-drop dashboard badge** — silent indicator on cards (no notification). See Apple Note.

### Autonomous (Claude can do solo)
3. **Sold comp auto-expiry** — archive comps >24 months (zero FMV weight anyway)
4. **dashboard price-drop badge** — show `📉 -$5k` chip on cards where price dropped

### Needs your input
5. **Dashboard redesign** — Porsche brand colors (Guards Red, Racing Yellow, GT Silver)
6. **AutoTrader Akamai** — intermittent, not urgent

### Low priority / may drop
7. **eBay sold comps** — Finding API rate-limited, FMV already 78% HIGH without it

---

## 10. Proxy & Infrastructure

- **DataImpulse** rotating residential `gw.dataimpulse.com:823`
- Mandatory for AutoTrader + eBay. Never falls back to bare IP.
- cars.com and Rennlist: direct curl_cffi (no proxy needed — works better without)
- BaT + pcarmarket: direct Playwright (no proxy needed)

---

## 11. Session Log

### April 17, 2026
- `fmv.py score_active_listings()`: added `created_at` + `auction_ends_at` to SELECT + result dict — was missing, causing sort to fail
- Dashboard sort fixed: `created_at DESC` (full timestamp) — was `date_last_seen` (date-only, all listings tied)
- Age badge fixed: `created_at` (full timestamp) not `date_first_seen` (date-only → midnight fallback)
- AutoTrader headed Playwright removed — no more Chrome windows popping up on screen

### April 16, 2026 — Evening
- Price-drop alerts removed entirely (spam during bootstrap — 305-608/cycle)
- Deal/watch alerts disabled (uncomment when ready)
- Dashboard sort fixed from `date_last_seen` → `created_at`
- Alert dedup cleaned (609 bad drop: keys removed)
- Autonomous batch: PCA Mart CDN URLs, NULL generation filled 1408→933, HANDOVER refresh

### April 16, 2026 — Day
- cars.com: 2 → 260 active listings (per-model slugs, direct curl_cffi, VIN-stop incremental)
- Rennlist 403 fixed (curl_cffi Chrome impersonation, 12s → 0.4s)
- Auction countdown timer (BaT Unix epoch + C&B ticking text, live JS)
- iMessage formatting cleanup (eBay URL strip, trim cap, removed redundant tags)
- C&B source_category DEALER→AUCTION fixed
- cars.com incremental mode (VIN-based stop, 3-page cap)

### April 15, 2026
- Cars & Bids scraper built (scraper_cnb.py)
- Built for Backroads → Playwright (last Distill dependency removed)
- FMV audit: NONE confidence 14→0, trim fallback logic fixed
- PWA installed on iPhone

### March 26 – April 14, 2026
- Full platform build: all 9 scrapers, FMV engine, iMessage alerts, dashboard, GitHub Pages
- BaT comp backfill: 6,010 comps
- DataImpulse proxy, launchd scheduling, archive capture

---

## 12. VIN Decoder Reference

**Position key:** 1-3=WMI (WP0=Porsche), 4-6=series, 10=model year, 11=plant  
**Decoder:** https://rennlist.com/forums/vindecoder.php

| Series | Model | Generation logic |
|---|---|---|
| AA2/AB2/AC2 | 911 Carrera RWD | ≤2004=996, ≤2008=997.1, ≤2012=997.2, ≤2015=991.1, ≤2019=991.2, 2019+=992 |
| AD2 | 911 Turbo | same splits |
| AF2 | GT3/GT3RS/GT2RS | same splits |
| CA2/CB2/CC2 | Boxster/Cayman/718 | ≤2004=986, ≤2011=987, ≤2016=981, 2017+=718 |
| AA0/AB0 | 964/993 | ≤1993=964, 1994+=993 |
| JA0/JB0 | 930 Turbo | ≤1989=930 |

---

## 13. Future Sources (Mid Priority)

Candidates to add — each needs a scraper built:

| Source | URL | Notes |
|---|---|---|
| DuPont Registry | dupontregistry.com | Dealer + private, strong Porsche inventory |
| Hagerty Marketplace | marketplace.hagerty.com | Collector-focused, air-cooled heavy |
| CarGurus | cargurus.com | Large retail, good price history data |
| Porsche NA Finder | porsche.com/usa/modelrange/finder | Factory/dealer CPO listings |
| Hemmings | hemmings.com | Air-cooled / vintage Porsche |
| iSeeCars | iseecars.com | Aggregator, may dedupe with existing |
| Carfax Listings | carfax.com/cars-for-sale | Private + dealer |

Build order recommendation: DuPont Registry first (high quality Porsche inventory), then Hagerty (air-cooled depth), then Porsche NA finder (CPO/factory).
