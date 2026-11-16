/* Mobile Number Location Finder - front-end logic for both pages.
 * index.html            -> initIndex()  (lookup, approximate map, live-location requester)
 * share_location.html   -> initShare()  (recipient consent + GPS sharing)
 */
(function () {
  'use strict';

  // ---------------------------------------------------------------- helpers
  const $ = (sel) => document.querySelector(sel);
  const show = (el, visible = true) => { if (el) el.hidden = !visible; };
  const setText = (el, text) => { if (el) el.textContent = text; };
  const ACTIVE = ['pending', 'sharing'];

  function escapeHtml(str) {
    return String(str).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
  }

  async function api(url, { method = 'GET', body, token } = {}) {
    const headers = { Accept: 'application/json' };
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    if (token) headers.Authorization = 'Bearer ' + token;
    let res;
    try {
      res = await fetch(url, {
        method, headers, cache: 'no-store', credentials: 'same-origin',
        body: body !== undefined ? JSON.stringify(body) : undefined
      });
    } catch (_) {
      const err = new Error('Network error. Please check your connection and try again.');
      err.status = 0;
      throw err;
    }
    let data = null;
    try { data = await res.json(); } catch (_) { /* non-JSON */ }
    if (!res.ok) {
      const err = new Error((data && data.error) || `Request failed (${res.status}).`);
      err.status = res.status;
      err.data = data;
      throw err;
    }
    return data;
  }

  function formatCountdown(ms) {
    if (ms <= 0) return '00:00';
    const t = Math.ceil(ms / 1000);
    const h = Math.floor(t / 3600);
    const m = String(Math.floor((t % 3600) / 60)).padStart(2, '0');
    const s = String(t % 60).padStart(2, '0');
    return h ? `${h}:${m}:${s}` : `${m}:${s}`;
  }

  function timeAgo(ms) {
    const s = Math.max(0, Math.round((Date.now() - ms) / 1000));
    if (s < 5) return 'just now';
    if (s < 60) return `${s} s ago`;
    return `${Math.round(s / 60)} min ago`;
  }

  function addOsmTiles(map) {
    L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 19,
      referrerPolicy: 'strict-origin', // OSM tile policy requires a Referer; origin only, never the token path
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors'
    }).addTo(map);
  }

  function liveIcon() {
    return L.divIcon({ className: 'live-marker', html: '<span class="live-marker-dot"></span>', iconSize: [22, 22], iconAnchor: [11, 11] });
  }

  function setButtonLoading(btn, loading, label) {
    if (!btn) return;
    btn.disabled = loading;
    show(btn.querySelector('.spinner-border'), loading);
    show(btn.querySelector('.btn-icon, .bi'), !loading);
    const lbl = btn.querySelector('.btn-label');
    if (lbl && label) lbl.textContent = label;
  }

  function buildCard({ icon, label, value, hint, badge, mono, live }) {
    const col = document.createElement('div');
    col.className = 'col-12 col-sm-6 col-lg-3';
    const card = document.createElement('div');
    card.className = 'info-card' + (live ? ' live' : '');
    const ic = document.createElement('div');
    ic.className = 'info-icon';
    ic.innerHTML = `<i class="bi ${icon}"></i>`; // icon names are constants
    const body = document.createElement('div');
    body.className = 'info-body';
    const l = document.createElement('div');
    l.className = 'info-label';
    l.textContent = label;
    const v = document.createElement('div');
    v.className = 'info-value' + (mono ? ' mono' : '');
    if (badge) {
      const b = document.createElement('span');
      b.className = `badge rounded-pill text-bg-${badge[1]}`;
      b.textContent = badge[0];
      v.appendChild(b);
    } else {
      v.textContent = value;
    }
    body.append(l, v);
    if (hint) {
      const h = document.createElement('div');
      h.className = 'info-hint';
      h.textContent = hint;
      body.appendChild(h);
    }
    card.append(ic, body);
    col.appendChild(card);
    return col;
  }

  // ======================================================== INDEX PAGE
  function initIndex() {
    const form = $('#lookupForm');
    const select = $('#countrySelect');
    const input = $('#phoneInput');
    const lookupBtn = $('#lookupBtn');
    const errorBox = $('#errorBox');
    const loadingBox = $('#loadingBox');
    const results = $('#results');
    const cards = $('#infoCards');
    const mapNote = $('#mapNote');
    const foliumLink = $('#foliumLink');

    const map = L.map('map', { worldCopyJump: true, scrollWheelZoom: false }).setView([20, 0], 2);
    addOsmTiles(map);
    map.on('click focus', () => map.scrollWheelZoom.enable());
    const approxLayer = L.featureGroup().addTo(map);
    const liveLayer = L.featureGroup().addTo(map);
    let lastLookup = null;

    // ---------- countries
    function guessRegion() {
      try {
        const tz = Intl.DateTimeFormat().resolvedOptions().timeZone || '';
        if (/^Asia\/(Kolkata|Calcutta)$/.test(tz)) return 'IN';
      } catch (_) { /* ignore */ }
      for (const lang of (navigator.languages || [navigator.language])) {
        const m = /[-_]([A-Za-z]{2})$/.exec(lang || '');
        if (m) return m[1].toUpperCase();
      }
      return '';
    }

    (async function loadCountries() {
      try {
        const data = await api('/api/countries');
        const frag = document.createDocumentFragment();
        frag.appendChild(new Option('Auto (number starts with +)', ''));
        data.countries.forEach((c) => frag.appendChild(new Option(`${c.name} (+${c.code})`, c.region)));
        select.replaceChildren(frag);
        const pref = guessRegion();
        if (pref && data.countries.some((c) => c.region === pref)) select.value = pref;
      } catch (e) {
        select.replaceChildren(new Option('Auto (number starts with +)', ''));
        showError('Could not load the country list. You can still type the full number starting with +.');
      }
    })();

    // ---------- errors / loading
    function showError(msg, field) {
      setText(errorBox.querySelector('.msg'), msg);
      show(errorBox);
      if (field) { field.classList.add('is-invalid'); field.focus(); }
    }
    function hideError() {
      show(errorBox, false);
      [input, select].forEach((el) => el.classList.remove('is-invalid'));
    }
    input.addEventListener('input', hideError);
    select.addEventListener('change', hideError);

    function setLoading(on) {
      setButtonLoading(lookupBtn, on, on ? 'Looking up…' : 'Get Location');
      show(loadingBox, on);
      if (on) show(results, false);
    }

    // ---------- lookup
    form.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      hideError();
      const number = input.value.trim();
      const region = select.value;
      if (!number) return showError('Please enter a mobile number.', input);
      if (!/^[+\d\s().\-/]{3,25}$/.test(number)) {
        return showError('Use digits only (spaces, dashes, brackets and a leading + are allowed).', input);
      }
      if (!number.startsWith('+') && !region) {
        return showError('Choose a country code, or start the number with + followed by the country code.', select);
      }
      setLoading(true);
      try {
        const data = await api('/api/lookup', { method: 'POST', body: { number, region } });
        lastLookup = data;
        renderCards(data.number);
        show(results);
        renderApprox(data);
        updateShareButtons();
      } catch (e) {
        showError(e.message);
      } finally {
        setLoading(false);
      }
    });

    function renderCards(n) {
      const items = [
        { icon: 'bi-flag', label: 'Country', value: n.country || 'Not determined' },
        { icon: 'bi-telephone-plus', label: 'Country calling code', value: n.country_calling_code },
        {
          icon: 'bi-pin-map', label: 'Region / area', value: n.region || 'Not available',
          hint: n.region ? 'Where the number range was allocated' : 'No finer detail in public metadata'
        },
        {
          icon: 'bi-broadcast-pin', label: 'Carrier', value: n.carrier || 'Not available',
          hint: n.carrier ? 'Original carrier. The number may have been ported.' : ''
        },
        { icon: 'bi-patch-check', label: 'Number validity', badge: n.is_valid ? ['Valid', 'success'] : ['Invalid', 'danger'], hint: n.is_valid ? n.number_type : '' },
        { icon: 'bi-check2-circle', label: 'Possible number', badge: n.is_possible ? ['Possible', 'success'] : ['Not possible', 'warning'], hint: n.possible_status },
        { icon: 'bi-clock', label: 'Time zone', value: n.time_zones.length ? n.time_zones.join(', ') : 'Not available' },
        { icon: 'bi-globe2', label: 'International format', value: n.international_format, mono: true, hint: `E.164: ${n.e164_format}` }
      ];
      cards.replaceChildren(...items.map((it, i) => {
        const col = buildCard(it);
        col.firstChild.style.animationDelay = `${i * 40}ms`;
        return col;
      }));
    }

    function renderApprox(data) {
      approxLayer.clearLayers();
      const a = data.approximate_area;
      const n = data.number;
      if (!a) {
        setText(mapNote, data.approx_note || 'No reliable geographic information is available, so nothing is plotted.');
        show(foliumLink, false);
        if (!liveLayer.getLayers().length) map.setView([20, 0], 2);
        return;
      }
      const label = a.precision === 'region' ? `${a.label}, ${n.country}` : a.label;
      const tip = `<strong>Approximate ${a.precision}:</strong> ${escapeHtml(label)}<br>` +
        '<small>From phone-number metadata. This is <b>not</b> the phone\'s current location.</small>';

      let bounds = null;
      if (Array.isArray(a.bbox)) {
        const [s, nn, w, e] = a.bbox;
        if (Math.abs(e - w) <= 60 && Math.abs(nn - s) <= 60) { // skip huge / antimeridian boxes
          bounds = L.latLngBounds([s, w], [nn, e]);
          L.rectangle(bounds, { color: '#1a73e8', weight: 1.5, dashArray: '6 6', fillOpacity: 0.06 })
            .bindTooltip('Approximate area (number metadata)', { sticky: true })
            .addTo(approxLayer);
        }
      }
      L.circleMarker([a.lat, a.lon], { radius: 9, color: '#fff', weight: 3, fillColor: '#1a73e8', fillOpacity: 1 })
        .bindPopup(tip).addTo(approxLayer);

      if (!liveLayer.getLayers().length) {
        if (bounds) map.fitBounds(bounds, { padding: [24, 24], maxZoom: 9 });
        else map.setView([a.lat, a.lon], a.precision === 'region' ? 7 : 4);
      }
      setText(mapNote, `Blue marker: approximate ${a.precision} "${label}" from number metadata (geocoded with OpenStreetMap). It is not where the phone is.`);
      foliumLink.href = '/map/approx?number=' + encodeURIComponent(n.e164_format);
      show(foliumLink);
    }

    // ---------- live location (requester side)
    const liveForm = $('#liveForm');
    const liveBtn = $('#liveBtn');
    const nameInput = $('#requesterName');
    const durationSelect = $('#durationSelect');
    const liveError = $('#liveError');
    const liveSession = $('#liveSession');
    const shareLink = $('#shareLink');
    const copyBtn = $('#copyBtn');
    const waBtn = $('#waBtn');
    const smsBtn = $('#smsBtn');
    const httpsWarning = $('#httpsWarning');
    const statusBadge = $('#liveStatusBadge');
    const countdownEl = $('#liveCountdown');
    const cancelBtn = $('#cancelBtn');
    const statusText = $('#liveStatusText');
    const liveDetails = $('#liveDetails');

    const STATUS = {
      pending:   ['Waiting for recipient', 'secondary'],
      sharing:   ['Sharing live', 'success'],
      stopped:   ['Stopped by recipient', 'dark'],
      declined:  ['Declined', 'danger'],
      expired:   ['Expired', 'warning'],
      cancelled: ['Cancelled', 'secondary']
    };
    const END_TEXT = {
      stopped: 'The recipient stopped sharing. Their location data has been deleted.',
      declined: 'The recipient declined the request. No location was shared.',
      expired: 'The link has expired. Any shared location data has been deleted.',
      cancelled: 'You cancelled the request. Any shared location data has been deleted.'
    };
    const live = { sid: null, token: null, poller: null, ticker: null, deadline: 0, fitted: false, minutes: 15, name: '' };

    function setStatus(status) {
      const [text, cls] = STATUS[status] || [status, 'secondary'];
      statusBadge.className = `badge rounded-pill text-bg-${cls}`;
      statusBadge.textContent = text;
    }

    function setFormEnabled(on) {
      [nameInput, durationSelect, liveBtn].forEach((el) => { el.disabled = !on; });
    }

    function updateShareButtons() {
      if (!live.sid) return;
      const url = shareLink.value;
      const msg = `Hi, this is ${live.name}. I'd like to see your live location. You can choose whether to share it ` +
        `using this private link (it expires in ${live.minutes} minutes): ${url}`;
      const n = lastLookup && lastLookup.number;
      if (n && n.is_valid) {
        waBtn.href = `https://wa.me/${n.e164_format.replace('+', '')}?text=${encodeURIComponent(msg)}`;
        smsBtn.href = `sms:${n.e164_format}?body=${encodeURIComponent(msg)}`;
      } else {
        waBtn.href = `https://wa.me/?text=${encodeURIComponent(msg)}`;
        smsBtn.href = `sms:?body=${encodeURIComponent(msg)}`;
      }
    }

    liveForm.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      show(liveError, false);
      nameInput.classList.remove('is-invalid');
      const name = nameInput.value.trim();
      if (name.length < 2) {
        nameInput.classList.add('is-invalid');
        nameInput.focus();
        setText(liveError, 'Please enter your name so the recipient knows who is asking.');
        return show(liveError);
      }
      setButtonLoading(liveBtn, true);
      try {
        const d = await api('/api/share', { method: 'POST', body: { requester_name: name, duration: Number(durationSelect.value) } });
        Object.assign(live, {
          sid: d.session_id, token: d.viewer_token, fitted: false,
          minutes: d.duration_minutes, name: d.requester_name,
          deadline: Date.now() + d.expires_in * 1000
        });
        shareLink.value = d.share_url;
        show(httpsWarning, !d.secure_context);
        updateShareButtons();
        setFormEnabled(false);
        show(cancelBtn);
        show(liveDetails, false);
        show(liveSession);
        setStatus('pending');
        setText(statusText, 'Link created. Waiting for the recipient to open it and give consent…');
        live.poller = setInterval(poll, 4000);
        live.ticker = setInterval(tick, 1000);
        tick();
      } catch (e) {
        setText(liveError, e.message);
        show(liveError);
        setFormEnabled(true);
      } finally {
        setButtonLoading(liveBtn, false);
        if (live.sid) liveBtn.disabled = true;
      }
    });

    function tick() {
      const left = live.deadline - Date.now();
      setText(countdownEl, formatCountdown(left));
      if (left <= 0) poll();
    }

    async function poll() {
      if (!live.sid) return;
      try {
        const d = await api(`/api/share/${encodeURIComponent(live.sid)}`, { token: live.token });
        live.deadline = Date.now() + d.expires_in * 1000;
        setStatus(d.status);
        if (!ACTIVE.includes(d.status)) return endLive(d.status);
        if (d.status === 'sharing' && d.location) drawLive(d.location);
        else setText(statusText, 'Link created. Waiting for the recipient to open it and give consent…');
      } catch (e) {
        if (e.status === 401 || e.status === 404) endLive('expired');
        else setText(statusText, 'Connection problem. Retrying…');
      }
    }

    function drawLive(loc) {
      liveLayer.clearLayers();
      const ll = [loc.latitude, loc.longitude];
      const when = new Date(loc.timestamp).toLocaleTimeString();
      L.circle(ll, { radius: Math.max(loc.accuracy || 0, 5), color: '#dc3545', weight: 1, fillOpacity: 0.1 }).addTo(liveLayer);
      L.marker(ll, { icon: liveIcon(), title: 'Live GPS location', zIndexOffset: 1000 })
        .bindPopup(`<strong>Live GPS location</strong><br>Shared with consent by the recipient<br>±${Math.round(loc.accuracy)} m · ${escapeHtml(when)}`)
        .addTo(liveLayer);
      if (!live.fitted) { map.setView(ll, 16); live.fitted = true; }

      const osm = `https://www.openstreetmap.org/?mlat=${loc.latitude}&mlon=${loc.longitude}#map=17/${loc.latitude}/${loc.longitude}`;
      liveDetails.replaceChildren(
        buildCard({ icon: 'bi-geo-alt-fill', label: 'Latitude', value: loc.latitude.toFixed(6), mono: true, live: true }),
        buildCard({ icon: 'bi-geo-alt-fill', label: 'Longitude', value: loc.longitude.toFixed(6), mono: true, live: true }),
        buildCard({ icon: 'bi-bullseye', label: 'Accuracy', value: `± ${Math.round(loc.accuracy)} m`, live: true }),
        buildCard({ icon: 'bi-clock-history', label: 'Last update', value: when, hint: timeAgo(loc.timestamp), live: true })
      );
      const linkCol = document.createElement('div');
      linkCol.className = 'col-12 small';
      const a = document.createElement('a');
      a.href = osm; a.target = '_blank'; a.rel = 'noopener';
      a.innerHTML = '<i class="bi bi-box-arrow-up-right me-1"></i>Open in OpenStreetMap';
      linkCol.appendChild(a);
      liveDetails.appendChild(linkCol);
      show(liveDetails);
      setText(statusText, `Actual GPS position shared by the recipient with consent (updated ${timeAgo(loc.timestamp)}).`);
    }

    function endLive(status) {
      clearInterval(live.poller);
      clearInterval(live.ticker);
      liveLayer.clearLayers();           // data is deleted on the server, so remove it here too
      liveDetails.replaceChildren();
      show(liveDetails, false);
      setStatus(status);
      setText(statusText, END_TEXT[status] || 'This sharing session has ended.');
      setText(countdownEl, '--:--');
      show(cancelBtn, false);
      Object.assign(live, { sid: null, token: null, fitted: false });
      setFormEnabled(true);
      liveBtn.querySelector('.bi').className = 'bi bi-arrow-repeat me-1';
    }

    cancelBtn.addEventListener('click', async () => {
      if (!live.sid) return;
      cancelBtn.disabled = true;
      try { await api(`/api/share/${encodeURIComponent(live.sid)}`, { method: 'DELETE', token: live.token }); } catch (_) { /* ends anyway */ }
      cancelBtn.disabled = false;
      endLive('cancelled');
    });

    copyBtn.addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(shareLink.value);
      } catch (_) {
        shareLink.select();
        document.execCommand('copy');
      }
      copyBtn.innerHTML = '<i class="bi bi-check2 me-1"></i>Copied';
      setTimeout(() => { copyBtn.innerHTML = '<i class="bi bi-clipboard me-1"></i>Copy'; }, 1800);
    });
  }

  // ======================================================== SHARE PAGE
  function initShare() {
    const body = document.body;
    const token = body.dataset.token;
    const initialState = body.dataset.state;
    const deadline = Date.now() + Number(body.dataset.expiresIn || 0) * 1000;
    const base = `/api/recipient/${encodeURIComponent(token)}`;

    const views = { request: $('#stateRequest'), sharing: $('#stateSharing'), ended: $('#stateEnded') };
    const consent = $('#consentCheck');
    const shareBtn = $('#shareBtn');
    const declineBtn = $('#declineBtn');
    const stopBtn = $('#stopBtn');
    const shareInfo = $('#shareInfo');
    const errBox = $('#shareError');

    let watchId = null, lastPos = null, lastSent = 0, sending = false, ended = false;
    let statusTimer = null, countdownTimer = null, heartbeat = null;
    let miniMap = null, miniMarker = null, miniCircle = null;

    const ENDED = {
      invalid:   ['bi-x-circle text-danger', 'Link not available', 'This location-sharing link is invalid or has expired. Nothing is being shared.'],
      expired:   ['bi-hourglass-bottom text-warning', 'Link expired', 'This request has expired. No location is being shared, and any shared data has been deleted.'],
      stopped:   ['bi-check-circle text-primary', 'Sharing stopped', 'You stopped sharing. Your location has been deleted from the server.'],
      declined:  ['bi-hand-thumbs-down text-secondary', 'Request declined', 'Nothing was shared. You can close this page.'],
      cancelled: ['bi-x-octagon text-secondary', 'Request cancelled', 'The requester cancelled this request. No location is being shared.']
    };

    function view(name) { Object.entries(views).forEach(([k, el]) => show(el, k === name)); }
    function showErr(msg) { setText(errBox, msg); show(errBox); }
    function hideErr() { show(errBox, false); }

    function stopWatch() {
      if (watchId !== null) navigator.geolocation.clearWatch(watchId);
      watchId = null;
      clearInterval(heartbeat);
    }

    function showEnded(kind) {
      ended = true;
      stopWatch();
      clearInterval(statusTimer);
      clearInterval(countdownTimer);
      hideErr();
      const [icon, title, text] = ENDED[kind] || ENDED.invalid;
      $('#endedIcon').className = `bi display-5 ${icon}`;
      setText($('#endedTitle'), title);
      setText($('#endedText'), text);
      view('ended');
    }

    function resetShareButton() {
      setButtonLoading(shareBtn, false, 'Share My Location');
      shareBtn.disabled = !consent.checked;
    }

    if (!ACTIVE.includes(initialState)) return showEnded(initialState);
    view('request');

    function tick() {
      const left = deadline - Date.now();
      document.querySelectorAll('.countdown').forEach((el) => setText(el, formatCountdown(left)));
      if (left <= 0 && !ended) showEnded('expired');
    }
    tick();
    countdownTimer = setInterval(tick, 1000);

    statusTimer = setInterval(async () => {
      try {
        const d = await api(`${base}/status`);
        if (!ACTIVE.includes(d.status)) showEnded(d.status);
      } catch (e) {
        if (e.status === 404) showEnded('invalid');
      }
    }, 10000);

    consent.addEventListener('change', () => { shareBtn.disabled = !consent.checked; });

    declineBtn.addEventListener('click', async () => {
      try { await api(`${base}/stop`, { method: 'POST', body: { reason: 'declined' } }); } catch (_) { /* ignore */ }
      showEnded('declined');
    });

    stopBtn.addEventListener('click', async () => {
      stopWatch();
      stopBtn.disabled = true;
      try { await api(`${base}/stop`, { method: 'POST', body: { reason: 'stopped' } }); } catch (_) { /* ignore */ }
      showEnded('stopped');
    });

    // Closing / leaving the page also stops sharing (and deletes the data server-side).
    window.addEventListener('pagehide', () => {
      if (watchId !== null && !ended && navigator.sendBeacon) {
        navigator.sendBeacon(`${base}/stop`, new Blob([JSON.stringify({ reason: 'stopped' })], { type: 'application/json' }));
      }
    });

    shareBtn.addEventListener('click', () => {
      hideErr();
      if (!consent.checked) return;
      if (!window.isSecureContext) {
        return showErr('Location sharing needs a secure (https://) connection. Ask the sender for an HTTPS link.');
      }
      if (!('geolocation' in navigator)) return showErr('Your browser does not support location sharing.');
      setButtonLoading(shareBtn, true, 'Waiting for permission…');
      watchId = navigator.geolocation.watchPosition(onPosition, onGeoError, {
        enableHighAccuracy: true, maximumAge: 5000, timeout: 30000
      });
      // Re-send the last fix periodically so the requester sees it is still live.
      heartbeat = setInterval(() => { if (lastPos && Date.now() - lastSent > 15000) send(lastPos); }, 5000);
    });

    function onPosition(pos) {
      if (ended) return;
      hideErr();
      if (views.sharing.hidden) {
        view('sharing');
        initMiniMap();
      }
      lastPos = pos;
      updateMiniMap(pos);
      if (Date.now() - lastSent >= 5000) send(pos);
    }

    function onGeoError(err) {
      if (ended) return;
      if (err.code === err.PERMISSION_DENIED) {
        stopWatch();
        view('request');
        resetShareButton();
        showErr('Location permission was denied, so nothing was shared. To share, allow location access for this site in your browser settings and try again.');
      } else if (err.code === err.POSITION_UNAVAILABLE) {
        showErr('Your position is currently unavailable. Make sure location services / GPS are turned on.');
        if (!lastPos) { stopWatch(); resetShareButton(); }
      } else {
        showErr('Getting your GPS position is taking longer than usual. Still trying…');
      }
    }

    async function send(pos) {
      if (sending || ended) return;
      sending = true;
      try {
        await api(`${base}/location`, {
          method: 'POST',
          body: {
            latitude: pos.coords.latitude,
            longitude: pos.coords.longitude,
            accuracy: pos.coords.accuracy,
            consent: true
          }
        });
        lastSent = Date.now();
        setText(shareInfo, `Last sent ${new Date().toLocaleTimeString()} · accuracy ± ${Math.round(pos.coords.accuracy)} m`);
      } catch (e) {
        if (e.status === 410 || e.status === 404) showEnded((e.data && e.data.status) || 'invalid');
        else setText(shareInfo, 'Could not send your location. Retrying…');
      } finally {
        sending = false;
      }
    }

    function initMiniMap() {
      if (miniMap) return;
      miniMap = L.map('shareMap', { zoomControl: true });
      addOsmTiles(miniMap);
      miniMap.setView([20, 0], 2);
      setTimeout(() => miniMap.invalidateSize(), 50);
    }

    function updateMiniMap(pos) {
      if (!miniMap) return;
      const ll = [pos.coords.latitude, pos.coords.longitude];
      const r = Math.max(pos.coords.accuracy || 0, 5);
      if (!miniMarker) {
        miniCircle = L.circle(ll, { radius: r, color: '#dc3545', weight: 1, fillOpacity: 0.1 }).addTo(miniMap);
        miniMarker = L.marker(ll, { icon: liveIcon(), title: 'Your location' }).addTo(miniMap);
        miniMap.setView(ll, 16);
      } else {
        miniMarker.setLatLng(ll);
        miniCircle.setLatLng(ll).setRadius(r);
      }
    }
  }

  // ---------------------------------------------------------------- boot
  function boot() {
    if (typeof L === 'undefined') {
      const msg = 'The map library could not be loaded. Please check your internet connection and reload.';
      const box = document.querySelector('#errorBox .msg') || document.querySelector('#shareError');
      if (box) { box.textContent = msg; (box.closest('[hidden]') || box).hidden = false; }
      return;
    }
    const page = document.body.dataset.page;
    if (page === 'index') initIndex();
    else if (page === 'share') initShare();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
