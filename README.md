# Mobile Number Location Finder

A simple Flask web app that:

1. **Reads phone-number metadata.** It uses the offline `phonenumbers` library to find the country, calling code, region, original carrier, validity, possible-number status, time zone, and international format. No paid API is needed.
2. **Shows the approximate country or region** on an OpenStreetMap map (Leaflet in the page, plus a Folium map you can open on its own).

Nothing is stored. Each search is computed fresh, so there is no database to set up.

> **Approximate metadata vs. actual location**
> Phone-number metadata tells you where a number range was **allocated** and by which carrier. It is **never** the phone's current location. Numbers get ported to other carriers and phones travel.

---

## Project structure

```
mobile-location-finder/
├── app.py                  # Flask backend (lookup API, geocoding, Folium map)
├── requirements.txt
├── vercel.json             # Vercel config (routes every request to the Flask function)
├── api/
│   └── index.py            # Vercel entry point, imports `app` from app.py
├── templates/
│   └── index.html          # main single-page UI
└── public/
    └── static/
        ├── style.css       # white and blue theme
        └── script.js       # front-end logic
```

Static files sit in `public/static/`. Vercel's CDN serves them directly, and Flask serves the same folder when you run the app locally.

## Install and run locally

Requires Python 3.9 or newer.

```bash
cd mobile-location-finder
python -m venv venv
# Windows:      venv\Scripts\activate
# macOS/Linux:  source venv/bin/activate
pip install -r requirements.txt
python app.py
```

Then open **http://127.0.0.1:5000**.

Try `+91 98765 43210`, `+44 20 7183 8750` or `+1 415 555 2671`. You can also pick a country in the dropdown and type the national number.

> The page loads Bootstrap, Bootstrap Icons and Leaflet from the jsDelivr CDN, and the map loads tiles from OpenStreetMap, so the browser needs internet access. The server uses the internet only to geocode the region name through Nominatim. If Nominatim can't be reached, the number details still work and the map says why nothing is plotted.

## Deploy to Vercel

Vercel runs Flask as a serverless function. The app needs no database or other storage, so it works there as-is.

### 1. Put the project on GitHub
```bash
cd mobile-location-finder
git init
git add .
git commit -m "Mobile Number Location Finder"
# create an empty repo on github.com, then:
git remote add origin https://github.com/<you>/mobile-location-finder.git
git branch -M main
git push -u origin main
```

### 2. Import it into Vercel
1. Go to <https://vercel.com/new>, sign in with GitHub, and **Import** the repository.
2. Leave **Framework Preset** as detected (or choose *Other*). There is no build command and no output directory. If the project sits in a subfolder of your repo, set **Root Directory** to `mobile-location-finder`.
3. Recommended: under **Settings → Environment Variables**, add `GEOCODER_USER_AGENT` = `mobile-number-location-finder/1.0 (your-email@example.com)`. Nominatim's usage policy asks apps to identify themselves.
4. Click **Deploy**. Vercel installs `requirements.txt` and gives you a URL like `https://mobile-location-finder.vercel.app`.

### 3. Check it
Open `https://<your-app>.vercel.app/api/health`. You should see `{"ok": true, "geocoding": true}`.

### Deploying with the Vercel CLI instead of GitHub
```bash
npm i -g vercel
cd mobile-location-finder
vercel            # first run links or creates the project (preview deploy)
vercel --prod     # production deploy
```

### Notes for Vercel
- `vercel.json` sends every request to the Flask app and passes the original path as `__path`. `app.py` restores it before routing, so keep both pieces.
- `TRUST_PROXY` isn't needed. The app detects Vercel and trusts its proxy headers automatically.
- Nominatim sometimes rate-limits shared cloud IPs. If that happens, the map shows *"lookup service could not be reached"* while the number details still work.
- Lookups are limited to 60 a minute per client IP. The counter is kept in memory, so on Vercel each instance counts separately.

## Configuration (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `HOST` / `PORT` | `127.0.0.1` / `5000` | Bind address |
| `TRUST_PROXY` | `0` | Set `1` behind nginx or a tunnel so the app honours `X-Forwarded-*` headers |
| `USE_ADHOC_SSL` | `0` | Set `1` for self-signed HTTPS (needs `pyOpenSSL`) |
| `ENABLE_GEOCODING` | `1` | Set `0` to turn off Nominatim lookups |
| `GEOCODER_USER_AGENT` | `mobile-number-location-finder/1.0` | Identify your app to Nominatim (put your contact details in it for production) |
| `FLASK_DEBUG` | `0` | Set `1` for the debug reloader during development only |

## API overview

| Method & path | Description |
|---|---|
| `GET /api/health` | Shows the app is up and whether geocoding is enabled |
| `GET /api/countries` | Country list for the selector |
| `POST /api/lookup` `{number, region}` | Number metadata and approximate area |
| `GET /map/approx?number=+E164` | Standalone Folium map of the approximate area |

## Security and privacy

- **Nothing is stored.** Numbers aren't saved to disk, a database or logs. Searching the same number again just recomputes the result.
- **Input validation.** Phone numbers, country codes and request size (8 KB maximum) are validated.
- **Headers.** API responses use `Cache-Control: no-store`. Every page sends a Content-Security-Policy, `X-Frame-Options: DENY`, `nosniff` and `Referrer-Policy: strict-origin` (OpenStreetMap's tile servers require a Referer, so only the site's origin is sent). HSTS is added on HTTPS.
- **Rate limiting.** 60 lookups a minute per client IP.

### Production checklist
- **On your own server:** run with a WSGI server, e.g. `gunicorn app:app`, or `waitress-serve app:app` on Windows.
- **Respect the [Nominatim usage policy](https://operations.osmfoundation.org/policies/nominatim/).** Set a real `GEOCODER_USER_AGENT`. The app already caches results in memory and limits itself to 1 request per second.
