"""Coordinate accuracy scoring and Nominatim geocoding."""
from __future__ import annotations

import math
import threading

import requests


def _haversine_km(lat1, lon1, lat2, lon2):
    """Return the great-circle distance in km between two lat/lon points."""
    R = 6371000  # radius of Earth in meters
    phi_1 = math.radians(lat1)
    phi_2 = math.radians(lat2)

    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = math.sin(delta_phi / 2.0) ** 2 + math.cos(phi_1) * math.cos(phi_2) * math.sin(delta_lambda / 2.0) ** 2
    
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    meters = R * c  # output distance in meters
    km = meters / 1000.0  # output distance in kilometers

    meters = round(meters, 3)
    km = round(km, 3)
    return km


def coord_accuracy(t_lat, t_lon, e_lat, e_lon):
    """
    Score AI-extracted coordinates against GBIF truth using km distance.

      < 1 km   → 1.00  (effectively exact — rounding / display precision)
      < 10 km  → 0.90  (same locality, minor error)
      < 50 km  → 0.30  (same region)
      < 100 km → 0.10  (same country area)
      ≥ 100 km → 0.00  (wrong place)
    """
    try:
        t_lat, t_lon = float(t_lat), float(t_lon)
        e_lat, e_lon = float(e_lat), float(e_lon)
    except (ValueError, TypeError):
        return None   # one side unparseable — treat as missing, not wrong

    dist = _haversine_km(t_lat, t_lon, e_lat, e_lon)

    if dist <   1: return 1.00
    if dist <  10: return 0.90
    if dist <  50: return 0.30
    if dist < 100: return 0.10
    return 0.00


# Nominatim's usage policy (https://operations.osmfoundation.org/policies/nominatim/)
# requires a valid identifying User-Agent and <=1 request/sec. The lock below
# serializes access across run_audit()'s ThreadPoolExecutor workers so the
# 1.1s pacing is actually enforced.

_GEOCODE_CACHE: dict = {}   # query string → (lat, lon, display_name) | None
_GEOCODE_LAST_REQUEST = [0.0]   # mutable box so the nested throttle can update it
_GEOCODE_LOCK = threading.Lock()

_NOMINATIM_HEADERS = {
    "User-Agent": "HerbariumAuditTool/1.0"
}


def _geocode_location(locality, state, country):
    """
    Geocode a location using Nominatim (OpenStreetMap).  Tries increasingly
    broad queries until one returns a result, caches everything to respect the
    1 req/s rate limit, and returns (lat_str, lon_str, display_name) or None.
    """
    import time

    def _try(query, retries=2):
        query = query.strip(", ")
        if not query:
            return None
        if query in _GEOCODE_CACHE:
            return _GEOCODE_CACHE[query]

        # Serialize so the pacing below is a genuine single-flight >=1.1s/request.
        with _GEOCODE_LOCK:
            if query in _GEOCODE_CACHE:   # may have been resolved while waiting for the lock
                return _GEOCODE_CACHE[query]

            for attempt in range(retries + 1):
                elapsed = time.time() - _GEOCODE_LAST_REQUEST[0]
                if elapsed < 1.1:
                    time.sleep(1.1 - elapsed)

                try:
                    resp = requests.get(
                        "https://nominatim.openstreetmap.org/search",
                        params={"q": query, "format": "json", "limit": 1},
                        headers=_NOMINATIM_HEADERS,
                        timeout=8,
                    )
                    _GEOCODE_LAST_REQUEST[0] = time.time()
                    if resp.status_code == 429:
                        # Rate-limited — back off and retry, and do NOT cache a
                        # negative result for what may just be a transient limit.
                        if attempt < retries:
                            time.sleep(2 ** (attempt + 1))
                            continue
                        return None
                    resp.raise_for_status()
                    r = resp.json()
                    if r:
                        result = (r[0]["lat"], r[0]["lon"], r[0].get("display_name", query))
                        _GEOCODE_CACHE[query] = result
                        return result
                    _GEOCODE_CACHE[query] = None   # genuine "no match" — safe to cache
                    return None
                except Exception:
                    # Network/parse error — don't poison the persisted cache with
                    # a transient failure; just give up on this attempt.
                    return None
            return None

    # Build location parts, filter empty
    def _val(v):
        return str(v).strip() if v and str(v).strip().lower() not in ("n/a", "nan", "none", "") else ""

    loc, sta, cou = _val(locality), _val(state), _val(country)

    # Build query list from most to least specific
    queries = []

    # Full combination
    queries.append(", ".join(p for p in [loc, sta, cou] if p))
    # Without locality
    queries.append(", ".join(p for p in [sta, cou] if p))
    # Country only
    queries.append(cou)

    # If locality has commas, try each comma-split part combined with state+country
    # e.g. "Vico Orto Botanico, Orto Botanico di Catania" → try each part
    if loc and "," in loc:
        parts = [p.strip() for p in loc.split(",") if p.strip()]
        for part in reversed(parts):   # reversed = try most general part first
            queries.append(", ".join(p for p in [part, sta, cou] if p))
            queries.append(part)       # part alone as last resort

    # Locality alone (in case no state/country)
    queries.append(loc)
    queries.append(sta)

    for q in queries:
        result = _try(q)
        if result:
            return result

    return None
