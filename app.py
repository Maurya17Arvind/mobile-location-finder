"""
Mobile Number Location Finder
=============================

A small Flask application that:

1. Reads the metadata encoded in a phone number (country, region where the
   number range was allocated, original carrier, time zones, validity) with
   the offline ``phonenumbers`` library. No paid API is needed.
2. Shows the approximate country/region on an OpenStreetMap map. The place
   name is geocoded with geopy + Nominatim. If no reliable place is known,
   nothing is plotted; coordinates are never invented.
3. Offers consent-based live GPS sharing. The user creates a one-time link,
   the recipient opens it, explicitly agrees and grants browser GPS
   permission, and only then are coordinates sent back to the requester.

IMPORTANT: number metadata tells you where a number was *allocated*, never
where the phone *is*. Only the live-sharing feature shows a real position,
and only with the recipient's explicit consent.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import math
import os
import re
import secrets
import threading
import time
from functools import lru_cache

import phonenumbers
from flask import Flask, abort, jsonify, render_template, request, url_for
from phonenumbers import (NumberParseException, PhoneNumberFormat,
                          PhoneNumberType, carrier, geocoder)
from phonenumbers import timezone as pn_timezone
from werkzeug.exceptions import HTTPException

try:  # Folium is used for the stand-alone "approximate area" map page.
    import folium
except ImportError:  # pragma: no cover
    folium = None

try:  # geopy is optional: without it the app still works, just without a map pin.
    from geopy.geocoders import Nominatim
except ImportError:  # pragma: no cover
    Nominatim = None


# --------------------------------------------------------------------------
# Configuration (override with environment variables)
# --------------------------------------------------------------------------
# Static assets live in public/static so Vercel's CDN can serve them directly;
# Flask serves the same folder when running locally.
app = Flask(__name__, static_folder="public/static", static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024  # small JSON bodies only
app.json.sort_keys = False

ON_SERVERLESS = bool(os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))
# Redis connection string. Vercel Marketplace Redis integrations (Upstash,
# Redis Cloud) add REDIS_URL and/or KV_URL to the project automatically.
REDIS_URL = os.environ.get("REDIS_URL") or os.environ.get("KV_URL") or ""

# Behind nginx / a tunnel / Vercel's edge, which set X-Forwarded-* headers.
if os.environ.get("TRUST_PROXY") == "1" or ON_SERVERLESS:
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


class _RestoreRewrittenPath:
    """vercel.json rewrites every request to /api/index?__path=<original path>.

    Vercel hands the function the rewritten path, so without this every route
    404s. Put the original path back and drop the helper query param.
    """

    PARAM = "__path"

    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        from urllib.parse import parse_qsl, urlencode
        pairs = parse_qsl(environ.get("QUERY_STRING", ""), keep_blank_values=True)
        original, rest = None, []
        for key, value in pairs:
            if key == self.PARAM and original is None:
                original = value
            else:
                rest.append((key, value))
        if original is not None:
            # WSGI PATH_INFO is UTF-8 bytes decoded as latin-1.
            environ["PATH_INFO"] = ("/" + original.lstrip("/")).encode("utf-8").decode("latin-1")
            environ["SCRIPT_NAME"] = ""
            environ["QUERY_STRING"] = urlencode(rest)
        return self.wsgi_app(environ, start_response)


if ON_SERVERLESS:
    app.wsgi_app = _RestoreRewrittenPath(app.wsgi_app)

PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
ENABLE_GEOCODING = os.environ.get("ENABLE_GEOCODING", "1") == "1"
GEOCODER_USER_AGENT = os.environ.get(
    "GEOCODER_USER_AGENT", "mobile-number-location-finder/1.0 (local app)")

SHARE_DURATIONS = (5, 15, 30, 60)       # minutes the requester may choose
ENDED_SESSION_GRACE = 10 * 60           # seconds a data-less tombstone is kept
MAX_ACTIVE_SESSIONS = 500
ACTIVE_STATUSES = ("pending", "sharing")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


# --------------------------------------------------------------------------
# Phone-number metadata (offline, via phonenumbers)
# --------------------------------------------------------------------------
NUMBER_TYPE_LABELS = {
    PhoneNumberType.MOBILE: "Mobile",
    PhoneNumberType.FIXED_LINE: "Fixed line",
    PhoneNumberType.FIXED_LINE_OR_MOBILE: "Fixed line or mobile",
    PhoneNumberType.TOLL_FREE: "Toll free",
    PhoneNumberType.PREMIUM_RATE: "Premium rate",
    PhoneNumberType.SHARED_COST: "Shared cost",
    PhoneNumberType.VOIP: "VoIP",
    PhoneNumberType.PERSONAL_NUMBER: "Personal number",
    PhoneNumberType.PAGER: "Pager",
    PhoneNumberType.UAN: "UAN",
    PhoneNumberType.VOICEMAIL: "Voicemail",
}

VR = phonenumbers.ValidationResult
POSSIBLE_REASONS = {
    VR.IS_POSSIBLE: "Possible",
    VR.IS_POSSIBLE_LOCAL_ONLY: "Possible (local dialling only)",
    VR.INVALID_COUNTRY_CODE: "Not possible: invalid country code",
    VR.TOO_SHORT: "Not possible: too short",
    VR.INVALID_LENGTH: "Not possible: invalid length",
    VR.TOO_LONG: "Not possible: too long",
}

PARSE_ERRORS = {
    NumberParseException.INVALID_COUNTRY_CODE:
        "Unknown country code. Pick a country, or start the number with + and a valid code.",
    NumberParseException.NOT_A_NUMBER: "That doesn't look like a phone number.",
    NumberParseException.TOO_SHORT_AFTER_IDD: "The number is too short.",
    NumberParseException.TOO_SHORT_NSN: "The number is too short.",
    NumberParseException.TOO_LONG: "The number is too long.",
}
ALLOWED_INPUT = re.compile(r"^[+\d\s().\-/]{3,25}$")


@lru_cache(maxsize=1)
def country_index() -> dict:
    """Region code -> {region, name, code} for every region phonenumbers supports."""
    countries = {}
    for region in phonenumbers.SUPPORTED_REGIONS:
        example = phonenumbers.example_number(region)
        name = geocoder.country_name_for_number(example, "en") if example else ""
        countries[region] = {
            "region": region,
            "name": name or region,
            "code": phonenumbers.country_code_for_region(region),
        }
    return countries


def describe_number(num: phonenumbers.PhoneNumber) -> dict:
    """Collect everything the phonenumbers metadata can tell us about a number."""
    is_valid = phonenumbers.is_valid_number(num)
    reason = phonenumbers.is_possible_number_with_reason(num)

    region_code = phonenumbers.region_code_for_number(num)
    country = ""
    if region_code:
        country = geocoder.country_name_for_number(num, "en") or \
            country_index().get(region_code, {}).get("name", "")
    else:
        # Invalid number: only name a country if the calling code is unambiguous.
        regions = [r for r in phonenumbers.region_codes_for_country_code(num.country_code)
                   if r != phonenumbers.UNKNOWN_REGION]
        if len(regions) == 1:
            region_code = regions[0]
            country = country_index().get(region_code, {}).get("name", "")

    description = geocoder.description_for_number(num, "en") if is_valid else ""
    region_desc = description if description and description != country else ""
    carrier_name = carrier.name_for_number(num, "en") if is_valid else ""
    zones = [z for z in pn_timezone.time_zones_for_number(num) if z != "Etc/Unknown"] \
        if is_valid else []

    return {
        "country": country or None,
        "region_code": region_code,
        "country_calling_code": f"+{num.country_code}",
        "region": region_desc or None,
        "carrier": carrier_name or None,
        "is_valid": is_valid,
        "is_possible": reason in (VR.IS_POSSIBLE, VR.IS_POSSIBLE_LOCAL_ONLY),
        "possible_status": POSSIBLE_REASONS.get(reason, "Unknown"),
        "number_type": NUMBER_TYPE_LABELS.get(phonenumbers.number_type(num), "Unknown type"),
        "time_zones": zones,
        "international_format": phonenumbers.format_number(num, PhoneNumberFormat.INTERNATIONAL),
        "national_format": phonenumbers.format_number(num, PhoneNumberFormat.NATIONAL),
        "e164_format": phonenumbers.format_number(num, PhoneNumberFormat.E164),
    }


# --------------------------------------------------------------------------
# Approximate area: geocode the *place name* from the metadata (never invent)
# --------------------------------------------------------------------------
_nominatim = Nominatim(user_agent=GEOCODER_USER_AGENT) \
    if (Nominatim is not None and ENABLE_GEOCODING) else None
_geo_cache: dict = {}
_geo_lock = threading.Lock()
_geo_last_call = [0.0]


class GeocodingUnavailable(Exception):
    """The geocoding service could not be reached (offline, blocked, rate-limited)."""


def _geocode(query: str, country_code: str | None) -> dict | None:
    """Geocode a place name with Nominatim (cached, max 1 request/second)."""
    key = (query.lower(), country_code)
    if key in _geo_cache:
        return _geo_cache[key]
    shared_key = "geo:" + _digest(f"{key[0]}|{country_code}")
    shared = store.backend.cache_get(shared_key)  # Redis cache shared by all instances
    if shared is not None:
        _geo_cache[key] = shared.get("r")
        return _geo_cache[key]
    if _nominatim is None:
        return None
    with _geo_lock:
        if key in _geo_cache:
            return _geo_cache[key]
        wait = 1.0 - (time.monotonic() - _geo_last_call[0])
        if wait > 0:
            time.sleep(wait)  # Nominatim usage policy: max 1 request per second
        try:
            kwargs = {"exactly_one": True, "timeout": 6, "language": "en"}
            if country_code:
                kwargs["country_codes"] = country_code.lower()
            loc = _nominatim.geocode(query, **kwargs)
        except Exception as exc:  # network errors etc. are not cached
            app.logger.warning("Geocoding failed for %r: %s", query, exc)
            raise GeocodingUnavailable(str(exc)) from exc
        finally:
            _geo_last_call[0] = time.monotonic()

        result = None
        if loc is not None:
            bbox = loc.raw.get("boundingbox")
            result = {
                "lat": round(loc.latitude, 5),
                "lon": round(loc.longitude, 5),
                "bbox": [float(v) for v in bbox] if bbox and len(bbox) == 4 else None,
                "display_name": loc.address,
            }
        if len(_geo_cache) > 2000:
            _geo_cache.clear()
        _geo_cache[key] = result
        store.backend.cache_set(shared_key, {"r": result}, 30 * 24 * 3600)
        return result


def approximate_area(info: dict) -> dict | None:
    """Return the geocoded region/country for a valid number, or None."""
    if not info["is_valid"] or not info["country"] or not info["region_code"]:
        return None
    attempts = []
    if info["region"]:
        attempts.append((f'{info["region"]}, {info["country"]}', "region", info["region"]))
    attempts.append((info["country"], "country", info["country"]))
    for query, precision, label in attempts:
        found = _geocode(query, info["region_code"])  # may raise GeocodingUnavailable
        if found:
            return {**found, "precision": precision, "label": label,
                    "source": "OpenStreetMap Nominatim, geocoded from number metadata"}
    return None


# --------------------------------------------------------------------------
# Consent-based live location sharing
#   * Locally: in-memory store (single process).
#   * On Vercel / serverless: Redis store (REDIS_URL), because every request
#     may run in a different short-lived instance.
# Only SHA-256 hashes of tokens are ever stored.
# --------------------------------------------------------------------------
def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _end(s: dict, status: str) -> None:
    s["status"] = status
    s["location"] = None  # delete location data as soon as sharing ends
    s["ended_at"] = time.time()


def _refresh(s: dict) -> None:
    if s["status"] in ACTIVE_STATUSES and time.time() >= s["expires_at"]:
        _end(s, "expired")


def _viewer_ok(s: dict, token: str) -> bool:
    return bool(token) and hmac.compare_digest(s["viewer_hash"], _digest(token))


class MemoryBackend:
    """Thread-safe in-memory backend for local / single-process use."""
    name = "memory"

    def __init__(self):
        self._lock = threading.Lock()
        self._sessions: dict[str, dict] = {}
        self._by_share: dict[str, str] = {}
        self._hits: dict[str, list] = {}

    def insert(self, s: dict) -> None:
        with self._lock:
            self._sessions[s["id"]] = s
            self._by_share[s["share_hash"]] = s["id"]

    def sid_for_share(self, share_hash: str) -> str | None:
        with self._lock:
            return self._by_share.get(share_hash)

    def mutate(self, sid: str, fn):
        """Run fn(session) under the lock; returns (session_copy, fn_result) or None."""
        with self._lock:
            s = self._sessions.get(sid)
            if not s:
                return None
            result = fn(s)
            return dict(s), result

    def count_active(self) -> int:
        with self._lock:
            return sum(1 for s in self._sessions.values() if s["status"] in ACTIVE_STATUSES)

    def purge(self) -> None:
        now = time.time()
        with self._lock:
            for sid, s in list(self._sessions.items()):
                _refresh(s)
                if s["ended_at"] and now - s["ended_at"] > ENDED_SESSION_GRACE:
                    self._by_share.pop(s["share_hash"], None)
                    del self._sessions[sid]
            for key, hits in list(self._hits.items()):
                if not hits or hits[-1] < now - 3600:
                    del self._hits[key]

    def rate_limited(self, key: str, limit: int, window: int) -> bool:
        now = time.time()
        with self._lock:
            hits = [t for t in self._hits.get(key, []) if t > now - window]
            hits.append(now)
            self._hits[key] = hits
            return len(hits) > limit

    def cache_get(self, key: str):
        return None

    def cache_set(self, key: str, value, ttl: int) -> None:
        pass


class RedisBackend:
    """Redis backend (Upstash / Redis Cloud / any Redis) for serverless hosting.
    Keys expire on their own, so no background clean-up job is needed."""
    name = "redis"
    P = "mlf:"

    def __init__(self, url: str):
        import redis  # imported lazily: only needed when REDIS_URL is set
        self._redis_mod = redis
        self.r = redis.Redis.from_url(url, decode_responses=True,
                                      socket_timeout=5, socket_connect_timeout=5)

    def _skey(self, sid: str) -> str:
        return f"{self.P}s:{sid}"

    def _tkey(self, share_hash: str) -> str:
        return f"{self.P}t:{share_hash}"

    @staticmethod
    def _ttl(s: dict) -> int:
        end = (s["ended_at"] or s["expires_at"]) + ENDED_SESSION_GRACE
        return max(1, int(end - time.time()))

    def _save(self, pipe, s: dict) -> None:
        ttl = self._ttl(s)
        pipe.set(self._skey(s["id"]), json.dumps(s), ex=ttl)
        pipe.set(self._tkey(s["share_hash"]), s["id"], ex=ttl)

    def insert(self, s: dict) -> None:
        pipe = self.r.pipeline()
        self._save(pipe, s)
        pipe.execute()

    def sid_for_share(self, share_hash: str) -> str | None:
        return self.r.get(self._tkey(share_hash))

    def mutate(self, sid: str, fn):
        """Optimistic transaction (WATCH/MULTI) so a 'stop' can never be
        overwritten by a location update arriving at the same moment."""
        key = self._skey(sid)
        for _ in range(5):
            with self.r.pipeline() as pipe:
                try:
                    pipe.watch(key)
                    raw = pipe.get(key)
                    if not raw:
                        pipe.unwatch()
                        return None
                    s = json.loads(raw)
                    result = fn(s)
                    pipe.multi()
                    self._save(pipe, s)
                    pipe.execute()
                    return dict(s), result
                except self._redis_mod.WatchError:
                    continue
        raise RuntimeError("Could not update the sharing session, please retry.")

    def count_active(self) -> int:
        return 0  # sessions expire automatically; abuse is limited by rate limiting

    def purge(self) -> None:
        pass  # Redis TTLs do this

    def rate_limited(self, key: str, limit: int, window: int) -> bool:
        k = f"{self.P}rl:{key}:{int(time.time() // window)}"
        pipe = self.r.pipeline()
        pipe.incr(k)
        pipe.expire(k, window + 5)
        count, _ = pipe.execute()
        return int(count) > limit

    def cache_get(self, key: str):
        try:
            raw = self.r.get(f"{self.P}c:{key}")
            return json.loads(raw) if raw else None
        except Exception:
            return None

    def cache_set(self, key: str, value, ttl: int) -> None:
        try:
            self.r.set(f"{self.P}c:{key}", json.dumps(value), ex=ttl)
        except Exception:
            pass


class ShareStore:
    """Sharing-session logic, independent of where sessions are stored."""

    def __init__(self, backend):
        self.backend = backend

    def _sid(self, share_token: str) -> str | None:
        if not share_token or not TOKEN_RE.match(share_token):
            return None
        return self.backend.sid_for_share(_digest(share_token))

    def purge(self) -> None:
        self.backend.purge()

    def create(self, requester: str, minutes: int):
        self.backend.purge()
        if self.backend.count_active() >= MAX_ACTIVE_SESSIONS:
            return None
        share_token = secrets.token_urlsafe(32)
        viewer_token = secrets.token_urlsafe(32)
        now = time.time()
        s = {
            "id": secrets.token_urlsafe(16),
            "share_hash": _digest(share_token),
            "viewer_hash": _digest(viewer_token),
            "requester": requester,
            "created_at": now,
            "expires_at": now + minutes * 60,
            "status": "pending",
            "location": None,
            "consented_at": None,
            "ended_at": None,
        }
        self.backend.insert(s)
        return dict(s), share_token, viewer_token

    def viewer_get(self, sid: str, token: str) -> dict | None:
        def fn(s):
            if not _viewer_ok(s, token):
                return False
            _refresh(s)
            return True
        out = self.backend.mutate(sid, fn) if sid else None
        return out[0] if out and out[1] else None

    def viewer_cancel(self, sid: str, token: str) -> dict | None:
        def fn(s):
            if not _viewer_ok(s, token):
                return False
            _refresh(s)
            if s["status"] in ACTIVE_STATUSES:
                _end(s, "cancelled")
            return True
        out = self.backend.mutate(sid, fn) if sid else None
        return out[0] if out and out[1] else None

    def recipient_get(self, token: str) -> dict | None:
        sid = self._sid(token)
        out = self.backend.mutate(sid, _refresh) if sid else None
        return out[0] if out else None

    def recipient_update(self, token: str, lat: float, lon: float, acc: float):
        """Returns (session_copy | None, http_status)."""
        def fn(s):
            _refresh(s)
            if s["status"] not in ACTIVE_STATUSES:
                return 410
            now = time.time()
            if s["consented_at"] is None:
                s["consented_at"] = now
            s["status"] = "sharing"
            s["location"] = {"latitude": round(lat, 6), "longitude": round(lon, 6),
                             "accuracy": round(acc, 1), "timestamp": int(now * 1000)}
            return 200
        sid = self._sid(token)
        out = self.backend.mutate(sid, fn) if sid else None
        return (out[0], out[1]) if out else (None, 404)

    def recipient_stop(self, token: str, reason: str) -> dict | None:
        def fn(s):
            _refresh(s)
            if s["status"] in ACTIVE_STATUSES:
                declined = reason == "declined" and s["status"] == "pending"
                _end(s, "declined" if declined else "stopped")
        sid = self._sid(token)
        out = self.backend.mutate(sid, fn) if sid else None
        return out[0] if out else None


if REDIS_URL:
    store = ShareStore(RedisBackend(REDIS_URL))
else:
    store = ShareStore(MemoryBackend())

LIVE_SHARING_AVAILABLE = store.backend.name == "redis" or not ON_SERVERLESS


def _janitor() -> None:
    while True:
        time.sleep(30)
        try:
            store.purge()
        except Exception:  # pragma: no cover
            app.logger.exception("purge failed")


if store.backend.name == "memory" and not ON_SERVERLESS:
    threading.Thread(target=_janitor, name="share-janitor", daemon=True).start()


def client_ip() -> str:
    return request.remote_addr or "unknown"


def rate_limited(bucket: str, limit: int, window: int) -> bool:
    try:
        return store.backend.rate_limited(f"{bucket}:{client_ip()}", limit, window)
    except Exception:  # never block users because the limiter's storage hiccuped
        app.logger.warning("rate limiter unavailable", exc_info=True)
        return False


def _expires_in(s: dict) -> int:
    return max(0, int(s["expires_at"] - time.time()))


def viewer_view(s: dict) -> dict:
    """What the requester may see (only via the secret viewer token)."""
    return {
        "ok": True,
        "session_id": s["id"],
        "status": s["status"],
        "expires_in": _expires_in(s),
        "consented_at": int(s["consented_at"] * 1000) if s["consented_at"] else None,
        "location": s["location"] if s["status"] == "sharing" else None,
    }


def recipient_view(s: dict) -> dict:
    """What the recipient's page needs. It never includes location data."""
    return {"ok": True, "status": s["status"], "expires_in": _expires_in(s)}


def _bearer_token() -> str:
    auth = request.headers.get("Authorization", "")
    return auth[7:].strip() if auth.startswith("Bearer ") else ""


def api_error(message: str, http_status: int, **extra):
    return jsonify(ok=False, error=message, **extra), http_status


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------
@app.get("/")
def index():
    return render_template("index.html", durations=SHARE_DURATIONS,
                           live_available=LIVE_SHARING_AVAILABLE)


@app.get("/api/health")
def api_health():
    return jsonify(ok=True, store=store.backend.name, live_sharing=LIVE_SHARING_AVAILABLE,
                   geocoding=_nominatim is not None)


@app.get("/share/<token>")
def share_page(token: str):
    s = store.recipient_get(token)
    if not s:
        return render_template("share_location.html", state="invalid", token="",
                               requester="", expires_in=0), 404
    return render_template("share_location.html", state=s["status"], token=token,
                           requester=s["requester"], expires_in=_expires_in(s))


@app.get("/map/approx")
def folium_map():
    """Stand-alone Folium map of the approximate area for a valid number."""
    if folium is None:
        abort(501, "Folium is not installed.")
    raw = request.args.get("number", "")[:25]
    try:
        num = phonenumbers.parse(raw, None)
    except NumberParseException:
        abort(400, "Invalid number.")
    info = describe_number(num)
    try:
        area = approximate_area(info)
    except GeocodingUnavailable:
        abort(503, "The geocoding service could not be reached. Please try again later.")
    if not area:
        abort(404, "No reliable geographic information is available for this number.")

    label = f'{area["label"]}, {info["country"]}' if area["precision"] == "region" else area["label"]
    note = (f"<b>Approximate {area['precision']}:</b> {html.escape(label)}<br>"
            "Based on phone-number metadata.<br><i>This is NOT the phone's current location.</i>")
    fmap = folium.Map(location=[area["lat"], area["lon"]],
                      zoom_start=7 if area["precision"] == "region" else 4,
                      tiles="OpenStreetMap")
    if area["bbox"]:
        s_, n_, w_, e_ = area["bbox"]
        if abs(e_ - w_) <= 60 and abs(n_ - s_) <= 60:
            folium.Rectangle(bounds=[[s_, w_], [n_, e_]], color="#1a73e8", weight=2,
                             dash_array="6 6", fill=True, fill_opacity=0.06).add_to(fmap)
            fmap.fit_bounds([[s_, w_], [n_, e_]])
    folium.Marker([area["lat"], area["lon"]], popup=folium.Popup(note, max_width=280),
                  tooltip="Approximate area (number metadata)",
                  icon=folium.Icon(color="blue", icon="info-sign")).add_to(fmap)
    return fmap.get_root().render()


# --------------------------------------------------------------------------
# JSON API: phone-number lookup
# --------------------------------------------------------------------------
@app.get("/api/countries")
def api_countries():
    items = sorted(country_index().values(), key=lambda c: c["name"])
    return jsonify(ok=True, countries=items)


@app.post("/api/lookup")
def api_lookup():
    if rate_limited("lookup", 60, 60):
        return api_error("Too many lookups. Please wait a minute and try again.", 429)
    data = request.get_json(silent=True) or {}
    raw = str(data.get("number", "")).strip()
    region = str(data.get("region", "") or "").strip().upper()

    if not raw:
        return api_error("Please enter a mobile number.", 400)
    if not ALLOWED_INPUT.match(raw):
        return api_error("Use digits only (spaces, dashes, brackets and a leading + are allowed).", 400)
    if region and region not in phonenumbers.SUPPORTED_REGIONS:
        return api_error("Unknown country selected.", 400)
    if not raw.startswith("+") and not region:
        return api_error("Choose a country code, or start the number with + and the country code.", 400)

    try:
        num = phonenumbers.parse(raw, None if raw.startswith("+") else region)
    except NumberParseException as exc:
        return api_error(PARSE_ERRORS.get(exc.error_type, "Could not read that number."), 400)

    info = describe_number(num)
    try:
        area = approximate_area(info)
        geo_down = False
    except GeocodingUnavailable:
        area, geo_down = None, True
    if area:
        note = None
    elif geo_down:
        note = ("The map lookup service (OpenStreetMap Nominatim) could not be reached, "
                "so the area is not plotted. Number details above are unaffected.")
    elif not info["is_valid"]:
        note = "The number is not valid, so no area is shown on the map."
    else:
        note = "No reliable geographic information is available, so nothing is plotted on the map."
    return jsonify(ok=True, number=info, approximate_area=area, approx_note=note)


# --------------------------------------------------------------------------
# JSON API: live location (requester side, protected by the viewer token)
# --------------------------------------------------------------------------
@app.post("/api/share")
def api_share_create():
    if not LIVE_SHARING_AVAILABLE:
        return api_error("Live location sharing isn't configured on this server yet: "
                         "connect a Redis database so REDIS_URL is set (see README).", 503)
    if rate_limited("share", 10, 600):
        return api_error("Too many sharing requests. Please wait a few minutes.", 429)
    data = request.get_json(silent=True) or {}
    requester = " ".join(str(data.get("requester_name", "")).split())[:40]
    if len(requester) < 2:
        return api_error("Please enter your name so the recipient knows who is asking.", 400)
    try:
        minutes = int(data.get("duration", 15))
    except (TypeError, ValueError):
        minutes = -1
    if minutes not in SHARE_DURATIONS:
        return api_error("Please choose a valid expiry time.", 400)

    created = store.create(requester, minutes)
    if created is None:
        return api_error("Too many active sharing requests. Please try again later.", 503)
    s, share_token, viewer_token = created
    base = PUBLIC_BASE_URL or request.host_url.rstrip("/")
    return jsonify(
        ok=True,
        session_id=s["id"],
        viewer_token=viewer_token,  # keep secret: authorises reading the location
        share_url=base + url_for("share_page", token=share_token),
        expires_in=minutes * 60,
        duration_minutes=minutes,
        requester_name=requester,
        secure_context=request.is_secure or base.startswith("https://"),
    ), 201


@app.get("/api/share/<sid>")
def api_share_status(sid: str):
    s = store.viewer_get(sid, _bearer_token())
    if not s:
        return api_error("Not authorised or session not found.", 401)
    return jsonify(viewer_view(s))


@app.delete("/api/share/<sid>")
def api_share_cancel(sid: str):
    s = store.viewer_cancel(sid, _bearer_token())
    if not s:
        return api_error("Not authorised or session not found.", 401)
    return jsonify(viewer_view(s))


# --------------------------------------------------------------------------
# JSON API: live location (recipient side, protected by the share token)
# --------------------------------------------------------------------------
@app.get("/api/recipient/<token>/status")
def api_recipient_status(token: str):
    s = store.recipient_get(token)
    if not s:
        return api_error("This link is invalid or has expired.", 404, status="invalid")
    return jsonify(recipient_view(s))


@app.post("/api/recipient/<token>/location")
def api_recipient_location(token: str):
    if rate_limited("loc", 120, 60):
        return api_error("Too many updates. Slowing down.", 429)
    data = request.get_json(silent=True) or {}
    if data.get("consent") is not True:
        return api_error("Explicit consent is required to share a location.", 403)
    try:
        lat = float(data["latitude"])
        lon = float(data["longitude"])
        acc = float(data.get("accuracy") or 0)
    except (KeyError, TypeError, ValueError):
        return api_error("Invalid coordinates.", 400)
    if not all(math.isfinite(v) for v in (lat, lon, acc)) or \
            not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return api_error("Invalid coordinates.", 400)
    acc = min(max(acc, 0.0), 100_000.0)

    s, status = store.recipient_update(token, lat, lon, acc)
    if status == 404:
        return api_error("This link is invalid or has expired.", 404, status="invalid")
    if status == 410:
        return api_error("This sharing session has ended.", 410, status=s["status"])
    return jsonify(recipient_view(s))


@app.post("/api/recipient/<token>/stop")
def api_recipient_stop(token: str):
    data = request.get_json(silent=True, force=True) or {}
    s = store.recipient_stop(token, str(data.get("reason", "stopped")))
    if not s:
        return api_error("This link is invalid or has expired.", 404, status="invalid")
    return jsonify(recipient_view(s))


# --------------------------------------------------------------------------
# Errors & security headers
# --------------------------------------------------------------------------
@app.errorhandler(HTTPException)
def handle_http_error(exc: HTTPException):
    if request.path.startswith("/api/"):
        return api_error(exc.description or exc.name, exc.code or 500)
    return exc


@app.errorhandler(Exception)
def handle_unexpected(exc: Exception):  # pragma: no cover
    if isinstance(exc, HTTPException):
        return handle_http_error(exc)
    app.logger.exception("Unhandled error")
    if request.path.startswith("/api/"):
        return api_error("Something went wrong on the server. Please try again.", 500)
    return "Internal server error", 500


CSP = ("default-src 'self'; "
       "script-src 'self' https://cdn.jsdelivr.net; "
       "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
       "font-src 'self' https://cdn.jsdelivr.net; "
       "img-src 'self' data: https://tile.openstreetmap.org https://*.tile.openstreetmap.org "
       "https://cdn.jsdelivr.net; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    # "strict-origin" sends only https://your-host (never the path, so share tokens
    # can't leak) - OpenStreetMap's tile servers reject requests with no Referer at all.
    resp.headers.setdefault("Referrer-Policy", "strict-origin")
    resp.headers.setdefault("Permissions-Policy", "geolocation=(self), camera=(), microphone=()")
    if not request.path.startswith("/map/"):  # the Folium page loads its own CDN assets
        resp.headers.setdefault("Content-Security-Policy", CSP)
        resp.headers.setdefault("X-Frame-Options", "DENY")
    if request.path.startswith(("/api/", "/share/")):
        resp.headers["Cache-Control"] = "no-store"
    if request.is_secure:
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return resp


if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    ssl_context = "adhoc" if os.environ.get("USE_ADHOC_SSL") == "1" else None  # needs pyOpenSSL
    app.run(host=host, port=port, debug=os.environ.get("FLASK_DEBUG") == "1",
            ssl_context=ssl_context, threaded=True)
