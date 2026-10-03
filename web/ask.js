// Ask AI: a chat over a context pack (saved posts, top hunt pieces, Reddit ideas) run by headless Claude Code.
// Relies on helpers in app.js (h, fill, $, getJSON) and lib from library.js.

const ask = { chat: null, picked: new Set(), chats: [], cards: new Set(), cardTitles: {} };

function askNote(text, ok = true) {
  const el = $('ask-notice');
  el.className = ok ? 'notice ok' : 'notice';
  el.textContent = text;
}

function renderAskSide() {
  const saved = (lib.data && lib.data.posts) || [];
  fill($('ask-saved'), saved.length ? saved.map((p) => h('li', {}, h('label', { class: 'check' },
    h('input', { type: 'checkbox', checked: ask.picked.has(p.id), disabled: Boolean(ask.chat),
      onchange: (e) => { if (e.target.checked) ask.picked.add(p.id); else ask.picked.delete(p.id); } }),
    h('span', { text: p.piece.title.slice(0, 70) || '(no caption)' })))) : h('li', { class: 'help', text: 'Nothing saved yet.' }));
  fill($('ask-cards'), [...ask.cards].map((id) => h('li', { class: 'help', text: `Board card: ${ask.cardTitles[id] || id}` })));
  fill($('ask-chats'), ask.chats.length ? ask.chats.map((c) => h('li', {},
    h('button', { class: 'chat-link' + (ask.chat && ask.chat.id === c.id ? ' on' : ''), type: 'button', text: `${c.title} (${c.turns})`,
      onclick: () => openChat(c.id) }))) : h('li', { class: 'help', text: 'No chats yet.' }));
  $('ask-top').disabled = $('ask-reddit').disabled = $('ask-reports').disabled = Boolean(ask.chat);
}

function renderThread() {
  const thread = $('ask-thread');
  const messages = ask.chat ? ask.chat.messages : [];
  fill(thread, messages.length ? messages.map((m) => h('li', { class: 'msg ' + m.role },
    h('span', { class: 'who', text: m.role === 'you' ? 'You' : 'AI' }),
    h('div', { class: 'bubble' }, m.text.split('\n').filter((line) => line.trim()).map((line) => h('p', { text: line.replace(/\*\*/g, '') })))))
    : h('li', { class: 'msg hint', text: 'Pick posts on the left, then ask anything. The first answer takes about a minute.' }));
  thread.scrollTop = thread.scrollHeight;
}

async function openChat(id) {
  try {
    ask.chat = await getJSON(`/api/chat?id=${encodeURIComponent(id)}`);
    $('ask-top').checked = Boolean(ask.chat.include_top);
    $('ask-reddit').checked = Boolean(ask.chat.include_reddit);
    renderAskSide();
    renderThread();
  } catch (err) { askNote(err.message, false); }
}

async function loadChats() {
  try { ask.chats = (await getJSON('/api/chats')).chats; } catch { ask.chats = []; }
  renderAskSide();
}

window.openAsk = (pieceId) => {
  const panel = $('ask');
  panel.hidden = false;
  if (pieceId) { ask.chat = null; ask.picked = new Set([pieceId]); $('ask-top').checked = false; $('ask-reddit').checked = false; }
  renderAskSide();
  renderThread();
  requestAnimationFrame(() => {
    panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
    setTimeout(() => $('ask-input').focus({ preventScroll: true }), 450);
  });
};
document.querySelectorAll('a[href="#ask"]').forEach((a) => a.addEventListener('click', (e) => { e.preventDefault(); window.openAsk(null); }));
$('ask-input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); $('ask-form').requestSubmit(); }   // Enter sends, Shift+Enter breaks a line
});

$('ask-btn').addEventListener('click', () => window.openAsk(null));
$('ask-new').addEventListener('click', () => { ask.chat = null; ask.picked = new Set(); ask.cards = new Set(); renderAskSide(); renderThread(); askNote(''); });
// /#ask?card=<id> from the Studio board opens a chat with that card in the pack.
(async () => {
  const match = location.hash.match(/^#ask\?card=([0-9a-f]{8})$/);
  if (!match) return;
  try {
    const data = await getJSON('/api/studio');
    const card = data.board.cards.find((c) => c.id === match[1]);
    if (card) { ask.cards = new Set([card.id]); ask.cardTitles[card.id] = card.title; }
  } catch { /* studio unavailable */ }
  $('ask-top').checked = false;
  setTimeout(() => window.openAsk(null), 600);
})();

$('ask-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const input = $('ask-input'), message = input.value.trim();
  if (!message) return;
  const send = $('ask-send');
  send.disabled = true;
  send.textContent = 'Thinking';
  ask.token = Math.random().toString(36).slice(2, 14);
  $('ask-abort').hidden = false;
  askNote(ask.chat ? 'Asking...' : 'Building the context pack and asking. About a minute.');
  try {
    ask.chat = await getJSON('/api/ask', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({
      chat_id: ask.chat ? ask.chat.id : null, message, pieces: [...ask.picked], token: ask.token, cards: [...ask.cards],
      include_top: $('ask-top').checked, include_reddit: $('ask-reddit').checked, include_reports: $('ask-reports').checked }) });
    input.value = '';
    askNote('');
    renderThread();
    loadChats();
  } catch (err) { askNote(err.message, false); }
  send.disabled = false;
  send.textContent = 'Ask';
  $('ask-abort').hidden = true;
});
$('ask-abort').addEventListener('click', () => {
  if (!ask.token) return;
  getJSON('/api/ask/abort', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token: ask.token }) }).catch(() => {});
});

loadChats();
