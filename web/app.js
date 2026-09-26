// Forest Care Bonn — web UI. Plain ES module, no build step.
// Every string that comes from the API is escaped before it is put into HTML.

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmtDate = (iso) => (iso ? String(iso).slice(0, 10) : '–');
const pct = (p) => `${Math.round(p * 100)}%`;
const bonnTime = (iso) => new Date(iso).toLocaleString('de-DE', { timeZone: 'Europe/Berlin', dateStyle: 'medium', timeStyle: 'short' });

async function api(path, options) {
  const r = await fetch(path, options);
  const body = await r.json().catch(() => ({}));
  if (!r.ok) {
    const d = body.detail;
    throw new Error(Array.isArray(d) ? d.map((x) => x.msg).join('; ') : d || r.statusText);
  }
  return body;
}

// status -> [colour group, glyph]. Glyphs are the secondary encoding next to colour.
const STATUS = {
  new: ['grow', '+'], expanding: ['grow', '↑'], stable: ['stable', '='], declining: ['shrink', '↓'],
  not_redetected: ['shrink', '∅'], awaiting_confirmation: ['open', '?'], unverified: ['open', '?'],
  not_surveyed: ['gap', '–'], not_target: ['lookalike', '×'], confirmed_present: ['stable', '✓'],
};
const REVIEW_LABEL = { pending: 'Pending review', confirmed: 'Confirmed', rejected: 'Rejected', uncertain: 'Uncertain', field_visit: 'Needs field visit' };

const state = {
  meta: null, summary: null, stands: [], queue: [], missions: [], observations: [], species: null,
  year: '', statusFilter: null, onlyInspect: false, showLookalikes: false, selectedStand: null, lastImport: null,
};

function storage(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(key) || '';
    localStorage.setItem(key, value);
  } catch { /* storage unavailable: the field simply is not remembered */ }
  return value;
}

function standMarker(status, inspect = false, extra = '') {
  const [group, glyph] = STATUS[status];
  return `<span class="stand-marker g-${group} ${inspect ? 'inspect' : ''} ${extra}" aria-hidden="true">${glyph}</span>`;
}
function statusChip(status) {
  return `<span class="status-chip">${standMarker(status)}${esc(state.meta.statuses[status].label)}</span>`;
}
function sourceBadge(kind) {
  if (kind === 'simulated') return '<span class="badge sim">SIMULATED</span>';
  if (kind === 'human') return '<span class="badge human">human</span>';
  if (kind === 'field_photos') return '<span class="badge real">field photo</span>';
  return `<span class="badge">${esc(kind)}</span>`;
}

// ---------------------------------------------------------------- map

let map, layers = {};

function setupMap(reference) {
  map = L.map('map', { zoomControl: true }).setView([50.705, 7.105], 12);
  const osm = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19, attribution: '© OpenStreetMap contributors',
  }).addTo(map);
  const bases = { OpenStreetMap: osm };
  const overlays = {};
  for (const w of state.meta.wms_layers) {
    const layer = L.tileLayer.wms(w.url, {
      layers: w.layers, format: w.format, transparent: w.kind === 'overlay', attribution: w.attribution,
      maxZoom: 21, opacity: w.kind === 'overlay' ? 0.65 : 1,
    });
    if (w.kind === 'base') bases[w.title] = layer;
    else overlays[`${w.title} <span class="badge real">live</span>`] = layer;
  }

  const poly = (className, tip) => ({
    style: { className, weight: 1.5 },
    onEachFeature: (f, l) => l.bindTooltip(tip(f.properties), { sticky: true }),
  });
  layers.districts = L.geoJSON(reference.districts, poly('ref-district', (p) => `${esc(p.district)} · Stadtbezirk ${esc(p.stadtbezirk)}`)).addTo(map);
  map.fitBounds(layers.districts.getBounds(), { padding: [10, 10] });
  layers.wooded = L.geoJSON(reference.wooded, poly('ref-wooded', (p) => `${esc(p.landuse)} (Bonn Realnutzung)`));
  layers.nsg = L.geoJSON(reference.nsg, poly('ref-nsg', (p) => `${esc(p.name)} (${esc(p.id)})`)).addTo(map);
  layers.ffh = L.geoJSON(reference.ffh, poly('ref-ffh', (p) => `FFH ${esc(p.name)} (${esc(p.id)})`)).addTo(map);
  layers.biotopes = L.geoJSON(reference.biotopes, poly('ref-biotope', (p) => `Protected biotope ${esc(p.id)}`));
  layers.gbif = L.geoJSON(reference.gbif, {
    pointToLayer: (f, latlng) => L.marker(latlng, {
      icon: L.divIcon({ className: '', html: '<div class="gbif-marker"></div>', iconSize: [10, 10] }),
    }),
    onEachFeature: (f, l) => {
      const p = f.properties;
      l.bindTooltip(`GBIF: ${esc(p.dataset)} · ${esc(p.year ?? 'no year')} · ±${esc(p.coordinate_uncertainty_m ?? '?')} m`
        + (p.automated_identification ? ' · automatic ID' : ''));
      l.bindPopup(`<b>External record (GBIF)</b><br>${esc(p.dataset)}<br>${esc(p.event_date ?? '')}<br>`
        + `Accuracy: ${esc(p.coordinate_uncertainty_m ?? 'not given')} m<br>Licence: ${esc(p.license)}<br>`
        + `<a href="${esc(p.url)}" target="_blank" rel="noopener">Open on GBIF</a>`);
    },
  }).addTo(map);
  layers.tracks = L.layerGroup().addTo(map);
  layers.observations = L.layerGroup().addTo(map);
  layers.stands = L.layerGroup().addTo(map);
  layers.highlight = L.layerGroup().addTo(map);

  L.control.layers(bases, {
    'Stands (robot + review)': layers.stands,
    'Robot observations': layers.observations,
    'Mission tracks': layers.tracks,
    'GBIF records of P. serotina <span class="badge real">real</span>': layers.gbif,
    'Naturschutzgebiete (LANUK) <span class="badge real">real</span>': layers.nsg,
    'FFH sites (LANUK) <span class="badge real">real</span>': layers.ffh,
    'Protected biotopes (LANUK) <span class="badge real">real</span>': layers.biotopes,
    'Wooded land use (Stadt Bonn) <span class="badge real">real</span>': layers.wooded,
    'Statistical districts (Stadt Bonn) <span class="badge real">real</span>': layers.districts,
    ...overlays,
  }, { collapsed: true }).addTo(map);
  L.control.scale({ imperial: false }).addTo(map);
}

function drawStands() {
  layers.stands.clearLayers();
  for (const s of visibleStands()) {
    const label = state.meta.statuses[s.status].label;
    const small = s.status === 'not_target';
    const m = L.marker([s.lat, s.lon], {
      icon: L.divIcon({
        className: '', html: standMarker(s.status, s.needs_inspection, s.id === state.selectedStand ? 'selected' : ''),
        iconSize: small ? [16, 16] : [24, 24],
      }),
      title: `${s.id}: ${label}`, zIndexOffset: small ? 0 : 1000, keyboard: true,
    });
    m.bindTooltip(`<b>${esc(s.id)}</b> · ${esc(label)}${s.needs_inspection ? ' · needs inspection' : ''}`);
    m.on('click', () => { location.hash = `#stand/${s.id}`; });
    m.addTo(layers.stands);
  }
}

function drawObservations() {
  layers.observations.clearLayers();
  for (const o of state.observations) {
    if (state.year && !o.observed_at.startsWith(state.year)) continue;
    L.circleMarker([o.lat, o.lon], { radius: o.target_probability == null ? 5 : 4, weight: 2,
      className: `obs obs-${o.review_status}${o.target_probability == null ? ' obs-photo' : ''}` })
      .bindTooltip(`${esc(o.uid)}<br>${esc(REVIEW_LABEL[o.review_status])} · ${o.target_probability == null ? 'field photo' : `p(P. serotina) ${pct(o.target_probability)}`}`)
      .on('click', () => { location.hash = `#obs/${o.id}`; })
      .addTo(layers.observations);
  }
}

function drawTracks(highlightId = null) {
  layers.tracks.clearLayers();
  for (const m of state.missions) {
    if (state.year && String(m.year) !== state.year && m.id !== highlightId) continue;
    L.polyline(m.track.map((line) => line.map(([lon, lat]) => [lat, lon])), {
      className: `track${m.protocol === 'opportunistic' ? ' track-photo' : ''}${m.id === highlightId ? ' track-selected' : ''}`,
      weight: m.id === highlightId ? 3 : 1.5,
    }).bindTooltip(`${esc(m.id)} · ${esc(m.area_name)} · ${fmtDate(m.started_at)}`).addTo(layers.tracks);
  }
}

function highlight(lat, lon, zoom = 18) {
  layers.highlight.clearLayers();
  L.circleMarker([lat, lon], { radius: 14, className: 'highlight-ring', weight: 3, fill: false, interactive: false }).addTo(layers.highlight);
  map.setView([lat, lon], Math.max(map.getZoom(), zoom));
}

function drawLegend() {
  const st = state.meta.statuses;
  const row = (status) => `<div class="row">${standMarker(status)} ${esc(st[status].label)}</div>`;
  $('#legend').innerHTML = `<details open><summary class="small"><b>Legend</b></summary>
    <h4>Stand status (confirmed data)</h4>
    ${['new', 'expanding', 'stable', 'confirmed_present', 'declining', 'not_redetected', 'awaiting_confirmation', 'not_surveyed', 'not_target'].map(row).join('')}
    <div class="row"><span class="stand-marker g-stable inspect" aria-hidden="true">=</span> “!” = needs inspection</div>
    <h4>Robot observations</h4>
    <div class="row"><svg width="14" height="14"><circle class="obs obs-pending" cx="7" cy="7" r="4" stroke-width="2"/></svg> pending review</div>
    <div class="row"><svg width="14" height="14"><circle class="obs obs-confirmed" cx="7" cy="7" r="4" stroke-width="2"/></svg> confirmed</div>
    <div class="row"><svg width="14" height="14"><circle class="obs obs-rejected" cx="7" cy="7" r="4" stroke-width="2"/></svg> rejected</div>
    <div class="row"><svg width="14" height="14"><circle class="obs obs-pending obs-photo" cx="7" cy="7" r="5" stroke-width="2"/></svg> field photo (blue outline)</div>
    <div class="row"><div class="gbif-marker" style="margin:0 2px"></div> external GBIF record</div></details>`;
}

// ---------------------------------------------------------------- data

async function loadAll() {
  const [summary, stands, queue, missions, observations] = await Promise.all([
    api('/api/summary'), api('/api/stands'), api('/api/review-queue'), api('/api/missions'), api('/api/observations'),
  ]);
  Object.assign(state, { summary, stands, queue, missions, observations });
  $('#sim-banner').hidden = !summary.simulated;
  $('#count-review').textContent = queue.length;
  const inspect = stands.filter((s) => s.needs_inspection).length;
  $('#count-inspect').textContent = `! ${inspect}`;
  $('#count-inspect').title = `${inspect} stand(s) need inspection`;
  const sel = $('#year-filter');
  if (sel.options.length === 1) {
    for (const y of summary.years) sel.add(new Option(y, y));
  }
  drawStands();
  drawObservations();
  drawTracks();
}

function visibleStands() {
  return state.stands.filter((s) => (state.showLookalikes || s.status !== 'not_target')
    && (!state.statusFilter || s.status === state.statusFilter)
    && (!state.onlyInspect || s.needs_inspection));
}

// ---------------------------------------------------------------- views

const body = () => $('#panel-body');

function viewReviewList() {
  const q = state.queue;
  const standById = Object.fromEntries(state.stands.map((s) => [s.id, s]));
  body().innerHTML = `
    <h2>Review queue</h2>
    <p class="muted small">${q.length} robot detection(s) are waiting for an expert decision. The model output is a
      suggestion, not a finding. The order puts first what is new, sensitive or ambiguous, and the reasons are listed on each item.</p>
    ${q.length ? '' : '<div class="callout">Nothing to review. New robot missions will appear here.</div>'}
    ${q.map((o) => {
      const s = standById[o.stand_id];
      return `<div class="card clickable" data-href="#obs/${o.id}" tabindex="0">
        <div class="row-flex">
          ${o.image_url ? `<img class="thumb" src="${esc(o.image_url)}" alt="Camera frame ${esc(o.uid)}" loading="lazy">` : '<div class="thumb"></div>'}
          <div class="grow1">
            <div class="row-flex"><b class="mono grow1">${esc(o.uid)}</b>${sourceBadge(o.source_kind)}</div>
            <div class="small muted">${fmtDate(o.observed_at)} · stand ${esc(o.stand_id)}${s ? ` (${esc(state.meta.statuses[s.status].label)})` : ''}</div>
            ${o.target_probability == null
              ? `<div class="small">Field photo · ${esc(o.original_filename)}</div><div class="small muted">no model prediction</div>`
              : `<div class="small">p(<i>P. serotina</i>) ${pct(o.target_probability)}</div>
            <div class="pbar"><span style="width:${pct(o.target_probability)}"></span></div>`}
          </div>
        </div>
        <ul class="reasons">${o.priority_reasons.map((r) => `<li>${esc(r)}</li>`).join('')}</ul>
      </div>`;
    }).join('')}`;
}

const HEIGHTS = ['seedling', 'shrub', 'small_tree', 'tree'];
const PHENOLOGY = ['vegetative', 'flowering', 'fruiting', 'autumn_colour'];
const fmtBytes = (n) => (n > 1e6 ? `${(n / 1e6).toFixed(1)} MB` : `${Math.round(n / 1e3)} kB`);
const compass = (deg) => ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'][Math.round(deg / 45) % 8];
const labelsText = (r) => [r.plant_count && `${r.plant_count} plant(s)`, r.height_class, r.phenology].filter(Boolean).join(', ');

function modelSection(o) {
  return `<h3>Model suggestion</h3>
    <dl class="kv">
      <dt>p(<i>P. serotina</i>)</dt><dd>${pct(o.target_probability)} <div class="pbar"><span style="width:${pct(o.target_probability)}"></span></div></dd>
      <dt>Top class</dt><dd><i>${esc(o.predicted_taxon)}</i> (${pct(o.confidence)})</dd>
      <dt>Alternatives</dt><dd>${o.alternatives.map((a) => `<i>${esc(a.taxon)}</i> ${pct(a.probability)}`).join(', ') || '–'}</dd>
      <dt>Reported</dt><dd>${esc(o.plant_count_est ?? '?')} plant(s) · ${esc(o.height_class ?? '–')} · ${esc(o.phenology ?? '–')}</dd>
      <dt>Model</dt><dd class="mono">${esc(o.provenance.model.name)} ${esc(o.provenance.model.version)}</dd>
    </dl>`;
}

function exifTable(exif) {
  const rows = Object.entries(exif).flatMap(([group, tags]) => Object.entries(tags || {}).map(([k, v]) =>
    `<tr><td class="muted">${esc(group)}</td><td>${esc(k)}</td><td class="mono">${esc(typeof v === 'object' ? JSON.stringify(v) : v)}</td></tr>`));
  return `<table class="small"><tbody>${rows.join('')}</tbody></table>`;
}

function photoSection(o) {
  const { derived: d, file: f, exif } = o.metadata;
  const nTags = Object.values(exif).reduce((n, g) => n + Object.keys(g || {}).length, 0);
  return `<h3>Field photo</h3>
    <p class="small">Taken by a person, so there is no model prediction. Your decision is the identification.</p>
    <dl class="kv">
      <dt>Original file</dt><dd>${esc(f.original_filename)} · ${fmtBytes(f.bytes)} · <a href="${esc(o.original_url)}" id="original-link">download original</a></dd>
      <dt>SHA-256</dt><dd class="mono">${esc(f.sha256.slice(0, 16))}… <span class="small muted">re-checked on every download</span></dd>
      <dt>Camera</dt><dd>${esc(d.camera)} · ${d.size_px.join(' × ')} px</dd>
      <dt>Captured</dt><dd>${esc(bonnTime(d.observed_at))}<div class="small muted">${esc(d.time_source)}</div></dd>
      <dt>Position accuracy</dt><dd>${d.accuracy_m != null ? `±${esc(d.accuracy_m)} m<div class="small muted">${esc(d.accuracy_source)}</div>`
        : `unknown<div class="small muted">not in EXIF; ${esc(d.accuracy_assumed_for_linking_m)} m assumed for stand grouping</div>`}</dd>
      <dt>Camera heading</dt><dd>${d.heading_deg != null ? `${Math.round(d.heading_deg)}° (${compass(d.heading_deg)})` : '–'}</dd>
      <dt>Altitude</dt><dd>${d.altitude_m != null ? `${Math.round(d.altitude_m)} m` : '–'}</dd>
      <dt>Photographer</dt><dd>${esc(o.provenance.photographer)}</dd>
    </dl>
    <details><summary class="small">All EXIF metadata (${nTags} tags, as stored in the original)</summary>${exifTable(exif)}</details>`;
}

function labelFields(o, latest) {
  const base = latest?.decision === 'confirmed' ? latest : {};
  const count = base.plant_count ?? (o.original_url ? '' : o.plant_count_est ?? '');
  const height = base.height_class ?? o.height_class ?? '';
  const phen = base.phenology ?? o.phenology ?? '';
  const opts = (list, sel) => `<option value="">–</option>${list.map((v) => `<option value="${v}" ${v === sel ? 'selected' : ''}>${v.replace('_', ' ')}</option>`).join('')}`;
  return `<fieldset class="labels"><legend class="small">If confirmed: labels (optional)</legend>
      <label class="small">Plants <input type="number" id="label-count" min="1" max="10000" value="${esc(count)}" placeholder="?"></label>
      <label class="small">Height <select id="label-height">${opts(HEIGHTS, height)}</select></label>
      <label class="small">Phenology <select id="label-phen">${opts(PHENOLOGY, phen)}</select></label>
      <p class="small muted">${o.original_url
        ? 'A photo carries no plant count. Without a count the stand counts as present, but it is not used for trends.'
        : 'Pre-filled with the robot’s estimate. Only values you change are stored as your label.'}</p>
    </fieldset>`;
}

async function viewObservation(id) {
  const [o, species] = await Promise.all([api(`/api/observations/${id}`), speciesProfile()]);
  const stand = state.stands.find((s) => s.id === o.stand_id);
  const ctx = o.context;
  const isPhoto = Boolean(o.original_url);
  highlight(o.lat, o.lon);
  const latest = o.reviews.at(-1);
  const inQueue = state.queue.findIndex((q) => q.id === o.id);
  const areas = [...ctx.nsg.map((a) => ({ ...a, kind: 'NSG' })), ...ctx.ffh.map((a) => ({ ...a, kind: 'FFH' }))];
  body().innerHTML = `
    <button class="back" data-href="#review">← Review queue</button>
    <div class="row-flex"><h2 class="grow1 mono">${esc(o.uid)}</h2>${sourceBadge(o.source_kind)}</div>
    <p class="small muted">${esc(bonnTime(o.observed_at))} (Bonn time) · ${isPhoto ? 'photo mission' : 'mission'} ${esc(o.mission_id)} · ${esc(REVIEW_LABEL[o.review_status])}</p>
    ${o.image_url ? `<a href="${esc(o.image_url)}" target="_blank" rel="noopener" title="Open full size"><img class="big-image" src="${esc(o.image_url)}" alt="${isPhoto ? 'Field photo' : 'Camera frame'} ${esc(o.uid)}"></a>` : '<p class="muted">No image was sent.</p>'}
    ${isPhoto ? photoSection(o) : modelSection(o)}
    ${o.qc_flags.length ? `<div>${o.qc_flags.map((f) => `<span class="flag">${esc(f.replaceAll('_', ' '))}</span>`).join('')}</div>` : ''}

    <div class="decision">
      <h3>Your decision</h3>
      ${latest ? `<p class="small">Latest decision: <b>${esc(state.meta.review_decisions[latest.decision])}</b>${labelsText(latest) ? ` (${esc(labelsText(latest))})` : ''}
        by ${esc(latest.reviewer)} ${sourceBadge(latest.source_kind)} on ${fmtDate(latest.reviewed_at)}. A new decision is added to the history and does not overwrite it.</p>` : ''}
      <div class="decision-buttons">
        <button class="confirm" data-decision="confirmed">Confirm <i>P. serotina</i><kbd>C</kbd></button>
        <button class="reject" data-decision="rejected">Reject<kbd>R</kbd></button>
        <button data-decision="uncertain">Uncertain<kbd>U</kbd></button>
        <button data-decision="field_visit">Needs field visit<kbd>F</kbd></button>
      </div>
      ${labelFields(o, latest)}
      <label class="small">If rejected, species seen
        <select id="corrected"><option value="">not specified</option>
          ${state.meta.correction_taxa.map((t) => `<option>${esc(t)}</option>`).join('')}</select></label>
      <textarea id="review-note" placeholder="Note (optional): what did you base the decision on?"></textarea>
      <p class="error" id="review-error" role="alert"></p>
      ${inQueue >= 0 ? `<p class="small muted">Item ${inQueue + 1} of ${state.queue.length}. After deciding, the next item opens.</p>` : ''}
    </div>

    <h3>Where</h3>
    <dl class="kv">
      <dt>District</dt><dd>${esc(ctx.district?.district)} · Stadtbezirk ${esc(ctx.district?.stadtbezirk)}${ctx.district?.snapped_m ? ` (snapped ${ctx.district.snapped_m} m)` : ''}</dd>
      <dt>Land use</dt><dd>${ctx.landuse ? `${esc(ctx.landuse.landuse)} (${esc(ctx.landuse.environment.replace('_', ' '))})` : 'not mapped as wooded'}</dd>
      <dt>Protected areas</dt><dd>${areas.length ? areas.map((a) => `${esc(a.kind)} <a href="${esc(a.factsheet_url)}" target="_blank" rel="noopener">${esc(a.name)}</a> ${a.inside ? '(inside)' : `(${Math.round(a.distance_m)} m)`}`).join('<br>') : `none within ${state.meta.thresholds.protected_area_buffer_m} m`}
        ${ctx.protected_biotopes.length ? `<br>Protected biotope ${esc(ctx.protected_biotopes[0].id)} (${ctx.protected_biotopes[0].inside ? 'inside' : `${Math.round(ctx.protected_biotopes[0].distance_m)} m`})` : ''}</dd>
      <dt>Position</dt><dd class="mono">${o.lat.toFixed(6)}, ${o.lon.toFixed(6)} ${o.gnss_accuracy_m != null ? `±${esc(o.gnss_accuracy_m)} m` : '(accuracy unknown)'}</dd>
    </dl>

    <h3>External records nearby</h3>
    ${externalRecords(ctx)}

    <h3>This location over time</h3>
    ${stand ? `<p><a href="#stand/${esc(stand.id)}">Stand ${esc(stand.id)}</a>: ${statusChip(stand.status)}</p>` : ''}
    ${o.stand_history.length ? `<div class="gallery">${o.stand_history.map((h) => `
      <figure data-href="#obs/${h.id}" tabindex="0">
        ${h.image_url ? `<img class="thumb" src="${esc(h.image_url)}" alt="Earlier image" loading="lazy">` : '<div class="thumb"></div>'}
        <figcaption>${fmtDate(h.observed_at)} · ${esc(REVIEW_LABEL[h.review_status])}${h.corrected_taxon ? ` as <i>${esc(h.corrected_taxon)}</i>` : ''}</figcaption>
      </figure>`).join('')}</div>` : '<p class="muted small">First observation at this location.</p>'}

    <h3>Identification hints (LANUK)</h3>
    <ul class="reasons">${species.identification.map((i) => `<li>${esc(i.text)}</li>`).join('')}</ul>
    <p class="small"><b>Look-alikes:</b> ${species.lookalikes.map((l) => `<i>${esc(l.taxon)}</i>: ${esc(l.hint)}`).join(' ')}</p>
    <p class="small muted">Summary of <a href="${esc(species.sources.lanuk_neobiota_kurz.url)}" target="_blank" rel="noopener">LANUK Neobiota NRW</a>, retrieved ${esc(species.sources.lanuk_neobiota_kurz.retrieved)}.</p>

    <h3>Provenance</h3>
    <dl class="kv small">
      <dt>Source</dt><dd>${sourceBadge(o.source_kind)} ${isPhoto
        ? `${esc(o.provenance.robot_id)}, photographer ${esc(o.provenance.photographer)}, imported with ${esc(o.provenance.importer.name)} ${esc(o.provenance.importer.version)}`
        : `robot ${esc(o.provenance.robot_id)}${o.provenance.simulator ? `, simulator ${esc(o.provenance.simulator.name)} ${esc(o.provenance.simulator.version)} seed ${esc(o.provenance.simulator.seed)}` : ''}`}</dd>
      <dt>Ingested</dt><dd>${esc(o.provenance.ingested_at)}</dd>
      ${o.provenance.mission_payload_sha256 ? `<dt>Payload SHA-256</dt><dd class="mono">${esc(o.provenance.mission_payload_sha256.slice(0, 16))}…</dd>` : ''}
      ${isPhoto ? `<dt>Original SHA-256</dt><dd class="mono">${esc(o.original_sha256.slice(0, 16))}…</dd>` : ''}
      <dt>${isPhoto ? 'Preview' : 'Image'} SHA-256</dt><dd class="mono">${esc((o.provenance.image_sha256 || '–').slice(0, 16))}…</dd>
      <dt>Context data</dt><dd>captured at ingest from the reference snapshots (retrieved ${esc(fmtDate(Object.values(ctx.reference_versions)[0]))})</dd>
    </dl>
    ${o.reviews.length ? `<table><thead><tr><th>Decision</th><th>Labels</th><th>By</th><th>When</th><th>Note</th></tr></thead><tbody>
      ${o.reviews.map((r) => `<tr><td>${esc(state.meta.review_decisions[r.decision])}${r.corrected_taxon ? ` (<i>${esc(r.corrected_taxon)}</i>)` : ''}</td>
      <td>${esc(labelsText(r))}</td><td>${esc(r.reviewer)} ${sourceBadge(r.source_kind)}</td><td>${fmtDate(r.reviewed_at)}</td><td>${esc(r.note ?? '')}</td></tr>`).join('')}
      </tbody></table>` : ''}`;

  $$('.decision-buttons button').forEach((b) => b.addEventListener('click', () => submitReview(o, b.dataset.decision)));
}

function externalRecords(ctx) {
  const g = ctx.gbif;
  const row = (r) => `<li><a href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.dataset)}</a>, ${esc(r.year ?? 'no year')},
    ${r.distance_m} m away, accuracy ${esc(r.coordinate_uncertainty_m ?? 'not given')} m${r.automated_identification ? ', <b>automatic ID only</b>' : ''}</li>`;
  return `<ul class="reasons">
      ${g.records.map(row).join('') || `<li>No GBIF record of <i>P. serotina</i> within ${g.within_m} m.</li>`}
      ${g.grid_records.map((r) => `<li>Grid-level record (±${Math.round(r.coordinate_uncertainty_m / 1000)} km) covers this spot: ${esc(r.dataset)}</li>`).join('')}
      <li>LANUK Neobiota portal: ${ctx.lanuk_neobiota_points_in_bonn} validated <i>P. serotina</i> find point(s) in all of Bonn.</li>
    </ul>`;
}

async function submitReview(o, decision) {
  const reviewer = $('#reviewer').value.trim();
  const err = $('#review-error');
  if (reviewer.length < 2) {
    err.textContent = 'Enter your name in the “Reviewer” field at the top first. Every decision is recorded with the person who made it.';
    $('#reviewer').focus();
    return;
  }
  const corrected = decision === 'rejected' ? $('#corrected').value || null : null;
  const labels = {};
  if (decision === 'confirmed') {
    // Store only what the expert set or changed; the device's own estimate stays on the observation.
    const count = parseInt($('#label-count').value, 10);
    if (count >= 1 && count !== o.plant_count_est) labels.plant_count = count;
    const height = $('#label-height').value;
    if (height && height !== o.height_class) labels.height_class = height;
    const phen = $('#label-phen').value;
    if (phen && phen !== o.phenology) labels.phenology = phen;
  }
  try {
    await api(`/api/observations/${o.id}/reviews`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ decision, reviewer, corrected_taxon: corrected, note: $('#review-note').value.trim() || null, ...labels }),
    });
  } catch (e) {
    err.textContent = `Could not save: ${e.message}`;
    return;
  }
  const idx = state.queue.findIndex((q) => q.id === o.id);
  await loadAll();
  const next = state.queue[Math.min(Math.max(idx, 0), state.queue.length - 1)];
  location.hash = next ? `#obs/${next.id}` : '#review';
}

function viewStands() {
  const counts = {};
  for (const s of state.stands) counts[s.status] = (counts[s.status] || 0) + 1;
  const shown = visibleStands();
  const order = Object.keys(STATUS);
  body().innerHTML = `
    <h2>Stands</h2>
    <p class="muted small">A stand groups robot detections at one place. Its status compares expert-confirmed plant counts
      between survey years, and uses the mission tracks to tell “not found” apart from “not surveyed”.
      “!” means a person should look again. It never means “remove”.</p>
    <div class="filters" role="group" aria-label="Filter by status">
      ${order.filter((k) => counts[k]).map((k) => `<button data-status="${k}" aria-pressed="${state.statusFilter === k}">
        ${standMarker(k)} ${esc(state.meta.statuses[k].label)} <span class="count">${counts[k]}</span></button>`).join('')}
    </div>
    <div class="toolbar small">
      <label><input type="checkbox" id="only-inspect" ${state.onlyInspect ? 'checked' : ''}> Needs inspection only</label>
      <label><input type="checkbox" id="show-lookalikes" ${state.showLookalikes ? 'checked' : ''}> Show look-alike locations</label>
    </div>
    <table>
      <thead><tr><th>Stand</th><th>Status</th><th>Where</th><th class="num">Plants</th><th>Last confirmed</th></tr></thead>
      <tbody>${shown.map((s) => `<tr class="clickable" data-href="#stand/${esc(s.id)}" tabindex="0">
        <td class="mono nowrap">${esc(s.id)}${s.needs_inspection ? ' <b title="needs inspection">!</b>' : ''}</td>
        <td>${statusChip(s.status)}</td>
        <td>${esc(s.area_name ?? '')}<div class="small muted">${esc(s.district ?? '')}</div></td>
        <td class="num">${esc(s.latest_plants_confirmed ?? '–')}</td>
        <td>${fmtDate(s.last_confirmed)}</td></tr>`).join('')}</tbody>
    </table>
    <p class="small muted">Plants = confirmed plants in the latest confirmed year (robot count estimate, a lower bound).</p>
    <h3>Export</h3>
    <p class="small"><a href="/api/export/stands.geojson">Stands as GeoJSON</a> (confirmed only) ·
      <a href="/api/export/lanuk-draft.csv">Draft for the LANUK Neobiota report form (CSV)</a></p>`;
  $$('.filters button').forEach((b) => b.addEventListener('click', () => {
    state.statusFilter = state.statusFilter === b.dataset.status ? null : b.dataset.status;
    if (b.dataset.status === 'not_target') state.showLookalikes = true;
    drawStands(); viewStands();
  }));
  $('#only-inspect').addEventListener('change', (e) => { state.onlyInspect = e.target.checked; drawStands(); viewStands(); });
  $('#show-lookalikes').addEventListener('change', (e) => { state.showLookalikes = e.target.checked; drawStands(); viewStands(); });
}

function yearChart(years) {
  const w = 400, h = 120, pad = 22, bw = 56;
  const max = Math.max(1, ...years.map((y) => y.plants_confirmed ?? 0));
  const step = (w - 2 * pad) / years.length;
  const bars = years.map((y, i) => {
    const x = pad + i * step + (step - bw) / 2;
    const base = h - pad;
    if (!y.surveyed) {
      return `<rect class="gap" x="${x}" y="${pad}" width="${bw}" height="${base - pad}" rx="4"><title>${y.year}: not surveyed</title></rect>
        <text x="${x + bw / 2}" y="${base - 30}" text-anchor="middle">not</text><text x="${x + bw / 2}" y="${base - 16}" text-anchor="middle">surveyed</text>
        <text x="${x + bw / 2}" y="${h - 6}" text-anchor="middle">${y.year}</text>`;
    }
    if (y.presence_only) {
      return `<rect class="gap" x="${x}" y="${base - 24}" width="${bw}" height="24" rx="4"><title>${y.year}: confirmed present, not counted</title></rect>
        <text x="${x + bw / 2}" y="${base - 30}" text-anchor="middle">present</text>
        <text x="${x + bw / 2}" y="${h - 6}" text-anchor="middle">${y.year}</text>`;
    }
    const v = y.plants_confirmed ?? 0;
    const bh = (v / max) * (base - pad - 14);
    return `<path class="bar" d="M${x},${base} v${-Math.max(bh - 4, 0)} q0,-4 4,-4 h${bw - 8} q4,0 4,4 v${Math.max(bh - 4, 0)} z">
        <title>${y.year}: ${v} confirmed plant(s), ${y.detections} detection(s)</title></path>
      <text x="${x + bw / 2}" y="${base - bh - 4}" text-anchor="middle">${v}${y.pending ? ` (+${y.pending} pending)` : ''}</text>
      <text x="${x + bw / 2}" y="${h - 6}" text-anchor="middle">${y.year}</text>`;
  }).join('');
  return `<svg class="chart" viewBox="0 0 ${w} ${h}" role="img" aria-label="Confirmed plants per survey year">
    <line class="axis" x1="${pad}" x2="${w - pad}" y1="${h - pad}" y2="${h - pad}"/>${bars}</svg>`;
}

async function viewStand(id) {
  state.selectedStand = id;
  drawStands();
  const s = await api(`/api/stands/${encodeURIComponent(id)}`);
  highlight(s.lat, s.lon, 17);
  const ctx = s.context;
  const st = state.meta.statuses[s.status];
  const crit = { near_protected: 'In or next to areas with rare / endangered species or biotopes', early_invasion: 'Early invasion with few trees',
    low_infestation: 'Area with low infestation', fruiting_solitary: 'Fruiting solitary specimen' };
  const mark = (v) => (v === true ? 'yes' : v === false ? 'no' : 'unknown');
  body().innerHTML = `
    <button class="back" data-href="#stands">← All stands</button>
    <div class="row-flex"><h2 class="grow1">Stand ${esc(s.id)}</h2>${s.source_kinds.map(sourceBadge).join(' ')}</div>
    <p>${statusChip(s.status)}</p>
    <p class="small muted">${esc(st.description)}</p>

    <h3>Why look again</h3>
    ${s.inspection_reasons.length ? `<ul class="reasons">${s.inspection_reasons.map((r) => `<li>${esc(r.text)}</li>`).join('')}</ul>`
      : '<p class="small muted">No inspection reasons at the moment.</p>'}

    <h3>Survey history</h3>
    ${yearChart(s.years)}
    <table>
      <thead><tr><th>Year</th><th>Surveyed</th><th class="num">Detections</th><th class="num">Confirmed</th><th class="num">Rejected</th><th class="num">Open</th><th class="num">Plants</th></tr></thead>
      <tbody>${s.years.map((y) => `<tr><td>${y.year}</td><td>${y.surveyed ? `yes (${y.missions.length})` : '<b>no</b>'}</td>
        <td class="num">${y.detections}</td><td class="num">${y.confirmed}</td><td class="num">${y.rejected}</td>
        <td class="num">${y.pending + y.uncertain}</td><td class="num">${y.presence_only ? 'present' : y.plants_confirmed ?? '–'}</td></tr>`).join('')}</tbody>
    </table>
    ${Object.keys(s.rejected_as).length ? `<p class="small">Rejected detections here were identified as: ${Object.entries(s.rejected_as).map(([k, v]) => `<i>${esc(k)}</i> ×${v}`).join(', ')}.</p>` : ''}

    <h3>Place</h3>
    <dl class="kv">
      <dt>Area</dt><dd>${esc(s.area_name ?? '–')}</dd>
      <dt>District</dt><dd>${esc(s.district)} · Stadtbezirk ${esc(s.stadtbezirk)}</dd>
      <dt>Land use</dt><dd>${esc(s.landuse ?? 'not mapped as wooded')}</dd>
      <dt>Protected</dt><dd>${s.protected.length ? s.protected.map(esc).join('<br>') : 'nothing within the buffer'}</dd>
      <dt>Centre</dt><dd class="mono">${s.lat.toFixed(6)}, ${s.lon.toFixed(6)} (spread ${s.radius_m} m)</dd>
    </dl>
    ${externalRecords(ctx)}

    <div class="callout">
      <b>Context for LANUK's published priorities.</b> LANUK names situations where control measures should come first.
      This table only shows which of them the data supports for this stand. It is <b>not a recommendation</b>; decisions
      are made by the responsible experts and landowners.
      <table class="small"><tbody>${s.lanuk_criteria.map((c) => `<tr><td>${esc(crit[c.code])}</td><td><b>${mark(c.applies)}</b></td><td class="muted">${esc(c.evidence)}</td></tr>`).join('')}</tbody></table>
    </div>

    <h3>Stand log (entered by people)</h3>
    ${s.notes.length ? s.notes.map((n) => `<div class="card note"><div class="row-flex"><b class="grow1">${esc(state.meta.note_kinds[n.kind])}${n.action_date ? ` · ${esc(n.action_date)}` : ''}</b>${sourceBadge(n.source_kind)}</div>
      <p>${esc(n.text)}</p><div class="small muted">${esc(n.recorded_by)} · ${fmtDate(n.recorded_at)}</div></div>`).join('') : '<p class="small muted">No entries yet.</p>'}
    <details><summary class="small">Add an entry</summary>
      <div class="card">
        <label class="small">Kind <select id="note-kind">${Object.entries(state.meta.note_kinds).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join('')}</select></label>
        <label class="small">Date of action <input type="date" id="note-date"></label>
        <textarea id="note-text" placeholder="What was decided or done, by whom, and why"></textarea>
        <p class="small muted">Recorded as the reviewer named at the top. The system itself never writes here.</p>
        <button id="note-save">Save entry</button> <span class="error" id="note-error" role="alert"></span>
      </div>
    </details>

    <h3>Observations (${s.observations.length})</h3>
    <div class="gallery">${s.observations.slice().reverse().map((o) => `
      <figure data-href="#obs/${o.id}" tabindex="0">
        ${o.image_url ? `<img class="thumb" src="${esc(o.image_url)}" alt="Frame ${esc(o.uid)}" loading="lazy">` : '<div class="thumb"></div>'}
        <figcaption>${fmtDate(o.observed_at)} · ${esc(REVIEW_LABEL[o.review_status])}</figcaption></figure>`).join('')}</div>`;

  $('#note-save').addEventListener('click', async () => {
    const recorded_by = $('#reviewer').value.trim();
    const err = $('#note-error');
    if (recorded_by.length < 2) { err.textContent = 'Enter your name in the “Reviewer” field first.'; return; }
    try {
      await api(`/api/stands/${encodeURIComponent(id)}/notes`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ kind: $('#note-kind').value, text: $('#note-text').value.trim(), recorded_by, action_date: $('#note-date').value || null }),
      });
    } catch (e) { err.textContent = e.message; return; }
    await loadAll();
    viewStand(id);
  });
}

function importReport(r) {
  if (!r) return '';
  const list = (items, fmt) => items.map((i) => `<li>${fmt(i)}</li>`).join('');
  return `<div class="callout import-report">
    <b>${r.accepted.length} photo(s) imported</b>${r.mission_id ? ` into <a href="#mission/${esc(r.mission_id)}">${esc(r.mission_id)}</a>
      ${sourceBadge(r.source_kind)} · <a href="#review">review them</a>` : ''}.
    ${r.duplicates.length ? `<div>${r.duplicates.length} already imported earlier (same file content), skipped.</div>` : ''}
    ${r.rejected.length ? `<div>Not imported:</div><ul class="reasons">${list(r.rejected, (i) => `${esc(i.file)}: ${esc(i.reason)}`)}</ul>` : ''}
    ${r.skipped.length ? `<div>Ignored (not photos):</div><ul class="reasons">${list(r.skipped, (i) => `${esc(i.file)}: ${esc(i.reason)}`)}</ul>` : ''}
    ${r.accepted.some((a) => a.flags.includes('accuracy_unknown') || a.flags.includes('timezone_assumed'))
      ? '<div class="small">Some photos lack a position accuracy or a time zone in their EXIF; this is flagged on each of them.</div>' : ''}
  </div>`;
}

async function importPhotos() {
  const status = $('#photo-status');
  const files = [...$('#photo-folder').files, ...$('#photo-files').files];
  const photographer = $('#photo-by').value.trim();
  if (!files.length) { status.textContent = 'Choose a folder or some photos first.'; return; }
  if (photographer.length < 2) { status.textContent = 'Enter who took the photos.'; return; }
  const fd = new FormData();
  for (const f of files) fd.append('files', f, f.webkitRelativePath || f.name);
  fd.append('photographer', photographer);
  fd.append('area_name', $('#photo-area').value.trim());
  fd.append('notes', $('#photo-notes').value.trim());
  fd.append('simulated', $('#photo-sim').checked ? 'true' : 'false');
  fd.append('source_label', (files[0].webkitRelativePath || '').split('/')[0]);
  status.textContent = `Uploading ${files.length} file(s)…`;
  $('#photo-import').disabled = true;
  try {
    state.lastImport = await api('/api/photo-missions', { method: 'POST', body: fd });
  } catch (e) {
    status.textContent = `Import failed: ${e.message}`;
    $('#photo-import').disabled = false;
    return;
  }
  await loadAll();
  if (state.lastImport.mission_id && location.hash !== `#mission/${state.lastImport.mission_id}`) location.hash = `#mission/${state.lastImport.mission_id}`;
  else viewMissions(state.lastImport.mission_id);
}

function viewMissions(selected = null) {
  const byYear = {};
  for (const m of state.missions) (byYear[m.year] ||= []).push(m);
  const cal = state.summary.review_outcome_by_model_probability;
  drawTracks(selected);
  const sel = state.missions.find((m) => m.id === selected);
  if (sel) {
    const pts = sel.track.flat().map(([lon, lat]) => [lat, lon]);
    if (pts.length) map.fitBounds(pts, { padding: [30, 30], maxZoom: 17 });
  }
  body().innerHTML = `
    <h2>Missions</h2>
    <p class="muted small">A robot survey stores the track it actually drove, so the system knows where plants could
      have been seen at all. A photo mission is a set of field photos. It records where someone found something, never
      where nothing was found (presence only).</p>

    <details class="card" ${state.lastImport ? 'open' : ''}>
      <summary><b>Import field photos</b> <span class="small muted">a folder of geotagged photos becomes one mission</span></summary>
      <div class="import-form">
        <label>Folder <input type="file" id="photo-folder" webkitdirectory multiple></label>
        <label>…or single photos <input type="file" id="photo-files" multiple accept="image/jpeg,image/png,image/tiff"></label>
        <label>Photographer <input id="photo-by" value="${esc($('#reviewer').value.trim())}" placeholder="Who took the photos"></label>
        <label>Area or occasion <input id="photo-area" placeholder="e.g. Kottenforst walk, 20 Sept"></label>
        <label>Notes <textarea id="photo-notes" placeholder="Optional"></textarea></label>
        <label class="small" style="display:flex;gap:6px;align-items:center"><input type="checkbox" id="photo-sim"> These are test pictures (store as simulated)</label>
        <div><button id="photo-import">Import</button> <span id="photo-status" class="small" role="status"></span></div>
        <p class="small muted">Each photo needs a GPS position in its EXIF. The original file is stored unchanged with its
          SHA-256; the review UI shows a smaller preview. Photos outside Bonn or without a position are listed and not imported.
          Large folders: <span class="mono">python -m forestcare import-photos FOLDER</span>.</p>
      </div>
      ${importReport(state.lastImport)}
    </details>

    ${Object.entries(byYear).reverse().map(([y, ms]) => `<h3>${y}</h3><table><tbody>
      ${ms.map((m) => `<tr class="clickable" data-href="#mission/${esc(m.id)}" tabindex="0" ${m.id === selected ? 'style="font-weight:600"' : ''}>
        <td>${fmtDate(m.started_at)}</td><td>${esc(m.area_name ?? '')}<div class="small muted mono">${esc(m.id)}</div>
        ${m.protocol === 'opportunistic' ? `<div class="small">Field photos by ${esc(m.provenance.photographer)} · presence only</div>` : ''}
        ${m.notes ? `<div class="small">${esc(m.notes)}</div>` : ''}</td>
        <td class="num">${m.observations} ${m.protocol === 'opportunistic' ? 'photos' : 'det.'}</td><td>${sourceBadge(m.source_kind)}</td></tr>`).join('')}
      </tbody></table>`).join('')}
    <h3>Model feedback from reviews</h3>
    <p class="small muted">How reviewers judged detections at each model probability. This shows whether the robot's
      reporting threshold and the queue order make sense. Reviewed images, field photos included, are training data for the next model version.</p>
    <table><thead><tr><th>p(<i>P. serotina</i>)</th><th class="num">Conf.</th><th class="num">Rej.</th><th class="num">Unc./visit</th><th class="num">Pending</th><th class="num">Share confirmed</th></tr></thead>
      <tbody>${cal.map((c) => {
        const decided = c.confirmed + c.rejected;
        return `<tr><td>${esc(c.band)}</td><td class="num">${c.confirmed}</td><td class="num">${c.rejected}</td>
        <td class="num">${c.uncertain + c.field_visit}</td><td class="num">${c.pending}</td><td class="num">${decided ? pct(c.confirmed / decided) : '–'}</td></tr>`;
      }).join('')}</tbody></table>`;
  $('#photo-import').addEventListener('click', importPhotos);
}

async function viewSources() {
  const src = await api('/api/sources');
  const ref = src.reference.sources;
  const rt = src.runtime;
  const n = (o) => Object.entries(o).map(([k, v]) => `${v} ${esc(k)}`).join(', ') || '0';
  body().innerHTML = `
    <h2>Data &amp; provenance</h2>
    <h3>What is real and what is simulated</h3>
    <table><tbody>
      <tr><td>Bonn boundary, districts, wooded land use</td><td><span class="badge real">real</span> Stadt Bonn open data</td></tr>
      <tr><td>Nature reserves, FFH sites, protected biotopes</td><td><span class="badge real">real</span> LANUK @LINFOS</td></tr>
      <tr><td>External records of <i>P. serotina</i></td><td><span class="badge real">real</span> GBIF, LANUK Neobiota</td></tr>
      <tr><td>Species facts &amp; ID hints</td><td><span class="badge real">real</span> summarised from LANUK pages</td></tr>
      <tr><td>Orthophotos, forest layers (map)</td><td><span class="badge real">live</span> Geobasis NRW, Wald und Holz NRW</td></tr>
      <tr><td>Robot missions &amp; tracks</td><td>${n(rt.missions)}</td></tr>
      <tr><td>Detections, images &amp; photos</td><td>${n(rt.observations)}</td></tr>
      <tr><td>Review decisions</td><td>${n(rt.reviews)}</td></tr>
      <tr><td>Stand log entries</td><td>${n(rt.stand_notes)}</td></tr>
    </tbody></table>
    <p class="small muted">Simulated plants were placed in real Bonn woodland so that the context lookups can be tested. They say nothing about where
      <i>P. serotina</i> actually grows.</p>

    <h3>Reference snapshots</h3>
    ${Object.values(ref).map((r) => `<div class="card">
      <div class="row-flex"><b class="grow1">${esc(r.title)}</b><span class="badge real">real</span></div>
      <div class="small">${esc(r.publisher)} · <a href="${esc(r.source_url)}" target="_blank" rel="noopener">source</a></div>
      <dl class="kv small">
        <dt>Licence</dt><dd>${esc(r.license)}</dd>
        <dt>Retrieved</dt><dd>${esc(r.retrieved_at)}</dd>
        <dt>Features</dt><dd>${esc(r.feature_count)}</dd>
        <dt>Used for</dt><dd>${esc(r.used_for)}</dd>
        <dt>Processing</dt><dd>${esc(r.processing)}</dd>
        <dt>SHA-256</dt><dd class="mono">${esc(r.sha256.slice(0, 16))}…</dd>
      </dl></div>`).join('')}
    <div class="callout">LANUK Neobiota portal: <b>${src.lanuk_neobiota.bonn_record_count}</b> validated <i>P. serotina</i> find points in Bonn,
      ${src.lanuk_neobiota.nrw_record_count} in all of NRW (at retrieval). The species is not on the EU list of concern, so reporting it is voluntary. That is one reason
      why regular local monitoring data could add something here.</div>

    <h3>Live map services</h3>
    <ul class="reasons">${src.live_map_services.map((w) => `<li>${esc(w.title)}: ${esc(w.publisher)}, ${esc(w.license)}</li>`).join('')}</ul>

    <h3>Species profile sources</h3>
    <ul class="reasons">${Object.values(src.species_profile_sources).map((s) => `<li><a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.title)}</a> (${esc(s.retrieved)})</li>`).join('')}</ul>

    <h3>Original photos</h3>
    <p class="small">Imported photos are kept byte-for-byte with the SHA-256 recorded at import.
      <button id="verify-originals">Check all originals now</button> <span id="verify-result" class="small" role="status"></span></p>

    <h3>Audit log</h3>
    <div id="events" class="small muted">loading…</div>
    <p class="small"><a href="/docs" target="_blank">API documentation (OpenAPI)</a>, including the robot ingest contract.</p>`;
  $('#verify-originals').addEventListener('click', async () => {
    const r = await api('/api/originals/verify');
    $('#verify-result').textContent = r.problems.length
      ? `${r.problems.length} problem(s): ${r.problems.map((p) => `${p.uid} ${p.problem}`).join('; ')}`
      : `${r.ok} of ${r.checked} originals unchanged.`;
  });
  const events = await api('/api/events?limit=25');
  $('#events').innerHTML = `<table><tbody>${events.map((e) => `<tr><td>${esc(e.at.slice(0, 16).replace('T', ' '))}</td>
    <td>${esc(e.kind)}</td><td>${esc(e.ref ?? '')}</td><td>${esc(e.actor)}</td></tr>`).join('')}</tbody></table>`;
}

let speciesCache;
async function speciesProfile() {
  speciesCache ||= await api('/api/species');
  return speciesCache;
}

// ---------------------------------------------------------------- routing

async function route() {
  const [name, arg] = (location.hash.slice(1) || 'review').split('/');
  const tab = { obs: 'review', stand: 'stands', mission: 'missions' }[name] || name;
  $$('.tabs a').forEach((a) => a.setAttribute('aria-selected', String(a.dataset.tab === tab)));
  if (name !== 'stand') { state.selectedStand = null; drawStands(); }
  if (name !== 'mission') drawTracks();
  if (!['obs', 'stand'].includes(name)) layers.highlight.clearLayers();
  try {
    if (name === 'obs') await viewObservation(Number(arg));
    else if (name === 'stand') await viewStand(decodeURIComponent(arg));
    else if (name === 'stands') viewStands();
    else if (name === 'missions' || name === 'mission') viewMissions(arg ? decodeURIComponent(arg) : null);
    else if (name === 'sources') await viewSources();
    else viewReviewList();
  } catch (e) {
    body().innerHTML = `<p class="error">Could not load: ${esc(e.message)}</p>`;
  }
  body().scrollTop = 0;
}

function wireGlobalEvents() {
  const panel = body();
  panel.addEventListener('click', (e) => {
    const target = e.target.closest('[data-href]');
    if (target && !e.target.closest('a')) location.hash = target.dataset.href;
  });
  panel.addEventListener('keydown', (e) => {
    const target = e.target.closest('[data-href]');
    if (target && e.key === 'Enter') location.hash = target.dataset.href;
  });
  document.addEventListener('keydown', (e) => {
    if (!location.hash.startsWith('#obs/') || e.target.closest('input, textarea, select') || e.ctrlKey || e.metaKey || e.altKey) return;
    const decision = { c: 'confirmed', r: 'rejected', u: 'uncertain', f: 'field_visit' }[e.key.toLowerCase()];
    if (decision) $(`.decision-buttons button[data-decision="${decision}"]`)?.click();
  });
  const reviewer = $('#reviewer');
  reviewer.value = storage('forestcare.reviewer');
  reviewer.addEventListener('change', () => storage('forestcare.reviewer', reviewer.value.trim()));
  $('#year-filter').addEventListener('change', (e) => { state.year = e.target.value; drawObservations(); drawTracks(); });
  window.addEventListener('hashchange', route);
}

async function main() {
  state.meta = await api('/api/meta');
  const keys = ['districts', 'wooded', 'nsg', 'ffh', 'biotopes', 'gbif'];
  const refs = await Promise.all(keys.map((k) => api(`/api/reference/${k}`)));
  setupMap(Object.fromEntries(keys.map((k, i) => [k, refs[i]])));
  drawLegend();
  wireGlobalEvents();
  await loadAll();
  await route();
}

main().catch((e) => { body().innerHTML = `<p class="error">Failed to start: ${esc(e.message)}</p>`; });
