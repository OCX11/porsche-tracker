"""
Dealer/source credibility weights for FMV calculations.

Weights range from 0.0–1.0. A source at weight 0.3 contributes 30% as much
as a source at 1.0 when computing weighted-mean FMV from sold comps.

Weights are loaded from data/dealer_weights.json at runtime so they can be
adjusted without touching code. The file is created with defaults on first run.

Tiers:
  High   >= 0.8  Ground-truth auction results or well-calibrated references
  Medium >= 0.5  Listed prices from reasonable marketplace sources
  Low    <  0.5  Sources known for aspirational or outlier pricing
"""
import json
from pathlib import Path

WEIGHTS_FILE = Path(__file__).parent / "data" / "dealer_weights.json"

# Canonical defaults keyed by lowercase dealer/source name.
# "_default" is used for any source not explicitly listed.
_DEFAULTS: dict[str, float] = {
    "bat":                           1.0,
    "bring a trailer":               1.0,
    "bringatrailer":                 1.0,
    "classic.com":                   1.0,
    "cars & bids":                   1.0,
    "carsandbids":                   1.0,
    "hagerty":                       0.8,
    "pcarmarket":                    0.6,
    "pca mart":                      0.6,
    "motorcars of the main line":    0.3,
    "_default":                      0.7,
}


def _write_defaults():
    """Create the JSON file with defaults if it doesn't exist yet."""
    WEIGHTS_FILE.parent.mkdir(exist_ok=True)
    if not WEIGHTS_FILE.exists():
        payload = {
            "_comment": (
                "Dealer/source credibility weights for FMV calculations. "
                "Range 0.0–1.0. High>=0.8 | Medium>=0.5 | Low<0.5. "
                "_default applies to any source not listed here."
            ),
            **_DEFAULTS,
        }
        WEIGHTS_FILE.write_text(json.dumps(payload, indent=2))


def load_weights() -> dict:
    """
    Load weights from dealer_weights.json.
    Falls back to built-in defaults if the file is missing or malformed.
    Creates the file with defaults on first call.
    """
    _write_defaults()
    try:
        raw = json.loads(WEIGHTS_FILE.read_text())
        return {
            k: float(v)
            for k, v in raw.items()
            if not k.startswith("_") and isinstance(v, (int, float))
        }
    except Exception:
        return {k: v for k, v in _DEFAULTS.items() if not k.startswith("_")}


def get_weight(name: str, weights: dict) -> float:
    """
    Return the weight for a dealer or source name.
    Lookup order: exact match → substring match → _default fallback.
    """
    key = (name or "").lower().strip()
    if key in weights:
        return weights[key]
    for wkey, wval in weights.items():
        if wkey in key or key in wkey:
            return wval
    return _DEFAULTS["_default"]


def tier(w: float) -> str:
    """Return 'high', 'medium', or 'low'."""
    if w >= 0.8:
        return "high"
    if w >= 0.5:
        return "medium"
    return "low"


def tier_badge_html(name: str, weights: dict, w: float = None) -> str:
    """
    Return a small HTML badge span for the weight tier of a dealer/source.
    Includes a hover tooltip explaining the tier and exact weight.
    """
    if w is None:
        w = get_weight(name, weights)
    t = tier(w)
    label = t.upper()
    pct = int(w * 100)

    if t == "high":
        tip = (
            f"High-credibility source (weight {w:.1f}). "
            "Treated as ground truth in FMV calculations — "
            "these are confirmed sale prices from reputable auction platforms."
        )
    elif t == "medium":
        tip = (
            f"Medium-credibility source (weight {w:.1f}, {pct}% influence). "
            "Listing prices from marketplace sources. "
            "Reasonable benchmark but asking price ≠ selling price."
        )
    else:
        tip = (
            f"Low-credibility source (weight {w:.1f}, {pct}% influence). "
            "Known for aspirational or above-market pricing. "
            "Contributes minimally to FMV — treat as a price ceiling, not a benchmark."
        )

    safe_tip = tip.replace('"', "&quot;")
    return (
        f'<span class="badge-weight badge-weight-{t}" '
        f'data-wtip="{safe_tip}">{label}</span>'
    )
