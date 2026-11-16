# Mobile Number Location Finder

A simple Flask web app that:

1. **Reads phone-number metadata.** It uses the offline `phonenumbers` library to find the country, calling code, region, original carrier, validity, possible-number status, time zone, and international format. No paid API is needed.
2. **Shows the approximate country or region** on an OpenStreetMap map (Leaflet in the page, plus a Folium map you can open on its own).
3. **Shows a live GPS location, with consent only.** The recipient opens a private link, agrees, and grants GPS permission in their browser. They can stop sharing at any time, and the link expires automatically.

> **Approximate metadata vs. actual location**
> Phone-number metadata tells you where a number range was **allocated** and by which carrier. It is **never** the phone's current location. Numbers get ported to other carriers and phones travel.
> The app only shows a real position (the red marker) when the phone's owner chooses to share it.

---

## Project structure

```
mobile-location-finder/
├── app.py                  # Flask backend (lookup API, geocoding, Folium map, sharing sessions)
├── requirements.txt
├── vercel.json             # Vercel config (routes every request to the Flask function)
├── api/
│   └── index.py            # Vercel entry point, imports `app` from app.py
├── templates/
│   ├── index.html          # main single-page UI
│   └── share_location.html # recipient's consent and sharing page
└── public/
    └── static/
        ├── style.css       # white and blue theme
        └── script.js       # front-end logic for both pages
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

## Testing live location on a real phone

Browsers allow GPS only on **HTTPS** pages, or on `localhost`. A link like `http://192.168.x.x:5000` will not work on a phone. Pick one of these options:

**Option A: HTTPS tunnel (easiest).** Use [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/) or [ngrok](https://ngrok.com/):

```bash
# terminal 1
python app.py
# terminal 2
cloudflared tunnel --url http://127.0.0.1:5000      # or: ngrok http 5000
```

Open the `https://…` URL the tunnel prints (not localhost). The share links then use that HTTPS address, and the phone can open them.
If links still show `http://`, set these variables before starting the app:

```bash
# Windows (PowerShell)
$env:TRUST_PROXY="1"; $env:PUBLIC_BASE_URL="https://your-tunnel-url"; python app.py
# macOS/Linux
TRUST_PROXY=1 PUBLIC_BASE_URL=https://your-tunnel-url python app.py
```

**Option B: self-signed HTTPS on your local network.**

```bash
pip install pyOpenSSL
# PowerShell: $env:USE_ADHOC_SSL="1"; $env:HOST="0.0.0.0"; python app.py
USE_ADHOC_SSL=1 HOST=0.0.0.0 python app.py
```

Then open `https://<your-PC-IP>:5000` and accept the certificate warning on both devices.

### How the flow works
1. Look up a number. This step is optional, but it lets the WhatsApp and SMS buttons pre-fill the recipient.
2. Under **Live GPS location**, enter your name, choose an expiry time (5 to 60 minutes) and click **Request Live Location**.
3. Send the private link by Copy, WhatsApp or SMS.
4. The recipient sees who is asking, what will be shared and for how long. They tick the consent box, tap **Share My Location**, and allow GPS in the browser.
5. Their position appears as a red marker with an accuracy circle. It updates about every 5 to 15 seconds.
6. Sharing ends when the recipient taps **Stop sharing** or closes the page, when you click **Cancel request**, or when the link expires. The location is deleted on the server and removed from your map.

## Deploy to Vercel

Vercel runs Flask as **serverless functions**, so each request may hit a fresh, short-lived instance and nothing kept in memory survives between requests. Phone lookup and the map work as they are. **Live location sharing needs a Redis database** to keep its sessions. When `REDIS_URL` (or `KV_URL`) is set, the app switches to Redis automatically. Redis also stores the rate-limit counters and a shared geocoding cache, and its keys expire on their own, so ended sessions are wiped automatically.

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
3. Click **Deploy**. Vercel installs `requirements.txt` and gives you a URL like `https://mobile-location-finder.vercel.app`.

Number lookup and the map should work at this point. The live-location panel shows *"Live sharing is not set up"* until you finish the next step.

### 3. Add Redis (free tier) for live sharing
1. In your Vercel project, open the **Storage** tab, then **Create Database** (or **Browse Marketplace**), and pick **Upstash for Redis** (or *Redis* by Redis Cloud). The free plan is enough.
2. **Connect** the database to the project for all environments. This adds the connection variables automatically.
3. Open **Settings → Environment Variables** and check that **`REDIS_URL`** or **`KV_URL`** exists, with a value starting `rediss://` or `redis://`.
   - If you only see `KV_REST_API_URL` / `KV_REST_API_TOKEN`, open the database in the Upstash console, copy the **TCP / Redis URL** (`rediss://default:<password>@<host>.upstash.io:6379`), and add it yourself as `REDIS_URL`.
4. Recommended: add `GEOCODER_USER_AGENT` = `mobile-number-location-finder/1.0 (your-email@example.com)`. Nominatim's usage policy asks apps to identify themselves.
5. Go to **Deployments**, open the menu on the latest deployment, and choose **Redeploy**. Environment variables only apply to new deployments.

### 4. Check it
- Open `https://<your-app>.vercel.app/api/health`. You should see `"store": "redis", "live_sharing": true`.
- Vercel sites are HTTPS by default, so share links open and **GPS works on phones** without any tunnel.

### Deploying with the Vercel CLI instead of GitHub
```bash
npm i -g vercel
cd mobile-location-finder
vercel            # first run links or creates the project (preview deploy)
vercel --prod     # production deploy
```
Then add Redis as in step 3 and run `vercel --prod` again.

### Notes for Vercel
- `PUBLIC_BASE_URL` and `TRUST_PROXY` aren't needed. The app detects Vercel and builds share links from the HTTPS host automatically. Set `PUBLIC_BASE_URL` only if you add a custom domain and want links to always use it.
- Nominatim sometimes rate-limits shared cloud IPs. If that happens, the map shows *"lookup service could not be reached"* while the number details still work. Results are cached in Redis for 30 days, so repeat lookups don't call Nominatim.
- Limits per client IP: 60 lookups a minute, 10 new share links per 10 minutes, and 120 location updates a minute.

## Configuration (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `HOST` / `PORT` | `127.0.0.1` / `5000` | Bind address |
| `REDIS_URL` (or `KV_URL`) | *(unset)* | Redis for sharing sessions, rate limits and the geocode cache. **Required on Vercel** for live sharing; leave it unset locally to use memory |
| `PUBLIC_BASE_URL` | *(request host)* | Base URL used in share links, e.g. `https://finder.example.com` |
| `TRUST_PROXY` | `0` | Set `1` behind nginx or a tunnel so the app honours `X-Forwarded-*` headers |
| `USE_ADHOC_SSL` | `0` | Set `1` for self-signed HTTPS (needs `pyOpenSSL`) |
| `ENABLE_GEOCODING` | `1` | Set `0` to turn off Nominatim lookups |
| `GEOCODER_USER_AGENT` | `mobile-number-location-finder/1.0` | Identify your app to Nominatim (put your contact details in it for production) |
| `FLASK_DEBUG` | `0` | Set `1` for the debug reloader during development only |

## API overview

| Method & path | Auth | Description |
|---|---|---|
| `GET /api/health` | none | Shows which storage is in use and whether live sharing is available |
| `GET /api/countries` | none | Country list for the selector |
| `POST /api/lookup` `{number, region}` | none | Number metadata and approximate area |
| `GET /map/approx?number=+E164` | none | Standalone Folium map of the approximate area |
| `POST /api/share` `{requester_name, duration}` | none | Creates a session. Returns `share_url` and a secret `viewer_token` |
| `GET /api/share/<id>` | `Authorization: Bearer <viewer_token>` | Status and live location (requester only) |
| `DELETE /api/share/<id>` | Bearer viewer token | Cancel and delete |
| `GET /share/<share_token>` | the share token itself | Recipient consent page |
| `POST /api/recipient/<share_token>/location` | share token + `consent: true` | Send GPS coordinates |
| `POST /api/recipient/<share_token>/stop` | share token | Stop or decline sharing (deletes the data) |

## Security and privacy

- **Secure tokens.** Tokens come from `secrets.token_urlsafe(32)` (256-bit). The server stores only their SHA-256 hashes and compares them in constant time.
- **Separate authorisation.** The recipient link can only *send* a location. Only the requester's viewer token, held in browser memory and never put in a URL, can *read* it.
- **Explicit consent.** The recipient must tick a consent box and also grant browser GPS permission. The server rejects any location sent without `consent: true`.
- **Expiry and deletion.** Links expire after 5 to 60 minutes. When a session is stopped, declined, cancelled or expires, the coordinates are wiped right away. A small record without location data is kept for 10 minutes so both sides can see the final status, then removed.
- **No public exposure.** Nothing is written to disk or logs. Responses use `Cache-Control: no-store` and `Referrer-Policy: strict-origin` (sends only the site's origin, never the URL path or token, which OpenStreetMap's tile servers require), along with a Content-Security-Policy, `X-Frame-Options: DENY` and `nosniff`.
- **Input validation.** Phone numbers, coordinate ranges, names, durations and request size (8 KB maximum) are all validated.

### Production checklist
- **Serve over HTTPS.** GPS sharing needs it. HSTS is sent automatically once the app runs on HTTPS.
- **On your own server:** without Redis, sessions live in memory, so run a single process with threads, e.g. `gunicorn -w 1 --threads 8 app:app`, or `waitress-serve --threads=8 app:app` on Windows. Set `REDIS_URL` to run several workers or to deploy on serverless platforms such as Vercel.
- **Rate limiting** is built in and stored in Redis when available. Tighten the limits in `app.py` if you need to.
- **Respect the [Nominatim usage policy](https://operations.osmfoundation.org/policies/nominatim/).** Set a real `GEOCODER_USER_AGENT`. The app already caches results and limits itself to 1 request per second.

## Ethical use
Use the live-location feature only with people who know you and agree to share. The app is built so that nobody can be tracked without seeing who is asking and actively agreeing, and they can stop at any time.
