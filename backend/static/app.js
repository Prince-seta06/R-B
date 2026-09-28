'use strict';
const BAND_COLOR = {Good:'#3F8F5B', Fair:'#C8A12A', Poor:'#D9752B', Critical:'#B3261E', Unrated:'#7C8A90'};
const ROLE = {JE:'Junior Engineer', SE:'Superintending Engineer', CE:'Chief Engineer'};
const S = {
  user: null, meta: null, assets: {building:[], road:[]}, selected: null,
  map: null, layer: null, temp: null, layers: {}, picking: null, chart: null,
  scoreSeq: 0, publicMeta: null, citizenKind: 'road', citizenPhotoUrl: null,
  officerGrievances: []
};

const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const esc = t => String(t ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const kindName = k => k === 'road' ? 'Road' : 'Building';
const tag = k => `<span class="kindtag ${k}">${kindName(k)}</span>`;
const comps = k => S.meta.kinds[k].components;

function toast(msg) {
  const t = document.createElement('div');
  t.className = 'toast';
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 3600);
}

function statusChip(st) {
  const s = st || 'Pending';
  const cls = 'st-' + s.replace(/\s+/g, '_');
  return `<span class="st-chip ${cls}">${esc(s)}</span>`;
}

/* "Resolved by Rajesh Patel, Chief Engineer on 28 Sep 2026, 11:10 AM IST" (built from server fields, all escaped) */
function outcomeLine(g) {
  if (g.status !== 'Resolved' && g.status !== 'Rejected') return '';
  const who = g.resolved_by ? `${esc(g.resolved_by)}${g.resolved_by_role ? ', ' + esc(g.resolved_by_role) : ''}` : 'a departmental officer';
  const when = g.resolved_at_display ? ` on ${esc(g.resolved_at_display)}` : '';
  return `${esc(g.status)} by ${who}${when}`;
}

function urgencyChip(urg) {
  const u = urg || 'Normal';
  return `<span class="urg-chip urg-${u}">${u === 'Emergency' ? '🚨 ' : ''}${esc(u)}</span>`;
}

async function api(path, opts = {}) {
  const o = {method: opts.method || 'GET', headers: {}, credentials: 'same-origin'};
  if (opts.json !== undefined) { o.headers['Content-Type'] = 'application/json'; o.body = JSON.stringify(opts.json); }
  else if (opts.body) o.body = opts.body;
  const r = await fetch(path, o);
  if (r.status === 401 && path !== '/api/login') { signOut(false); throw new Error('Session expired, sign in again'); }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    const d = data.detail;
    throw new Error(typeof d === 'string' ? d
      : Array.isArray(d) ? d.map(x => (x.loc ? x.loc.slice(1).join('.') + ': ' : '') + x.msg).join('; ')
      : 'Request failed (' + r.status + ')');
  }
  return data;
}

/* ---------- auth & screen switches ---------- */
fetch('/api/public').then(r => r.json()).then(j => { if (j.demo_accounts) $('#demo-box').classList.remove('hidden'); }).catch(() => {});
$$('.demo button').forEach(b => b.onclick = () => { $('#u').value = b.dataset.u; $('#p').value = 'demo123'; });

$('#login-form').onsubmit = async e => {
  e.preventDefault(); $('#login-err').textContent = '';
  try {
    await api('/api/login', {method:'POST', json:{username:$('#u').value, password:$('#p').value}});
    $('#p').value = '';
    await boot();
  } catch (err) { $('#login-err').textContent = err.message; }
};

function signOut(callServer = true) {
  if (callServer) fetch('/api/logout', {method:'POST', credentials:'same-origin'}).catch(() => {});
  S.selected = null; S.user = null;
  closeModal(); stopPick(); renderEmptyDrawer();
  $('#app').classList.add('hidden');
  $('#citizen-portal').classList.add('hidden');
  $('#login').classList.remove('hidden');
}
$('#logout').onclick = () => signOut(true);

$('#btn-goto-citizen').onclick = () => {
  $('#login').classList.add('hidden');
  $('#citizen-portal').classList.remove('hidden');
  initCitizenPortal();
};

$('#btn-goto-login').onclick = () => {
  $('#citizen-portal').classList.add('hidden');
  $('#login').classList.remove('hidden');
};

async function boot() {
  S.meta = await api('/api/meta');
  S.user = S.meta.user;
  $('#login').classList.add('hidden');
  $('#citizen-portal').classList.add('hidden');
  $('#app').classList.remove('hidden');
  $('#who').innerHTML = `<b>${esc(S.user.name)}</b>`; $('#who').title = ROLE[S.user.role] || S.user.role;
  $$('.write-only').forEach(b => b.classList.toggle('hidden', !S.meta.can_write));
  $$('.register-only').forEach(b => b.classList.toggle('hidden', !S.meta.can_register));
  $('#band-filter').innerHTML = '<option value="">All conditions</option>' + S.meta.bands.map(b => `<option>${b}</option>`).join('');
  $('#legend').innerHTML = S.meta.bands.map(b => `<span><i class="dot" style="background:${BAND_COLOR[b]}"></i>${b}</span>`).join('')
    + '<span class="sep"></span><span><i class="dot" style="background:#7C8A90"></i>Building</span><span><i class="line"></i>Road</span>'
    + '<span class="hint">Dashed outline / line = inspection overdue</span>';
  initMap();
  await loadAssets();
  updateGrievanceBadge();
  showView('map');
}

/* ---------- views ---------- */
$$('header nav button').forEach(b => b.onclick = () => showView(b.dataset.view));
function showView(v) {
  $$('header nav button').forEach(b => b.setAttribute('aria-selected', b.dataset.view === v));
  ['map','dash','prio','grv','audit'].forEach(x => $('#v-' + x).classList.toggle('hidden', x !== v));
  if (v === 'map' && S.map) setTimeout(() => S.map.invalidateSize(), 50);
  if (v === 'dash') loadDashboard().catch(e => toast(e.message));
  if (v === 'prio') loadPriorities().catch(e => toast(e.message));
  if (v === 'grv') loadOfficerGrievances().catch(e => toast(e.message));
  if (v === 'audit') loadAudit().catch(e => toast(e.message));
}

/* ---------- map ---------- */
function initMap() {
  if (S.map) return;
  S.map = L.map('map').setView([22.3, 72.6], 7);
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {maxZoom:19, referrerPolicy:'strict-origin-when-cross-origin', attribution:'© OpenStreetMap contributors'}).addTo(S.map);
  S.layer = L.layerGroup().addTo(S.map);
  S.temp = L.layerGroup().addTo(S.map);
  S.map.on('click', e => onMapClick(e.latlng));
}

async function loadAssets() {
  const [building, road] = await Promise.all([api('/api/assets/building'), api('/api/assets/road')]);
  S.assets = {building, road};
  renderMap(true);
  if (S.selected) openAsset(S.selected.kind, S.selected.id, false); else renderEmptyDrawer();
}

function visibleAssets() {
  const q = $('#search').value.trim().toLowerCase(), band = $('#band-filter').value, od = $('#overdue-only').checked, kf = $('#kind-filter').value;
  return ['building','road'].filter(k => kf === 'all' || kf === k).flatMap(k => S.assets[k])
    .filter(a => (!q || a.name.toLowerCase().includes(q) || a.asset_id.toLowerCase().includes(q)) && (!band || a.band === band) && (!od || a.overdue));
}

function assetPoints(a) { return a.kind === 'road' ? a.path : [[a.lat, a.lng]]; }

function renderMap(fit) {
  S.layer.clearLayers(); S.layers = {};
  const list = visibleAssets();
  list.forEach(a => {
    const color = BAND_COLOR[a.band] || BAND_COLOR.Unrated;
    const grvText = a.unresolved_grievances_count ? `<br>⚠️ ${a.unresolved_grievances_count} pending citizen complaint(s)` : '';
    const tip = `${esc(a.name)}<br>${esc(a.band)}${a.cci != null ? ' · index ' + a.cci : ''}${a.kind === 'road' ? '<br>' + a.length_km + ' km' : ''}${grvText}`;
    let m;
    if (a.kind === 'road') {
      m = L.polyline(a.path, {color, weight: 4 + a.criticality, opacity: .92, dashArray: a.overdue ? '9 7' : null, lineCap:'round'});
    } else {
      m = L.circleMarker([a.lat, a.lng], {radius: 7 + a.criticality * 1.5, color: a.overdue ? '#16303A' : '#fff',
        weight: a.overdue ? 2.5 : 2, dashArray: a.overdue ? '3 3' : null, fillColor: color, fillOpacity: .95});
    }
    m.bindTooltip(tip, {sticky: a.kind === 'road'});
    m.on('click', ev => { if (S.picking) return; L.DomEvent.stopPropagation(ev); openAsset(a.kind, a.id, true); });
    m.addTo(S.layer); S.layers[a.kind + a.id] = m;
  });
  const pts = list.flatMap(assetPoints);
  if (fit && pts.length) S.map.fitBounds(L.latLngBounds(pts).pad(.15));
}
['search','band-filter','overdue-only','kind-filter'].forEach(id => $('#' + id).addEventListener('input', () => renderMap(false)));

/* ---------- drawer ---------- */
function renderEmptyDrawer() {
  $('#drawer').innerHTML = `<div class="empty"><h2>Select an asset</h2>
  Click a building marker or a road line to see its inspection history and citizen grievances. Colour is the latest condition, larger markers and thicker lines
  are more critical assets, and dashed outlines mean the yearly inspection is overdue.</div>`;
}

function factsHtml(a) {
  const crit = ['', 'Low', 'Medium', 'High'][a.criticality];
  const last = `<dt>Last inspected</dt><dd>${a.last_inspected ? esc(a.last_inspected) : 'Never'} ${a.overdue ? '<span class="warn">(overdue)</span>' : ''}</dd>`;
  const div = `<dt>Division</dt><dd>${esc(a.division)} (${esc(a.circle)})</dd>`;
  if (a.kind === 'road') return `<dl class="facts">
      <dt>Class</dt><dd>${esc(a.road_class)}${a.road_ref ? ' · ' + esc(a.road_ref) : ''}</dd>
      <dt>Length</dt><dd>${esc(a.length_km)} km, ${esc(a.lanes)} lane${a.lanes > 1 ? 's' : ''}, ${esc(a.surface)}</dd>
      <dt>Built</dt><dd>${esc(a.year_built || 'unknown')}</dd>
      <dt>Traffic</dt><dd>${a.traffic_aadt != null ? esc(a.traffic_aadt) + ' vehicles/day' : 'not recorded'}</dd>
      <dt>Criticality</dt><dd>${crit}</dd>${div}${last}</dl>
    <div class="links"><b>Linked records (read-only)</b>
      Works monitoring: ${a.wms_work_id ? esc(a.wms_work_id) : 'none linked'}<br>
      GujRAMS record: ${a.gujrams_id ? esc(a.gujrams_id) : 'none linked'}</div>`;
  return `<dl class="facts">
      <dt>Type</dt><dd>${esc(a.type)}</dd>${div}
      <dt>Built</dt><dd>${esc(a.year_built || 'unknown')}, ${esc(a.floors)} floor${a.floors > 1 ? 's' : ''}${a.area_sqm ? ', ' + esc(a.area_sqm) + ' sq m' : ''}</dd>
      <dt>Criticality</dt><dd>${crit}</dd>${last}</dl>
    <div class="links"><b>Linked records (read-only)</b>
      Works monitoring: ${a.wms_work_id ? esc(a.wms_work_id) : 'none linked'}<br>
      Nearest state road in GujRAMS: ${a.gujrams_road_id ? esc(a.gujrams_road_id) : 'none linked'}</div>`;
}

function inspectionHtml(k, i) {
  const write = S.meta.can_write && !i.voided;
  const canVoid = S.meta.can_void && !i.voided;
  return `<div class="insp ${i.voided ? 'voided' : ''}" data-id="${i.id}">
    <div class="insp-head"><span><b>${esc(i.inspected_on)}</b> <small>by ${esc(i.inspector || 'unknown')}</small></span>
      <span class="chip ${esc(i.band)}">${esc(i.band)} · ${esc(i.cci)}</span></div>
    ${i.voided ? `<p class="voidnote"><b>Voided:</b> ${esc(i.void_reason)}</p>` : ''}
    <div class="bars">${comps(k).map(c => { const v = i.ratings[c.key];
      return `<span>${esc(c.label)}</span><span class="bar" title="${esc(S.meta.rating_labels[v])}"><i class="r${v}" style="width:${v * 25}%"></i></span>`; }).join('')}</div>
    ${i.reason ? `<div class="rule">Band set by a safety rule, not the average: ${esc(i.reason)}.</div>` : ''}
    ${i.notes ? `<p class="notes">${esc(i.notes)}</p>` : ''}
    ${i.photos.length ? `<div class="photos">${i.photos.map(p => `<a href="${esc(p)}" target="_blank" rel="noopener"><img src="${esc(p)}" alt="Inspection photo" loading="lazy"></a>`).join('')}</div>` : ''}
    ${i.summary ? `<div class="summary">${esc(i.summary)}</div>` : ''}
    <div class="row-actions">
      ${!i.voided && !i.summary ? `<button class="btn ghost small sum-btn" data-id="${i.id}">Generate summary</button>` : ''}
      ${write ? `<label class="btn ghost small">Add photo<input type="file" class="photo-input hidden" data-id="${i.id}" accept="image/jpeg,image/png,image/webp"></label>` : ''}
      ${canVoid ? `<button class="btn danger small void-btn" data-id="${i.id}">Void</button>` : ''}
    </div></div>`;
}

async function openAsset(kind, id, pan) {
  S.selected = {kind, id};
  let a; try { a = await api(`/api/assets/${kind}/${id}`); } catch (e) { toast(e.message); return; }
  if (pan && S.map) {
    if (kind === 'road') S.map.fitBounds(L.latLngBounds(a.path).pad(.3));
    else S.map.setView([a.lat, a.lng], Math.max(S.map.getZoom(), 13));
  }

  const grvHtml = (a.grievances && a.grievances.length) ? `
    <div class="drawer-grv-sec">
      <h3>Citizen Complaints (${a.grievances.length})</h3>
      ${a.grievances.map(g => `
        <div class="grv-mini-item">
          <div class="gm-head">
            <b>${esc(g.ticket_id)}</b>
            ${statusChip(g.status)}
          </div>
          <div>${urgencyChip(g.urgency)} <b>${esc(g.category)}</b></div>
          <div style="color:var(--ink-soft);font-size:12px;margin:2px 0">${esc(g.description)}</div>
          <small style="color:var(--ink-soft)">Reported on ${esc((g.created_at || '').slice(0, 10))} by ${esc(g.citizen_name)}</small>
        </div>
      `).join('')}
    </div>
  ` : '';

  $('#drawer').innerHTML = `
    <h2 class="title">${esc(a.name)}</h2>
    <p class="asset-id">${tag(kind)} ${esc(a.asset_id)}</p>
    <span class="chip ${esc(a.band)}">${esc(a.band)}${a.cci != null ? ' · index ' + esc(a.cci) : ''}</span>
    ${factsHtml(a)}
    ${grvHtml}
    ${S.meta.can_write ? '<button class="btn" id="new-insp">Record inspection</button>' : `<p class="hint">Only Junior Engineers record inspections.${S.meta.can_void ? ' You can void a wrong inspection (a reason is required and is logged).' : ''}</p>`}
    <div id="insp-form"></div>
    <h3>Inspection history</h3>
    ${a.inspections.length ? a.inspections.map(i => inspectionHtml(kind, i)).join('') : '<p class="empty" style="padding:0">No inspections recorded yet.</p>'}`;
  if ($('#new-insp')) $('#new-insp').onclick = () => showInspectionForm(kind, a);
}

$('#drawer').addEventListener('click', async e => {
  const sum = e.target.closest('.sum-btn'), vd = e.target.closest('.void-btn');
  if (sum) {
    sum.disabled = true; sum.textContent = 'Generating…';
    try { await api(`/api/inspections/${S.selected.kind}/${sum.dataset.id}/summary`, {method:'POST'}); await openAsset(S.selected.kind, S.selected.id, false); }
    catch (err) { toast(err.message); sum.disabled = false; sum.textContent = 'Generate summary'; }
  }
  if (vd) {
    const reason = window.prompt('Why is this inspection being voided? (recorded in the audit log)');
    if (reason === null) return;
    try { await api(`/api/inspections/${S.selected.kind}/${vd.dataset.id}/void`, {method:'POST', json:{reason}}); toast('Inspection voided'); await loadAssets(); }
    catch (err) { toast(err.message); }
  }
});

$('#drawer').addEventListener('change', async e => {
  const inp = e.target.closest('.photo-input');
  if (!inp || !inp.files[0]) return;
  const fd = new FormData(); fd.append('file', inp.files[0]);
  try { await api(`/api/inspections/${S.selected.kind}/${inp.dataset.id}/photo`, {method:'POST', body:fd}); toast('Photo added'); await openAsset(S.selected.kind, S.selected.id, false); }
  catch (err) { toast(err.message); }
});

function showInspectionForm(kind, a) {
  const host = $('#insp-form');
  if (host.innerHTML) { host.innerHTML = ''; return; }
  host.innerHTML = `<form class="inspect" id="insp-f">
    <p style="margin:0 0 10px"><b>Rate each component</b> (1 serious damage, 4 minor or none)</p>
    ${comps(kind).map(c => `<div class="rate-row"><label for="r-${c.key}">${esc(c.label)}${c.safety ? ' <span class="hint">(safety-critical)</span>' : ''}</label>
      <select id="r-${c.key}" data-k="${c.key}">${[4,3,2,1].map(v => `<option value="${v}" ${v === 3 ? 'selected' : ''}>${v} – ${esc(S.meta.rating_labels[v])}</option>`).join('')}</select></div>`).join('')}
    <div class="preview">Resulting condition: <span class="chip" id="pv"></span></div>
    <div class="rule hidden" id="pv-rule"></div>
    <label class="field"><span>Notes</span><textarea id="i-notes" rows="3" maxlength="2000" placeholder="What did you find?"></textarea></label>
    <label class="field"><span>Photo (optional, JPEG/PNG/WebP up to 5 MB)</span><input id="i-photo" type="file" accept="image/jpeg,image/png,image/webp"></label>
    <div class="row-actions"><button class="btn" type="submit" id="i-save">Save inspection</button>
      <button class="btn ghost" type="button" id="i-cancel">Cancel</button></div>
    <p class="err" id="i-err" role="alert"></p></form>`;
  const vals = () => Object.fromEntries($$('#insp-f select').map(s => [s.dataset.k, +s.value]));
  const upd = async () => {
    const seq = ++S.scoreSeq;
    try {
      const r = await api(`/api/score/${kind}`, {method:'POST', json:{ratings: vals()}});
      if (seq !== S.scoreSeq || !$('#pv')) return;
      $('#pv').className = 'chip ' + r.band; $('#pv').textContent = `${r.band} · index ${r.cci}`;
      const rule = $('#pv-rule'); rule.classList.toggle('hidden', !r.reason);
      rule.textContent = r.reason ? `Band set by a safety rule, not the average: ${r.reason}.` : '';
    } catch (err) { }
  };
  $$('#insp-f select').forEach(s => s.onchange = upd); upd();
  $('#i-cancel').onclick = () => host.innerHTML = '';
  $('#insp-f').onsubmit = async e => {
    e.preventDefault(); $('#i-err').textContent = ''; $('#i-save').disabled = true;
    try {
      const created = await api(`/api/assets/${kind}/${a.id}/inspections`, {method:'POST', json:{ratings: vals(), notes: $('#i-notes').value}});
      const f = $('#i-photo').files[0];
      if (f) { const fd = new FormData(); fd.append('file', f);
        try { await api(`/api/inspections/${kind}/${created.id}/photo`, {method:'POST', body:fd}); }
        catch (pe) { toast('Inspection saved, but the photo failed: ' + pe.message); } }
      toast('Inspection saved'); await loadAssets();
    } catch (err) { $('#i-err').textContent = err.message; $('#i-save').disabled = false; }
  };
}

/* ---------- dashboard ---------- */
$('#dash-kind').onchange = () => loadDashboard().catch(e => toast(e.message));
async function loadDashboard() {
  const d = await api('/api/dashboard?kind=' + $('#dash-kind').value);
  $('#dash-title').textContent = `Condition by ${d.grouped_by}`;
  const split = Object.entries(d.by_kind).map(([k, n]) => `${n} ${k === 'road' ? 'road' : 'building'}${n === 1 ? '' : 's'}`).join(' · ');
  const cards = [['Assets', d.total, split], ['Need attention (poor or critical)', d.needs_attention], ['Critical', d.critical],
    ['Inspection overdue', d.overdue], ['Average condition index', d.avg_cci ?? '–']];
  if (d.road_km != null) cards.push(['Road km in poor or critical condition', d.road_km_needs_attention, `of ${d.road_km} km`]);
  $('#stats').innerHTML = cards.map(([l, v, sub]) => `<div class="stat"><b>${esc(v)}</b><span>${esc(l)}</span>${sub ? `<br><span>${esc(sub)}</span>` : ''}</div>`).join('');
  if (S.chart) S.chart.destroy();
  S.chart = new Chart($('#chart'), {type:'bar',
    data:{labels:d.groups.map(g => g.name), datasets:['Good','Fair','Poor','Critical','Unrated'].map(b => ({label:b, backgroundColor:BAND_COLOR[b], data:d.groups.map(g => g.bands[b])}))},
    options:{responsive:true, maintainAspectRatio:false, plugins:{legend:{position:'bottom'}, title:{display:true, text:'Number of assets by condition band'}},
      scales:{x:{stacked:true}, y:{stacked:true, beginAtZero:true, ticks:{precision:0}}}}});
}

/* ---------- priorities ---------- */
$('#prio-kind').onchange = () => loadPriorities().catch(e => toast(e.message));
async function loadPriorities() {
  const p = await api('/api/priorities?limit=15&kind=' + $('#prio-kind').value);
  $('#prio-body').innerHTML = p.items.length ? p.items.map((r, i) => `<tr data-kind="${r.kind}" data-id="${r.id}" tabindex="0">
    <td>${i + 1}</td><td><b>${esc(r.name)}</b><br>${tag(r.kind)} <small style="color:var(--ink-soft)">${esc(r.asset_id)} · ${esc(r.type_label)}${r.kind === 'road' ? ', ' + esc(r.length_km) + ' km' : ''}</small></td>
    <td>${esc(r.district)}</td><td><span class="chip ${esc(r.band)}">${esc(r.band)}</span></td><td class="num">${esc(r.cci)}</td><td class="num"><b>${esc(r.priority)}</b></td>
    <td>${r.weakest.length ? esc(r.weakest.join(', ')) : 'None rated major or worse'}${r.reason ? `<br><span class="hint">Safety rule: ${esc(r.reason)}</span>` : ''}${r.overdue ? '<br><span class="warn">Inspection overdue</span>' : ''}</td></tr>`).join('')
    : '<tr><td colspan="7">No rated assets yet. Record an inspection to see priorities.</td></tr>';
  $('#unrated-title').textContent = p.uninspected_total ? `Not yet inspected (${p.uninspected_total})` : '';
  $('#unrated-table').classList.toggle('hidden', !p.uninspected_total);
  $('#unrated-body').innerHTML = p.uninspected.map(r => `<tr data-kind="${r.kind}" data-id="${r.id}" tabindex="0">
    <td><b>${esc(r.name)}</b><br>${tag(r.kind)} <small style="color:var(--ink-soft)">${esc(r.asset_id)}</small></td><td>${esc(r.district)}</td>
    <td>${['', 'Low', 'Medium', 'High'][r.criticality]}</td></tr>`).join('');
  $$('#prio-body tr[data-id], #unrated-body tr[data-id]').forEach(tr => {
    const go = () => { showView('map'); openAsset(tr.dataset.kind, +tr.dataset.id, true); };
    tr.onclick = go; tr.onkeydown = e => { if (e.key === 'Enter') go(); };
  });
}

/* ---------- officer grievance management ---------- */
function setGrievanceBadge(stats) {
  const b = $('#grv-badge');
  if (b) {
    b.textContent = stats.pending;
    b.classList.toggle('hidden', !stats.pending);
  }
}

async function updateGrievanceBadge() {
  try { setGrievanceBadge(await api('/api/grievances/stats')); } catch(e) {}
}

function grievanceFilters() {
  return {kind: $('#grv-kind-filter').value, status: $('#grv-status-filter').value,
          urgency: $('#grv-urgency-filter').value, search: $('#grv-search').value.trim()};
}

let grvReq = 0;   // only the newest request may draw: quick filter changes / fast typing can return out of order
async function loadOfficerGrievances() {
  const seq = ++grvReq;
  const f = grievanceFilters();
  const params = new URLSearchParams();
  if (f.kind !== 'all') params.set('kind', f.kind);
  if (f.status) params.set('status', f.status);
  if (f.urgency) params.set('urgency', f.urgency);
  if (f.search) params.set('search', f.search);
  const filtered = f.kind !== 'all' || f.status || f.urgency || f.search;
  $('#grv-reset').classList.toggle('hidden', !filtered);

  const tbody = $('#grv-body');
  let stats, list;
  try {
    [stats, list] = await Promise.all([api('/api/grievances/stats'), api('/api/grievances?' + params)]);
  } catch (err) {
    if (seq !== grvReq) return;
    // never leave the previous (unfiltered) rows on screen looking like the result of the new filters
    S.officerGrievances = [];
    $('#grv-count').textContent = '';
    tbody.innerHTML = `<tr><td colspan="9" style="text-align:center;padding:24px;color:var(--critical)">
      Could not load grievances (${esc(err.message)}). <button type="button" class="btn ghost small" id="grv-retry">Retry</button></td></tr>`;
    $('#grv-retry').onclick = () => loadOfficerGrievances();
    toast(err.message);
    return;
  }
  if (seq !== grvReq) return;   // a newer request has been issued; drop this stale answer
  S.officerGrievances = list;
  setGrievanceBadge(stats);

  const cards = [
    ['Total Grievances', stats.total, `${stats.road_count} roads · ${stats.building_count} buildings`],
    ['Pending Action', stats.pending, 'Awaiting initial investigation'],
    ['In Progress', stats.under_review + stats.work_order_issued, 'Under review or work order issued'],
    ['Resolved', stats.resolved, 'Completed redressal'],
    ['🚨 Emergency Hazards', stats.emergency, 'Priority safety risks']
  ];
  $('#grv-stats').innerHTML = cards.map(([l, v, sub]) => `
    <div class="stat"><b>${esc(v)}</b><span>${esc(l)}</span>${sub ? `<br><span style="font-size:12px">${esc(sub)}</span>` : ''}</div>
  `).join('');
  $('#grv-count').textContent = `Showing ${list.length} of ${stats.total} grievance${stats.total === 1 ? '' : 's'}${filtered ? ' (filters applied; the cards above always show every grievance in your area)' : ''}`;

  if (!list.length) {
    tbody.innerHTML = `<tr><td colspan="9" style="text-align:center;color:var(--ink-soft);padding:24px">${filtered ? 'No grievances match these filters.' : 'No grievances yet.'}</td></tr>`;
    return;
  }

  tbody.innerHTML = list.map(g => `
    <tr data-gid="${g.id}">
      <td><b>${esc(g.ticket_id)}</b></td>
      <td><span class="kindtag ${g.kind}">${g.kind === 'road' ? '🛣️ Road' : '🏢 Building'}</span></td>
      <td><b>${esc(g.asset_name)}</b><br><small style="color:var(--ink-soft)">${esc(g.district)}</small></td>
      <td><b>${esc(g.category)}</b>${g.specific_location ? `<br><small style="color:var(--ink-soft)">${esc(g.specific_location)}</small>` : ''}</td>
      <td>${esc(g.citizen_name)}<br><small>${esc(g.citizen_phone)}</small></td>
      <td>${urgencyChip(g.urgency)}</td>
      <td>${statusChip(g.status)}</td>
      <td><small>${esc((g.created_at || '').slice(0, 10))}</small></td>
      <td><button class="btn ghost small grv-act-btn" data-gid="${g.id}">Review / Action</button></td>
    </tr>
  `).join('');

  $$('.grv-act-btn').forEach(btn => {
    btn.onclick = e => {
      e.stopPropagation();
      const g = S.officerGrievances.find(x => x.id === +btn.dataset.gid);
      if (g) openGrievanceModal(g);
    };
  });
  $$('#grv-body tr[data-gid]').forEach(tr => {
    tr.onclick = () => {
      const g = S.officerGrievances.find(x => x.id === +tr.dataset.gid);
      if (g) openGrievanceModal(g);
    };
  });
}

['grv-kind-filter', 'grv-status-filter', 'grv-urgency-filter'].forEach(id => {
  $('#' + id).onchange = () => loadOfficerGrievances();
});
let grvSearchTimer;
$('#grv-search').oninput = () => { clearTimeout(grvSearchTimer); grvSearchTimer = setTimeout(loadOfficerGrievances, 300); };
$('#grv-reset').onclick = () => {
  $('#grv-kind-filter').value = 'all'; $('#grv-status-filter').value = ''; $('#grv-urgency-filter').value = '';
  $('#grv-search').value = '';
  loadOfficerGrievances();
};

function openGrievanceModal(g) {
  const root = openModal(`
    <h2>Grievance Details & Action</h2>
    <p style="color:var(--ink-soft);margin:-8px 0 14px">Ticket: <b>${esc(g.ticket_id)}</b> · Filed: ${esc((g.created_at || '').replace('T', ' ').slice(0, 19))}</p>
    <div class="grv-meta" style="background:#F8FAF9;padding:12px;border:1px solid var(--line);border-radius:var(--radius);margin-bottom:14px">
      <div><b>Asset:</b> ${tag(g.kind)} ${esc(g.asset_name)} (${esc(g.district)} District)</div>
      ${g.sub_type ? `<div><b>Type/Class:</b> ${esc(g.sub_type)}</div>` : ''}
      <div><b>Category:</b> ${esc(g.category)} ${urgencyChip(g.urgency)}</div>
      ${g.specific_location ? `<div><b>Specific Location / Chainage:</b> ${esc(g.specific_location)}</div>` : ''}
      <div><b>Citizen:</b> ${esc(g.citizen_name)} · 📞 <a href="tel:${esc(g.citizen_phone)}">${esc(g.citizen_phone)}</a> ${g.citizen_email ? '· ✉️ ' + esc(g.citizen_email) : ''}</div>
    </div>
    <div style="margin-bottom:14px">
      <b>Citizen Description:</b>
      <div class="grv-desc">${esc(g.description)}</div>
    </div>
    ${g.photo_url ? `<div style="margin-bottom:14px"><b>Uploaded Photo:</b><br><a href="${esc(g.photo_url)}" target="_blank" rel="noopener"><img src="${esc(g.photo_url)}" class="preview-img" style="max-width:200px;max-height:200px" alt="Defect photo"></a></div>` : ''}

    ${outcomeLine(g) ? `<div class="grv-res ${g.status === 'Rejected' ? 'rejected' : ''}" style="margin:0 0 14px"><b>${outcomeLine(g)}</b></div>` : ''}
    <div id="grv-action-f" style="border-top:1px solid var(--line);padding-top:14px">
      <label class="field"><span>Update Status</span>
        <select id="ga-status">
          <option value="Pending" ${g.status === 'Pending' ? 'selected' : ''}>Pending</option>
          <option value="Under Review" ${g.status === 'Under Review' ? 'selected' : ''}>Under Review (Inspection in progress)</option>
          <option value="Work Order Issued" ${g.status === 'Work Order Issued' ? 'selected' : ''}>Work Order Issued (Repair assigned)</option>
          <option value="Resolved" ${g.status === 'Resolved' ? 'selected' : ''}>Resolved (Defect repaired & inspected)</option>
          <option value="Rejected" ${g.status === 'Rejected' ? 'selected' : ''}>Rejected (Not under R&B jurisdiction / duplicate)</option>
        </select>
      </label>
      <label class="field"><span id="ga-notes-label">Officer Remarks / Action Taken <small style="font-weight:400">(shown to the citizen)</small></span>
        <textarea id="ga-notes" rows="3" placeholder="Enter inspection findings, work order details, or resolution remarks...">${esc(g.resolution_notes || '')}</textarea>
      </label>
      <div class="row-actions">
        <button class="btn" type="submit" id="ga-save">Save & Update Status</button>
        <button class="btn ghost" type="button" id="ga-cancel">Close</button>
      </div>
      <p class="err" id="ga-err" role="alert"></p>
    </div>
  `);

  $('#ga-cancel').onclick = closeModal;
  const syncNotesLabel = () => {
    const closing = ['Resolved', 'Rejected'].includes($('#ga-status').value);
    $('#ga-notes-label').innerHTML = closing
      ? 'Officer Remarks / Action Taken <b style="color:var(--critical)">(required to close; shown to the citizen)</b>'
      : 'Officer Remarks / Action Taken <small style="font-weight:400">(shown to the citizen)</small>';
    $('#ga-notes').required = closing;
  };
  $('#ga-status').onchange = syncNotesLabel;
  syncNotesLabel();
  root.querySelector('form').onsubmit = async e => {
    e.preventDefault();
    $('#ga-err').textContent = '';
    if (['Resolved', 'Rejected'].includes($('#ga-status').value) && $('#ga-notes').value.trim().length < 3) {
      $('#ga-err').textContent = 'Please enter remarks: the citizen sees them as the reason for closing the ticket.';
      return;
    }
    $('#ga-save').disabled = true;
    try {
      await api(`/api/grievances/${g.id}`, {
        method: 'PATCH',
        json: {
          status: $('#ga-status').value,
          resolution_notes: $('#ga-notes').value
        }
      });
      closeModal();
      toast('Grievance status updated');
      await loadOfficerGrievances();
    } catch (err) {
      $('#ga-err').textContent = err.message;
      $('#ga-save').disabled = false;
    }
  };
}

/* ---------- audit ---------- */
async function loadAudit() {
  const rows = await api('/api/audit?limit=100');
  $('#audit-body').innerHTML = rows.length ? rows.map(r => `<tr><td>${esc(r.at.replace('T', ' '))}</td><td>${esc(r.user)}</td><td>${esc(r.action)}</td>
    <td>${r.kind ? tag(r.kind) : ''} ${esc(r.asset)}</td><td>${esc(r.detail)}</td></tr>`).join('') : '<tr><td colspan="5">Nothing recorded yet.</td></tr>';
}

/* ---------- modal + map picking ---------- */
function closeModal() { $('#modal-root').innerHTML = ''; }
function openModal(html) {
  const root = $('#modal-root');
  root.innerHTML = `<div class="modal-bg"><form class="modal" role="dialog" aria-modal="true">${html}</form></div>`;
  const first = root.querySelector('input,select'); if (first) first.focus();
  return root;
}
document.addEventListener('keydown', e => {
  const bg = $('#modal-root .modal-bg');
  if (e.key === 'Escape') { if (S.picking) cancelPick(); else if (bg && !bg.classList.contains('hidden')) closeModal(); }
  if (e.key === 'Tab' && bg && !bg.classList.contains('hidden')) {
    const f = [...bg.querySelectorAll('input,select,button,textarea')].filter(x => !x.disabled && x.offsetParent !== null);
    if (!f.length) return;
    const a = document.activeElement;
    if (e.shiftKey && (a === f[0] || !bg.contains(a))) { e.preventDefault(); f[f.length - 1].focus(); }
    else if (!e.shiftKey && (a === f[f.length - 1] || !bg.contains(a))) { e.preventDefault(); f[0].focus(); }
  }
});
function km(a, b) {
  const R = 6371.0088, r = x => x * Math.PI / 180, h = Math.sin(r(b[0] - a[0]) / 2) ** 2 + Math.cos(r(a[0])) * Math.cos(r(b[0])) * Math.sin(r(b[1] - a[1]) / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(h));
}
const pathKm = pts => pts.reduce((s, p, i) => i ? s + km(pts[i - 1], p) : s, 0);

function startPick(mode, done) {
  const bg = $('#modal-root .modal-bg'); bg.classList.add('hidden');
  showView('map'); S.picking = {mode, pts:[], done, bg};
  $('#pickmsg-text').textContent = mode === 'point' ? 'Click the map to set the building location' : 'Click points along the road from start to end, then press Finish';
  $('#pick-undo').classList.toggle('hidden', mode !== 'path'); $('#pick-done').classList.toggle('hidden', mode !== 'path');
  $('#pickmsg').classList.remove('hidden');
}
function drawTemp() {
  S.temp.clearLayers(); const p = S.picking; if (!p || !p.pts.length) return;
  L.polyline(p.pts, {color:'#2F5D7C', weight:4, dashArray:'6 6'}).addTo(S.temp);
  p.pts.forEach((pt, i) => L.circleMarker(pt, {radius: i === 0 ? 7 : 5, color:'#fff', weight:2, fillColor:'#2F5D7C', fillOpacity:1}).addTo(S.temp));
}
function onMapClick(ll) {
  const p = S.picking; if (!p) return;
  if (p.mode === 'point') { finishPick([[+ll.lat.toFixed(5), +ll.lng.toFixed(5)]]); return; }
  p.pts.push([+ll.lat.toFixed(5), +ll.lng.toFixed(5)]); drawTemp();
}
function finishPick(pts) { const p = S.picking; S.picking = null; hidePickUi(); p.bg.classList.remove('hidden'); p.done(pts); }
function cancelPick() { const p = S.picking; S.picking = null; hidePickUi(); if (p) p.bg.classList.remove('hidden'); }
function stopPick() { S.picking = null; hidePickUi(); }
function hidePickUi() { $('#pickmsg').classList.add('hidden'); if (S.temp) S.temp.clearLayers(); }
$('#pick-undo').onclick = () => { if (S.picking) { S.picking.pts.pop(); drawTemp(); } };
$('#pick-done').onclick = () => { const p = S.picking; if (!p) return; if (p.pts.length < 2) { toast('Add at least two points'); return; } finishPick(p.pts.slice()); };
$('#pick-cancel').onclick = cancelPick;

/* ---------- register building ---------- */
const opts = arr => arr.map(t => `<option>${esc(t)}</option>`).join('');
$('#add-building').onclick = () => {
  const root = openModal(`<h2>Register a building</h2>
    <label class="field"><span>Name</span><input id="b-name" required maxlength="120"></label>
    <div class="two">
      <label class="field"><span>Type</span><select id="b-type">${opts(S.meta.kinds.building.types)}</select></label>
      <label class="field"><span>District</span><select id="b-dist">${opts(S.meta.districts)}</select></label>
    </div>
    <div class="two">
      <label class="field"><span>Year built</span><input id="b-year" type="number" min="1800" max="${new Date().getFullYear()}"></label>
      <label class="field"><span>Floors</span><input id="b-floors" type="number" min="1" max="60" value="1" required></label>
    </div>
    <div class="two">
      <label class="field"><span>Area (sq m)</span><input id="b-area" type="number" min="1"></label>
      <label class="field"><span>Criticality</span><select id="b-crit"><option value="1">Low</option><option value="2" selected>Medium</option><option value="3">High</option></select></label>
    </div>
    <div class="two">
      <label class="field"><span>Latitude</span><input id="b-lat" type="number" step="any" required></label>
      <label class="field"><span>Longitude</span><input id="b-lng" type="number" step="any" required></label>
    </div>
    <button type="button" class="btn ghost" id="b-pick">Pick location on map</button>
    <div class="row-actions"><button class="btn" type="submit">Save building</button><button type="button" class="btn ghost" id="b-cancel">Cancel</button></div>
    <p class="err" id="b-err" role="alert"></p>`);
  $('#b-cancel').onclick = closeModal;
  $('#b-pick').onclick = () => startPick('point', pts => { $('#b-lat').value = pts[0][0]; $('#b-lng').value = pts[0][1]; });
  root.querySelector('form').onsubmit = async e => {
    e.preventDefault(); $('#b-err').textContent = '';
    try {
      const nb = await api('/api/assets/building', {method:'POST', json:{name:$('#b-name').value, type:$('#b-type').value, district:$('#b-dist').value,
        lat:+$('#b-lat').value, lng:+$('#b-lng').value, year_built:$('#b-year').value ? +$('#b-year').value : null, floors:+$('#b-floors').value,
        area_sqm:$('#b-area').value ? +$('#b-area').value : null, criticality:+$('#b-crit').value}});
      closeModal(); toast('Registered ' + nb.asset_id); S.selected = {kind:'building', id:nb.id}; await loadAssets(); showView('map');
    } catch (err) { $('#b-err').textContent = err.message; }
  };
};

/* ---------- register road ---------- */
$('#add-road').onclick = () => {
  const K = S.meta.kinds.road; let path = [];
  const root = openModal(`<h2>Register a road</h2>
    <label class="field"><span>Name</span><input id="r-name" required maxlength="120" placeholder="e.g. Kamrej - Bardoli Road"></label>
    <div class="two">
      <label class="field"><span>Class</span><select id="r-class">${K.classes.map(c => `<option>${esc(c.name)}</option>`).join('')}</select></label>
      <label class="field"><span>Road number (optional)</span><input id="r-ref" maxlength="20" placeholder="e.g. SH-6"></label>
    </div>
    <div class="two">
      <label class="field"><span>Surface</span><select id="r-surface">${opts(K.surfaces)}</select></label>
      <label class="field"><span>Lanes</span><select id="r-lanes">${K.lanes.map(n => `<option ${n === 2 ? 'selected' : ''}>${n}</option>`).join('')}</select></label>
    </div>
    <div class="two">
      <label class="field"><span>District</span><select id="r-dist">${opts(S.meta.districts)}</select></label>
      <label class="field"><span>Year built</span><input id="r-year" type="number" min="1800" max="${new Date().getFullYear()}"></label>
    </div>
    <div class="two">
      <label class="field"><span>Traffic (vehicles/day, optional)</span><input id="r-aadt" type="number" min="0"></label>
      <label class="field"><span>Criticality</span><select id="r-crit"><option value="1">Low</option><option value="2">Medium</option><option value="3">High</option></select></label>
    </div>
    <button type="button" class="btn ghost" id="r-draw">Draw route on map</button>
    <p class="pathinfo" id="r-pathinfo">No route drawn yet. Click along the road from start to end; length is calculated from it.</p>
    <div class="row-actions"><button class="btn" type="submit">Save road</button><button type="button" class="btn ghost" id="r-cancel">Cancel</button></div>
    <p class="err" id="r-err" role="alert"></p>`);
  const setCrit = () => { const c = K.classes.find(x => x.name === $('#r-class').value); $('#r-crit').value = c ? c.criticality : 2; };
  $('#r-class').onchange = setCrit; setCrit();
  $('#r-cancel').onclick = closeModal;
  $('#r-draw').onclick = () => startPick('path', pts => {
    path = pts; $('#r-pathinfo').textContent = `Route set: ${pts.length} points, about ${pathKm(pts).toFixed(1)} km (the server recalculates the exact length).`;
  });
  root.querySelector('form').onsubmit = async e => {
    e.preventDefault(); $('#r-err').textContent = '';
    if (path.length < 2) { $('#r-err').textContent = 'Draw the route on the map first.'; return; }
    try {
      const nr = await api('/api/assets/road', {method:'POST', json:{name:$('#r-name').value, road_class:$('#r-class').value, road_ref:$('#r-ref').value || null,
        surface:$('#r-surface').value, lanes:+$('#r-lanes').value, district:$('#r-dist').value, path,
        year_built:$('#r-year').value ? +$('#r-year').value : null, traffic_aadt:$('#r-aadt').value ? +$('#r-aadt').value : null, criticality:+$('#r-crit').value}});
      closeModal(); toast(`Registered ${nr.asset_id} (${nr.length_km} km)`); S.selected = {kind:'road', id:nr.id}; await loadAssets(); showView('map');
    } catch (err) { $('#r-err').textContent = err.message; }
  };
};

/* ---------- citizen portal (public) ---------- */
$$('.citizen-nav button').forEach(b => b.onclick = () => {
  $$('.citizen-nav button').forEach(x => { x.classList.remove('active'); x.setAttribute('aria-selected', 'false'); });
  b.classList.add('active'); b.setAttribute('aria-selected', 'true');
  const tab = b.dataset.ctab;
  $('#c-pane-report').classList.toggle('hidden', tab !== 'report');
  $('#c-pane-track').classList.toggle('hidden', tab !== 'track');
});

async function initCitizenPortal() {
  if (!S.publicMeta) {
    try { S.publicMeta = await api('/api/public/meta'); }
    catch (e) { toast('Failed to load portal configuration: ' + e.message); return; }
  }
  const distSel = $('#cg-district');
  if (!distSel.children.length) {
    distSel.innerHTML = S.publicMeta.districts.map(d => `<option value="${esc(d)}">${esc(d)}</option>`).join('');
    distSel.onchange = () => populateCitizenAssets();
  }
  setCitizenKind(S.citizenKind || 'road');
}

function setCitizenKind(kind) {
  S.citizenKind = kind;
  $('#cbtn-type-building').classList.toggle('active', kind === 'building');
  $('#cbtn-type-road').classList.toggle('active', kind === 'road');
  $('#cg-asset-label').textContent = kind === 'road' ? 'Road' : 'Government Building';
  const cats = S.publicMeta ? (S.publicMeta.categories[kind] || []) : [];
  $('#cg-category').innerHTML = cats.map(c => `<option value="${esc(c)}">${esc(c)}</option>`).join('');
  populateCitizenAssets();
}
$('#cbtn-type-building').onclick = () => setCitizenKind('building');
$('#cbtn-type-road').onclick = () => setCitizenKind('road');

function populateCitizenAssets() {
  if (!S.publicMeta) return;
  const dist = $('#cg-district').value;
  const isRoad = S.citizenKind === 'road';
  const list = (isRoad ? S.publicMeta.roads : S.publicMeta.buildings).filter(a => a.district === dist);
  const sel = $('#cg-asset');
  if (!list.length) {
    sel.innerHTML = `<option value="">No ${isRoad ? 'roads' : 'buildings'} found in ${esc(dist)}</option>`;
  } else {
    sel.innerHTML = list.map(a => `<option value="${a.id}">${esc(a.name)} (${isRoad ? esc(a.road_class) : esc(a.type)})</option>`).join('');
  }
}

$('#cg-photo').onchange = async () => {
  const f = $('#cg-photo').files[0];
  if (!f) return;
  const fd = new FormData(); fd.append('file', f);
  try {
    const res = await api('/api/public/grievances/photo', {method:'POST', body:fd});
    S.citizenPhotoUrl = res.photo_url;
    $('#cg-photo-img').src = res.photo_url;
    $('#cg-photo-box').classList.remove('hidden');
  } catch (e) {
    toast('Photo upload failed: ' + e.message);
    $('#cg-photo').value = '';
  }
};

$('#citizen-form').onsubmit = async e => {
  e.preventDefault();
  $('#cg-err').textContent = '';
  const submitBtn = $('#cg-submit');
  submitBtn.disabled = true; submitBtn.textContent = 'Submitting grievance…';
  try {
    const assetId = +$('#cg-asset').value;
    if (!assetId) throw new Error(`Please select a valid ${S.citizenKind}.`);
    const payload = {
      kind: S.citizenKind,
      building_id: S.citizenKind === 'building' ? assetId : null,
      road_id: S.citizenKind === 'road' ? assetId : null,
      category: $('#cg-category').value,
      urgency: $('#cg-urgency').value,
      specific_location: $('#cg-location').value.trim(),
      description: $('#cg-desc').value.trim(),
      citizen_name: $('#cg-name').value.trim(),
      citizen_phone: $('#cg-phone').value.trim(),
      citizen_email: $('#cg-email').value.trim() || null,
      photo_url: S.citizenPhotoUrl
    };
    const res = await api('/api/public/grievances', {method:'POST', json:payload});
    const filedPhone = payload.citizen_phone;
    $('#citizen-form').reset();
    S.citizenPhotoUrl = null;
    $('#cg-photo-box').classList.add('hidden');
    $('#cg-success').classList.remove('hidden');
    $('#cg-success').innerHTML = `
      <h3 style="margin:0 0 6px;color:#03543F">Grievance Registered Successfully!</h3>
      <p style="margin:0 0 10px">Your Tracking ID is: <b style="font-size:16px">${esc(res.ticket_id)}</b></p>
      <p style="font-size:13px;color:#046C4E;margin:0 0 14px">Our jurisdictional engineers have been notified. Track progress anytime with this Tracking ID <b>and</b> the mobile number you just entered. Please note the ID down.</p>
      <button class="btn" type="button" id="btn-track-this" data-tid="${esc(res.ticket_id)}">Track Grievance Status Now</button>
    `;
    $('#btn-track-this').onclick = () => {
      $('#ctab-track').click();
      $('#ct-query').value = res.ticket_id;
      $('#ct-phone').value = filedPhone;
      trackGrievance(res.ticket_id, filedPhone);
    };
  } catch (err) {
    $('#cg-err').textContent = err.message;
  } finally {
    submitBtn.disabled = false; submitBtn.textContent = 'Submit Grievance';
  }
};

async function trackGrievance(ticket, phone) {
  const t = (ticket || $('#ct-query').value).trim();
  const p = (phone || $('#ct-phone').value).trim();
  if (!t || !p) { $('#ct-err').textContent = 'Please enter both your Tracking ID and the mobile number you registered with.'; return; }
  $('#ct-err').textContent = '';
  $('#ct-results').innerHTML = '<p style="color:var(--ink-soft)">Searching records…</p>';
  try {
    const g = await api('/api/public/grievances/track', {method:'POST', json:{ticket_id:t, phone:p}});
    $('#ct-results').innerHTML = renderTrackingCard(g);
  } catch (err) {
    $('#ct-err').textContent = err.message;
    $('#ct-results').innerHTML = '';
  }
}
$('#ct-search-btn').onclick = () => trackGrievance();
['ct-query', 'ct-phone'].forEach(id => { $('#' + id).onkeydown = e => { if (e.key === 'Enter') { e.preventDefault(); trackGrievance(); } }; });

function renderTrackingCard(g) {
  const rejected = g.status === 'Rejected';
  const closed = rejected || g.status === 'Resolved';
  const steps = rejected ? ['Submitted', 'Under Review', 'Rejected'] : ['Submitted', 'Under Review', 'Work Order Issued', 'Resolved'];
  let curIdx = 0;
  if (g.status === 'Under Review') curIdx = 1;
  else if (g.status === 'Work Order Issued') curIdx = 2;
  else if (g.status === 'Resolved') curIdx = 3;
  else if (rejected) curIdx = 2;

  const timelineHtml = `
    <div class="timeline">
      ${steps.map((st, i) => {
        const isDone = i < curIdx || (closed && i === curIdx && !rejected);
        const isActive = i === curIdx && !closed;
        const cls = rejected && i === curIdx ? 'rejected' : (isDone ? 'done' : (isActive ? 'active' : ''));
        return `<div class="t-step ${cls}"><div class="t-dot">${cls === 'rejected' ? '✕' : (isDone ? '✓' : (i + 1))}</div><div class="t-label">${esc(st)}</div></div>`;
      }).join('')}
    </div>
  `;

  return `
    <div class="grv-card">
      <div class="grv-card-head">
        <div>
          <span class="grv-card-title">${esc(g.ticket_id)}</span>
          <span class="kindtag ${g.kind}">${g.kind === 'road' ? '🛣️ Road' : '🏢 Building'}</span>
        </div>
        <div>${urgencyChip(g.urgency)} ${statusChip(g.status)}</div>
      </div>
      <div class="grv-meta">
        <b>${esc(g.asset_name)}</b> (${esc(g.district)} district)
        ${g.specific_location ? ' · ' + esc(g.specific_location) : ''}
        <br><small>Reported by ${esc(g.citizen_name)} on ${esc((g.created_at || '').slice(0, 10))}</small>
      </div>
      ${timelineHtml}
      <div class="grv-desc"><b>${esc(g.category)}:</b> ${esc(g.description)}</div>
      ${g.photo_url ? `<a href="${esc(g.photo_url)}" target="_blank" rel="noopener"><img src="${esc(g.photo_url)}" class="preview-img" alt="Defect photo"></a>` : ''}
      ${closed || g.resolution_notes ? `<div class="grv-res ${rejected ? 'rejected' : ''}">
        <b>${closed ? outcomeLine(g) : 'Latest update from the department'}</b>
        ${g.resolution_notes ? `<div style="margin-top:4px"><b>Remarks:</b> ${esc(g.resolution_notes)}</div>`
          : (closed ? '<div style="margin-top:4px"><i>No remarks were recorded.</i></div>' : '')}
      </div>` : ''}
    </div>
  `;
}

/* ---------- start ---------- */
boot().catch(() => {});
