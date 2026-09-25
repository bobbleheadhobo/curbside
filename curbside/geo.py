"""Distance helper. Marketplace gives coordinates, we care about drive-ability."""
from __future__ import annotations

import math

EARTH_RADIUS_MI = 3958.8


def haversine_miles(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_MI * math.asin(math.sqrt(a))


# --- coarse distance from a place name --------------------------------------
#
# Facebook's search feed gives a city and state but no coordinates -- those only
# arrive with the item page, which costs a 15-second rate-limited request. Its
# results are also not confined to your area at all: a single "free" search
# returned listings from Kansas City, Amarillo, Sacramento, San Francisco,
# Redmond, Findlay and one place called "This, GES".
#
# So the gate needs a way to reject the obviously-distant BEFORE paying for
# detail. Two rules, in order:
#
#   1. A state that is not the home state is definitively too far. This alone
#      catches most of the noise, and needs no city table.
#   2. A known city gets its centroid distance.
#
# Anything else returns None, meaning "cannot tell" -- and the gate lets those
# through. Failing open costs one detail fetch; failing closed silently drops a
# listing that might have been the one you wanted.

HOME_STATE = "NM"
OUT_OF_STATE_MILES = 9999.0

CITY_COORDS: dict[str, tuple[float, float]] = {
    # Albuquerque metro
    "albuquerque": (35.0844, -106.6504),
    "los ranchos de albuquerque": (35.1614, -106.6428),
    "rio rancho": (35.2328, -106.6630),
    "corrales": (35.2378, -106.6064),
    "bernalillo": (35.3000, -106.5511),
    "placitas": (35.3053, -106.4278),
    "algodones": (35.3892, -106.4839),
    "isleta": (34.9042, -106.6919),
    "tijeras": (35.0842, -106.3819),
    "cedar crest": (35.1281, -106.3436),
    "sandia park": (35.1717, -106.3711),
    "edgewood": (35.0578, -106.1911),
    "moriarty": (34.9900, -106.0489),
    "bosque farms": (34.8548, -106.7003),
    "peralta": (34.8384, -106.6889),
    "los lunas": (34.8062, -106.7331),
    "belen": (34.6626, -106.7764),
    # elsewhere in New Mexico -- known, and known to be far
    "santa fe": (35.6870, -105.9378),
    "espanola": (35.9911, -106.0806),
    "jemez springs": (35.7714, -106.6914),
    "socorro": (34.0584, -106.8914),
    "grants": (35.1473, -107.8523),
    "gallup": (35.5281, -108.7426),
    "taos": (36.4072, -105.5731),
    "las cruces": (32.3199, -106.7637),
    "roswell": (33.3943, -104.5230),
    "artesia": (32.8423, -104.4030),
    "farmington": (36.7281, -108.2187),
    "truth or consequences": (33.1284, -107.2528),
}


def approx_distance_miles(place: str | None, home_lat: float,
                          home_lng: float) -> float | None:
    """Rough miles from home for a "City, ST" string, or None if unknown."""
    if not place:
        return None
    name, _, state = place.partition(",")
    state = state.strip().upper()
    if state and len(state) == 2 and state != HOME_STATE:
        return OUT_OF_STATE_MILES
    coords = CITY_COORDS.get(name.strip().lower())
    if coords is None:
        return None
    return round(haversine_miles(home_lat, home_lng, *coords), 1)
