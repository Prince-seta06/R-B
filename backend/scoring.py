"""Condition scoring for buildings and roads.

Rating scale (both asset kinds): 1 = serious damage, 2 = major, 3 = moderate, 4 = minor / none.

The component lists, weights, band thresholds and safety rules below are ILLUSTRATIVE defaults.
They must be calibrated by R&B engineers before the numbers are used for real decisions.
"""
import datetime as dt
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

KINDS = ("building", "road")

WEIGHTS = {
    "building": {"structure": 0.30, "roof": 0.20, "electrical": 0.15, "plumbing": 0.15,
                 "fire_safety": 0.10, "finishes": 0.10},
    "road": {"pavement": 0.25, "potholes": 0.20, "riding_quality": 0.15, "drainage": 0.15,
             "shoulders": 0.10, "safety_furniture": 0.15},
}
COMPONENT_LABELS = {
    "building": {"structure": "Structure", "roof": "Roof & waterproofing", "electrical": "Electrical",
                 "plumbing": "Plumbing & drainage", "fire_safety": "Fire safety", "finishes": "Finishes"},
    "road": {"pavement": "Pavement surface (cracking, ravelling)", "potholes": "Potholes & patching",
             "riding_quality": "Riding quality (rutting, roughness)", "drainage": "Drains & culverts",
             "shoulders": "Shoulders & embankment", "safety_furniture": "Markings, signs & barriers"},
}
# A component in this set at 1 forces Critical; at 2 caps the band at Poor. Whatever the weighted average says.
SAFETY_CRITICAL = {
    "building": {"structure", "electrical", "fire_safety"},
    "road": {"potholes", "shoulders"},
}
RATING_LABELS = {1: "Serious damage", 2: "Major damage", 3: "Moderate damage", 4: "Minor or none"}
CRITICALITY_FACTOR = {1: 1.0, 2: 1.25, 3: 1.5}
INSPECTION_INTERVAL_DAYS = {"building": 365, "road": 365}
# Assets forced into Critical are ranked as if their index were no better than this.
CRITICAL_INDEX_CEILING = 24.9
BAND_ORDER = ["Critical", "Poor", "Fair", "Good"]


def today_ist() -> dt.date:
    return dt.datetime.now(IST).date()


def validate_ratings(kind: str, ratings: dict) -> dict:
    keys = WEIGHTS[kind]
    if set(ratings) != set(keys):
        raise ValueError(f"ratings must contain exactly: {', '.join(keys)}")
    out = {}
    for k, v in ratings.items():
        if not isinstance(v, int) or isinstance(v, bool) or v not in (1, 2, 3, 4):
            raise ValueError(f"rating for {k} must be an integer 1-4")
        out[k] = v
    return out


def compute_cci(kind: str, ratings: dict) -> float:
    """Condition index 0-100 (100 = as new)."""
    score = sum(w * (ratings[k] - 1) / 3 for k, w in WEIGHTS[kind].items())
    return round(score * 100, 1)


def band_for(kind: str, cci: float, ratings: dict) -> str:
    safety = SAFETY_CRITICAL[kind]
    if any(ratings.get(k) == 1 for k in safety):
        return "Critical"
    if cci >= 75:
        band = "Good"
    elif cci >= 50:
        band = "Fair"
    elif cci >= 25:
        band = "Poor"
    else:
        band = "Critical"
    if any(ratings.get(k) == 2 for k in safety) and band in ("Good", "Fair"):
        band = "Poor"
    return band


def override_reason(kind: str, cci: float, ratings: dict, band: str) -> str | None:
    """Human-readable reason when a safety rule (not the average) decided the band."""
    plain = "Good" if cci >= 75 else "Fair" if cci >= 50 else "Poor" if cci >= 25 else "Critical"
    if band == plain:
        return None
    hits = sorted(k for k in SAFETY_CRITICAL[kind] if ratings.get(k) in (1, 2))
    return ", ".join(COMPONENT_LABELS[kind][k].lower() for k in hits)


def priority_score(cci, criticality, band=None) -> float:
    """(100 - index) x criticality. Assets in the Critical band never rank as better than the ceiling,
    so a safety override cannot be outranked by a merely mediocre building."""
    if cci is None:
        return 0.0
    eff = min(cci, CRITICAL_INDEX_CEILING) if band == "Critical" else cci
    return round((100 - eff) * CRITICALITY_FACTOR.get(criticality or 2, 1.25), 1)


def is_overdue(kind: str, last_inspected, today=None) -> bool:
    today = today or today_ist()
    return last_inspected is None or (today - last_inspected).days > INSPECTION_INTERVAL_DAYS[kind]
