/* Mobile Number Location Finder - front-end logic (lookup + approximate map). */
(function () {
  'use strict';

  // ---------------------------------------------------------------- helpers
  const $ = (sel) => document.querySelector(sel);
  const show = (el, visible = true) => { if (el) el.hidden = !visible; };
  const setText = (el, text) => { if (el) el.textContent = text; };

  function escapeHtml(str) {
    return String(str).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
  }

  async function api(url, { method = 'GET', body } = {}) {
    const headers = { Accept: 'application/json' };
    if (body !== undefined) headers['Content-Type'] = 'application/json';
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

  function addOsmTiles(map) {
    L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 19,
      referrerPolicy: 'strict-origin', // OSM tile policy requires a Referer; send the origin only
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors'
    }).addTo(map);
  }

  function setButtonLoading(btn, loading, label) {
    if (!btn) return;
    btn.disabled = loading;
    show(btn.querySelector('.spinner-border'), loading);
    show(btn.querySelector('.btn-icon, .bi'), !loading);
    const lbl = btn.querySelector('.btn-label');
    if (lbl && label) lbl.textContent = label;
  }

  function buildCard({ icon, label, value, hint, badge, mono }) {
    const col = document.createElement('div');
    col.className = 'col-12 col-sm-6 col-lg-3';
    const card = document.createElement('div');
    card.className = 'info-card';
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
        renderCards(data.number);
        show(results);
        renderApprox(data);
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
        map.setView([20, 0], 2);
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

      if (bounds) map.fitBounds(bounds, { padding: [24, 24], maxZoom: 9 });
      else map.setView([a.lat, a.lon], a.precision === 'region' ? 7 : 4);
      setText(mapNote, `Blue marker: approximate ${a.precision} "${label}" from number metadata (geocoded with OpenStreetMap). It is not where the phone is.`);
      foliumLink.href = '/map/approx?number=' + encodeURIComponent(n.e164_format);
      show(foliumLink);
    }
  }

  // ---------------------------------------------------------------- boot
  function boot() {
    if (typeof L === 'undefined') {
      const msg = 'The map library could not be loaded. Please check your internet connection and reload.';
      const box = document.querySelector('#errorBox .msg');
      if (box) { box.textContent = msg; (box.closest('[hidden]') || box).hidden = false; }
      return;
    }
    if (document.body.dataset.page === 'index') initIndex();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
