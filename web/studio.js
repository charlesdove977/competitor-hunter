// My Studio: your analytics, your ranked posts, topic lists, and the board. Standalone page (no app.js).

const $ = (id) => document.getElementById(id);
const PAGE = 12;
const compact = new Intl.NumberFormat('en', { notation: 'compact', maximumFractionDigits: 1 });
const num = (n) => (n == null ? 'n/a' : compact.format(n));
const pct = (n, d = 2) => (n == null ? 'n/a' : n.toFixed(d) + '%');
const words = (s) => String(s || '').replace(/_/g, ' ');
const safeLink = (u) => (/^https:\/\/(www\.)?(youtube\.com|youtu\.be|instagram\.com|reddit\.com)\//.test(u || '') ? u : null);
const safeThumb = (u) => (u && (u.startsWith('https://i.ytimg.com/') || u.startsWith('/runs/')) ? u : null);
const POST = (url, body) => getJSON(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });

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
const fill = (el, ...nodes) => el.replaceChildren(...nodes.flat().filter((n) => n != null));
async function getJSON(url, options) {
  const res = await fetch(url, options);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}
function mdNodes(text) {
  const out = [];
  let list = null;
  for (const raw of text.split('\n')) {
    const line = raw.replace(/\*\*/g, '').trimEnd();
    if (!line.trim()) { list = null; continue; }
    if (/^#{1,3} /.test(line)) { list = null; out.push(h('h5', { text: line.replace(/^#+\s*/, '') })); }
    else if (/^[-*] /.test(line)) { if (!list) { list = h('ul'); out.push(list); } list.append(h('li', { text: line.slice(2) })); }
    else out.push(h('p', { text: line }));
  }
  return out;
}
function notice(id, text, ok = true) { const el = $(id); el.className = ok ? 'notice ok' : 'notice'; el.textContent = text; }

const st = { data: null, filter: 'all', shown: PAGE, report: null, poll: null, startedAt: 0 };

// ---------------------------------------------------------------------------
// Theme (shared with the home page through localStorage + settings)
// ---------------------------------------------------------------------------
function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem('ch-theme', theme); } catch (e) { /* private mode */ }
  $('theme-btn').textContent = theme === 'dark' ? 'Light' : 'Dark';
}
applyTheme(document.documentElement.dataset.theme || 'light');
$('theme-btn').addEventListener('click', () => {
  const theme = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  applyTheme(theme);
  POST('/api/settings', { theme }).catch(() => {});
});

// ---------------------------------------------------------------------------
// Analytics + my posts
// ---------------------------------------------------------------------------
function renderAnalytics() {
  const r = st.data.result, mine = r && r.mine;
  fill($('health'),
    h('li', { class: st.data.has_youtube ? '' : 'down', text: st.data.has_youtube ? 'YouTube connected' : 'YouTube: no OAuth token' }),
    h('li', { class: st.data.has_instagram ? '' : 'down', text: st.data.has_instagram ? 'Instagram connected' : 'Instagram: no token' }),
    r ? h('li', { text: `last pull ${r.generated_at.slice(0, 10)}` }) : null);
  const tile = (label, value, sub) => h('li', {}, h('p', { class: 'label', text: label }), h('p', { class: 'value', text: value }), h('p', { class: 'sub', text: sub || '' }));
  const tiles = [];
  if (mine && mine.youtube) {
    const y = mine.youtube;
    tiles.push(tile('YouTube subscribers', num(y.subs), `${y.videos} videos in window`), tile('Views', num(y.views), y.ad_views ? `${num(y.ad_views)} from ads` : 'all organic'),
      tile('Subscribers gained', num(y.subs_gained), `${(y.subs_gained / Math.max(1, y.views) * 10000).toFixed(1)} per 10K views`), tile('Watch hours', num(Math.round(y.minutes / 60)), 'last 80 days'));
  }
  if (mine && mine.instagram) {
    const g = mine.instagram;
    tiles.push(tile('Instagram followers', num(g.followers), `${g.posts} posts in window`), tile('Reach', num(g.reach), `${num(g.views)} views`),
      tile('Saves', num(g.saves), 'the strongest signal'), tile('Shares', num(g.shares), ''));
  }
  fill($('kpis'), tiles.length ? tiles : h('li', {}, h('p', { class: 'label', text: 'No pull yet' }), h('p', { class: 'value', text: '0' }), h('p', { class: 'sub', text: 'Press Analyze my content' })));
}

function postCard(p, i) {
  const link = safeLink(p.url), thumb = safeThumb(p.thumb), j = p.jev || {}, o = p.own || {};
  const big = p.platform === 'youtube' && thumb ? thumb.replace('mqdefault', 'maxresdefault') : thumb;
  const stat = (label, value) => h('div', {}, h('dt', { text: label }), h('dd', { text: value }));
  const meter = (label, value) => h('div', { class: 'meter' }, h('span', { text: label }), h('i', {}, h('b', { vars: { '--w': Math.round((value || 0) * 100) } })), h('span', { text: String(Math.round((value || 0) * 100)) }));
  return h('article', { class: 'target' + (i === 0 ? ' lead' : ''), vars: { '--i': i % PAGE } },
    h('a', { class: 'thumb', href: link, target: '_blank', rel: 'noopener' }, h('span', { class: 'rank-no', text: String(p.rank).padStart(2, '0') }),
      thumb ? h('img', { src: big, alt: '', loading: 'lazy', onerror: (e) => { if (e.target.src !== new URL(thumb, location).href) e.target.src = thumb; else e.target.remove(); } }) : h('span', { class: 'blank', text: p.format })),
    h('div', { class: 'body' },
      h('div', { class: 'meta' }, h('span', { class: 'who', text: p.platform === 'youtube' ? 'YouTube' : 'Instagram' }), h('span', { text: p.format }), h('span', { text: `${p.age_days}d ago` }),
        h('button', { class: 'save-btn', type: 'button', text: 'Save', onclick: async (e) => {
          try { await POST('/api/library/save', { piece_id: p.id, run_id: st.data.run_id, folder: null }); e.target.textContent = 'Saved'; e.target.classList.add('on'); }
          catch (err) { notice('mine-notice', err.message, false); }
        } })),
      h('h4', {}, h('a', { href: link, target: '_blank', rel: 'noopener', text: p.title || '(no caption)' })),
      h('dl', { class: 'stats' }, stat('Views', num(p.views)), stat('Breakout', p.breakout == null ? 'n/a' : p.breakout.toFixed(1) + 'x'),
        stat(p.platform === 'youtube' ? 'Subs gained' : 'Saves + shares', p.platform === 'youtube' ? num(o.subs_gained) : num((o.saves || 0) + (o.shares || 0)))),
      h('div', { class: 'scoreline' }, h('div', { class: 'score-num' }, p.score.toFixed(1), h('small', { text: 'Studio score' })),
        h('div', { class: 'meters' }, meter('Demand', p.demand), meter('Fit', p.fit), meter('Convert', p.conv_pct))),
      h('div', { class: 'verdict' },
        h('span', { text: `${p.conversion == null ? 'n/a' : p.conversion} ${p.conversion_label || ''}` }),
        p.platform === 'youtube' && o.avg_view_pct != null ? h('span', { text: `${o.avg_view_pct.toFixed(0)}% watched` }) : null,
        h('span', { text: `ER ${pct(p.er)}` }), j.topic_lane ? h('span', { text: words(j.topic_lane) }) : null,
        j.hook_type ? h('span', { text: `hook: ${words(j.hook_type)}` }) : null)));
}

function renderPosts() {
  const r = st.data.result;
  if (!r) { fill($('posts-grid')); fill($('posts-filters')); return; }
  $('posts-note').textContent = `${r.pieces.length} of your posts from the last ${r.window_days} days, best converters first.`;
  const keys = [['all', 'All'], ['longform', 'YouTube longform'], ['short', 'YouTube Shorts'], ['instagram', 'Instagram']]
    .filter(([k]) => k === 'all' || r.pieces.some((p) => (k === 'instagram' ? p.platform === 'instagram' : p.format === k)));
  fill($('posts-filters'), keys.map(([k, label]) => h('button', { class: 'chip-btn', type: 'button', 'aria-pressed': String(st.filter === k), text: label,
    onclick: () => { st.filter = k; st.shown = PAGE; renderPosts(); } })));
  const list = r.pieces.filter((p) => st.filter === 'all' || (st.filter === 'instagram' ? p.platform === 'instagram' : p.format === st.filter));
  fill($('posts-grid'), list.slice(0, st.shown).map(postCard));
  $('posts-more').hidden = list.length <= st.shown;
}
$('posts-more').addEventListener('click', () => { st.shown += PAGE; renderPosts(); });

// ---------------------------------------------------------------------------
// Jobs: my hunt, topics, make
// ---------------------------------------------------------------------------
function jobLine(key) {
  const job = st.data.jobs[key];
  if (!job) return '';
  const age = Math.round((Date.now() / 1000) - job.started);
  return job.status === 'running' ? `${job.msg} (${Math.floor(age / 60)}m ${String(age % 60).padStart(2, '0')}s)` : job.status === 'done' ? '' : `${job.status}: ${job.msg}`;
}
function renderJobs() {
  const hunt = st.data.jobs.hunt;
  const running = hunt && hunt.status === 'running';
  $('run-mine').disabled = running;
  $('run-mine').querySelector('.run-label').textContent = running ? 'Working' : 'Analyze my content';
  $('abort-mine').hidden = !running;
  $('mine-status').textContent = jobLine('hunt');
  const topicsRunning = ['topics-longform', 'topics-short'].filter((k) => st.data.jobs[k] && st.data.jobs[k].status === 'running');
  $('topics-longform').disabled = $('topics-short').disabled = topicsRunning.length > 0;
  $('topics-status').textContent = topicsRunning.map(jobLine).join(' · ');
  const busy = Object.values(st.data.jobs).some((j) => j.status === 'running');
  clearTimeout(st.poll);
  if (busy) st.poll = setTimeout(load, 2500);
}

$('run-mine').addEventListener('click', async () => {
  const platforms = [...document.querySelectorAll('input[name="platform"]:checked')].map((el) => el.value);
  try { st.data = await POST('/api/studio/hunt', { platforms }); notice('mine-notice', 'Pulling your channels. A few minutes.'); render(); }
  catch (err) { notice('mine-notice', err.message, false); }
});
$('abort-mine').addEventListener('click', () => POST('/api/studio/abort', { job: 'hunt' }).then((d) => { st.data = d; render(); }).catch((err) => notice('mine-notice', err.message, false)));
for (const fmt of ['longform', 'short']) {
  $('topics-' + fmt).addEventListener('click', async () => {
    try { st.data = await POST('/api/studio/topics', { format: fmt }); notice('topics-notice', `Making your top 10 ${fmt} ideas. About a minute.`); render(); }
    catch (err) { notice('topics-notice', err.message, false); }
  });
}

// ---------------------------------------------------------------------------
// Topics
// ---------------------------------------------------------------------------
async function openReport(file) {
  try { st.report = await getJSON(`/api/studio/topic?file=${encodeURIComponent(file)}`); renderTopics(); }
  catch (err) { notice('topics-notice', err.message, false); }
}
function renderTopics() {
  const reports = st.data.topics;
  fill($('topics-reports'), reports.length ? reports.map((r) => h('button', { class: 'chip-btn', type: 'button', 'aria-pressed': String(st.report && st.report.file === r.file),
    text: `${r.format} · ${r.created.slice(0, 10)} · ${r.count}`, onclick: () => openReport(r.file) })) : h('p', { class: 'help', text: 'No lists yet.' }));
  if (!st.report && reports.length) return openReport(reports[0].file);
  if (!st.report) return fill($('ideas'));
  const onBoard = new Set(st.data.board.cards.map((c) => c.title));
  fill($('ideas'), st.report.ideas.map((idea, i) => h('article', { class: 'idea card' },
    h('div', { class: 'meta' }, h('span', { class: 'who', text: `#${i + 1}` }), h('span', { text: st.report.format })),
    h('h4', { text: idea.title }),
    h('blockquote', { text: idea.hook }),
    h('p', { class: 'help', text: idea.angle }),
    h('p', { class: 'help', text: idea.why }),
    h('p', { class: 'links' }, (idea.inspiration || []).map((s) => safeLink(s.url) ? h('a', { href: s.url, target: '_blank', rel: 'noopener', text: s.title || s.url }) : h('span', { class: 'help', text: s.title }))),
    onBoard.has(idea.title) ? h('span', { class: 'help', text: 'On the board' })
      : h('button', { class: 'btn-secondary', type: 'button', text: 'Add to board', onclick: async () => {
        try { st.data = await POST('/api/studio/board', { action: 'add', idea, format: st.report.format, source: st.report.file }); notice('board-notice', `Added: ${idea.title}`); render(); }
        catch (err) { notice('topics-notice', err.message, false); }
      } }))));
}

// ---------------------------------------------------------------------------
// Board
// ---------------------------------------------------------------------------
const LABELS = { ideas: 'Ideas', chosen: 'Chosen', scripted: 'Scripted', filmed: 'Filmed', posted: 'Posted' };
async function boardDo(body, done) {
  try { st.data = await POST('/api/studio/board', body); if (done) notice('board-notice', done); render(); }
  catch (err) { notice('board-notice', err.message, false); }
}
async function makeDo(card, what) {
  try { st.data = await POST('/api/studio/make', { id: card.id, what }); notice('board-notice', `${what === 'script' ? 'Writing the script' : 'Making thumbnail concepts'} for "${card.title}". About a minute.`); render(); }
  catch (err) { notice('board-notice', err.message, false); }
}
function openAsset(card, what) {
  fetch(`/studio/cards/${card.id}/${what}.md`).then((r) => r.ok ? r.text() : Promise.reject(new Error('Not made yet')))
    .then((text) => {
      const drawer = $('drawer');
      fill(drawer, h('div', { class: 'modal-card asset' }, h('div', { class: 'modal-head' }, h('h3', { class: 'tagline', text: `${card.title} · ${what}` }),
        h('span', {}, h('a', { class: 'ghost tiny', href: `/studio/cards/${card.id}/${what}.md`, download: `${what}.md`, text: 'Download .md' }),
          h('button', { class: 'ghost tiny', type: 'button', text: 'Close', onclick: () => { drawer.hidden = true; } }))),
        h('div', { class: 'asset-body breakdown' }, mdNodes(text))));
      drawer.hidden = false;
      drawer.onclick = (e) => { if (e.target === drawer) drawer.hidden = true; };
    }).catch((err) => notice('board-notice', err.message, false));
}

function boardCard(card) {
  const jobKey = (what) => `make-${card.id}-${what}`;
  const making = (what) => st.data.jobs[jobKey(what)] && st.data.jobs[jobKey(what)].status === 'running';
  const assets = card.assets || {};
  return h('article', { class: 'kcard' + (card.done ? ' done' : ''), draggable: 'true',
    ondragstart: (e) => { e.dataTransfer.setData('text/plain', card.id); e.dataTransfer.effectAllowed = 'move'; } },
    h('div', { class: 'kcard-head' },
      h('label', { class: 'check' }, h('input', { type: 'checkbox', checked: card.done, onchange: (e) => boardDo({ action: 'update', id: card.id, fields: { done: e.target.checked } }) }), h('span', { class: 'kcard-title', text: card.title })),
      h('span', { class: 'chip', text: card.format })),
    card.hook ? h('blockquote', { text: card.hook }) : null,
    card.angle ? h('p', { class: 'help', text: card.angle }) : null,
    card.inspiration && card.inspiration.length ? h('p', { class: 'links' }, card.inspiration.map((s) => safeLink(s.url) ? h('a', { href: s.url, target: '_blank', rel: 'noopener', text: s.title || s.url }) : h('span', { class: 'help', text: s.title }))) : null,
    h('textarea', { class: 'notes-box', rows: 2, placeholder: 'Notes', 'aria-label': 'Notes', onblur: (e) => { if (e.target.value !== (card.notes || '')) boardDo({ action: 'update', id: card.id, fields: { notes: e.target.value } }); } }, card.notes || ''),
    h('div', { class: 'kcard-tools' },
      h('button', { class: 'ghost tiny', type: 'button', text: making('script') ? 'Writing...' : (assets.script ? 'Script' : 'Write script'), disabled: making('script'),
        onclick: () => assets.script ? openAsset(card, 'script') : makeDo(card, 'script') }),
      assets.script ? h('button', { class: 'ghost tiny', type: 'button', text: 'Rewrite', onclick: () => makeDo(card, 'script') }) : null,
      card.format === 'longform' ? h('button', { class: 'ghost tiny', type: 'button', text: making('thumbnails') ? 'Making...' : (assets.thumbnails ? 'Thumbnails' : 'Thumbnail ideas'), disabled: making('thumbnails'),
        onclick: () => assets.thumbnails ? openAsset(card, 'thumbnails') : makeDo(card, 'thumbnails') }) : null,
      card.inspiration && card.inspiration.some((s) => s.url) ? h('button', { class: 'ghost tiny', type: 'button', text: 'Download thumbs',
        onclick: () => POST('/api/studio/download', { id: card.id }).then((d) => notice('board-notice', `${d.count} thumbnails saved to ${d.path}`)).catch((err) => notice('board-notice', err.message, false)) }) : null,
      h('a', { class: 'ghost tiny', href: `/#ask?card=${card.id}`, text: 'Ask AI' }),
      h('button', { class: 'ghost tiny danger', type: 'button', text: 'Delete', onclick: () => { if (window.confirm(`Delete "${card.title}"?`)) boardDo({ action: 'delete', id: card.id }, 'Deleted.'); } })),
    h('p', { class: 'help', text: [jobLine(jobKey('script')), jobLine(jobKey('thumbnails'))].filter(Boolean).join(' · ') }));
}

function renderBoard() {
  const cards = st.data.board.cards;
  fill($('kanban'), st.data.statuses.map((status) => h('section', { class: 'kcol', 'data-status': status,
    ondragover: (e) => { e.preventDefault(); e.currentTarget.classList.add('over'); },
    ondragleave: (e) => e.currentTarget.classList.remove('over'),
    ondrop: (e) => { e.preventDefault(); e.currentTarget.classList.remove('over'); const id = e.dataTransfer.getData('text/plain'); if (id) boardDo({ action: 'update', id, fields: { status } }); } },
    h('h3', { class: 'tagline' }, LABELS[status], h('small', { text: ` ${cards.filter((c) => c.status === status).length}` })),
    cards.filter((c) => c.status === status).map(boardCard))));
}
$('board-add').addEventListener('submit', (e) => {
  e.preventDefault();
  const title = $('board-title').value.trim();
  if (!title) return;
  boardDo({ action: 'add', idea: { title }, format: $('board-format').value, source: 'manual' }, `Added: ${title}`).then(() => { $('board-title').value = ''; });
});

// ---------------------------------------------------------------------------
function render() {
  renderAnalytics();
  renderPosts();
  renderJobs();
  renderTopics();
  renderBoard();
}
async function load() {
  try { st.data = await getJSON('/api/studio'); render(); }
  catch (err) { notice('mine-notice', `Could not reach the server: ${err.message}`, false); }
}
load();
