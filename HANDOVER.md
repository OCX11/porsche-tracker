# Vehicle Market Analyzer — Project Handover Summary
*Last updated: April 9, 2026 (post-audit)*

---

## 1. Project Overview & Goals

A Porsche-focused market intelligence platform running autonomously on a Mac Mini M4. Scrapes 7 active sources, tracks price history, scores every listing against FMV using 5,700+ BaT sold comps, and sends iMessage alerts the moment a new listing hits the DB. Long-term goal: become the most informed buyer in the air-cooled, water-cooled, and GT Porsche market.

### Business Context
Owner operates a small, focused performance car dealership. All purchases are investments — short-term flips or long-term holds. Core price range: $70K–$150K. GT/collector cars have no ceiling.

---

## 2. Target Vehicles

### Tier 1 — GT / Collector (alert immediately)
- 911: GT3, GT3 RS, GT2, GT2 RS, R, Speedster, Sport Classic, Touring (996/997/991/992)
- 911: All air-cooled — 930, 964, 993 (pre-1998)
- Cayman: GT4, GT4 RS, Spyder, R (987/981/718)
- Boxster: Spyder (987/981/718)
- Any Turbo S variant
- 356, 914-6

### Tier 2 — Standard (alert only at DEAL — 10%+ below FMV)
- 911: Carrera, S, 4S, GTS, Targa (996/997/991/992)
- Cayman: S, GTS (987/981/718)
- Boxster: Base, S, GTS (987/981/718)

### Never
- Cayenne, Panamera, Macan, Taycan — excluded at scrape level
- Salvage, rebuilt, flood, frame damage
- Year: 1986–2026 | Mileage: <100k | Price: <$5,000 (non-auction)

---

## 3. System Architecture

### Infrastructure
- **Machine:** Mac Mini M4, 24/7, user: claw
- **Scrape schedule:** Every 12 minutes flat (launchd `StartInterval=720`)
- **Dashboard push:** Every 2 minutes (launchd `StartInterval=120`)
- **Alert delivery:** iMessage via `notify_imessage.py` → Messages.app → iPhone

### Key Files
```
~/porsche-tracker/
├── scraper.py              # BaT, PCA Mart, pcarmarket scrapers + DEALERS list
├── scraper_autotrader.py   # AutoTrader Playwright (mobile site) ✅ LIVE
├── scraper_carscom.py      # Cars.com curl_cffi — PARKED (Cloudflare block)
├── scraper_ebay.py         # eBay Browse API (OAuth2) ✅ LIVE
├── scraper_rennlist.py     # Rennlist Playwright — BROKEN (403 block)
├── distill_poller.py       # Polls Distill → Built for Backroads only
├── db.py                   # DB layer, tier classification, upsert logic
├── main.py                 # Entry point — scrape + snapshot + dashboards + alerts
├── fmv.py                  # FMV engine — weighted median, generation bucketing
├── notify_imessage.py      # iMessage alerts — LIVE ✅
├── new_dashboard.py        # Primary dashboard → docs/index.html (GitHub Pages)
├── git_push_dashboard.sh   # Auto-pushes docs/ to GitHub Pages every 2 min
├── run_daily.sh            # Launched by launchd every 12 min
├── live_feed.py            # Live feed → docs/live_feed.html
├── comp_scraper.py         # Ongoing BaT sold comp scraping
├── enrich_bat_vins.py      # BaT VIN/mileage enricher (run overnight)
├── enrich_ebay_mileage.py  # eBay per-item mileage enricher (run on-demand)
└── data/
    ├── inventory.db              # SQLite — listings, price_history, sold_comps
    ├── imessage_config.json      # {"recipient": "6108361111"} ✅
    ├── seen_alerts_imessage.json # Dedup for iMessage alerts
    ├── proxy_config.json         # DataImpulse (gw.dataimpulse.com:823)
    └── autotrader_state.json     # {"bootstrapped": true}
```

### launchd Jobs
| Label | Script | Interval | Status |
|---|---|---|---|
| com.porschetracker.scrape | run_daily.sh | 720s (12 min) | ✅ Running |
| com.porschetracker.gitpush | git_push_dashboard.sh | 120s (2 min) | ✅ Running |
| com.porschetracker.distill-poller | distill_poller.py | KeepAlive | ✅ Running |
| com.porschetracker.distill-watcher | distill_watcher.py | KeepAlive | ✅ Running |
| com.porschetracker.distill-receiver | distill_receiver.py | KeepAlive | ✅ Running |
| com.porschetracker.archive-capture | archive_capture.py | 600s (10 min) | ❓ Undocumented |

---

## 4. Active Data Sources

| Source | Method | Status | Listings | Images |
|---|---|---|---|---|
| PCA Mart | `scraper.py` cookie-auth | ✅ Working | ~72 | ❌ Local paths |
| Bring a Trailer | `scraper.py` Playwright | ✅ Working | ~40 | ✅ Yes |
| cars.com | Distill Desktop | ✅ Working | ~18 | ❌ No |
| AutoTrader | `scraper_autotrader.py` | ⚠️ Intermittent | ~9 | Via link preview |
| pcarmarket | `scraper.py` | ✅ Working | ~9 | ✅ Yes |
| Built for Backroads | Distill Desktop | ✅ Working | ~9 | ✅ Yes |
| eBay Motors | `scraper_ebay.py` Browse API | ⚠️ Degraded | ~7 | ✅ Yes |
| Rennlist | `scraper_rennlist.py` | ❌ BROKEN | 11 stale | ✅ Yes |

**Total active: ~179 listings (was ~350+ at peak — Rennlist + eBay degradation)**

### Source Notes
- **Rennlist:** Returns 0 on every run — 403 block on proxy+Playwright. Existing 11 listings are stale/stuck. Fix needed urgently (high-value private seller source).
- **eBay Motors:** Collapsed from 81–293 listings at launch to ~7 active. No explicit errors — likely OAuth token or query issue. Investigate.
- **AutoTrader:** Zeros out ~50% of cycles (Akamai mobile site). Known issue. Dedicated chat for fixes.
- **cars.com (scraper_carscom.py):** Intentionally parked — Cloudflare blocked. Distill feeds it instead.
- **Cars.com (capitalized):** Legacy duplicate dealer name — archived April 9. Now all under `cars.com`.

### Proxy
DataImpulse rotating residential (`gw.dataimpulse.com:823`).
- Username: 7dffcde9c33e2eab45cb
- Password: 068a3aeba25658b5
- Balance: ~$50 topped up April 1 (~16 months runway)
- **Policy: mandatory, no bare-IP fallback** on AutoTrader/Cars.com scrapers

---

## 5. Database

| Table | Count | Notes |
|---|---|---|
| listings (active) | ~179 | Down from ~350 peak |
| listings (sold/archived) | ~1,200+ | Historical |
| sold_comps | 5,710 | Growing — FMV truth layer |
| bat_reserve_not_met | 1,784 | Price floor signal |
| hagerty_valuations | 22 | Good condition only |

---

## 6. FMV Engine

`fmv.py` — wired into dashboards and alerts every cycle.
- Source: BaT sold comps (weight 1.0)
- Groups by: generation + trim family
- Recency decay: full weight ≤6 months, decays to 0.3 at 24 months
- Outputs: weighted median, price_low/high, RNM floor, confidence, comp count

---

## 7. Alert System — LIVE ✅

**File:** `notify_imessage.py`
**Recipient:** 6108361111

### Two-layer alert system (order each cycle):
1. **`notify_new_listings(conn, new_ids)`** — fires for EVERY new listing the moment it hits DB. No FMV required. Format:
```
🆕 NEW: 2019 Porsche 911 GT3 RS
💰 $289,000
🛣️  8,400 mi
📍 Bring a Trailer [AUCTION]  [GT/Collector]
🔗 https://bringatrailer.com/listing/...
```

2. **`notify_imessage.main()`** — deal/watch scoring alerts. Format:
```
🔥 DEAL: 2022 Porsche 911 GT3
💰 $239,900  -15% vs FMV ($282,000)
🛣️  12,000 mi
📍 AutoTrader [RETAIL]  [GT/Collector]
🔗 https://...
```

**Thresholds:**
- Tier 1: alert on DEAL (10%+ below) OR WATCH (5–10% below)
- Tier 2: alert only on DEAL (10%+ below)
- Price floor: listings under $20,000 skipped (auction bids / salvage)
- Confidence gate: NONE confidence skipped
- Dedup: `seen_alerts_imessage.json` — new-listing key `"new:{url}"`, deal key `"{url}"`

---

## 8. Dashboard — GitHub Pages ✅

URL: https://ocx11.github.io/porsche-tracker/
- Auto-regenerated every scrape cycle by `new_dashboard.py` → `docs/index.html`
- Auto-pushed to GitHub every 2 minutes by `git_push_dashboard.sh`
- Filters: Generation, Model, Year, Price, Source, Deals only, GT/Collector only
- No pre-checked filters on load ✅
- Listing age shows accurate timestamps (uses `created_at`, not `date_first_seen`) ✅
- All report links working ✅

---

## 9. Open Issues & Next Steps

### 🔴 Fix Now
1. **Rennlist scraper broken** — 403 on every run. Proxy+Playwright being blocked. Fix in Rennlist chat. High value source — private sellers, GT/air-cooled.
2. **eBay Motors collapsed** — ~7 listings vs 200+ at launch. Investigate OAuth/query in eBay chat.
3. **AutoTrader zeros ~50% of runs** — Akamai block on mobile URL. Fix in AutoTrader chat.

### 🟡 Short Term
4. **Cars & Bids active listings** — handoff prompt ready in C&B chat. High Tier 1 value.
5. **Built for Backroads → Playwright** — last Distill dependency. Cancel Distill sub after.
6. **PCA Mart image URLs** — local `/static/img_cache/` paths don't send via iMessage.
7. **archive-capture daemon** — undocumented, runs every 10 min. Audit what it does.
8. **Legacy dashboard** — `dashboard.py` still generating `static/dashboard.html` every cycle. Dead output. Remove from `main.py`.

### 🟢 Medium Term
9. **FMV accuracy audit** — some cars way off. Likely generation bucketing issues.
10. **PWA / iOS app** — add `manifest.json` to `docs/`. 2-hour build, installs from Safari.
11. **eBay sold comps** — eBay Finding API gives sold listings. Gold for air-cooled FMV.

### 🔵 Phase 4 (needs 30+ days data)
12. Price trend prediction, velocity signals, seasonality, buy/sell score

---

## 10. Known Issues

| Issue | Severity | Status |
|---|---|---|
| Rennlist 403 block | High | BROKEN — fix in Rennlist chat |
| eBay Motors collapse (200→7) | High | Investigate OAuth/query |
| AutoTrader ~50% zero cycles | Medium | Known — Akamai mobile block |
| PCA Mart images local-only | Low | `/static/img_cache/` not public |
| archive-capture undocumented | Low | Needs audit |
| Legacy dashboard.py still running | Low | Dead output, remove from main.py |

---

## 11. Session Log

### March 26–30, 2026
- BaT backfill: 5,652 comps | eBay Browse API activated | DataImpulse proxy activated
- FMV pipeline wired | iMessage alerts live — 98 alerts first run

### April 1, 2026
- `scraper_autotrader.py` built (mobile site, bootstrap complete)
- DataImpulse topped up $50

### April 2, 2026
- `scraper_carscom.py` built (curl_cffi, 61 listings, 100% images)
- `scraper_ebay.py` built (Browse API, OAuth2, 81 listings)
- `enrich_ebay_mileage.py` built

### April 4, 2026
- `scraper_rennlist.py` built — Rennlist migrated off Distill
- Distill now only serves Built for Backroads

### April 8, 2026
- iMessage new-listing alerts wired (`notify_new_listings`)
- Dashboard live feed filter removed
- All dashboard links fixed (live_feed path, GitHub Pages)
- `.gitignore` hardened (data/, secrets, img_cache excluded)
- Scheduler fixed: 1800s → 720s (30 min → 12 min)
- `enrich_rennlist.py` removed from run loop
- `git_push_dashboard.sh` created — GitHub Pages auto-updates every 2 min
- Listing age display fixed (created_at vs date_first_seen)
- Independent dealers disabled from DEALERS list
- iMessage alert wiring restored in main.py after git restore wipe

### April 9, 2026 — Post-Audit
- **Audit performed** — Claude Code cold-read of full system
- `notify_gunther.py` removed from `run_daily.sh` (Telegram dead, was running every cycle)
- Alert price floor added: listings <$20k skipped (BaT auction bids / salvage)
- `Cars.com` (capitalized) duplicate archived — all normalized to `cars.com`
- HANDOVER.md rewritten to reflect actual system state (not aspirational)
- **Key findings:** Rennlist broken, eBay collapsed, AutoTrader intermittent, 179 active vs 350+ expected
