// Saved posts: bookmarks, folders, the analysis panel, the in-app player and the carousel viewer.
// Relies on the helpers in app.js (h, fill, $, getJSON, num, pct, words, platTag, safeLink, safeThumb, app).

const lib = { data: { folders: [], posts: [] }, folder: 'all', open: new Set(), poll: null, picker: null, slide: {}, pinned: null, modal: null, query: '' };
const RECENT = 5;
const libPost = (url, body) => getJSON(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
const savedIds = () => new Set(lib.data.posts.map((p) => p.id));
const fileUrl = (p, name) => `/library/${encodeURIComponent(p.id)}/${name}`;

function note(text, ok = true) {
  const el = $('saved-notice');
  el.className = ok ? 'notice ok' : 'notice';
  el.textContent = text;
}

window.markSaved = () => {
  const ids = savedIds();
  document.querySelectorAll('.save-btn').forEach((b) => {
    const on = ids.has(b.dataset.id);
    b.textContent = on ? 'Saved' : 'Save';
    b.classList.toggle('on', on);
  });
};

window.loadLibrary = async () => {
  try { lib.data = await getJSON('/api/library'); } catch (err) { note(err.message, false); return; }
  renderLibrary();
  window.markSaved();
  if (window.renderAskSide) renderAskSide();
  clearTimeout(lib.poll);
  if (lib.data.posts.some((p) => p.running)) lib.poll = setTimeout(window.loadLibrary, 2500);
};

async function act(url, body, done) {
  try {
    const res = await libPost(url, body);
    lib.data = { folders: res.folders, posts: res.posts };
    renderLibrary();
    window.markSaved();
    if (done) note(typeof done === 'function' ? done(res) : done);
    if (lib.data.posts.some((p) => p.running)) { clearTimeout(lib.poll); lib.poll = setTimeout(window.loadLibrary, 2500); }
    return res;
  } catch (err) { note(err.message, false); return null; }
}

// ---------------------------------------------------------------------------
// Bookmark picker on a target card
// ---------------------------------------------------------------------------
function closePicker() { if (lib.picker) { lib.picker.remove(); lib.picker = null; } }
document.addEventListener('click', (e) => { if (lib.picker && !lib.picker.contains(e.target) && !e.target.classList.contains('save-btn')) closePicker(); });

window.openPicker = (button, piece) => {
  if (lib.picker && lib.picker.dataset.id === piece.id) return closePicker();
  closePicker();
  const save = (folder) => act('/api/library/save', { piece_id: piece.id, run_id: app.result.run_id, folder },
    () => { closePicker(); return piece.kind === 'static' ? `${piece.title.slice(0, 50)} saved. Pulling its slides now.` : `${piece.title.slice(0, 50)} saved.`; })
    .then(() => $('saved-window').scrollIntoView({ behavior: 'smooth', block: 'start' }));
  const box = h('div', { class: 'picker', 'data-id': piece.id },
    h('p', { class: 'eyebrow', text: 'Save to' }),
    h('button', { class: 'pick', type: 'button', text: 'No folder', onclick: () => save(null) }),
    lib.data.folders.map((f) => h('button', { class: 'pick', type: 'button', text: f.name, onclick: () => save(f.id) })),
    h('form', { class: 'pick-new', onsubmit: async (e) => {
      e.preventDefault();
      const name = e.target.elements.name.value.trim();
      if (!name) return;
      const res = await act('/api/library/folder', { action: 'create', name });
      const made = res && res.folders.find((f) => f.name === name);
      if (made) save(made.id);
    } }, h('input', { name: 'name', placeholder: 'New folder', autocomplete: 'off', 'aria-label': 'New folder name' }), h('button', { class: 'ghost', type: 'submit', text: 'Create' })));
  button.closest('.body').append(box);
  lib.picker = box;
};

// ---------------------------------------------------------------------------
// Folder bar
// ---------------------------------------------------------------------------
function renderFolders() {
  const posts = lib.data.posts;
  const chip = (id, label, count) => h('button', { class: 'chip-btn', type: 'button', 'aria-pressed': String(lib.folder === id),
    text: `${label} ${count}`, onclick: () => { lib.folder = id; renderLibrary(); } });
  const active = lib.data.folders.find((f) => f.id === lib.folder);
  fill($('folder-bar'),
    chip('all', 'All', posts.length),
    chip('none', 'Unfiled', posts.filter((p) => !p.folder).length),
    lib.data.folders.map((f) => chip(f.id, f.name, posts.filter((p) => p.folder === f.id).length)),
    h('form', { class: 'folder-new', onsubmit: (e) => {
      e.preventDefault();
      const name = e.target.elements.name.value.trim();
      if (name) act('/api/library/folder', { action: 'create', name }, `Folder ${name} created.`).then(() => { e.target.reset(); });
    } }, h('input', { name: 'name', placeholder: 'New folder', autocomplete: 'off', 'aria-label': 'New folder name' }), h('button', { class: 'ghost', type: 'submit', text: '+ Folder' })),
    active ? h('span', { class: 'folder-tools' },
      h('button', { class: 'ghost', type: 'button', text: 'Rename', onclick: () => {
        const name = window.prompt('Rename folder', active.name);
        if (name && name.trim()) act('/api/library/folder', { action: 'rename', id: active.id, name: name.trim() }, 'Renamed.');
      } }),
      h('button', { class: 'ghost danger', type: 'button', text: 'Delete folder', onclick: () => {
        if (window.confirm(`Delete folder "${active.name}"? Its posts stay saved, unfiled.`)) act('/api/library/folder', { action: 'delete', id: active.id }, 'Folder deleted.').then(() => { lib.folder = 'all'; renderLibrary(); });
      } })) : null);
}

// ---------------------------------------------------------------------------
// Saved cards
// ---------------------------------------------------------------------------
function statusLine(p) {
  const s = p.status;
  if (p.running) return h('p', { class: 'job running' }, `${p.running}: ${s ? s.msg : 'starting'} `,
    h('button', { class: 'ghost danger tiny', type: 'button', text: 'Abort', onclick: () => act('/api/library/abort', { id: p.id }, 'Stopped.') }));
  if (s && s.error) return h('p', { class: 'job failed', text: `Failed: ${s.msg}` });
  return null;
}

function savedCard(p) {
  const piece = p.piece, a = p.analysis || {}, j = piece.jev || {};
  const thumb = safeThumb(piece.thumb), carousel = piece.kind === 'static', busy = Boolean(p.running);
  const run = (mode, label, title) => h('button', { class: 'ghost act', type: 'button', text: label, title, disabled: busy,
    onclick: () => act('/api/library/analyze', { id: p.id, mode }, `${label} started.`) });
  const actions = carousel
    ? [run('fetch', 'Pull slides', 'Download every slide of the carousel'), run('both', 'Analyze', 'Claude reads the slides and writes why it worked'),
       (a.media && a.media.slides && a.media.slides.length) ? h('button', { class: 'ghost act', type: 'button', text: 'Download slides', title: 'Copy the slides into your Downloads folder, named after the carousel',
         onclick: () => act('/api/library/download', { id: p.id }, (res) => `${res.count} slides saved to ${res.path}`) }) : null]
    : [run('fetch', 'Load video', 'Pull the video so it plays here'), run('transcribe', 'Transcribe', 'Whisper transcript, spoken hook, caption'),
       run('watch', 'Watch', 'Frame sheets, then Claude reads them'), run('both', 'Both', 'Transcript + frames + why it worked'),
       run('ffmpeg', 'FFmpeg only', 'Download and cut frame sheets, no Claude')];
  actions.push(h('button', { class: 'ghost act', type: 'button', text: 'Ask AI', title: 'Open a chat about this post', onclick: () => window.openAsk(p.id) }));
  const hasResults = a.updated || (a.media && (a.media.video || (a.media.slides && a.media.slides.length)));
  const opened = lib.open.has(p.id);
  return h('article', { class: 'saved-card' + (opened ? ' opened' : '') },
    h('div', { class: 'saved-top' },
      h('a', { class: 'thumb', href: safeLink(piece.url), target: '_blank', rel: 'noopener', 'aria-label': `Open: ${piece.title}` },
        thumb ? h('img', { src: thumb, alt: '', loading: 'lazy' }) : h('span', { class: 'blank', text: piece.format }),
        h('span', { class: 'rank-no', text: piece.score.toFixed(0) })),
      h('div', { class: 'body' },
        h('div', { class: 'meta' }, h('span', { class: 'who', text: piece.competitor.split('|')[0].trim() }),
          h('span', { text: `${platTag(piece.platform)} ${piece.format}` }), h('span', { text: `saved ${p.saved_at.slice(0, 10)}` })),
        h('h4', {}, h('a', { href: safeLink(piece.url), target: '_blank', rel: 'noopener', text: piece.title || '(no caption)' })),
        h('div', { class: 'verdict' },
          h('span', { text: `hunt ${piece.score.toFixed(1)}` }), h('span', { text: `breakout ${piece.breakout == null ? 'n/a' : piece.breakout.toFixed(1) + 'x'}` }),
          h('span', { text: `ER ${pct(piece.er)}` }), piece.views != null ? h('span', { text: `${num(piece.views)} views` }) : null,
          j.topic_lane ? h('span', { text: words(j.topic_lane) }) : null, j.hook_type ? h('span', { text: `hook: ${words(j.hook_type)}` }) : null,
          j.proof != null ? h('span', { text: `proof ${Math.round(j.proof)}/3` }) : null, j.replicable != null ? h('span', { text: `replicable ${Math.round(j.replicable * 100)}%` }) : null),
        h('div', { class: 'saved-tools' },
          h('label', { class: 'move' }, 'Folder',
            h('select', { 'aria-label': 'Folder', onchange: (e) => act('/api/library/move', { id: p.id, folder: e.target.value || null }, 'Moved.') },
              h('option', { value: '', text: 'Unfiled', selected: !p.folder }),
              lib.data.folders.map((f) => h('option', { value: f.id, text: f.name, selected: p.folder === f.id })))),
          h('button', { class: 'ghost danger', type: 'button', text: 'Remove', disabled: busy, onclick: () => {
            if (window.confirm('Remove this saved post and its files?')) act('/api/library/remove', { id: p.id }, 'Removed.');
          } })),
        h('div', { class: 'actions-row' }, actions),
        statusLine(p),
        hasResults ? h('button', { class: 'ghost open', type: 'button', text: opened ? 'Close analysis' : 'Open analysis',
          onclick: () => { if (opened) lib.open.delete(p.id); else lib.open.add(p.id); renderLibrary(); } }) : null)),
    opened ? analysisPanel(p) : null);
}

// ---------------------------------------------------------------------------
// The analysis panel: player or carousel viewer, hook, caption, transcript, sheets, breakdown
// ---------------------------------------------------------------------------
function mdNodes(text) {
  const out = [];
  let list = null;
  for (const raw of text.split('\n')) {
    const line = raw.replace(/\*\*/g, '').trimEnd();
    if (!line.trim()) { list = null; continue; }
    if (line.startsWith('## ')) { list = null; out.push(h('h5', { text: line.slice(3) })); }
    else if (/^[-*] /.test(line)) { if (!list) { list = h('ul'); out.push(list); } list.append(h('li', { text: line.slice(2) })); }
    else { list = null; out.push(h('p', { text: line.replace(/^#+\s*/, '') })); }
  }
  return out;
}

function carouselViewer(p, slides) {
  const idx = lib.slide[p.id] || 0;
  const show = (i) => { lib.slide[p.id] = (i + slides.length) % slides.length; renderLibrary(); };
  return h('div', { class: 'viewer' },
    h('img', { class: 'slide', src: fileUrl(p, slides[idx]), alt: `Slide ${idx + 1} of ${slides.length}` }),
    h('div', { class: 'viewer-bar' },
      h('button', { class: 'ghost', type: 'button', text: 'Prev', onclick: () => show(idx - 1) }),
      h('span', { class: 'counter', text: `${idx + 1} / ${slides.length}` }),
      h('button', { class: 'ghost', type: 'button', text: 'Next', onclick: () => show(idx + 1) }),
      h('button', { class: 'ghost act', type: 'button', text: 'Download all', onclick: () => act('/api/library/download', { id: p.id }, (res) => `${res.count} slides saved to ${res.path}`) })),
    h('div', { class: 'strip' }, slides.map((s, i) => h('button', { class: 'strip-btn' + (i === idx ? ' on' : ''), type: 'button', 'aria-label': `Slide ${i + 1}`, onclick: () => show(i) },
      h('img', { src: fileUrl(p, s), alt: '', loading: 'lazy' })))));
}

function analysisPanel(p) {
  const a = p.analysis || {}, media = a.media || {};
  const panel = h('div', { class: 'analysis' });
  if (media.slides && media.slides.length) panel.append(carouselViewer(p, media.slides));
  else if (media.video) panel.append(h('video', { class: 'player', controls: true, preload: 'metadata', src: fileUrl(p, 'media.mp4') }));
  const columns = h('div', { class: 'analysis-grid' });
  panel.append(columns);
  if (a.transcript) {
    columns.append(h('section', {}, h('h5', { text: 'Spoken hook' }),
      h('blockquote', { text: a.transcript.silent ? 'No speech in this video.' : a.transcript.hook || '(nothing in the first 8 seconds)' }),
      h('p', { class: 'fine', text: a.transcript.silent ? '' : `${a.transcript.words} words, Whisper ${a.transcript.model}` })));
  }
  columns.append(h('section', {}, h('h5', { text: p.piece.platform === 'youtube' ? 'Description' : 'Caption' }),
    h('p', { class: 'caption', text: p.piece.description || '(none)' })));
  if (a.transcript && !a.transcript.silent) {
    const box = h('details', {}, h('summary', {}, 'Full transcript'));
    box.addEventListener('toggle', async () => {
      if (!box.open || box.dataset.loaded) return;
      box.dataset.loaded = '1';
      try {
        const t = await getJSON(fileUrl(p, 'transcript.json'));
        box.append(h('div', { class: 'q' }, t.segments.map((s) => h('p', {}, h('span', { class: 'ts', text: `${Math.floor(s.start / 60)}:${String(Math.floor(s.start % 60)).padStart(2, '0')}` }), ' ' + s.text))));
      } catch (err) { box.append(h('p', { class: 'fine', text: err.message })); }
    });
    columns.append(h('section', { class: 'wide' }, box));
  }
  if (a.sheets && a.sheets.length) {
    columns.append(h('section', { class: 'wide' }, h('h5', { text: 'Frame sheets' }),
      h('div', { class: 'sheets' }, a.sheets.map((s) => h('a', { href: fileUrl(p, s), target: '_blank', rel: 'noopener' },
        h('img', { src: fileUrl(p, s), alt: s === 'hook.jpg' ? 'Hook sheet, first 20 seconds' : s, loading: 'lazy' }))))));
  }
  if (a.breakdown) {
    const box = h('section', { class: 'wide breakdown' }, h('h5', { text: 'Why it worked' }), h('p', { class: 'fine', text: 'Loading Claude\'s breakdown' }));
    fetch(fileUrl(p, 'breakdown.md')).then((r) => r.text()).then((text) => fill(box, h('h5', { text: 'Why it worked' }), mdNodes(text),
      a.fable_cost_usd ? h('p', { class: 'fine', text: `Written by ${modelLabel(a.breakdown_model || 'fable')}, $${a.fable_cost_usd.toFixed(2)} API-equivalent` }) : null))
      .catch((err) => fill(box, h('p', { class: 'fine', text: err.message })));
    columns.append(box);
  }
  return panel;
}

function inFolder(p) { return lib.folder === 'all' || (lib.folder === 'none' ? !p.folder : p.folder === lib.folder); }

function renderLibrary() {
  renderFolders();
  const all = [...lib.data.posts].sort((a, b) => b.saved_at.localeCompare(a.saved_at)).filter(inFolder);
  const pinned = all.find((p) => p.id === lib.pinned);
  const recent = all.slice(0, RECENT);
  const shown = pinned && !recent.includes(pinned) ? [pinned, ...recent] : recent;
  fill($('saved-grid'), shown.length ? shown.map(savedCard) : h('p', { class: 'help', text: lib.data.posts.length ? 'Nothing in this folder yet.' : 'Nothing saved yet. Press Save on any post above.' }),
    all.length > shown.length ? h('div', { class: 'more' }, h('button', { class: 'btn-secondary', type: 'button', text: `See all ${all.length} saved`, onclick: openBrowser })) : null);
  if (lib.modal) renderBrowser();
}

// A pop-up to find any saved post: search box, folder chips, compact rows.
function openBrowser() {
  if (lib.modal) return;
  lib.modal = h('div', { class: 'modal', role: 'dialog', 'aria-label': 'All saved posts', onclick: (e) => { if (e.target === lib.modal) closeBrowser(); } },
    h('div', { class: 'modal-card' },
      h('div', { class: 'modal-head' }, h('h3', { class: 'tagline', text: 'All saved posts' }),
        h('button', { class: 'ghost tiny', type: 'button', text: 'Close', onclick: closeBrowser })),
      h('input', { type: 'search', class: 'modal-search', placeholder: 'Search titles, creators, folders', 'aria-label': 'Search saved posts', value: lib.query,
        oninput: (e) => { lib.query = e.target.value; renderBrowser(); } }),
      h('div', { class: 'folder-bar modal-folders' }),
      h('ul', { class: 'browse' })));
  document.body.append(lib.modal);
  document.addEventListener('keydown', escBrowser);
  renderBrowser();
  lib.modal.querySelector('.modal-search').focus();
}
function escBrowser(e) { if (e.key === 'Escape') closeBrowser(); }
function closeBrowser() { if (!lib.modal) return; lib.modal.remove(); lib.modal = null; document.removeEventListener('keydown', escBrowser); }
function renderBrowser() {
  const q = lib.query.trim().toLowerCase();
  const folderName = (p) => (lib.data.folders.find((f) => f.id === p.folder) || { name: 'Unfiled' }).name;
  const chip = (id, label) => h('button', { class: 'chip-btn', type: 'button', 'aria-pressed': String(lib.folder === id), text: label,
    onclick: () => { lib.folder = id; renderLibrary(); } });
  fill(lib.modal.querySelector('.modal-folders'), chip('all', 'All'), chip('none', 'Unfiled'), lib.data.folders.map((f) => chip(f.id, f.name)));
  const rows = [...lib.data.posts].sort((a, b) => b.saved_at.localeCompare(a.saved_at)).filter(inFolder)
    .filter((p) => !q || `${p.piece.title} ${p.piece.competitor} ${folderName(p)}`.toLowerCase().includes(q));
  fill(lib.modal.querySelector('.browse'), rows.length ? rows.map((p) => h('li', {},
    safeThumb(p.piece.thumb) ? h('img', { src: safeThumb(p.piece.thumb), alt: '' }) : h('span', { class: 'blank' }),
    h('div', { class: 'browse-text' }, h('b', { text: p.piece.title.slice(0, 90) || '(no caption)' }),
      h('span', { class: 'help', text: `${p.piece.competitor.split('|')[0].trim()} · ${platTag(p.piece.platform)} ${p.piece.format} · ${folderName(p)} · score ${p.piece.score.toFixed(0)}` })),
    h('span', { class: 'browse-actions' },
      h('button', { class: 'ghost tiny', type: 'button', text: 'Open', onclick: () => { lib.pinned = p.id; lib.open.add(p.id); closeBrowser(); renderLibrary(); $('saved-grid').scrollIntoView({ behavior: 'smooth', block: 'start' }); } }),
      h('button', { class: 'ghost tiny', type: 'button', text: 'Ask AI', onclick: () => { closeBrowser(); window.openAsk(p.id); } }))))
    : h('li', { class: 'help', text: 'No saved posts match.' }));
}
