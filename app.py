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

Nothing is stored: every lookup is computed fresh from the number.

IMPORTANT: number metadata tells you where a number was *allocated*, never
where the phone *is*.
"""
from __future__ import annotations

import html
import os
import re
import threading
import time
from functools import lru_cache

import phonenumbers
from flask import Flask, abort, jsonify, render_template, request
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

ENABLE_GEOCODING = os.environ.get("ENABLE_GEOCODING", "1") == "1"
GEOCODER_USER_AGENT = os.environ.get(
    "GEOCODER_USER_AGENT", "mobile-number-location-finder/1.0 (local app)")


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
# Rate limiting (in memory, per instance; nothing about users is stored)
# --------------------------------------------------------------------------
_hits: dict[str, list] = {}
_hits_lock = threading.Lock()


def rate_limited(bucket: str, limit: int, window: int) -> bool:
    key = f"{bucket}:{request.remote_addr or 'unknown'}"
    now = time.time()
    with _hits_lock:
        if len(_hits) > 10_000:
            _hits.clear()
        hits = [t for t in _hits.get(key, ()) if t > now - window]
        hits.append(now)
        _hits[key] = hits
        return len(hits) > limit


def api_error(message: str, http_status: int, **extra):
    return jsonify(ok=False, error=message, **extra), http_status


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------
@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/health")
def api_health():
    return jsonify(ok=True, geocoding=_nominatim is not None)


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
    # "strict-origin" sends only https://your-host - OpenStreetMap's tile servers
    # reject requests with no Referer at all.
    resp.headers.setdefault("Referrer-Policy", "strict-origin")
    resp.headers.setdefault("Permissions-Policy", "geolocation=(), camera=(), microphone=()")
    if not request.path.startswith("/map/"):  # the Folium page loads its own CDN assets
        resp.headers.setdefault("Content-Security-Policy", CSP)
        resp.headers.setdefault("X-Frame-Options", "DENY")
    if request.path.startswith("/api/"):
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
