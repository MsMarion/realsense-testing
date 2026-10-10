import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

const $ = (id) => document.getElementById(id);
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const deg = (r) => `${(r * 180 / Math.PI).toFixed(1)}°`;
const fmtDist = (m) => m == null ? '–' : m < 0.1 ? `${(m * 1000).toFixed(1)} mm` : m < 1 ? `${(m * 100).toFixed(1)} cm` : `${m.toFixed(3)} m`;

const cams = new Map();      // serial -> camera card state
let ws = null;
let rates = {};

// ---------------------------------------------------------------- websocket

function connect() {
  ws = new WebSocket(`ws://${location.host}/ws`);
  ws.binaryType = 'arraybuffer';
  ws.onopen = () => {
    setConn(true);
    if (cloud.serial) send({ type: 'points', serial: cloud.serial });
  };
  ws.onclose = () => { setConn(false); setTimeout(connect, 1000); };
  ws.onmessage = onMessage;
}
const send = (obj) => ws && ws.readyState === 1 && ws.send(JSON.stringify(obj));

function setConn(ok) {
  const el = $('conn');
  el.textContent = ok ? 'connected' : 'disconnected';
  el.className = `pill ${ok ? 'good' : 'bad'}`;
}

const decoder = new TextDecoder();
function onMessage(ev) {
  if (typeof ev.data === 'string') {
    const m = JSON.parse(ev.data);
    if (m.type === 'imu') onImu(m.samples);
    else if (m.type === 'stats') onStats(m.rates);
    else if (m.type === 'msg') {
      if (/^cam\/[^/]+\/info$/.test(m.topic)) onInfo(m.header);
      else if (m.topic === 'calib/pair') onPair(m.header);
      else if (/^calib\/[^/]+\/board$/.test(m.topic)) onBoard(m.topic.split('/')[1], m.header);
    }
    return;
  }
  const hl = new DataView(ev.data).getUint32(0, true);
  const hdr = JSON.parse(decoder.decode(new Uint8Array(ev.data, 4, hl)));
  const payload = new Uint8Array(ev.data, 4 + hl);
  const [root, serial, kind] = hdr.topic.split('/');
  if (root === 'cam') {
    const cam = getCam(serial);
    if (kind === 'points') onPoints(serial, hdr, payload);
    else cam.tiles[kind]?.show(payload);
  } else {
    fxTile(hdr.topic).show(payload);
  }
}

// ---------------------------------------------------------------- camera cards

const TILE_DEFS = [
  ['color', 'Color', true],
  ['depth_vis', 'Depth', true],
  ['ir_left', 'IR Left', false],
  ['ir_right', 'IR Right', false],
];

function getCam(serial) {
  let cam = cams.get(serial);
  if (cam) return cam;
  $('noCams')?.remove();
  const el = document.createElement('div');
  el.className = 'cam';
  el.innerHTML = `
    <div class="cam-head"><span class="badge">…</span><span class="name">${serial}</span><div class="facts"></div></div>
    <div class="tiles"></div>`;
  $('cameras').append(el);
  cam = { serial, el, tiles: {}, info: null, board: null, measure: { pts: [], distance: null, hover: null, hoverXYZ: null, busy: false } };
  const tilesEl = el.querySelector('.tiles');
  for (const [kind, label, measurable] of TILE_DEFS) cam.tiles[kind] = makeTile(tilesEl, label, measurable ? cam : null);
  cams.set(serial, cam);
  refreshSelectors();
  return cam;
}

function makeTile(parent, label, measureCam) {
  const el = document.createElement('div');
  el.className = 'tile' + (measureCam ? ' measure' : '');
  el.innerHTML = `<img alt="${label}"><canvas class="overlay"></canvas>
    <span class="label">${label}</span><span class="rate"></span>
    <div class="wait">waiting for stream…</div><span class="readout" hidden></span>`;
  parent.append(el);
  const img = el.querySelector('img');
  let url = null, pending = false;
  const tile = {
    el, img,
    canvas: el.querySelector('canvas'),
    rate: el.querySelector('.rate'),
    readout: el.querySelector('.readout'),
    show(bytes) {
      if (pending) return;  // still decoding the previous frame -> skip this one
      pending = true;
      const u = URL.createObjectURL(new Blob([bytes], { type: 'image/jpeg' }));
      img.onload = () => {
        if (url) URL.revokeObjectURL(url);
        url = u; pending = false;
        el.querySelector('.wait')?.remove();
      };
      img.onerror = () => { URL.revokeObjectURL(u); pending = false; };
      img.src = u;
    },
  };
  if (measureCam) attachMeasure(measureCam, tile);
  return tile;
}

function onInfo(h) {
  const cam = getCam(h.serial);
  const first = !cam.info;
  cam.info = h;
  cam.el.querySelector('.badge').textContent = h.model;
  cam.el.querySelector('.name').textContent = h.name;
  const usbWarn = h.usb && !h.usb.startsWith('3') ? ' style="color:var(--warn)"' : '';
  const facts = [
    ['serial', h.serial],
    ['FW', h.firmware],
    [`USB`, `<span${usbWarn}>${h.usb}${usbWarn ? ' (reduced modes)' : ''}</span>`],
    h.depth_profile && ['depth', `${h.depth_profile[0]}×${h.depth_profile[1]}@${h.depth_profile[2]}`],
    ['fps', h.fps?.toFixed(1)],
    ['center', fmtDist(h.center_m)],
    h.asic_temp != null && ['ASIC', `${h.asic_temp.toFixed(1)}°C`],
    h.projector_temp != null && ['projector', `${h.projector_temp.toFixed(1)}°C`],
    h.exposure != null && ['exposure', `${h.exposure} µs`],
    h.gain != null && ['gain', h.gain],
    h.laser_power != null && ['laser', `${h.laser_power.toFixed(0)} mW${h.emitter ? '' : ' (off)'}`],
    h.imu && ['IMU', `${h.imu_hz.toFixed(0)} Hz`],
  ].filter(Boolean);
  cam.el.querySelector('.facts').innerHTML = facts.map(([k, v]) => `<span>${k} <b>${v}</b></span>`).join('');
  for (const [kind] of TILE_DEFS) {
    const streaming = kind === 'depth_vis' || h.streams.includes(kind);
    cam.tiles[kind].el.hidden = !streaming;
  }
  if (first) refreshSelectors();
}

// ---------------------------------------------------------------- click-to-measure

function contentRect(img) {
  const W = img.clientWidth, H = img.clientHeight;
  const iw = img.naturalWidth || 16, ih = img.naturalHeight || 9;
  const s = Math.min(W / iw, H / ih);
  return { x: (W - iw * s) / 2, y: (H - ih * s) / 2, w: iw * s, h: ih * s };
}

function toNorm(tile, ev) {
  const r = contentRect(tile.img);
  const b = tile.canvas.getBoundingClientRect();
  const x = (ev.clientX - b.left - r.x) / r.w;
  const y = (ev.clientY - b.top - r.y) / r.h;
  return x < 0 || x > 1 || y < 0 || y > 1 ? null : [x, y];
}

async function probe(serial, points) {
  const res = await fetch('/api/probe', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ serial, points }),
  });
  return res.json();
}

function attachMeasure(cam, tile) {
  const m = cam.measure;
  tile.canvas.addEventListener('mousemove', (ev) => {
    m.hover = toNorm(tile, ev);
    hoverProbe(cam);
    drawOverlay(cam);
  });
  tile.canvas.addEventListener('mouseleave', () => { m.hover = null; m.hoverXYZ = null; drawOverlay(cam); });
  tile.canvas.addEventListener('click', async (ev) => {
    const p = toNorm(tile, ev);
    if (!p) return;
    if (m.pts.length >= 2) { m.pts = []; m.distance = null; }
    m.pts.push({ p, xyz: null });
    const pts = m.pts;
    drawOverlay(cam);
    const r = await probe(cam.serial, pts.map((q) => q.p));
    if (pts !== m.pts || !r.points) return;  // cleared meanwhile
    r.points.forEach((xyz, i) => { pts[i].xyz = xyz; });
    m.distance = pts.length === 2 ? r.distance ?? null : null;
    drawOverlay(cam);
  });
  tile.canvas.addEventListener('contextmenu', (ev) => { ev.preventDefault(); clearMeasure(cam); });
}

function clearMeasure(cam) {
  cam.measure.pts = [];
  cam.measure.distance = null;
  drawOverlay(cam);
}

async function hoverProbe(cam) {
  const m = cam.measure;
  if (m.busy || !m.hover) return;
  m.busy = true;
  try {
    const r = await probe(cam.serial, [m.hover]);
    m.hoverXYZ = m.hover ? r.points?.[0] ?? null : null;
    drawOverlay(cam);
  } finally {
    setTimeout(() => { m.busy = false; }, 80);
  }
}

function drawOverlay(cam) {
  const m = cam.measure;
  for (const kind of ['color', 'depth_vis']) {
    const tile = cam.tiles[kind];
    const c = tile.canvas, dpr = devicePixelRatio || 1;
    c.width = c.clientWidth * dpr;
    c.height = c.clientHeight * dpr;
    const g = c.getContext('2d');
    g.scale(dpr, dpr);
    const r = contentRect(tile.img);
    const P = ([x, y]) => [r.x + x * r.w, r.y + y * r.h];

    if (m.hover) {
      const [hx, hy] = P(m.hover);
      g.strokeStyle = 'rgba(255,255,255,.7)';
      g.lineWidth = 1;
      g.beginPath();
      g.moveTo(hx - 10, hy); g.lineTo(hx + 10, hy);
      g.moveTo(hx, hy - 10); g.lineTo(hx, hy + 10);
      g.stroke();
    }
    if (m.pts.length === 2) {
      const [a, b] = m.pts.map((q) => P(q.p));
      g.strokeStyle = '#4cc2ff';
      g.lineWidth = 2.5;
      g.beginPath(); g.moveTo(...a); g.lineTo(...b); g.stroke();
      if (m.distance != null) {
        const text = fmtDist(m.distance);
        g.font = '600 14px system-ui, sans-serif';
        const tw = g.measureText(text).width;
        const mx = (a[0] + b[0]) / 2, my = (a[1] + b[1]) / 2 - 14;
        g.fillStyle = 'rgba(0,0,0,.75)';
        g.fillRect(mx - tw / 2 - 6, my - 12, tw + 12, 22);
        g.fillStyle = '#fff';
        g.fillText(text, mx - tw / 2, my + 4);
      }
    }
    m.pts.forEach((q, i) => {
      const [x, y] = P(q.p);
      g.fillStyle = q.xyz ? '#4cc2ff' : '#ff5d5d';
      g.strokeStyle = '#000';
      g.lineWidth = 2;
      g.beginPath(); g.arc(x, y, 5, 0, Math.PI * 2); g.fill(); g.stroke();
      g.fillStyle = '#fff';
      g.font = '600 11px system-ui, sans-serif';
      g.fillText('AB'[i], x + 8, y - 6);
    });

    const bd = cam.board;
    if (bd?.found && Date.now() / 1000 - bd.grab_ts < 0.5) {
      g.fillStyle = '#3ccf7e';
      for (const p of bd.px) { const [x, y] = P(p); g.fillRect(x - 2, y - 2, 4, 4); }
      const [o, ...ends] = bd.axes.map(P);
      ['#ff6b6b', '#51d88a', '#4cc2ff'].forEach((color, i) => {
        g.strokeStyle = color; g.lineWidth = 3;
        g.beginPath(); g.moveTo(...o); g.lineTo(...ends[i]); g.stroke();
      });
    }

    const parts = [];
    if (m.hover) parts.push(m.hoverXYZ ? `z ${fmtDist(m.hoverXYZ[2])}` : 'z: no depth');
    if (m.pts.length === 2) parts.push(m.distance != null ? `A→B ${fmtDist(m.distance)}` : 'A→B: no depth at a point');
    else if (m.pts.length === 1) parts.push('click point B');
    tile.readout.hidden = !parts.length;
    tile.readout.textContent = parts.join('  ·  ');
  }
}

addEventListener('keydown', (ev) => { if (ev.key === 'Escape') cams.forEach(clearMeasure); });
addEventListener('resize', () => cams.forEach(drawOverlay));

// ---------------------------------------------------------------- stats table

function onStats(r) {
  rates = r;
  let hz = 0, kb = 0;
  const rows = Object.keys(r).sort().map((t) => {
    const s = r[t];
    hz += s.hz; kb += s.kbps;
    return `<tr><td>${t}</td><td>${s.hz.toFixed(1)}</td><td>${s.kbps.toLocaleString()}</td><td>${s.lat_ms ?? '–'}</td></tr>`;
  });
  document.querySelector('#topics tbody').innerHTML = rows.join('');
  $('busSummary').textContent = `${Object.keys(r).length} topics · ${hz.toFixed(0)} msg/s · ${(kb / 1024).toFixed(1)} MB/s`;
  for (const cam of cams.values()) {
    for (const [kind, tile] of Object.entries(cam.tiles)) {
      const s = r[`cam/${cam.serial}/${kind}`];
      tile.rate.textContent = s ? `${s.hz.toFixed(0)} Hz · ${s.lat_ms} ms` : '';
    }
  }
  for (const [topic, tile] of fxTiles) {
    const s = r[topic];
    tile.rate.textContent = s ? `${s.hz.toFixed(0)} Hz` : '';
  }
}

// ---------------------------------------------------------------- fx (plugin) tiles

const fxTiles = new Map();
function fxTile(topic) {
  let t = fxTiles.get(topic);
  if (!t) {
    $('fxPanel').hidden = false;
    t = makeTile($('fxTiles'), topic, null);
    fxTiles.set(topic, t);
  }
  return t;
}

// ---------------------------------------------------------------- selectors

const camLabel = (s) => `${cams.get(s)?.info?.model ?? ''} ${s}`.trim();

function refreshSelectors() {
  const fill = (sel, options) => {
    const cur = sel.value;
    sel.innerHTML = options.map(([v, label]) => `<option value="${v}">${label}</option>`).join('');
    if (options.some(([v]) => v === cur)) sel.value = cur;
  };
  const camOpts = [...cams.keys()].map((s) => [s, camLabel(s)]);
  fill($('cloudCam'), cams.size > 1 ? [...camOpts, ['all', 'Both (fused)']] : camOpts);
  if (!cloud.serial && cams.size) setCloudCam($('cloudCam').value);
  const imuSerials = [...cams.values()].filter((c) => c.info?.imu || imuStates.has(c.serial)).map((c) => c.serial);
  fill($('imuCam'), imuSerials.map((s) => [s, camLabel(s)]));
}

// ---------------------------------------------------------------- point cloud

const MAX_POINTS = 80000;
const F16 = new Float32Array(65536);
for (let h = 0; h < 65536; h++) {
  const s = h & 0x8000 ? -1 : 1, e = (h >> 10) & 0x1f, f = h & 0x3ff;
  F16[h] = e === 0 ? s * 2 ** -14 * (f / 1024) : e === 31 ? (f ? NaN : s * Infinity) : s * 2 ** (e - 15) * (1 + f / 1024);
}

// camera frame (x right, y down, z forward) -> three.js (y up, z toward viewer): flip y and z
const FLIP = new THREE.Matrix4().makeScale(1, -1, -1);
function camPoseToThree(T) {
  // T maps camera-B points into camera-A's frame; in three.js space that is FLIP * T * FLIP
  const m = new THREE.Matrix4().fromArray(T).transpose();  // T is row-major
  return FLIP.clone().multiply(m).multiply(FLIP);
}

const cloud = (() => {
  const el = $('cloud');
  const renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(devicePixelRatio);
  el.append(renderer.domElement);
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(60, 1, 0.001, 50);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  const material = new THREE.PointsMaterial({ size: 2, sizeAttenuation: false, vertexColors: true });

  const resize = () => {
    const w = el.clientWidth, h = el.clientHeight;
    renderer.setSize(w, h);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
  };
  new ResizeObserver(resize).observe(el);
  resize();

  const state = { serial: null, fitted: false, layers: new Map(), renderer, scene, camera, controls, material };
  state.fit = (depth) => {
    controls.target.set(0, 0, -depth);
    camera.position.set(depth * 0.35, depth * 0.25, depth * 0.35);
    controls.update();
  };
  return state;
})();

// one layer (points + a small axes marker for the camera) per camera
function cloudLayer(serial) {
  let L = cloud.layers.get(serial);
  if (L) return L;
  const geom = new THREE.BufferGeometry();
  const pos = new THREE.BufferAttribute(new Float32Array(MAX_POINTS * 3), 3).setUsage(THREE.DynamicDrawUsage);
  const col = new THREE.BufferAttribute(new Uint8Array(MAX_POINTS * 3), 3, true).setUsage(THREE.DynamicDrawUsage);
  geom.setAttribute('position', pos);
  geom.setAttribute('color', col);
  geom.setDrawRange(0, 0);
  const points = new THREE.Points(geom, cloud.material);
  points.frustumCulled = false;
  const group = new THREE.Group();
  group.add(points, new THREE.AxesHelper(0.05));
  group.matrixAutoUpdate = false;
  cloud.scene.add(group);
  L = { serial, geom, pos, col, group, n: 0 };
  cloud.layers.set(serial, L);
  return L;
}

// Pose of `serial` in the fused view: identity for the reference camera,
// saved calibration (or the live board estimate) for the other one.
function fusedPose(serial) {
  const saved = calib.saved;
  if (saved && saved.ref === serial) return { T: null, src: 'reference' };
  if (saved && saved.other === serial) return { T: saved.T_ref_other, src: 'saved calibration' };
  const live = calib.live;
  if (live && Date.now() / 1000 - live.ts < 1) {
    if (live.ref === serial) return { T: null, src: 'reference' };
    if (live.other === serial) return { T: live.T, src: 'live board' };
  }
  if (!saved && !live && cams.size && [...cams.keys()].sort()[0] === serial) return { T: null, src: 'reference' };
  return null;
}

function updateLayerVisibility() {
  for (const L of cloud.layers.values()) {
    if (cloud.serial === 'all') {
      const p = fusedPose(L.serial);
      L.group.visible = !!p;
      L.group.matrix.copy(p?.T ? camPoseToThree(p.T) : new THREE.Matrix4());
    } else {
      L.group.visible = L.serial === cloud.serial;
      L.group.matrix.identity();
    }
    L.group.matrixWorldNeedsUpdate = true;
  }
}

function setCloudCam(serial) {
  cloud.serial = serial;
  cloud.fitted = false;
  for (const L of cloud.layers.values()) L.geom.setDrawRange(0, 0);
  send({ type: 'points', serial });
}

function onPoints(serial, hdr, payload) {
  if (cloud.serial !== 'all' && serial !== cloud.serial) return;
  const L = cloudLayer(serial);
  const n = Math.min(hdr.n, MAX_POINTS);
  const xyz = new Uint16Array(payload.slice(0, n * 6).buffer);
  const P = L.pos.array;
  const zs = [];
  for (let i = 0; i < n; i++) {
    P[i * 3] = F16[xyz[i * 3]];
    P[i * 3 + 1] = -F16[xyz[i * 3 + 1]];
    P[i * 3 + 2] = -F16[xyz[i * 3 + 2]];
    if (!cloud.fitted && i % 50 === 0) zs.push(-P[i * 3 + 2]);
  }
  L.col.array.set(payload.subarray(hdr.n * 6, hdr.n * 6 + n * 3));
  L.pos.needsUpdate = true;
  L.col.needsUpdate = true;
  L.geom.setDrawRange(0, n);
  L.n = n;
  updateLayerVisibility();
  if (!cloud.fitted && zs.length && (cloud.serial !== 'all' || !fusedPose(serial)?.T)) {
    zs.sort((a, b) => a - b);
    cloud.medianZ = zs[zs.length >> 1];
    cloud.fit(cloud.medianZ);
    cloud.fitted = true;
  }
  const shown = [...cloud.layers.values()].filter((l) => l.group.visible);
  let text = `${shown.reduce((a, l) => a + l.n, 0).toLocaleString()} points`;
  if (cloud.serial === 'all') {
    const other = [...cams.keys()].find((s) => fusedPose(s)?.T);
    text += other ? ` · ${camLabel(other)} placed by ${fusedPose(other).src}` : ' · second camera hidden until calibrated (or board seen by both)';
  }
  $('cloudStats').textContent = text;
}

$('cloudCam').addEventListener('change', (e) => setCloudCam(e.target.value));
$('cloudReset').addEventListener('click', () => { cloud.fitted = false; });
$('ptSize').addEventListener('input', (e) => { cloud.material.size = +e.target.value; });

// ---------------------------------------------------------------- camera-to-camera calibration

const calib = { live: null, saved: null, history: [], boards: {} };

const rowMajor = (T, r, c) => T[r * 4 + c];
function eulerDeg(T) {
  const d = 180 / Math.PI;
  return [Math.atan2(rowMajor(T, 2, 1), rowMajor(T, 2, 2)) * d,
    Math.asin(-Math.max(-1, Math.min(1, rowMajor(T, 2, 0)))) * d,
    Math.atan2(rowMajor(T, 1, 0), rowMajor(T, 0, 0)) * d];
}
function poseHtml(T, extra = '') {
  const t = [3, 7, 11].map((i) => T[i] * 1000);
  const r = eulerDeg(T);
  return `<div><span class="muted">x / y / z</span> <b>${t[0].toFixed(1)}</b> <b>${t[1].toFixed(1)}</b> <b>${t[2].toFixed(1)}</b> mm
    <span class="muted">· distance</span> <b>${Math.hypot(...t).toFixed(1)}</b> mm</div>
    <div><span class="muted">rot x / y / z</span> <b>${r[0].toFixed(2)}°</b> <b>${r[1].toFixed(2)}°</b> <b>${r[2].toFixed(2)}°</b></div>${extra}`;
}

function onBoard(serial, h) {
  const cam = cams.get(serial);
  if (cam) { cam.board = h; drawOverlay(cam); }
  calib.boards[serial] = h;
  document.querySelector('#boardTable tbody').innerHTML = Object.entries(calib.boards).map(([s, b]) => {
    const fresh = Date.now() / 1000 - b.grab_ts < 1;
    const status = !fresh ? '<span class="no">stale</span>'
      : b.found ? '<span class="ok">● tracking</span>'
        : b.markers ? `<span class="warn">${b.markers} markers, too few corners</span>` : '<span class="no">not seen</span>';
    const ok = fresh && b.found;
    const errCls = ok && b.err_px > 1 ? 'warn' : '';
    return `<tr><td>${camLabel(s)}</td><td>${status}</td><td>${ok ? b.corners : '–'}</td>
      <td>${ok ? fmtDist(b.dist_m) : '–'}</td><td class="${errCls}">${ok ? b.err_px.toFixed(2) + ' px' : '–'}</td></tr>`;
  }).join('');
}

function onPair(h) {
  calib.live = h;
  calib.history.push(h.T);
  if (calib.history.length > 20) calib.history.shift();
  // jitter = std of the translation over recent detections; low = board is still & detections agree
  const ts = calib.history.map((T) => [T[3], T[7], T[11]]);
  const mean = [0, 1, 2].map((i) => ts.reduce((a, t) => a + t[i], 0) / ts.length);
  const jitter = Math.sqrt(ts.reduce((a, t) => a + (t[0] - mean[0]) ** 2 + (t[1] - mean[1]) ** 2 + (t[2] - mean[2]) ** 2, 0) / ts.length) * 1000;
  $('pairTitle').textContent = `${camLabel(h.other)} relative to ${camLabel(h.ref)} (live)`;
  $('pairLive').className = 'pose';
  $('pairLive').innerHTML = poseHtml(h.T,
    `<div><span class="muted">jitter</span> <b class="${jitter > 2 ? 'warn' : 'ok'}">${jitter.toFixed(2)}</b> mm ${jitter > 2 ? '(hold the board still)' : ''}</div>`);
}

setInterval(() => {
  if (calib.live && Date.now() / 1000 - calib.live.ts > 1) {
    calib.live = null;
    calib.history = [];
    $('pairLive').className = 'pose muted';
    $('pairLive').textContent = 'Board not seen by both cameras.';
  }
}, 500);

function showCalib(saved, samples) {
  if (samples != null) $('sampleCount').textContent = `${samples} sample${samples === 1 ? '' : 's'}`;
  if (saved === undefined) return;
  calib.saved = saved;
  $('calibResult').innerHTML = !saved ? '<span class="muted">No saved calibration yet.</span>'
    : saved.error ? `<span class="warn">${saved.error}</span>`
      : `<h3>Saved: ${camLabel(saved.other)} in ${camLabel(saved.ref)} frame · ${saved.created}</h3>` +
        poseHtml(saved.T_ref_other,
          `<div><span class="muted">spread over ${saved.samples} samples</span> <b>${saved.spread_mm.mean}</b> mm avg / ${saved.spread_mm.max} max ·
           <b>${saved.spread_deg.mean}°</b> avg</div>`);
  updateLayerVisibility();
}

async function calibPost(path) {
  return (await fetch(path, { method: 'POST' })).json();
}
$('capBtn').addEventListener('click', async () => {
  const r = await calibPost('/api/calib/capture');
  showCalib(undefined, r.samples);
  if (r.error) $('sampleCount').textContent = `${r.samples} samples · ${r.error}`;
});
$('resetBtn').addEventListener('click', async () => showCalib(undefined, (await calibPost('/api/calib/reset')).samples));
$('solveBtn').addEventListener('click', async () => {
  const r = await calibPost('/api/calib/solve');
  if (r.error) $('calibResult').innerHTML = `<span class="warn">${r.error}</span>`;
  else showCalib(r);
});
fetch('/api/calib/state').then((r) => r.json()).then((s) => showCalib(s.saved, s.samples));

// ---------------------------------------------------------------- IMU

const imuStates = new Map();
const CHART_LEN = 300;
const CHART_DECIMATE = 4;   // ~200 Hz gyro -> ~50 Hz on the chart

function imuState(serial) {
  let s = imuStates.get(serial);
  if (!s) {
    s = { roll: 0, pitch: 0, yaw: 0, last: null, n: 0, accel: [0, 0, 0] };
    imuStates.set(serial, s);
    refreshSelectors();
    $('imuEmpty').hidden = true;
    $('imuBody').hidden = false;
  }
  return s;
}

function makeChart(id, title, unit, range) {
  const colors = [css('--x'), css('--y'), css('--z')];
  return new Chart($(id), {
    type: 'line',
    data: {
      labels: Array(CHART_LEN).fill(''),
      datasets: ['x', 'y', 'z'].map((a, i) => ({
        label: a, data: Array(CHART_LEN).fill(null), borderColor: colors[i],
        borderWidth: 1.5, pointRadius: 0, tension: 0,
      })),
    },
    options: {
      animation: false, responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { labels: { color: css('--muted'), boxWidth: 10 } },
        title: { display: true, text: `${title} (${unit})`, color: css('--text'), align: 'start' },
      },
      scales: {
        x: { display: false },
        y: { suggestedMin: -range, suggestedMax: range, ticks: { color: css('--muted') }, grid: { color: css('--line') } },
      },
    },
  });
}
const accelChart = makeChart('accelChart', 'Accelerometer', 'm/s²', 10);
const gyroChart = makeChart('gyroChart', 'Gyroscope', 'rad/s', 1);
let chartsDirty = false;

function pushChart(chart, v) {
  chart.data.datasets.forEach((ds, i) => { ds.data.push(v[i]); ds.data.shift(); });
}

function onImu(samples) {
  const selected = $('imuCam').value;
  for (const smp of samples) {
    const s = imuState(smp.serial);
    const [ax, ay, az] = smp.accel;
    const [gx, gy, gz] = smp.gyro;
    // gravity reads as "up" in the camera frame (x right, y down, z forward)
    const accRoll = Math.atan2(ax, -ay);
    const accPitch = Math.atan2(az, Math.hypot(ax, ay));
    if (s.last == null) {
      s.roll = accRoll; s.pitch = accPitch;
    } else {
      let dt = (smp.cam_ts - s.last) / 1000;
      if (dt < 0 || dt > 0.1) dt = 0;
      const k = 0.98;  // complementary filter: trust gyro short-term, accel long-term
      s.roll = k * (s.roll - gz * dt) + (1 - k) * accRoll;
      s.pitch = k * (s.pitch + gx * dt) + (1 - k) * accPitch;
      if (Math.abs(gy) > 0.015) s.yaw += gy * dt;  // deadband: yaw has no accel reference, so ignore gyro bias
    }
    s.last = smp.cam_ts;
    s.accel = smp.accel;
    if (smp.serial === selected && s.n++ % CHART_DECIMATE === 0) {
      pushChart(accelChart, smp.accel);
      pushChart(gyroChart, smp.gyro);
      chartsDirty = true;
    }
  }
}

$('imuCam').addEventListener('change', () => {
  for (const ch of [accelChart, gyroChart]) ch.data.datasets.forEach((ds) => ds.data.fill(null));
});
$('yawReset').addEventListener('click', () => {
  const s = imuStates.get($('imuCam').value);
  if (s) s.yaw = 0;
});

const cube = (() => {
  const el = $('cube');
  const renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(devicePixelRatio);
  el.append(renderer.domElement);
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(40, 1, 0.1, 20);
  camera.position.set(1.0, 0.7, 1.6);
  camera.lookAt(0, 0, 0);
  scene.add(new THREE.AmbientLight(0xffffff, 0.6));
  const sun = new THREE.DirectionalLight(0xffffff, 1.6);
  sun.position.set(1, 2, 2);
  scene.add(sun);

  // a D435-shaped bar: lenses on the front (-z) face
  const body = new THREE.Group();
  body.add(new THREE.Mesh(new THREE.BoxGeometry(0.9, 0.25, 0.25),
    new THREE.MeshStandardMaterial({ color: 0x9aa3ad, metalness: 0.4, roughness: 0.5 })));
  const face = new THREE.Mesh(new THREE.PlaneGeometry(0.86, 0.21), new THREE.MeshStandardMaterial({ color: 0x111418 }));
  face.position.z = -0.126;
  face.rotation.y = Math.PI;
  body.add(face);
  const lensMat = new THREE.MeshStandardMaterial({ color: 0x223344, metalness: 0.8, roughness: 0.2 });
  for (const x of [-0.33, -0.1, 0.12, 0.33]) {
    const lens = new THREE.Mesh(new THREE.CylinderGeometry(0.045, 0.045, 0.02, 24), lensMat);
    lens.rotation.x = Math.PI / 2;
    lens.position.set(x, 0, -0.13);
    body.add(lens);
  }
  body.add(new THREE.ArrowHelper(new THREE.Vector3(0, 0, -1), new THREE.Vector3(0, 0, -0.13), 0.35, 0x4cc2ff, 0.08, 0.05));
  scene.add(body);
  scene.add(new THREE.GridHelper(2, 10, 0x2a313b, 0x1e232b).translateY(-0.4));

  const resize = () => {
    renderer.setSize(el.clientWidth, el.clientHeight);
    camera.aspect = el.clientWidth / el.clientHeight;
    camera.updateProjectionMatrix();
  };
  new ResizeObserver(resize).observe(el);
  resize();
  return { renderer, scene, camera, body };
})();

function drawBubble(roll, pitch) {
  const c = $('bubble'), g = c.getContext('2d');
  const W = c.width, R = W / 2 - 6, cx = W / 2, cy = W / 2, max = 30 * Math.PI / 180;
  g.clearRect(0, 0, W, W);
  g.strokeStyle = css('--line'); g.lineWidth = 2;
  g.beginPath(); g.arc(cx, cy, R, 0, Math.PI * 2); g.stroke();
  g.beginPath(); g.arc(cx, cy, 12, 0, Math.PI * 2); g.stroke();
  g.beginPath(); g.moveTo(cx - R, cy); g.lineTo(cx + R, cy); g.moveTo(cx, cy - R); g.lineTo(cx, cy + R); g.stroke();
  // bubble floats to the high side
  let bx = roll / max, by = -pitch / max;
  const len = Math.hypot(bx, by);
  if (len > 1) { bx /= len; by /= len; }
  const level = Math.abs(roll) < 0.0175 && Math.abs(pitch) < 0.0175;
  g.fillStyle = level ? css('--good') : css('--warn');
  g.beginPath(); g.arc(cx + bx * (R - 10), cy + by * (R - 10), 10, 0, Math.PI * 2); g.fill();
}

// ---------------------------------------------------------------- render loop

function frame() {
  cloud.controls.update();
  if (!cloud.fitted && cloud.medianZ && [...cloud.layers.values()].some((l) => l.n)) { cloud.fit(cloud.medianZ); cloud.fitted = true; }
  cloud.renderer.render(cloud.scene, cloud.camera);

  const s = imuStates.get($('imuCam').value);
  if (s) {
    cube.body.rotation.set(s.pitch, -s.yaw, s.roll, 'YXZ');
    cube.renderer.render(cube.scene, cube.camera);
    $('roll').textContent = deg(s.roll);
    $('pitch').textContent = deg(s.pitch);
    $('yaw').textContent = deg(s.yaw);
    $('amag').textContent = `${Math.hypot(...s.accel).toFixed(2)} m/s²`;
    drawBubble(s.roll, s.pitch);
  }
  if (chartsDirty) {
    accelChart.update('none');
    gyroChart.update('none');
    chartsDirty = false;
  }
  requestAnimationFrame(frame);
}

connect();
requestAnimationFrame(frame);
