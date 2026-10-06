// Competitor Hunter front end. No framework, no build step.
// All scraped text (titles, names) is written with textContent, never innerHTML.

const TAU = Math.PI * 2;
const $ = (id) => document.getElementById(id);
const PAGE = 12;
const PHASES = ['scrape', 'pilot', 'judge', 'rank'];
const STAGE_TAG = { fable: 'Claude', scrape: 'Collect', pilot: 'Tune', judge: 'Jev', rank: 'Rank', system: 'System' };
const STATUS = { scrape: 'Collecting', pilot: 'Tuning Jev', judge: 'Judging', rank: 'Ranking' };

// Result filters. Everything in DEFAULT_FILTER persists in this browser; competitor and shown are per-view.
const FILTER_KEY = 'competitor-hunter.filter';
const DEFAULT_FILTER = { format: 'all', days: 0, from: '', to: '', breakout: 0, lane: 'all', hideOff: true, hook: 'all', minScore: 0, minViews: 0, news: false, sure: false, sort: 'score' };
function loadFilter() {
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(FILTER_KEY) || '{}') || {}; } catch (err) { saved = {}; }
  const f = { ...DEFAULT_FILTER };
  for (const k of Object.keys(DEFAULT_FILTER)) if (k in saved && typeof saved[k] === typeof DEFAULT_FILTER[k]) f[k] = saved[k];
  return { ...f, competitor: null, shown: PAGE };
}
function saveFilter() {
  const out = {};
  for (const k of Object.keys(DEFAULT_FILTER)) out[k] = app.filter[k];
  try { localStorage.setItem(FILTER_KEY, JSON.stringify(out)); } catch (err) { /* private window: filters just do not persist */ }
}
function passes(p, f) {
  const j = p.jev || {};
  if (f.format !== 'all' && p.format !== f.format) return false;
  if (f.from || f.to) {                       // a custom range wins over the quick "last N days"
    const day = (p.published || '').slice(0, 10);
    if (f.from && day < f.from) return false;
    if (f.to && day > f.to) return false;
  } else if (f.days && p.age_days > f.days) return false;
  if (f.breakout && !(p.breakout >= f.breakout)) return false;
  if (f.lane !== 'all' ? j.topic_lane !== f.lane : (f.hideOff && j.topic_lane === 'off_niche')) return false;
  if (f.hook !== 'all' && j.hook_type !== f.hook) return false;
  if (f.minScore && p.score < f.minScore) return false;
  if (f.minViews && !(p.views >= f.minViews)) return false;   // carousels have no view count and drop out here
  if (f.news && !(j.newsjack >= 0.6)) return false;
  if (f.sure && !j.sure) return false;
  return true;
}
const SORTS = {
  score: (a, b) => b.score - a.score, breakout: (a, b) => (b.breakout || 0) - (a.breakout || 0),
  views: (a, b) => (b.views || 0) - (a.views || 0), newest: (a, b) => a.age_days - b.age_days,
};
function filteredPieces() {
  const f = app.filter;
  if (!app.result) return [];
  return app.result.pieces.filter((p) => passes(p, f) && (!f.competitor || keyOf(p) === f.competitor)).sort(SORTS[f.sort] || SORTS.score);
}

const app = {
  roster: [], result: null, running: false, source: null, startedAt: 0, timer: null,
  acquired: 0, pieces: 0,
  filter: loadFilter(),
  hot: null, settings: { model: 'fable' }, models: {}, ticker: [],
};
const modelLabel = (id) => app.models[id] || id;

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------
function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'text') el.textContent = v;
    else if (k === 'vars') for (const [name, value] of Object.entries(v)) el.style.setProperty(name, value);
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const child of children.flat()) if (child != null) el.append(child);
  return el;
}
// replaceChildren(null) writes the text "null"; drop empty slots first.
const fill = (el, ...nodes) => el.replaceChildren(...nodes.flat().filter((n) => n != null));
const compact = new Intl.NumberFormat('en', { notation: 'compact', maximumFractionDigits: 1 });
const num = (n) => (n == null ? 'n/a' : compact.format(n));
const pct = (n, digits = 2) => (n == null ? 'n/a' : n.toFixed(digits) + '%');
const keyOf = (c) => `${c.platform}|${c.name || c.competitor}`;
const platTag = (p) => (p === 'youtube' ? 'YT' : 'IG');
const words = (s) => String(s).replace(/_/g, ' ');
const safeLink = (u) => (/^https:\/\/(www\.)?(youtube\.com|instagram\.com)\//.test(u || '') ? u : null);
const safeThumb = (u) => (u && (u.startsWith('https://i.ytimg.com/') || u.startsWith('/runs/')) ? u : null);
const blankThumb = (p) => h('span', { class: 'blank' },
  h('b', { text: ((p.competitor || p.handle || '?').split('|')[0].trim()[0] || '?').toUpperCase() }), h('small', { text: p.format }));
const duration = (s) => (s >= 60 ? `${Math.floor(s / 60)}m ${String(Math.round(s % 60)).padStart(2, '0')}s` : `${Math.round(s)}s`);

async function getJSON(url, options) {
  const res = await fetch(url, options);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

// ---------------------------------------------------------------------------
// Threat map: every competitor on one X/Y graph.
//   X = breakout   mean of their 3 best pieces, each as a multiple of their own median
//   Y = virality   organic views in the window          dot size = engagement rate
// Both axes are log: the field spans orders of magnitude. Top right = big and spiking.
// Competitors with no data yet hold formation (before a hunt) or wait in the dock under the plot.
// ---------------------------------------------------------------------------
const radar = {
  canvas: $('radar'), ctx: $('radar').getContext('2d'), w: 0, h: 0, blips: [], motes: [], axes: null, docked: 0, scan: 0, last: 0,
  animate: !matchMedia('(prefers-reduced-motion: reduce)').matches && innerWidth > 680,
};
const plotBox = () => ({ l: 58, t: 34, r: radar.w - 18, b: radar.h - 84 });
function readTheme() {
  const css = getComputedStyle(document.documentElement);
  radar.rgb = css.getPropertyValue('--map-accent-rgb').trim() || '41, 151, 255';
  radar.ink = css.getPropertyValue('--map-ink-rgb').trim() || '255, 255, 255';
  radar.surface = css.getPropertyValue('--map-surface').trim() || '#272729';
}
const middle = (list) => [...list].sort((x, y) => x - y)[Math.floor(list.length / 2)];

function logAxis(values) {
  const lo = Math.log10(Math.min(...values)), hi = Math.log10(Math.max(...values));
  const pad = Math.max(0.06, (hi - lo) * 0.09), from = lo - pad, to = hi + pad;
  let ticks = [];
  for (let e = Math.floor(from); e <= Math.ceil(to); e++) {
    for (const m of [1, 2, 5]) if (Math.log10(m) + e >= from && Math.log10(m) + e <= to) ticks.push({ value: m * 10 ** e, m });
  }
  if (ticks.length > 8) ticks = ticks.filter((tick) => tick.m === 1);
  return { at: (v) => (Math.log10(v) - from) / (to - from), ticks: ticks.map((tick) => tick.value) };
}

function setBlips(roster, result) {
  // Only pieces that pass the current filters count, so the map and the cards always show the same field.
  const stats = new Map();
  for (const p of result ? result.pieces.filter((piece) => passes(piece, app.filter)) : []) {
    if (!stats.has(keyOf(p))) stats.set(keyOf(p), { pieces: 0, views: 0, breakouts: [] });
    const s = stats.get(keyOf(p));
    s.pieces += 1; s.views += p.views || 0;
    if (p.breakout != null) s.breakouts.push(p.breakout);
  }
  const data = new Map((result ? result.competitors : []).filter((c) => stats.has(keyOf(c))).map((c) => {
    const s = stats.get(keyOf(c)), best = s.breakouts.sort((x, y) => y - x).slice(0, 3);
    return [keyOf(c), { ...c, pieces: s.pieces, total_views: s.views, breakout: best.length ? best.reduce((sum, v) => sum + v, 0) / best.length : null }];
  }));
  const plotted = [...data.values()].filter((c) => c.breakout > 0 && c.total_views > 0);
  radar.axes = null;
  if (plotted.length) {
    const x = logAxis(plotted.map((c) => c.breakout)), y = logAxis(plotted.map((c) => c.total_views));
    radar.axes = { x, y, mx: middle(plotted.map((c) => x.at(c.breakout))), my: middle(plotted.map((c) => y.at(c.total_views))) };
  }
  const maxEr = Math.max(...plotted.map((c) => c.er || 0), 1e-9);
  const leaders = new Set([...plotted].sort((x, y) => y.total_views - x.total_views).slice(0, 6).map(keyOf));
  const before = new Map(radar.blips.map((blip) => [blip.key, blip]));
  radar.docked = 0;
  radar.blips = roster.map((c) => {
    const d = data.get(keyOf(c)), on = plotted.includes(d), was = before.get(keyOf(c));
    return {
      key: keyOf(c), name: c.name, platform: c.platform, data: d, labelled: leaders.has(keyOf(c)),
      fx: on ? radar.axes.x.at(d.breakout) : null, fy: on ? radar.axes.y.at(d.total_views) : null,   // 0..1 across the plot
      dock: on ? null : radar.docked++,
      size: on ? 4 + 6 * Math.sqrt((d.er || 0) / maxEr) : 4,
      state: on ? 'ranked' : 'unknown', pingAt: 0, x: was ? was.x : null, y: was ? was.y : null,
    };
  });
  redraw();
}

function sizeRadar() {
  const rect = radar.canvas.getBoundingClientRect();
  const dpr = Math.min(devicePixelRatio || 1, 2);
  radar.canvas.width = rect.width * dpr;
  radar.canvas.height = rect.height * dpr;
  radar.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  radar.w = rect.width;
  radar.h = rect.height;
  radar.motes = Array.from({ length: 0 }, () => ({
    x: Math.random() * rect.width, y: Math.random() * rect.height, r: 24 + Math.random() * 46,
    v: 8 + Math.random() * 16, a: 0.025 + Math.random() * 0.035,
  }));
  redraw();
}

function blipTarget(blip) {
  const { l, t, r, b } = plotBox();
  if (blip.fx != null) return [l + blip.fx * (r - l), b - blip.fy * (b - t)];
  if (radar.axes) return [l + 8 + (blip.dock + 0.5) * Math.min(24, (r - l - 16) / radar.docked), radar.h - 22];   // the dock
  const cols = Math.ceil(Math.sqrt(radar.blips.length * 1.8)), rows = Math.ceil(radar.blips.length / cols);        // formation
  return [l + ((blip.dock % cols) + 0.5) * ((r - l) / cols), t + (Math.floor(blip.dock / cols) + 0.5) * ((b - t) / rows)];
}

// Platform is carried by shape, not colour: circle = YouTube, diamond = Instagram.
function markPath(ctx, x, y, size, platform) {
  ctx.beginPath();
  if (platform === 'instagram') {
    const d = size * 1.25;
    ctx.moveTo(x, y - d); ctx.lineTo(x + d, y); ctx.lineTo(x, y + d); ctx.lineTo(x - d, y); ctx.closePath();
  } else ctx.arc(x, y, size, 0, TAU);
}

function drawRadar(now) {
  const { ctx, w, h } = radar;
  const dt = Math.min(0.05, (now - radar.last) / 1000 || 0);
  radar.last = now;
  const { l, t, r, b } = plotBox();
  if (r - l < 80 || b - t < 80) {                             // canvas not laid out yet
    if (radar.animate) requestAnimationFrame(drawRadar);
    return;
  }
  readTheme();
  radar.scan = (radar.scan + dt * (app.running ? 0.55 : 0.14)) % 1.3;
  const line = (x0, y0, x1, y1) => { ctx.beginPath(); ctx.moveTo(x0, y0); ctx.lineTo(x1, y1); ctx.stroke(); };
  ctx.clearRect(0, 0, w, h);

  // Shadow embers rising behind the map.
  for (const m of radar.motes) {
    m.y -= m.v * dt * (app.running ? 2.2 : 1);
    if (m.y < -m.r) { m.y = h + m.r; m.x = Math.random() * w; }
    const g = ctx.createRadialGradient(m.x, m.y, 0, m.x, m.y, m.r);
    g.addColorStop(0, `rgba(0,0,0,0)`);
    g.addColorStop(1, 'rgba(0,0,0,0)');
    ctx.fillStyle = g;
    ctx.fillRect(m.x - m.r, m.y - m.r, m.r * 2, m.r * 2);
  }

  // Plot floor. The top-right corner is the danger zone (big and spiking), so that is where it glows.
  const zone = ctx.createRadialGradient(r, t, 0, r, t, Math.hypot(r - l, b - t));
  zone.addColorStop(0, 'rgba(0,0,0,0)');
  zone.addColorStop(0.55, 'rgba(0,0,0,0)');
  zone.addColorStop(1, 'rgba(0,0,0,0)');
  ctx.fillStyle = zone;
  ctx.fillRect(l, t, r - l, b - t);

  ctx.lineWidth = 1;
  ctx.textBaseline = 'middle';
  ctx.font = '400 10px system-ui, -apple-system, sans-serif';
  const ax = radar.axes;
  if (ax) {
    ctx.fillStyle = `rgba(${radar.ink},0.9)`;
    ctx.strokeStyle = `rgba(${radar.rgb},0.09)`;
    ctx.textAlign = 'center';
    for (const tick of ax.x.ticks) {
      const x = l + ax.x.at(tick) * (r - l);
      line(x, t, x, b);
      ctx.fillText(tick + 'x', x, b + 14);
    }
    ctx.textAlign = 'right';
    for (const tick of ax.y.ticks) {
      const y = b - ax.y.at(tick) * (b - t);
      line(l, y, r, y);
      ctx.fillText(num(tick), l - 8, y);
    }
    // The field's own median on each axis splits the map into four zones.
    const mx = l + ax.mx * (r - l), my = b - ax.my * (b - t);
    ctx.setLineDash([4, 5]);
    ctx.strokeStyle = `rgba(${radar.ink},0.22)`;
    line(mx, t, mx, b);
    line(l, my, r, my);
    ctx.setLineDash([]);
    ctx.font = '500 9.5px system-ui, -apple-system, sans-serif';
    ctx.fillStyle = `rgba(${radar.ink},0.36)`;
    ctx.textAlign = 'right';
    ctx.fillText('BIG + SPIKING', r - 8, t + 12);
    ctx.fillText('SMALL, SPIKING', r - 8, b - 10);
    ctx.textAlign = 'left';
    ctx.fillText('BIG, STEADY', l + 8, t + 12);
    ctx.fillText('QUIET', l + 8, b - 10);
    ctx.fillStyle = `rgba(${radar.rgb},0.95)`;
    ctx.textAlign = 'center';
    ctx.fillText('BREAKOUT: BEST 3 PIECES VS THEIR OWN MEDIAN', (l + r) / 2, b + 34);
    ctx.save(); ctx.translate(13, (t + b) / 2); ctx.rotate(-Math.PI / 2);
    ctx.fillText('VIEWS, LAST 80 DAYS', 0, 0);
    ctx.restore();
    const king = radar.blips.filter((blip) => blip.data).sort((x, y) => y.data.total_views - x.data.total_views)[0];
    if (king && !app.running) {
      ctx.font = '600 13px system-ui, -apple-system, sans-serif';
      ctx.textAlign = 'right';
      ctx.fillStyle = `rgb(${radar.ink})`;
      ctx.fillText(`${king.name.split('|')[0].trim()} leads: ${num(king.data.total_views)} views`, r - 8, b - 26);
    }
    if (radar.docked) {
      ctx.fillStyle = `rgba(${radar.ink},0.75)`;
      ctx.textAlign = 'left';
      ctx.fillText('OFF MAP', 6, h - 22);
    }
  } else {
    ctx.fillStyle = `rgba(${radar.ink},0.8)`;
    ctx.textAlign = 'center';
    ctx.fillText(app.running ? 'ACQUIRING TARGETS' : 'NO HUNT DATA YET', (l + r) / 2, b + 24);
  }
  ctx.strokeStyle = `rgba(${radar.rgb},0.14)`;
  ctx.beginPath(); ctx.moveTo(l, t); ctx.lineTo(r, t); ctx.lineTo(r, b); ctx.stroke();
  ctx.strokeStyle = `rgba(${radar.rgb},0.6)`;
  ctx.beginPath(); ctx.moveTo(l, t); ctx.lineTo(l, b); ctx.lineTo(r, b); ctx.stroke();

  // Scan line sweeping left to right; marks flare as it passes.
  const scanX = l + (radar.scan - 0.15) * (r - l);
  if (radar.animate) {
    ctx.save();
    ctx.beginPath(); ctx.rect(l, t, r - l, b - t); ctx.clip();
    const trail = ctx.createLinearGradient(scanX - 140, 0, scanX, 0);
    trail.addColorStop(0, `rgba(${radar.rgb},0)`);
    trail.addColorStop(1, `rgba(${radar.rgb},${app.running ? 0.34 : 0.16})`);
    ctx.fillStyle = trail;
    ctx.fillRect(scanX - 140, t, 140, b - t);
    ctx.strokeStyle = `rgba(${radar.ink},0.7)`;
    line(scanX, t, scanX, b);
    ctx.restore();
  }

  // Live mode: while a hunt runs the map looks like it is working. Packets stream toward acquired marks,
  // brackets spin on them, a second scan sweeps vertically, and the last few events tick in the corner.
  if (app.running && radar.animate) {
    ctx.save();
    ctx.beginPath(); ctx.rect(l, t, r - l, b - t); ctx.clip();
    const scanY = t + ((radar.scan * 1.37) % 1.3 - 0.15) * (b - t);
    const vtrail = ctx.createLinearGradient(0, scanY - 90, 0, scanY);
    vtrail.addColorStop(0, `rgba(${radar.rgb},0)`); vtrail.addColorStop(1, `rgba(${radar.rgb},0.18)`);
    ctx.fillStyle = vtrail; ctx.fillRect(l, scanY - 90, r - l, 90);
    ctx.strokeStyle = `rgba(${radar.ink},0.35)`; line(l, scanY, r, scanY);
    if (!radar.packets) radar.packets = Array.from({ length: 36 }, () => ({ t: Math.random(), s: 0.25 + Math.random() * 0.5, from: [l + Math.random() * (r - l), Math.random() < 0.5 ? t : b], i: Math.floor(Math.random() * 999) }));
    const lit = radar.blips.filter((blip) => blip.state !== 'unknown');
    for (const pk of radar.packets) {
      pk.t += dt * pk.s;
      const target = lit.length ? lit[pk.i % lit.length] : null;
      if (pk.t >= 1 || !target) { pk.t = 0; pk.from = [l + Math.random() * (r - l), Math.random() < 0.5 ? t : b]; pk.i = Math.floor(Math.random() * 999); if (!target) continue; }
      const x = pk.from[0] + (target.x - pk.from[0]) * pk.t, y = pk.from[1] + (target.y - pk.from[1]) * pk.t;
      ctx.fillStyle = `rgba(${radar.rgb},${0.25 + 0.6 * pk.t})`;
      ctx.fillRect(x - 1.5, y - 1.5, 3, 3);
    }
    for (const blip of lit) {
      ctx.save(); ctx.translate(blip.x, blip.y); ctx.rotate(now / 900 + blip.x);
      ctx.strokeStyle = `rgba(${radar.rgb},0.85)`; ctx.lineWidth = 1.2;
      const k = blip.size + 7;
      for (const [sx, sy] of [[1, 1], [-1, 1], [-1, -1], [1, -1]]) {
        ctx.beginPath(); ctx.moveTo(sx * k, sy * (k - 4)); ctx.lineTo(sx * k, sy * k); ctx.lineTo(sx * (k - 4), sy * k); ctx.stroke();
      }
      ctx.restore(); ctx.lineWidth = 1;
    }
    ctx.restore();
    ctx.font = '400 11px ui-monospace, Menlo, monospace';
    ctx.textAlign = 'left';
    app.ticker.forEach((text, i) => {
      ctx.fillStyle = `rgba(${radar.ink},${0.35 + 0.13 * i})`;
      ctx.fillText(text.toUpperCase(), l + 8, b - 26 - (app.ticker.length - 1 - i) * 15);
    });
    ctx.font = '400 10px system-ui, -apple-system, sans-serif';
  }

  // Legend.
  ctx.font = '400 9.5px system-ui, -apple-system, sans-serif';
  ctx.textAlign = 'left';
  ctx.fillStyle = `rgb(${radar.rgb})`;
  markPath(ctx, l + 5, 14, 4, 'youtube'); ctx.fill();
  markPath(ctx, l + 92, 14, 4, 'instagram'); ctx.fill();
  ctx.fillStyle = `rgba(${radar.ink},0.95)`;
  ctx.fillText('YOUTUBE', l + 15, 14);
  ctx.fillText('INSTAGRAM', l + 103, 14);
  ctx.textAlign = 'right';
  ctx.fillText('SIZE = ENGAGEMENT RATE', r, 14);

  const named = [];
  for (const blip of radar.blips) {
    const [tx, ty] = blipTarget(blip);
    const rising = radar.animate && blip.fx != null;          // ranked marks rise from the origin corner
    if (blip.x == null || !radar.animate) { blip.x = rising ? l : tx; blip.y = rising ? b : ty; }
    blip.x += (tx - blip.x) * Math.min(1, dt * 5);
    blip.y += (ty - blip.y) * Math.min(1, dt * 5);
    const behind = scanX - blip.x;
    const lit = !radar.animate ? 0.5 : behind >= 0 && behind < 170 ? 1 - behind / 170 : 0;
    const focus = blip.key === app.hot || blip.key === app.filter.competitor;
    if (blip.state === 'unknown') {
      ctx.strokeStyle = `rgba(173,170,170,${0.4 + 0.5 * lit})`;
      markPath(ctx, blip.x, blip.y, 4, blip.platform); ctx.stroke();
    } else {
      ctx.shadowBlur = 0;
      ctx.fillStyle = focus ? `rgb(${radar.ink})` : `rgba(${radar.rgb},${0.62 + 0.38 * lit})`;
      markPath(ctx, blip.x, blip.y, blip.size, blip.platform); ctx.fill();
      ctx.shadowBlur = 0;
      ctx.strokeStyle = radar.surface; ctx.lineWidth = 1.5; ctx.stroke(); ctx.lineWidth = 1;   // ring keeps overlapping marks apart
      const age = (now - blip.pingAt) / 1100;
      if (blip.pingAt && age < 1) {
        ctx.strokeStyle = `rgba(${radar.ink},${1 - age})`;
        ctx.beginPath(); ctx.arc(blip.x, blip.y, blip.size + age * 30, 0, TAU); ctx.stroke();
      }
    }
    if (focus) {
      ctx.strokeStyle = `rgb(${radar.ink})`;
      ctx.beginPath(); ctx.arc(blip.x, blip.y, blip.size + 6, 0, TAU); ctx.stroke();
    }
    if (focus || blip.labelled) named.push([blip, focus]);
  }

  // Names last so no mark covers them; a name gives way when it would sit on one already drawn.
  ctx.font = '500 10px system-ui, -apple-system, sans-serif';
  const boxes = [];
  for (const [blip, focus] of named.sort((x, y) => y[1] - x[1])) {
    const text = blip.name.split('|')[0].trim().toUpperCase(), tw = ctx.measureText(text).width;
    const right = blip.x + blip.size + 10 + tw < r;
    const tx = blip.x + (right ? blip.size + 8 : -blip.size - 8);
    const box = { x0: right ? tx : tx - tw, x1: right ? tx + tw : tx, y: blip.y };
    if (!focus && boxes.some((o) => Math.abs(o.y - box.y) < 13 && o.x0 < box.x1 + 6 && box.x0 < o.x1 + 6)) continue;
    boxes.push(box);
    ctx.textAlign = right ? 'left' : 'right';
    ctx.fillStyle = focus ? `rgb(${radar.ink})` : `rgba(${radar.ink},0.72)`;
    ctx.fillText(text, tx, blip.y);
  }

  if (radar.animate) requestAnimationFrame(drawRadar);
}
function redraw() { if (!radar.animate && radar.w) drawRadar(performance.now()); }

function blipAt(event) {
  const rect = radar.canvas.getBoundingClientRect();
  const mx = event.clientX - rect.left, my = event.clientY - rect.top;
  let best = null, bestDist = 18;
  for (const blip of radar.blips) {
    const d = Math.hypot(blip.x - mx, blip.y - my);
    if (d < bestDist) { best = blip; bestDist = d; }
  }
  return best;
}
radar.canvas.addEventListener('mousemove', (e) => {
  const blip = blipAt(e);
  setHot(blip ? blip.key : null);
  if (blip) showTip(e, competitorTip(blip.data || blip)); else hideTip();
});
radar.canvas.addEventListener('mouseleave', () => { setHot(null); hideTip(); });
radar.canvas.addEventListener('click', (e) => { const blip = blipAt(e); if (blip && blip.data) selectCompetitor(blip.key); });

// ---------------------------------------------------------------------------
// Tooltip + cross-highlighting
// ---------------------------------------------------------------------------
function competitorTip(c) {
  const rows = c.total_views == null
    ? [['Platform', c.platform === 'youtube' ? 'YouTube' : 'Instagram'], ['Status', 'Not scouted yet']]
    : [['Platform', c.platform === 'youtube' ? 'YouTube' : 'Instagram'], ['Pieces in filter', c.pieces], ['Views in filter', num(c.total_views)],
       ['Breakout, best 3', c.breakout ? c.breakout.toFixed(1) + 'x' : 'n/a'],
       ['Median views', num(c.median_views)], ['Engagement rate', pct(c.er)],
       ...(c.subs ? [['Subscribers', num(c.subs)]] : []), ['Best hunt score', c.best_score == null ? 'n/a' : c.best_score]];
  return [h('strong', { text: c.name }), h('dl', {}, rows.flatMap(([k, v]) => [h('dt', { text: k }), h('dd', { text: String(v) })]))];
}
function showTip(event, nodes) {
  const tip = $('tip');
  tip.replaceChildren(...nodes);
  tip.hidden = false;
  const pad = 16, rect = tip.getBoundingClientRect();
  tip.style.left = Math.min(event.clientX + pad, innerWidth - rect.width - pad) + 'px';
  tip.style.top = Math.min(event.clientY + pad, innerHeight - rect.height - pad) + 'px';
}
function hideTip() { $('tip').hidden = true; }
function setHot(key) {
  if (app.hot === key) return;
  app.hot = key;
  document.querySelectorAll('.rung').forEach((el) => el.classList.toggle('hot', el.dataset.key === key));
  redraw();
}
function selectCompetitor(key) {
  app.filter.competitor = app.filter.competitor === key ? null : key;
  app.filter.shown = PAGE;
  document.querySelectorAll('.rung').forEach((el) => el.classList.toggle('selected', el.dataset.key === app.filter.competitor));
  renderTargets();
  redraw();
  if (app.filter.competitor) $('filters').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

// ---------------------------------------------------------------------------
// Results
// ---------------------------------------------------------------------------
function renderKpis(r) {
  const t = r.totals, pilots = r.jev.pilots, conductor = r.conductor || {};
  const byPlatform = ['youtube', 'instagram'].map((p) => [p, r.competitors.filter((c) => c.platform === p).length]).filter(([, n]) => n);
  const health = pilots.length ? pilots.map((p) => p.health.toFixed(2)).join(' to ') : 'n/a';
  const tiles = [
    ['Competitors', t.competitors, byPlatform.map(([p, n]) => `${p === 'youtube' ? 'YouTube' : 'Instagram'} ${n}`).join(' · ')],
    ['Posts collected', t.pieces.toLocaleString(), `last ${r.window_days} days`],
    ['Jev decisions', t.jev_calls.toLocaleString(), `$${t.jev_cost.toFixed(3)} · ${t.jev_seconds || 0}s`],
    ['Judge tuning', `v${r.jev.version}`, `confidence ${health}`],
    ['Ads left out', t.paid, 'paid placements'],
    ['Conductor', conductor.mode === 'claude' ? modelLabel(conductor.model) : 'Direct',
      conductor.mode === 'claude' ? `${conductor.turns || 0} turns · ${duration(conductor.seconds || 0)}` : 'base preset, no tuning'],
  ];
  $('kpis').replaceChildren(...tiles.map(([label, value, sub]) =>
    h('li', {}, h('p', { class: 'label', text: label }), h('p', { class: 'value', text: String(value) }), h('p', { class: 'sub', text: sub }))));
}

function tier(fromTop, n) {
  if (fromTop === 0) return 'S';
  const share = fromTop / Math.max(1, n - 1);
  return share <= 0.15 ? 'A' : share <= 0.35 ? 'B' : share <= 0.6 ? 'C' : share <= 0.8 ? 'D' : 'E';
}

// log: values span orders of magnitude, so each row is a dot on a log axis (a bar would imply a zero baseline).
function renderLadder(el, title, meta, rows, value, format, foot, log = false) {
  const sorted = [...rows].sort((a, b) => value(a) - value(b));   // least at the top, most at the bottom
  const max = Math.max(...sorted.map(value), 1e-9);
  const lo = Math.floor(Math.log10(Math.max(1, Math.min(...sorted.map(value)))));
  const decades = Math.max(1, Math.ceil(Math.log10(max)) - lo);
  const at = (v) => (log ? ((Math.log10(Math.max(1, v)) - lo) / decades) * 100 : (v / max) * 100).toFixed(2);
  const axis = log ? h('div', { class: 'rung axis', 'aria-hidden': 'true' }, h('span'), h('span'),
    h('span', { class: 'track' }, Array.from({ length: decades + 1 }, (_, d) =>
      h('span', { class: 'tick', vars: { '--w': (d / decades) * 100 }, text: num(10 ** (lo + d)) }))), h('span')) : null;
  const rungs = sorted.map((c, i) =>
    h('button', {
      class: 'rung' + (keyOf(c) === app.filter.competitor ? ' selected' : ''), type: 'button', 'data-key': keyOf(c),
      'aria-label': `${c.name}, ${title} ${format(value(c))}. Show only their pieces.`,
      onclick: () => selectCompetitor(keyOf(c)),
      onmousemove: (e) => { setHot(keyOf(c)); showTip(e, competitorTip(c)); },
      onmouseleave: () => { setHot(null); hideTip(); },
    },
      h('span', { class: 'tier', text: tier(sorted.length - 1 - i, sorted.length) }),
      h('span', { class: 'name' }, c.name.split('|')[0].trim(), h('span', { class: 'plat', text: platTag(c.platform) })),
      h('span', { class: 'track' + (log ? ' dots' : ''), vars: { '--n': decades } },
        h('span', { class: log ? 'dot' : 'bar', vars: { '--w': at(value(c)), '--i': i } })),
      h('span', { class: 'val', text: format(value(c)) })));
  fill(el,
    h('header', { class: 'window-head' }, h('h3', { text: title }), h('span', { class: 'window-meta', text: meta })),
    axis,
    h('div', { class: 'ladder-rows' }, rungs),
    foot ? h('p', { class: 'ladder-foot', text: foot }) : null);
}

function renderLadders(r) {
  const scouted = r.competitors.filter((c) => c.pieces > 0);
  const missing = r.competitors.filter((c) => c.pieces === 0).map((c) => c.name);
  const rated = scouted.filter((c) => c.er != null);
  const hasIg = scouted.some((c) => c.platform === 'instagram');
  renderLadder($('ladder-views'), 'Views', `last ${r.window_days} days, log scale`, scouted, (c) => c.total_views, num,
    hasIg ? 'Instagram views count reel plays. Carousels have no public view count.' : null, true);
  renderLadder($('ladder-er'), 'Engagement', 'likes and comments per 100 views', rated, (c) => c.er, (v) => pct(v),
    rated.length < scouted.length ? `${scouted.length - rated.length} competitors have no rated pieces.` : null);
  $('ladder-note').textContent = 'Least at the top, most at the bottom. S is the top rung. Click a name to see only their posts.'
    + (missing.length ? ` No pieces in the window: ${missing.join(', ')}.` : '');
}

function targetCard(p, i) {
  const link = safeLink(p.url), thumb = safeThumb(p.thumb), j = p.jev;
  const big = p.platform === 'youtube' && thumb ? thumb.replace('mqdefault', 'maxresdefault') : thumb;
  const img = thumb ? h('img', { src: big, alt: '', loading: 'lazy', onerror: (e) => { if (e.target.src !== new URL(thumb, location).href) e.target.src = thumb; else e.target.replaceWith(blankThumb(p)); } }) : null;
  const stat = (label, value) => h('div', {}, h('dt', { text: label }), h('dd', { text: value }));
  const meter = (label, value) => h('div', { class: 'meter' }, h('span', { text: label }),
    h('i', {}, h('b', { vars: { '--w': Math.round(value * 100) } })), h('span', { text: String(Math.round(value * 100)) }));
  return h('article', { class: 'target' + (i === 0 ? ' lead' : ''), vars: { '--i': i % PAGE } },
    h('a', { class: 'thumb', href: link, target: '_blank', rel: 'noopener', 'aria-label': `Open: ${p.title}` },
      h('span', { class: 'rank-no', text: String(p.rank).padStart(2, '0') }), img || blankThumb(p)),
    h('div', { class: 'body' },
      h('div', { class: 'meta' }, h('span', { class: 'who', text: p.competitor.split('|')[0].trim() }),
        h('span', { text: `${platTag(p.platform)} ${p.format}` }), h('span', { text: p.age_days === 0 ? 'today' : `${p.age_days}d ago` }),
        h('button', { class: 'save-btn', type: 'button', 'data-id': p.id, text: 'Save', onclick: (e) => window.openPicker(e.currentTarget, p) })),
      h('h4', {}, h('a', { href: link, target: '_blank', rel: 'noopener', text: p.title || '(no caption)' })),
      h('dl', { class: 'stats' },
        p.kind === 'video' ? stat('Views', num(p.views)) : stat('Likes + comments', num((p.likes || 0) + (p.comments || 0))),
        stat('Breakout', p.breakout == null ? 'n/a' : p.breakout.toFixed(1) + 'x'), stat('Engagement', pct(p.er))),
      h('div', { class: 'scoreline' },
        h('div', { class: 'score-num' }, p.score.toFixed(1), h('small', { text: 'Hunt score' })),
        h('div', { class: 'meters' }, meter('Demand', p.demand), meter('Fit', p.fit))),
      h('div', { class: 'verdict', 'aria-label': 'Jev verdict' },
        h('span', { text: words(j.topic_lane) }), h('span', { text: `hook: ${words(j.hook_type)}` }),
        h('span', { text: `proof ${Math.round(j.proof)}/3` }), h('span', { text: `replicable ${Math.round(j.replicable * 100)}%` }),
        j.newsjack >= 0.6 ? h('span', { text: 'news-pegged' }) : null,
        j.sure ? h('span', { text: `jev sure ${Math.round((j.conf || 0) * 100)}%` }) : h('span', { class: 'unsure', text: 'jev unsure' }))));
}

function renderTargets() {
  const r = app.result, f = app.filter;
  if (!r) return;
  const formats = [['all', 'All'], ['longform', 'YouTube longform'], ['short', 'YouTube Shorts'], ['reel', 'Instagram reels'], ['carousel', 'Instagram carousels']]
    .filter(([key]) => key === 'all' || r.pieces.some((p) => p.format === key));
  const lanes = [...new Set(r.pieces.map((p) => (p.jev || {}).topic_lane).filter(Boolean))].sort();
  const hooks = [...new Set(r.pieces.map((p) => (p.jev || {}).hook_type).filter(Boolean))].sort();
  const days = r.pieces.map((p) => (p.published || '').slice(0, 10)).filter(Boolean).sort();
  const list = filteredPieces();
  const active = Object.keys(DEFAULT_FILTER).filter((k) => f[k] !== DEFAULT_FILTER[k]).length;
  const set = (patch) => { Object.assign(f, patch); f.shown = PAGE; saveFilter(); renderTargets(); setBlips(app.roster, r); };
  const select = (label, key, options, parse = (v) => v) => h('label', { class: 'fcol' }, h('span', { text: label }),
    h('select', { onchange: (e) => set({ [key]: parse(e.target.value) }) },
      options.map(([value, text]) => h('option', { value: String(value), text, selected: String(f[key]) === String(value) }))));
  const date = (label, key) => h('label', { class: 'fcol' }, h('span', { text: label }),
    h('input', { type: 'date', value: f[key], min: days[0], max: days[days.length - 1], onchange: (e) => set({ [key]: e.target.value }) }));
  const check = (label, key) => h('label', { class: 'check small' },
    h('input', { type: 'checkbox', checked: f[key], onchange: (e) => set({ [key]: e.target.checked }) }), h('span', { text: label }));
  fill($('filters'),
    h('div', { class: 'chip-row' },
      formats.map(([key, label]) => h('button', { class: 'chip-btn', type: 'button', 'aria-pressed': String(f.format === key), text: label,
        onclick: () => set({ format: key }) })),
      f.competitor ? h('button', { class: 'chip-btn clear', type: 'button', text: `${f.competitor.split('|')[1]} only · clear`,
        onclick: () => selectCompetitor(f.competitor) }) : null),
    h('div', { class: 'filter-bar' },
      select('Posted', 'days', [[0, `Any time, ${r.window_days} days`], [7, 'Last 7 days'], [14, 'Last 14 days'], [30, 'Last 30 days']], Number),
      date('From', 'from'), date('To', 'to'),
      select('Breakout', 'breakout', [[0, 'Any'], [1, '1x or more'], [2, '2x or more'], [5, '5x or more'], [10, '10x or more']], Number),
      select('Topic', 'lane', [['all', 'All topics'], ...lanes.map((l) => [l, words(l)])]),
      select('Hook', 'hook', [['all', 'All hooks'], ...hooks.map((k) => [k, words(k)])]),
      select('Min views', 'minViews', [[0, 'Any'], [1000, '1K or more'], [10000, '10K or more'], [50000, '50K or more'], [100000, '100K or more'], [500000, '500K or more']], Number),
      select('Sort by', 'sort', [['score', 'Hunt score'], ['breakout', 'Breakout'], ['views', 'Views'], ['newest', 'Newest']]),
      h('label', { class: 'fcol' }, h('span', { text: `Min hunt score: ${f.minScore}` }),
        h('input', { type: 'range', min: 0, max: 100, step: 5, value: f.minScore,
          oninput: (e) => { e.target.previousSibling.textContent = `Min hunt score: ${e.target.value}`; },
          onchange: (e) => set({ minScore: Number(e.target.value) }) })),
      h('div', { class: 'fcol toggles' }, check('Hide off-topic', 'hideOff'), check('News-pegged only', 'news'), check('Jev sure only', 'sure'))),
    h('div', { class: 'filter-foot' },
      h('span', { class: 'help', text: `${list.length.toLocaleString()} of ${r.pieces.length.toLocaleString()} posts${active ? ` · ${active} filter${active > 1 ? 's' : ''} on` : ''}` }),
      active ? h('button', { class: 'chip-btn clear', type: 'button', text: 'Reset filters', onclick: () => set({ ...DEFAULT_FILTER }) }) : null));
  $('targets').replaceChildren(...(list.length
    ? list.slice(0, f.shown).map(targetCard)
    : [h('p', { class: 'section-note', text: 'No ranked pieces match these filters. Loosen one or reset.' })]));
  $('more').hidden = list.length <= f.shown;
  if (window.markSaved) window.markSaved();
}

function renderJev(r) {
  const j = r.jev;
  const questions = Object.entries(j.questions).map(([key, q]) => {
    const criteria = Array.isArray(q.criteria) ? q.criteria.map((text, i) => [`Level ${i}`, text]) : Object.entries(q.criteria || {});
    const p = (j.pilots[j.pilots.length - 1] || { questions: {} }).questions[key] || {};
    const read = p.mean_conf != null ? `confidence ${p.mean_conf.toFixed(2)}` : p.unknown_share != null ? `${Math.round(p.unknown_share * 100)}% unknown` : '';
    return h('details', {}, h('summary', {}, key, h('small', { text: `${q.type}${read ? ' · ' + read : ''}` })),
      h('div', { class: 'q' }, h('p', { text: q.instructions }),
        criteria.length ? h('dl', {}, criteria.flatMap(([label, text]) => [h('dt', { text: words(label) }), h('dd', { text })])) : null));
  });
  fill($('jev-panel'),
    h('summary', {}, 'How posts were judged', h('small', { text: `${j.model}, question set v${j.version}${j.adopted ? ', adopted as the new base' : ''}` })),
    h('p', { class: 'help', text: 'Jev, a decision model, answered these questions for every post. Claude only tuned the wording on a pilot batch.' }),
    h('ul', { class: 'pilots' }, j.pilots.map((p) => h('li', {}, `Pilot ${p.round} · ${p.n} pieces`, h('b', { text: p.health.toFixed(3) }), `${p.flags.length} flags`))),
    j.tuning_notes.length ? h('ul', { class: 'notes' }, j.tuning_notes.map((text) => h('li', { text }))) : null,
    questions);
}

function renderPaid(r) {
  const rows = r.paid.slice(0, 12).map((p) => h('tr', {},
    h('td', {}, h('a', { href: safeLink(p.url), target: '_blank', rel: 'noopener', text: p.title || '(no caption)' }),
      h('small', { text: `${p.competitor.split('|')[0].trim()} · ${p.reason}` })),
    h('td', { class: 'num', text: num(p.views) }), h('td', { class: 'num', text: pct(p.er) })));
  fill($('paid-panel'),
    h('summary', {}, `Paid placements left out (${r.paid.length})`, h('small', { text: `top ${rows.length} by views` })),
    h('p', { class: 'help', text: 'Bought reach, not earned. These never enter a rank or the map.' }),
    r.paid.length ? h('table', {}, h('thead', {}, h('tr', {}, h('th', { text: 'Post' }), h('th', { class: 'num', text: 'Views' }), h('th', { class: 'num', text: 'ER' }))),
      h('tbody', {}, rows)) : h('p', { class: 'help', text: 'Nothing flagged in this window.' }));
}

function render(result) {
  app.result = result;
  app.filter = loadFilter();
  $('empty').hidden = true;
  $('results').hidden = false;
  setBlips(app.roster, result);
  $('read-targets').textContent = app.roster.length;
  $('read-acquired').textContent = result.competitors.filter((c) => c.pieces > 0).length;
  $('read-pieces').textContent = result.totals.pieces.toLocaleString();
  renderKpis(result);
  renderLadders(result);
  renderTargets();
  renderJev(result);
  renderPaid(result);
  renderReddit(result);
}

function renderReddit(r) {
  const rd = r.reddit || { posts: [], blocked: [], subs: [] };
  $('reddit-tile').hidden = !rd.posts.length && !rd.subs.length;
  $('reddit-note').textContent = rd.posts.length
    ? `Top posts of the week from r/${rd.subs.filter((s) => !rd.blocked.includes(s)).join(', r/')}, ranked by how well they fit your audience.${rd.blocked.length ? ` Reddit blocked r/${rd.blocked.join(', r/')} this time.` : ''}`
    : 'No Reddit posts came back this hunt. Add communities in Settings.';
  fill($('reddit-list'), rd.posts.map((p) => h('li', {},
    h('span', { class: 'sub', text: `r/${p.sub}` }),
    h('a', { href: /^https:\/\/www\.reddit\.com\//.test(p.url) ? p.url : '#', target: '_blank', rel: 'noopener', text: p.title }),
    h('span', { class: 'fit', text: p.jev && p.jev.icp_fit != null ? `fit ${(p.jev.icp_fit / 3 * 100).toFixed(0)}%` : '' }))));
}

// ---------------------------------------------------------------------------
// Live run
// ---------------------------------------------------------------------------
function setPhase(stage, done) {
  const at = PHASES.indexOf(stage);
  if (at < 0) return;
  document.querySelectorAll('#phases li').forEach((li, i) => {
    li.classList.toggle('done', i < at || (i === at && done));
    li.classList.toggle('active', i === at && !done);
  });
  $('read-status').textContent = done && stage === 'rank' ? 'Hunt complete' : STATUS[stage];
}
function phaseNote(stage, text) {
  const note = document.querySelector(`#phases li[data-phase="${stage}"] .phase-note`);
  if (note) note.textContent = text;
}

function logLine(e) {
  const log = $('log');
  const stick = log.scrollTop + log.clientHeight >= log.scrollHeight - 30;
  const failed = e.kind === 'end' && e.data && !e.data.ok;
  log.append(h('li', { class: 'k-' + e.kind + (failed ? ' failed' : '') },   // k- prefix: 'target' is also the card class
    h('time', { text: new Date(e.ts * 1000).toLocaleTimeString('en-GB') }),
    h('span', { class: 'tag', text: STAGE_TAG[e.stage] || e.stage }), h('span', { class: 'text', text: e.msg })));
  while (log.children.length > 500) log.firstChild.remove();
  if (stick) log.scrollTop = log.scrollHeight;
}

function onEvent(e) {
  if (!app.startedAt) app.startedAt = e.ts * 1000;
  logLine(e);
  if (['say', 'phase', 'target', 'progress', 'log', 'done'].includes(e.kind)) app.ticker = [...app.ticker.slice(-4), e.msg.slice(0, 64)];
  const d = e.data || {};
  if (e.stage === 'scrape' && e.kind === 'phase') {
    app.acquired = 0; app.pieces = 0;
    setBlips(d.competitors, null);
    $('read-targets').textContent = d.competitors.length;
  }
  if (e.kind === 'target') {
    const blip = radar.blips.find((b) => b.key === keyOf(d));
    if (blip) { blip.state = 'acquired'; blip.size = 4; blip.pingAt = performance.now(); }
    app.acquired += 1; app.pieces += d.pieces;
    $('read-acquired').textContent = app.acquired;
    $('read-pieces').textContent = app.pieces.toLocaleString();
    phaseNote('scrape', `${app.acquired} / ${radar.blips.length} acquired`);
    redraw();
  }
  if (e.kind === 'phase' && PHASES.includes(e.stage)) setPhase(e.stage, false);
  if (e.stage === 'scrape' && e.kind === 'done') phaseNote('scrape', `${d.pieces.toLocaleString()} pieces`);
  if (e.stage === 'pilot' && e.kind === 'done') phaseNote('pilot', `round ${d.round} · health ${d.health.toFixed(2)}`);
  if (e.stage === 'judge' && e.kind === 'progress') phaseNote('judge', `${d.done.toLocaleString()} / ${d.total.toLocaleString()}`);
  if (e.stage === 'rank' && e.kind === 'done') { phaseNote('rank', `${d.organic.toLocaleString()} ranked`); setPhase('rank', true); }
  if (e.kind === 'end') { finish(d.ok, e.msg); radar.packets = null; app.ticker = []; }
}

function setRunning(running) {
  app.running = running;
  $('run').disabled = running;
  $('run').querySelector('.run-label').textContent = running ? 'Working' : 'Find winning content';
  $('stop').hidden = !running;
  document.querySelectorAll('input[name="platform"]').forEach((el) => { el.disabled = running; });
  clearInterval(app.timer);
  if (running) app.timer = setInterval(() => { if (app.startedAt) $('elapsed').textContent = duration((Date.now() - app.startedAt) / 1000); }, 1000);
}

async function finish(ok, message) {
  setRunning(false);
  if (app.source) { app.source.close(); app.source = null; }
  if (window.loadReports) window.loadReports();
  if (!ok) {
    $('notice').textContent = message;
    $('read-status').textContent = 'Stopped';
    document.querySelectorAll('#phases li.active').forEach((li) => li.classList.remove('active'));
    return;
  }
  render(await getJSON('/api/result'));
  $('results').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function attach(runId) {
  setRunning(true);
  app.startedAt = 0;
  $('notice').textContent = '';
  $('elapsed').textContent = '';
  $('log').replaceChildren();
  $('log-window').hidden = false;
  $('log-meta').textContent = `run ${runId}`;
  document.querySelectorAll('#phases li').forEach((li) => li.classList.remove('active', 'done'));
  $('read-status').textContent = `Summoning ${modelLabel(app.settings.model)}`;
  app.source = new EventSource(`/api/events?run=${encodeURIComponent(runId)}`);
  app.source.onmessage = (m) => onEvent(JSON.parse(m.data));
  app.source.addEventListener('closed', () => { if (app.running) finish(false, 'The run ended without a result. Read the log.'); });
}

$('run').addEventListener('click', async () => {
  const platforms = [...document.querySelectorAll('input[name="platform"]:checked')].map((el) => el.value);
  if (!platforms.length) { $('notice').textContent = 'Pick at least one hunting ground.'; return; }
  try {
    const { run_id } = await getJSON('/api/run', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ platforms }) });
    attach(run_id);
  } catch (err) { $('notice').textContent = err.message; }
});
$('stop').addEventListener('click', () => getJSON('/api/stop', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }).catch(() => {}));
$('more').addEventListener('click', () => { app.filter.shown += PAGE; renderTargets(); });

// ---------------------------------------------------------------------------
// Roster: brain competitors plus the ones added here (apps/competitor-hunter/roster.json)
// ---------------------------------------------------------------------------
function renderRoster() {
  const added = app.roster.filter((c) => c.source === 'app').length;
  $('roster-meta').textContent = `${app.roster.length} targets` + (added ? ` · ${added} added here` : '');
  for (const p of ['youtube', 'instagram']) $(`count-${p}`).textContent = app.roster.filter((c) => c.platform === p).length;
  $('read-targets').textContent = app.roster.length;
  fill($('roster-list'), app.roster.map((c) => h('li', { class: c.source === 'app' ? 'mine' : '' },
    h('span', { class: 'plat', text: platTag(c.platform) }), c.name.split('|')[0].trim(),
    c.source === 'app' ? h('button', { type: 'button', 'aria-label': `Remove ${c.name}`, title: 'Remove', text: '\u00d7',
      onclick: () => changeRoster('/api/roster/remove', { platform: c.platform, handle: c.handle }, `${c.name} removed.`) }) : null)));
  setBlips(app.roster, app.result);
}

async function changeRoster(url, body, done) {
  const notice = $('roster-notice');
  try {
    const res = await getJSON(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    app.roster = res.roster;
    renderRoster();
    notice.className = 'notice ok';
    notice.textContent = res.added ? `${res.added.name} added. They join the next hunt.` : done;
    return true;
  } catch (err) {
    notice.className = 'notice';
    notice.textContent = err.message;
    return false;
  }
}

$('roster-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const input = $('roster-input');
  if (!input.value.trim()) return;
  const platform = document.querySelector('input[name="roster-platform"]:checked').value;
  if (await changeRoster('/api/roster', { input: input.value, platform })) input.value = '';
});

// ---------------------------------------------------------------------------
// Settings: which Claude model conducts hunts and writes breakdowns
// ---------------------------------------------------------------------------
function renderSettings() {
  const select = $('model-select'), known = Object.keys(app.models);
  const current = app.settings.model, custom = !known.includes(current);
  fill(select, known.map((id) => h('option', { value: id, text: `${app.models[id]} (${id})`, selected: id === current })),
    h('option', { value: '__custom', text: 'Custom model id', selected: custom }));
  $('model-custom-field').hidden = !custom;
  $('model-custom').value = custom ? current : '';
  $('settings-meta').textContent = `Current model: ${modelLabel(current)}.`;
  $('theme-select').value = app.settings.theme || 'light';
  $('subs-input').value = (app.settings.reddit_subs || []).join(', ');
  applyTheme(app.settings.theme || 'light');
}
function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem('ch-theme', theme); } catch (e) { /* private mode */ }
  $('theme-btn').textContent = theme === 'dark' ? 'Light' : 'Dark';
  redraw();
}
$('theme-btn').addEventListener('click', async () => {
  const theme = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  applyTheme(theme);
  app.settings.theme = theme;
  $('theme-select').value = theme;
  try { await getJSON('/api/settings', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ theme }) }); } catch (e) { /* keeps the local choice */ }
});
$('model-select').addEventListener('change', (e) => { $('model-custom-field').hidden = e.target.value !== '__custom'; });
$('settings-btn').addEventListener('click', () => {
  const panel = $('settings');
  panel.hidden = !panel.hidden;
  $('settings-btn').setAttribute('aria-expanded', String(!panel.hidden));
  if (!panel.hidden) panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
});
$('settings-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const pick = $('model-select').value;
  const model = pick === '__custom' ? $('model-custom').value.trim() : pick;
  const theme = $('theme-select').value;
  const reddit_subs = $('subs-input').value.split(',').map((s) => s.trim()).filter(Boolean);
  const notice = $('settings-notice');
  try {
    const res = await getJSON('/api/settings', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ model, theme, reddit_subs }) });
    app.settings = { model: res.model, theme: res.theme, reddit_subs: res.reddit_subs };
    app.models = res.models;
    renderSettings();
    renderHealth(await getJSON('/api/state'));
    notice.className = 'notice ok';
    notice.textContent = `Saved. ${modelLabel(res.model)} runs the next hunt, the next breakdown and Ask AI.`;
  } catch (err) { notice.className = 'notice'; notice.textContent = err.message; }
});

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------
function renderHealth(state) {
  const hl = state.health;
  const credits = hl.jev_credits == null ? '' : ` $${hl.jev_credits.toFixed(2)}`;
  const chips = [
    [modelLabel(app.settings.model), hl.claude, 'Claude Code is not on PATH. Runs fall back to the base Jev preset.'],
    ['Jev' + credits, hl.jev_skill && hl.jev_key, 'Jev needs the claude-x-jev skill and OPENROUTER_API_KEY.'],
    ['YouTube', hl.youtube_key, 'YOUTUBE_DATA_API_KEY is missing from the repo .env.'],
    ['Apify', hl.apify_key, 'APIFY_API_TOKEN is missing from the repo .env. Instagram will be skipped.'],
    [`${state.window_days}-day window`, true, ''],
  ];
  $('health').replaceChildren(...chips.map(([label, ok, why]) => h('li', { class: ok ? '' : 'down', title: ok ? 'Ready' : why, text: label })));
  if (hl.jev_credits != null && hl.jev_credits < 0.15) $('notice').textContent = `Jev credit is low ($${hl.jev_credits.toFixed(2)} on OpenRouter). A full hunt costs about $0.10.`;
}

async function boot() {
  new ResizeObserver(sizeRadar).observe(radar.canvas);
  if (radar.animate) requestAnimationFrame(drawRadar);
  const state = await getJSON('/api/state');
  app.roster = state.roster;
  app.settings = state.settings || app.settings;
  app.models = state.models || {};
  renderSettings();
  $('window-days').textContent = state.window_days;
  renderHealth(state);
  renderRoster();
  try { render(await getJSON('/api/result')); } catch { $('empty').hidden = false; }
  if (window.loadLibrary) window.loadLibrary();
  if (state.running) attach(state.run_id);
}
boot().catch((err) => { $('notice').textContent = `Could not reach the server: ${err.message}`; });
