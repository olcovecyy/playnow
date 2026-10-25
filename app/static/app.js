'use strict';

let initialShareToken = null;
const incomingFragment = new URLSearchParams(location.hash.slice(1));
if (incomingFragment.has('access')) {
  initialShareToken = incomingFragment.get('access');
  incomingFragment.delete('access');
  const remainingFragment = incomingFragment.toString();
  history.replaceState(history.state, '', location.pathname + location.search + (remainingFragment ? `#${remainingFragment}` : ''));
}

const icons = {
  lock: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="5" y="10" width="14" height="11" rx="3"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/><path d="M12 14v3"/></svg>',
  play: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M7 4.7c0-.8.9-1.2 1.5-.8l12 7.2a1 1 0 0 1 0 1.8l-12 7.2c-.6.4-1.5 0-1.5-.8z"/></svg>',
  arrow: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 12h15m-6-6 6 6-6 6"/></svg>',
  eye: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/></svg>',
  upload: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M12 16V3m-5 5 5-5 5 5M4 15v4a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-4"/></svg>',
  film: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="3" y="3" width="18" height="18" rx="3"/><path d="M7 3v18M17 3v18M3 8h4M3 16h4M17 8h4M17 16h4M7 12h10"/></svg>',
  shield: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6z"/><path d="m8 12 3 3 5-6"/></svg>'
};
const $ = (id) => document.getElementById(id);
const state = { role: null, csrf: '', scopedVideoId: null, requestedVideoId: new URLSearchParams(location.search).get('video'), shareVideoId: null, displayedShareId: null, selectionRevision: 0, preparingFile: false, keepAwake: true, videos: [], uploads: [], storage: null, upload: null, pendingSelection: null, editId: null, editExtension: '', adminPage: location.pathname.startsWith('/admin') };
let toastTimer;
let processingTimer;
let playerVideoId = null;
let playerQuality = null;
let playerSourceRevision = 0;
let shareViewRevision = 0;
let screenWakeLock = null;
let screenWakeLockRequest = null;
let screenWakeLockRevision = 0;
const REACTIONS = Object.freeze(['👍', '🤣', '🤪', '😭', '😮']);
const REACTION_LABELS = Object.freeze({ '👍': 'Líbí se mi', '🤣': 'To je sranda', '🤪': 'Bláznivé', '😭': 'Rozplakalo mě', '😮': 'Překvapilo mě' });
const engagementCache = new Map();
const reactionRequests = new Map();
const engagementControllers = new Set();
let engagementEpoch = 0;
let engagementReadSequence = 0;
let engagementMutationSequence = 0;
let playbackGeneration = 0;
let playbackTracker = null;
function node(tag, classes, text) { const el = document.createElement(tag); if (classes) el.className = classes; if (text !== undefined) el.textContent = text; return el; }
function icon(name, classes) { const el = node('span', classes); el.innerHTML = icons[name]; return el; }
function button(text, classes, handler) { const el = node('button', classes, text); el.type = 'button'; if (handler) el.addEventListener('click', handler); return el; }
function toast(message) { $('toast').textContent = message; $('toast').classList.add('visible'); clearTimeout(toastTimer); toastTimer = setTimeout(() => $('toast').classList.remove('visible'), 5500); }
function size(bytes) { if (!Number.isFinite(Number(bytes))) return '—'; const n = Number(bytes); if (n < 1024) return `${n} B`; const units = ['KiB', 'MiB', 'GiB', 'TiB']; const p = Math.min(3, Math.floor(Math.log(n) / Math.log(1024)) - 1); return `${(n / (1024 ** (p + 1))).toLocaleString('cs-CZ', { maximumFractionDigits: p > 0 ? 1 : 0 })} ${units[p]}`; }
function duration(seconds) { if (!seconds || seconds < 1) return ''; const s = Math.floor(seconds); return s >= 3600 ? `${Math.floor(s / 3600)}:${String(Math.floor(s / 60) % 60).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}` : `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`; }
function eta(seconds) { if (!Number.isFinite(seconds) || seconds < 0) return ''; if (seconds < 60) return 'méně než minutu'; if (seconds < 3600) return `asi ${Math.ceil(seconds / 60)} min`; return `asi ${Math.floor(seconds / 3600)} h ${Math.ceil((seconds % 3600) / 60)} min`; }
function date(value) { if (!value) return ''; const d = new Date(typeof value === 'number' ? value * 1000 : value); return Number.isNaN(d.getTime()) ? '' : d.toLocaleDateString('cs-CZ', { day: 'numeric', month: 'short', year: 'numeric' }); }
function remember(upload) { try { const all = JSON.parse(localStorage.getItem('playnow.uploads') || '{}'); all[upload.id] = { filename: upload.file.name, size: upload.file.size, lastModified: upload.file.lastModified, content_fingerprint: upload.contentFingerprint || null }; localStorage.setItem('playnow.uploads', JSON.stringify(all)); } catch (_) {} }
function remembered(id) { try { return JSON.parse(localStorage.getItem('playnow.uploads') || '{}')[id]; } catch (_) { return null; } }
function applySession(session) { const nextRole = session.role || null, nextCsrf = session.csrf || '', nextVideo = session.video_id || null; if (state.role !== nextRole || state.csrf !== nextCsrf || state.scopedVideoId !== nextVideo) resetEngagementSession(); state.role = nextRole; state.csrf = nextCsrf; state.scopedVideoId = nextVideo; }
function forget(id) { try { const all = JSON.parse(localStorage.getItem('playnow.uploads') || '{}'); delete all[id]; localStorage.setItem('playnow.uploads', JSON.stringify(all)); } catch (_) {} }
async function api(path, options = {}) {
  const requestEpoch = engagementEpoch;
  const opts = { credentials: 'same-origin', ...options, headers: { ...(options.headers || {}) } };
  if (opts.method && opts.method !== 'GET') opts.headers['X-CSRF-Token'] = state.csrf;
  if (opts.json !== undefined) { opts.body = JSON.stringify(opts.json); opts.headers['Content-Type'] = 'application/json'; delete opts.json; }
  const response = await fetch(path, opts);
  let body; try { body = await response.json(); } catch (error) { if (error.name === 'AbortError' || opts.signal?.aborted) throw error; body = {}; }
  if (opts.signal?.aborted) { const aborted = new Error('Request aborted'); aborted.name = 'AbortError'; throw aborted; }
  if (!response.ok) {
    const error = new Error(body.error || `Požadavek se nepodařil (${response.status}).`); error.status = response.status; error.data = body;
    if (response.status === 401 && requestEpoch === engagementEpoch && path !== '/api/login' && path !== '/api/share/redeem') {
      state.role = null; state.scopedVideoId = null;
      if (state.upload) { state.upload.paused = true; state.upload.controller?.abort(); }
      renderLogin('Přístup vypršel nebo byl zrušený. Můžeš se přihlásit heslem.');
      const refreshEpoch = engagementEpoch, refreshController = new AbortController();
      engagementControllers.add(refreshController);
      try {
        const fresh = await fetch('/api/session', { credentials: 'same-origin', signal: refreshController.signal });
        if (fresh.ok && refreshEpoch === engagementEpoch && !refreshController.signal.aborted) {
          const session = await fresh.json();
          if (refreshEpoch === engagementEpoch && !refreshController.signal.aborted) applySession(session);
        }
      } catch (_) {} finally { engagementControllers.delete(refreshController); }
    }
    throw error;
  }
  return body;
}
function header() {
  const target = $('header-actions'); target.replaceChildren(); target.classList.toggle('authenticated-nav', !!state.role);
  if (state.role && !(state.adminPage && state.role !== 'admin')) {
    if (state.role === 'admin' && state.adminPage) target.append(node('span', 'admin-view-badge', 'Správce'));
    else if (state.role !== 'admin') { const badge = node('span', 'private-badge'); badge.append(icon('lock'), node('span', '', state.scopedVideoId ? 'Sdílené video' : 'Soukromá knihovna')); target.append(badge); }
    if (!state.adminPage) { const manager = node('a', 'nav-link', 'Správce'); manager.href = '/admin'; target.append(manager); }
    target.append(button('Odhlásit', 'nav-link', logout));
  } else {
    const badge = node('span', 'private-badge'); badge.append(icon('lock'), node('span', '', 'Soukromá knihovna')); target.append(badge);
  }
}
function renderLogin(message = '') {
  resetEngagementSession();
  ++state.selectionRevision; state.preparingFile = false;
  clearTimeout(processingTimer); closePlayer(); releaseScreenWakeLock(); if ($('share-dialog').open) $('share-dialog').close();
  header(); const main = $('app'); main.replaceChildren();
  const layout = node('section', 'login-layout');
  const story = node('div', 'login-story'); const eyebrow = node('p', 'eyebrow'); eyebrow.append(node('span', 'tiny-line'), document.createTextNode('MALÉ KINO. NAŠE VELKÉ CHVÍLE.'));
  const heading = node('h1'); heading.append(document.createTextNode('Dobrý večer.')); heading.append(document.createElement('br')); heading.append(node('em', '', 'Co si pustíme?'));
  story.append(eyebrow, heading, node('p', 'lead', 'Naše videa na jednom místě. Pro rodinu, přátele a všechny společné večery.'), node('div', 'story-note', 'Udělej si pohodlí. Zbytek počká.'));
  const cinema = node('div', 'cinema-card'); cinema.setAttribute('aria-hidden', 'true'); cinema.append(node('span', 'cinema-caption', 'A LITTLE TIME TOGETHER'), node('span', 'cinema-play', '▶')); story.append(cinema);
  const card = node('div', 'login-card'); card.append(icon(state.adminPage ? 'upload' : 'lock', 'card-icon'), node('h2', '', state.adminPage ? 'Vítej ve správě.' : 'Místo máš rezervované.'), node('p', 'subtext', state.adminPage ? 'Zadej správcovské heslo a přidej do našeho kina další video.' : 'Zadej naše společné heslo. A vyber si něco, na co máš právě náladu.'));
  const form = node('form'); const label = node('label', '', state.adminPage ? 'Správcovské heslo' : 'Heslo do kina'); label.htmlFor = 'login-password';
  const wrap = node('div', 'password-wrap'); const input = node('input'); input.id = 'login-password'; input.type = 'password'; input.autocomplete = 'current-password'; input.required = true; input.placeholder = 'Tvoje vstupenka'; input.maxLength = 1024;
  const toggle = button('', 'password-toggle', () => { input.type = input.type === 'password' ? 'text' : 'password'; toggle.setAttribute('aria-label', input.type === 'password' ? 'Zobrazit heslo' : 'Skrýt heslo'); toggle.setAttribute('aria-pressed', String(input.type === 'text')); }); toggle.setAttribute('aria-label', 'Zobrazit heslo'); toggle.append(icon('eye')); wrap.append(input, toggle);
  const error = node('p', 'form-error', message); error.setAttribute('role', 'alert'); const submit = node('button', 'button primary'); submit.type = 'submit'; submit.append(document.createTextNode(state.adminPage ? 'Otevřít správu' : 'Play now'), icon('arrow'));
  form.append(label, wrap, error, submit); form.addEventListener('submit', async (event) => { event.preventDefault(); submit.disabled = true; error.textContent = ''; try { const session = await api('/api/login', { method: 'POST', json: { role: state.adminPage ? 'admin' : 'viewer', password: input.value } }); applySession(session); input.value = ''; await loadCatalog(); openRequestedVideo(); } catch (err) { error.textContent = err.message; } finally { submit.disabled = false; } });
  const foot = node('div', 'login-foot'); foot.append(icon('shield'), node('span', '', 'Videa jsou soukromá. Přístup mají jen ti, se kterými sdílíme heslo.')); card.append(form, foot);
  const switchLink = node('a', 'nav-link', state.adminPage ? '← Zpátky do kina' : 'Správce'); switchLink.href = state.adminPage ? '/' : '/admin'; switchLink.style.cssText = 'display:block;text-align:center;margin-top:17px;font-size:10px'; card.append(switchLink);
  layout.append(story, card); main.append(layout);
}
async function logout() {
  if (state.upload && !state.upload.paused && !confirm('Nahrávání se pozastaví. Chceš se odhlásit?')) return;
  ++state.selectionRevision; state.preparingFile = false; state.pendingSelection = null;
  resetEngagementSession();
  if (state.upload) { state.upload.paused = true; state.upload.controller?.abort(); }
  releaseScreenWakeLock();
  try { await api('/api/logout', { method: 'POST' }); } catch (error) { if (error.status !== 401) { toast(error.message); return; } }
  state.role = null; state.scopedVideoId = null; state.upload = null; state.csrf = ''; try { const session = await api('/api/session'); applySession(session); } catch (_) {} renderLogin();
}
async function loadCatalog() {
  const read = engagementReadContext(); const result = await api('/api/videos'); if (read.epoch !== engagementEpoch || !state.role) return; state.videos = result.videos || []; state.storage = result.storage; ingestCatalogEngagement(state.videos, read);
  if (state.role === 'admin' && state.adminPage) { const uploads = await api('/api/uploads'); state.uploads = uploads.uploads || []; }
  renderCatalog();
  scheduleProcessingPoll();
}
function renderCatalog() {
  header(); const main = $('app'); main.replaceChildren(); const catalog = node('section', 'catalog'); const top = node('div', 'catalog-heading'); const title = node('div'); title.append(node('p', 'eyebrow', state.adminPage ? 'ZA OPONOU' : state.scopedVideoId ? 'JEN PRO TEBE' : 'VEČER PODLE TEBE'), node('h1', '', state.adminPage ? 'Správce videí.' : state.scopedVideoId ? 'Video pro tebe.' : 'Vítej v našem kině.'), node('p', 'catalog-intro', state.adminPage ? 'Nahrávej, přejmenovávej a sdílej naše videa.' : state.scopedVideoId ? 'Udělej si pohodlí. Tohle video s tebou někdo sdílel.' : 'Vyber si video, udělej si pohodlí a stiskni Play now.'));
  const n = state.videos.length; top.append(title, node('span', 'catalog-count', `${n} ${n === 1 ? 'video' : n > 1 && n < 5 ? 'videa' : 'videí'}`)); catalog.append(top);
  if (state.role === 'admin' && state.adminPage) catalog.append(renderAdmin());
  if (state.adminPage) catalog.append(node('div', 'section-line', 'V knihovně'));
  if (!n) {
    const empty = node('div', 'empty-catalog'); empty.append(icon('film', 'empty-illustration'), node('h2', '', 'První promítání se teprve chystá.'), node('p', '', state.role === 'admin' && state.adminPage ? 'Nahraj první video nahoře. Až bude připravené, objeví se tady pro všechny naše diváky.' : 'Zatím tu žádné video není. Jakmile přidáme první, najdeš ho právě tady.'));
    if (state.role === 'admin' && !state.adminPage) { const link = node('a', 'button primary', 'Nahrát první video'); link.href = '/admin'; empty.append(link); }
    catalog.append(empty);
  } else { const grid = node('div', 'videos-grid'); grid.id = 'video-collection'; state.videos.forEach(video => grid.append(videoCard(video))); catalog.append(grid); }
  main.append(catalog); updateUploadProgress();
}
function videoCard(video) {
  const card = node('article', 'video-card'); const poster = node('div', 'video-poster');
  if (video.poster_url) { const image = node('img'); image.src = video.poster_url; image.alt = ''; image.loading = 'lazy'; image.addEventListener('error', () => image.remove()); poster.append(image); }
  else poster.append(node('div', 'poster-art', 'pn.'));
  const open = button('', 'video-open', () => openVideo(video)); open.setAttribute('aria-label', `Přehrát ${video.title}`); open.append(icon('play', 'play-disc'), node('span', 'play-now', 'PLAY NOW')); poster.append(open);
  if (video.duration) poster.append(node('span', 'duration', duration(video.duration)));
  const title = node('h2', '', video.title || video.filename); card.append(poster, title);
  if (state.role === 'admin' && state.adminPage) card.append(node('p', 'admin-filename', video.filename || ''));
  if (video.description) card.append(node('p', 'description', video.description));
  const meta = node('div', 'video-meta'); const parts = [date(video.created_at), size(video.size)].filter(Boolean); meta.textContent = parts.join(' · '); card.append(meta);
  const quality = originalQuality(video); if (quality) card.append(node('p', 'source-quality', `Originál · ${quality}`));
  card.append(createEngagementSurface(video, 'card'));
  const status = processingStatus(video); if (status !== 'none') { const badge = node('div', `processing-badge status-${status}`); if (status === 'queued' || status === 'processing') badge.append(node('span', 'status-dot')); badge.append(document.createTextNode(({ queued: 'Čeká na přípravu snadné verze', processing: 'Připravujeme snadné přehrávání', ready: 'Snadné přehrávání připravené', failed: 'Snadná verze se nepodařila' })[status] || '')); card.append(badge); }
  if (state.role === 'admin' && state.adminPage) { const actions = node('div', 'video-admin-actions'); actions.append(button('Upravit', '', () => editVideo(video)), button('Sdílet', '', () => openSharing(video))); if (status === 'failed' || (status === 'none' && (Math.max(Number(video.source_width || 0), Number(video.source_height || 0)) >= 3800 || Number(video.source_fps || 0) > 60))) actions.append(button(status === 'failed' ? 'Připravit znovu' : 'Připravit snadnou verzi', '', () => prepareVideo(video))); actions.append(button('Smazat', '', () => deleteVideo(video))); card.append(actions); }
  return card;
}
function normalizedEngagement(value) {
  const input = value || {}, reactions = {};
  REACTIONS.forEach(reaction => { const count = Number(input.reactions?.[reaction]); reactions[reaction] = Number.isFinite(count) ? Math.max(0, Math.floor(count)) : 0; });
  const count = Number(input.views);
  return { views: Number.isFinite(count) ? Math.max(0, Math.floor(count)) : 0, reactions, my_reaction: REACTIONS.includes(input.my_reaction) ? input.my_reaction : null };
}
function engagementReadContext() { return { epoch: engagementEpoch, sequence: ++engagementReadSequence, mutation: engagementMutationSequence }; }
function resetEngagementSession() {
  ++engagementEpoch; stopPlaybackTracking();
  engagementControllers.forEach(controller => controller.abort()); engagementControllers.clear(); reactionRequests.clear(); engagementCache.clear();
}
function receiveEngagement(identifier, incoming, context, kind = 'read', notify = true) {
  if (!state.role || context.epoch !== engagementEpoch) return false;
  const previous = engagementCache.get(identifier);
  if (kind === 'read' && (reactionRequests.has(identifier) || previous && (previous.mutation > context.mutation || previous.sequence > context.sequence))) return false;
  const value = normalizedEngagement(incoming);
  if (previous) { value.views = Math.max(previous.value.views, value.views); if (kind === 'view') { value.reactions = previous.value.reactions; value.my_reaction = previous.value.my_reaction; } }
  const entry = { value, sequence: Math.max(previous?.sequence || 0, context.sequence || 0), mutation: kind === 'read' ? previous?.mutation || 0 : ++engagementMutationSequence };
  engagementCache.set(identifier, entry);
  const video = state.videos.find(item => item.id === identifier); if (video) video.engagement = value;
  if (notify) updateEngagementSurfaces(identifier);
  return true;
}
function ingestCatalogEngagement(videos, context) {
  videos.forEach(video => { receiveEngagement(video.id, video.engagement, context, 'read', false); video.engagement = engagementCache.get(video.id)?.value || normalizedEngagement(video.engagement); });
}
function engagementFor(video) { return engagementCache.get(video.id)?.value || normalizedEngagement(video.engagement); }
function localizedCount(value) { return Number(value).toLocaleString('cs-CZ'); }
function fillEngagementSurface(surface, video) {
  const current = engagementFor(video), busy = reactionRequests.has(video.id), active = document.activeElement;
  const restoreReaction = surface.contains(active) ? active.dataset.reaction : null; surface.replaceChildren();
  const views = node('div', 'engagement-views'); views.append(icon('eye'), node('span', '', `${localizedCount(current.views)} zhlédnutí`)); surface.append(views);
  const reactions = node('div', 'reaction-list'); reactions.setAttribute('role', 'group'); reactions.setAttribute('aria-label', 'Reakce na video'); reactions.setAttribute('aria-busy', String(busy));
  REACTIONS.forEach(reaction => {
    const selected = current.my_reaction === reaction, control = button('', `reaction-button${selected ? ' is-selected' : ''}`, () => changeReaction(video.id, reaction));
    control.dataset.reaction = reaction; control.disabled = busy; control.setAttribute('aria-pressed', String(selected)); control.setAttribute('aria-label', `${REACTION_LABELS[reaction]}, ${localizedCount(current.reactions[reaction])} reakcí. ${selected ? 'Odebrat moji reakci' : 'Přidat moji reakci'}`); control.title = REACTION_LABELS[reaction];
    const face = node('span', 'reaction-emoji', reaction), count = node('span', 'reaction-count', localizedCount(current.reactions[reaction])); face.setAttribute('aria-hidden', 'true'); count.setAttribute('aria-hidden', 'true'); control.append(face, count); reactions.append(control);
  }); surface.append(reactions);
  if (restoreReaction) { const replacement = Array.from(reactions.querySelectorAll('button')).find(control => control.dataset.reaction === restoreReaction); if (replacement && !replacement.disabled) replacement.focus({ preventScroll: true }); }
}
function createEngagementSurface(video, mode) { const surface = node('div', `engagement-panel ${mode === 'card' ? 'card-engagement' : 'player-engagement'}`); surface.dataset.engagementVideo = video.id; surface.dataset.engagementMode = mode; fillEngagementSurface(surface, video); return surface; }
function renderPlayerEngagement(video) { const surface = $('player-engagement'); surface.hidden = false; surface.dataset.engagementVideo = video.id; surface.dataset.engagementMode = 'player'; fillEngagementSurface(surface, video); }
function updateEngagementSurfaces(identifier) {
  if (!state.role) return; const video = state.videos.find(item => item.id === identifier); if (!video) return;
  document.querySelectorAll('[data-engagement-video]').forEach(surface => { if (surface.dataset.engagementVideo === identifier) fillEngagementSurface(surface, video); });
}
async function refreshEngagement(identifier, tracker = null) {
  const context = engagementReadContext(), controller = new AbortController(); engagementControllers.add(controller);
  try { const result = await api(`/api/engagement/${encodeURIComponent(identifier)}`, { signal: controller.signal }); if (!controller.signal.aborted && (!tracker || currentPlaybackTracker(tracker))) receiveEngagement(identifier, result, context); }
  catch (_) {} finally { engagementControllers.delete(controller); }
}
async function refreshEngagementSession(context) {
  if (context.epoch !== engagementEpoch) return;
  try { const session = await api('/api/session'); if (context.epoch !== engagementEpoch) return; applySession(session); if (!state.role) renderLogin('Přístup vypršel nebo byl zrušený. Můžeš se přihlásit heslem.'); } catch (_) {}
}
async function changeReaction(identifier, reaction) {
  if (!state.role || !REACTIONS.includes(reaction) || reactionRequests.has(identifier)) return;
  const video = state.videos.find(item => item.id === identifier); if (!video) return;
  const current = engagementFor(video), desired = current.my_reaction === reaction ? null : reaction, context = engagementReadContext(), controller = new AbortController();
  reactionRequests.set(identifier, controller); engagementControllers.add(controller); updateEngagementSurfaces(identifier);
  try { const result = await api(`/api/engagement/${encodeURIComponent(identifier)}/reaction`, { method: 'PUT', json: { reaction: desired }, signal: controller.signal }); if (!controller.signal.aborted) receiveEngagement(identifier, result, context, 'reaction'); }
  catch (error) { if (!controller.signal.aborted && context.epoch === engagementEpoch) { if (error.status === 403) await refreshEngagementSession(context); if (context.epoch === engagementEpoch) toast('Reakci se nepodařilo uložit. Zkus to prosím znovu.'); } }
  finally { engagementControllers.delete(controller); if (reactionRequests.get(identifier) === controller) { reactionRequests.delete(identifier); updateEngagementSurfaces(identifier); } }
}
function currentPlaybackTracker(tracker) { return playbackTracker === tracker && tracker.epoch === engagementEpoch && playerVideoId === tracker.videoId && $('player-dialog').open && !!state.role; }
function beginPlaybackTracking(video) {
  stopPlaybackTracking();
  const tracker = { generation: ++playbackGeneration, epoch: engagementEpoch, videoId: video.id, clock: PlayNowWatchTime.create(), active: false, hasPlayed: false, ticket: null, minimum: 10, starting: false, qualifying: false, qualified: false, disabled: false, nextStart: 0, nextQualify: 0, controller: new AbortController(), timer: null };
  playbackTracker = tracker; engagementControllers.add(tracker.controller); tracker.timer = setInterval(() => samplePlaybackTracker(tracker), 250);
}
function stopPlaybackTracking() { const tracker = playbackTracker; playbackTracker = null; if (!tracker) return; clearInterval(tracker.timer); tracker.controller.abort(); engagementControllers.delete(tracker.controller); }
function suspendPlaybackTracking() { if (playbackTracker) { playbackTracker.active = false; PlayNowWatchTime.reset(playbackTracker.clock); } }
function samplePlaybackTracker(tracker) {
  if (!currentPlaybackTracker(tracker) || tracker.disabled || tracker.qualified) return;
  const player = $('video-player');
  PlayNowWatchTime.sample(tracker.clock, { nowMs: performance.now(), mediaTime: player.currentTime, playbackRate: player.playbackRate, playing: tracker.active, paused: player.paused, seeking: player.seeking, readyState: player.readyState, ended: player.ended, visible: !document.hidden });
  if (tracker.active && !player.paused && !player.seeking && player.readyState >= 3 && !document.hidden) ensurePlaybackTicket(tracker);
  qualifyPlayback(tracker);
}
async function ensurePlaybackTicket(tracker) {
  if (!currentPlaybackTracker(tracker) || tracker.disabled || tracker.ticket || tracker.starting || tracker.qualified || performance.now() < tracker.nextStart) return;
  tracker.starting = true;
  try {
    const result = await api(`/api/engagement/${encodeURIComponent(tracker.videoId)}/playback`, { method: 'POST', json: {}, signal: tracker.controller.signal });
    if (!currentPlaybackTracker(tracker) || tracker.controller.signal.aborted) return;
    if (typeof result.playback_id !== 'string' || !result.playback_id) throw new Error('Invalid playback ticket');
    tracker.ticket = result.playback_id; tracker.minimum = Math.max(10, Number(result.minimum_seconds) || 10); qualifyPlayback(tracker);
  } catch (error) { if (currentPlaybackTracker(tracker) && !tracker.controller.signal.aborted) { if (error.status === 401 || error.status === 403) { tracker.disabled = true; if (error.status === 403) refreshEngagementSession({ epoch: tracker.epoch }); } else tracker.nextStart = performance.now() + 3000; } }
  finally { tracker.starting = false; }
}
async function qualifyPlayback(tracker) {
  if (!currentPlaybackTracker(tracker) || tracker.disabled || tracker.qualified || tracker.qualifying || !tracker.ticket || performance.now() < tracker.nextQualify || !PlayNowWatchTime.reached(tracker.clock, tracker.minimum)) return;
  tracker.qualifying = true; const context = engagementReadContext();
  try {
    const played = Math.max(tracker.minimum, Math.floor(tracker.clock.seconds * 1000) / 1000);
    const result = await api(`/api/engagement/${encodeURIComponent(tracker.videoId)}/playback/${encodeURIComponent(tracker.ticket)}`, { method: 'POST', json: { played_seconds: played }, signal: tracker.controller.signal });
    if (!currentPlaybackTracker(tracker) || tracker.controller.signal.aborted) return;
    if (result.engagement) receiveEngagement(tracker.videoId, result.engagement, context, 'view');
    if (result.qualified) { tracker.qualified = true; clearInterval(tracker.timer); tracker.timer = null; }
    else tracker.nextQualify = performance.now() + (Math.max(0.25, Math.min(60, Number(result.retry_after) || 1)) * 1000) + 100;
  } catch (error) { if (currentPlaybackTracker(tracker) && !tracker.controller.signal.aborted) { if ([400, 401, 403, 404].includes(error.status)) { tracker.disabled = true; if (error.status === 403) refreshEngagementSession(context); } else tracker.nextQualify = performance.now() + 3000; } }
  finally { tracker.qualifying = false; }
}
const trackedPlayer = $('video-player');
trackedPlayer.addEventListener('playing', () => { const tracker = playbackTracker; if (!tracker || !currentPlaybackTracker(tracker)) return; tracker.hasPlayed = true; tracker.active = true; PlayNowWatchTime.reset(tracker.clock); samplePlaybackTracker(tracker); });
function finishPlaybackInterval() {
  const tracker = playbackTracker;
  if (tracker && currentPlaybackTracker(tracker) && tracker.active && !trackedPlayer.seeking && !tracker.disabled && !tracker.qualified) {
    PlayNowWatchTime.sample(tracker.clock, { nowMs: performance.now(), mediaTime: trackedPlayer.currentTime, playbackRate: trackedPlayer.playbackRate, playing: true, paused: false, seeking: false, readyState: 3, ended: false, visible: !document.hidden });
    qualifyPlayback(tracker);
  }
  suspendPlaybackTracking();
}
['pause', 'waiting', 'stalled', 'ended'].forEach(event => trackedPlayer.addEventListener(event, finishPlaybackInterval));
['seeking', 'emptied', 'error'].forEach(event => trackedPlayer.addEventListener(event, suspendPlaybackTracking));
trackedPlayer.addEventListener('seeked', () => { const tracker = playbackTracker; if (!tracker || !currentPlaybackTracker(tracker)) return; PlayNowWatchTime.reset(tracker.clock); tracker.active = tracker.hasPlayed && !trackedPlayer.paused && trackedPlayer.readyState >= 3; samplePlaybackTracker(tracker); });
trackedPlayer.addEventListener('ratechange', () => { if (playbackTracker) PlayNowWatchTime.reset(playbackTracker.clock); });
function processingStatus(video) { return video.processing_status || video.compatibility?.status || 'none'; }
function resolutionLabel(width, height) {
  const longer = Math.max(Number(width || 0), Number(height || 0)), shorter = Math.min(Number(width || 0), Number(height || 0));
  if (longer >= 7600 && shorter >= 4000) return '8K';
  if (longer >= 3800 && shorter >= 2000) return '4K';
  if (longer >= 1900 && shorter >= 1060) return 'Full HD';
  if (longer >= 1260 && shorter >= 700) return 'HD';
  return longer && shorter ? `${shorter}p` : '';
}
function originalQuality(video) {
  const parts = [resolutionLabel(video.source_width, video.source_height)].filter(Boolean), fps = Number(video.source_fps || 0);
  if (fps > 0) parts.push(`${Math.round(fps)} fps`);
  if (video.source_hdr) parts.push('HDR'); return parts.join(' · ');
}
function compatibleQuality(video) {
  const info = video.compatibility || {}, parts = [resolutionLabel(info.width, info.height)].filter(Boolean);
  if (Number(info.fps) > 0) parts.push(`${Math.round(Number(info.fps))} fps`); return parts.join(' · ');
}
function versionLabel(video, quality) { const label = quality === 'original' ? originalQuality(video) : compatibleQuality(video); return label ? `${label} — ${quality === 'original' ? 'originál' : 'menší verze'}` : quality === 'original' ? 'Originál' : 'Menší verze'; }
function setPlayerSource(video, quality, autoplay = true) {
  suspendPlaybackTracking();
  const player = $('video-player'), current = Number(player.currentTime) || 0; playerQuality = quality; $('player-error').hidden = true;
  const source = quality === 'compatible' ? video.compatible_url : (video.original_url || video.stream_url || `/media/${encodeURIComponent(video.id)}?quality=original`);
  if (!source) return; const sourceRevision = ++playerSourceRevision; player.hidden = false; $('player-preparation').hidden = true;
  if (current > 0) player.addEventListener('loadedmetadata', () => { if (sourceRevision !== playerSourceRevision || playerVideoId !== video.id) return; try { player.currentTime = Math.min(current, Math.max(0, (player.duration || current) - .1)); } catch (_) {} }, { once: true });
  player.src = source; syncPlayerDetails(video);
  if (autoplay) { const promise = player.play(); if (promise) promise.catch(() => {}); }
}
function syncPlayerDetails(video) {
  const status = processingStatus(video), ready = !!video.compatible_url && status === 'ready';
  $('player-quality-row').hidden = !ready;
  if (ready) { const select = $('player-quality'); select.options[0].textContent = versionLabel(video, 'compatible'); select.options[1].textContent = versionLabel(video, 'original'); select.value = playerQuality || 'compatible'; }
  const note = $('player-quality-note');
  if (playerQuality === 'original') { note.hidden = false; note.textContent = `Přehráváš originál${originalQuality(video) ? ` (${originalQuality(video)})` : ''}. Plynulost závisí na zařízení a prohlížeči; velký soubor potřebuje rychlé připojení.`; }
  else if (playerQuality === 'compatible') { note.hidden = false; note.textContent = `Menší verze${compatibleQuality(video) ? ` (${compatibleQuality(video)})` : ''} pro pohodlné přehrávání. Originál zůstává uložený v plné kvalitě.`; }
  else note.hidden = true;
  if (!$('video-player').getAttribute('src')) {
    $('player-preparation').hidden = false; $('video-player').hidden = true;
    const spinner = $('player-preparation').querySelector('.spinner'); spinner.hidden = !['queued', 'processing'].includes(status);
    if (ready) { $('player-preparation-title').textContent = 'Připraveno. Můžeme promítat.'; $('player-preparation-text').textContent = 'Snadná verze videa je hotová. Originál zůstává uložený v plné kvalitě.'; $('player-original-button').textContent = 'Play now'; }
    else if (status === 'failed') { $('player-preparation-title').textContent = 'Originál je v bezpečí.'; $('player-preparation-text').textContent = 'Příprava snadné verze se nepodařila. Můžeš zkusit pustit originál; přehrávání závisí na zařízení a připojení.'; $('player-original-button').textContent = 'Pustit originál'; }
    else { $('player-preparation-title').textContent = 'Připravujeme pohodlné přehrávání.'; $('player-preparation-text').textContent = 'U velkých videí může příprava trvat déle. Originál už je uložený a můžeš ho zkusit pustit hned; potřebuje rychlé připojení a podporované zařízení.'; $('player-original-button').textContent = 'Pustit originál'; }
  }
}
function openVideo(video) {
  closePlayer(); playerVideoId = video.id; playerQuality = null; $('player-title').textContent = video.title || video.filename; $('player-description').textContent = video.description || ''; $('player-error').hidden = true; $('player-dialog').showModal();
  beginPlaybackTracking(video); renderPlayerEngagement(video); refreshEngagement(video.id, playbackTracker);
  const status = processingStatus(video);
  if (status === 'ready' && video.compatible_url) setPlayerSource(video, 'compatible');
  else if (status === 'none') setPlayerSource(video, 'original');
  else syncPlayerDetails(video);
}
function closePlayer() { ++playerSourceRevision; stopPlaybackTracking(); const player = $('video-player'); player.pause(); player.removeAttribute('src'); player.load(); playerVideoId = null; playerQuality = null; if ($('player-engagement')) { $('player-engagement').hidden = true; $('player-engagement').replaceChildren(); } if ($('player-dialog').open) $('player-dialog').close(); }
$('player-quality').addEventListener('change', () => { const video = state.videos.find(item => item.id === playerVideoId); if (video) setPlayerSource(video, $('player-quality').value); });
$('player-original-button').addEventListener('click', () => { const video = state.videos.find(item => item.id === playerVideoId); if (video) setPlayerSource(video, video.compatible_url && processingStatus(video) === 'ready' ? 'compatible' : 'original'); });
function scheduleProcessingPoll(immediate = false) {
  clearTimeout(processingTimer);
  if (!state.role || document.hidden || !state.videos.some(video => ['queued', 'processing'].includes(processingStatus(video)))) return;
  processingTimer = setTimeout(async () => {
    if (!state.role || document.hidden) return;
    try { const read = engagementReadContext(); const result = await api('/api/videos'); if (read.epoch !== engagementEpoch || !state.role) return; state.videos = result.videos || []; state.storage = result.storage; ingestCatalogEngagement(state.videos, read); const collection = $('video-collection'); if (collection) { collection.replaceChildren(); state.videos.forEach(video => collection.append(videoCard(video))); } const playing = state.videos.find(video => video.id === playerVideoId); if (playing) syncPlayerDetails(playing); }
    catch (_) {} finally { scheduleProcessingPoll(); }
  }, immediate ? 20 : 15000);
}
document.addEventListener('visibilitychange', () => { scheduleProcessingPoll(true); updateScreenWakeLock(); if (playbackTracker) PlayNowWatchTime.reset(playbackTracker.clock); });
window.addEventListener('pagehide', releaseScreenWakeLock);
$('close-player').addEventListener('click', closePlayer); $('player-dialog').addEventListener('close', () => { if (!$('player-dialog').open) closePlayer(); }); $('player-dialog').addEventListener('click', (event) => { if (event.target === $('player-dialog')) { const rect = $('player-dialog').getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) closePlayer(); } });
$('video-player').addEventListener('error', () => {
  if (!$('video-player').getAttribute('src')) return;
  const video = state.videos.find(item => item.id === playerVideoId), error = $('player-error');
  if (playerQuality === 'original' && video?.compatible_url) error.textContent = `Originál se v tomto prohlížeči nepodařilo přehrát. Nahoře přepni na „${versionLabel(video, 'compatible')}“.`;
  else if (playerQuality === 'original') error.textContent = 'Originál se v tomto prohlížeči nepodařilo přehrát. Zkus jiný prohlížeč nebo počkej na přípravu menší verze.';
  else error.textContent = 'Menší verzi se teď nepodařilo přehrát. Zkus obnovit stránku nebo ji otevři v jiném prohlížeči.';
  error.hidden = false;
});
function shareTimestamp(value) {
  const parsed = new Date(value); return Number.isNaN(parsed.getTime()) ? 'neznámý čas' : parsed.toLocaleString('cs-CZ', { day: 'numeric', month: 'numeric', year: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit' });
}
function sharingIsOpen(videoId, revision) { return $('share-dialog').open && state.shareVideoId === videoId && shareViewRevision === revision; }
async function refreshShareList(videoId, revision) {
  const result = await api(`/api/videos/${encodeURIComponent(videoId)}/shares`);
  if (!sharingIsOpen(videoId, revision)) return;
  const list = $('share-list'); list.replaceChildren();
  const shares = Array.isArray(result.shares) ? result.shares : [];
  if (!shares.length) { list.append(node('p', 'share-empty', 'Zatím tu nejsou žádné odkazy.')); return; }
  shares.forEach(share => {
    const row = node('div', `share-item${share.revoked_at ? ' is-revoked' : ''}`), detail = node('div', 'share-item-detail');
    detail.append(node('strong', '', `Odkaz ${String(share.id).slice(0, 6)}`), node('span', '', `Vytvořen ${shareTimestamp(share.created_at)}`));
    if (share.revoked_at) detail.append(node('span', 'share-revoked-at', `Zrušen ${shareTimestamp(share.revoked_at)}`));
    const actions = node('div', 'share-item-actions'); actions.append(node('span', 'share-status', share.revoked_at ? 'Zrušený' : 'Aktivní'));
    if (!share.revoked_at) { const revoke = button('Zrušit přístup', 'small-link danger'); revoke.addEventListener('click', () => revokeSharing(videoId, share, revoke, revision)); actions.append(revoke); }
    row.append(detail, actions); list.append(row);
  });
}
async function openSharing(video) {
  const revision = ++shareViewRevision; state.shareVideoId = video.id; state.displayedShareId = null; $('share-video-title').textContent = video.title || video.filename;
  $('share-error').textContent = ''; $('share-url').value = ''; $('share-new').hidden = true; $('create-share').disabled = false; $('create-share').textContent = 'Vytvořit odkaz'; $('share-list').replaceChildren(node('p', 'share-empty', 'Načítáme odkazy…'));
  $('share-dialog').showModal();
  try { await refreshShareList(video.id, revision); } catch (error) { if (sharingIsOpen(video.id, revision)) $('share-error').textContent = error.message; }
}
function clearSharing() { ++shareViewRevision; state.shareVideoId = null; state.displayedShareId = null; $('share-url').value = ''; $('share-new').hidden = true; $('share-error').textContent = ''; }
$('close-share').addEventListener('click', () => $('share-dialog').close());
$('share-dialog').addEventListener('close', () => { if (!$('share-dialog').open) clearSharing(); });
$('create-share').addEventListener('click', async () => {
  const videoId = state.shareVideoId, revision = shareViewRevision; if (!videoId) return;
  const submit = $('create-share'); submit.disabled = true; $('share-error').textContent = '';
  try {
    const result = await api(`/api/videos/${encodeURIComponent(videoId)}/shares`, { method: 'POST', json: {} });
    if (!sharingIsOpen(videoId, revision)) return;
    if (typeof result.url !== 'string' || !result.url) throw new Error('Odkaz se nepodařilo vytvořit. Zkus to znovu.');
    state.displayedShareId = result.share?.id || null; $('share-url').value = result.url; $('share-new').hidden = false; submit.textContent = 'Vytvořit další odkaz';
    await refreshShareList(videoId, revision); toast('Odkaz je připravený. Zkopíruj ho pro své diváky.');
  } catch (error) { if (sharingIsOpen(videoId, revision)) $('share-error').textContent = error.message; }
  finally { if (sharingIsOpen(videoId, revision)) submit.disabled = false; }
});
$('copy-share').addEventListener('click', async () => {
  const field = $('share-url'); if (!field.value) return;
  try { if (!navigator.clipboard?.writeText) throw new Error('Clipboard unavailable'); await navigator.clipboard.writeText(field.value); toast('Odkaz je zkopírovaný.'); }
  catch (_) { field.focus(); field.select(); field.setSelectionRange(0, field.value.length); toast('Odkaz je vybraný. Zkopíruj ho pomocí nabídky prohlížeče nebo klávesové zkratky.'); }
});
async function revokeSharing(videoId, share, control, revision) {
  if (!confirm(`Zrušit odkaz vytvořený ${shareTimestamp(share.created_at)}? Přestane fungovat i lidem, kteří už ho otevřeli.`)) return;
  control.disabled = true; $('share-error').textContent = '';
  try { await api(`/api/videos/${encodeURIComponent(videoId)}/shares/${encodeURIComponent(share.id)}`, { method: 'DELETE' }); if (sharingIsOpen(videoId, revision) && state.displayedShareId === share.id) { state.displayedShareId = null; $('share-url').value = ''; $('share-new').hidden = true; } await refreshShareList(videoId, revision); toast('Přístup přes tento odkaz je zrušený.'); }
  catch (error) { if (sharingIsOpen(videoId, revision)) { $('share-error').textContent = error.message; control.disabled = false; } }
}
function filenameParts(filename) {
  const name = String(filename || ''), dot = name.lastIndexOf('.');
  return dot > 0 && dot < name.length - 1 ? { base: name.slice(0, dot), extension: name.slice(dot) } : { base: name, extension: '' };
}
function editedFilename() {
  const base = $('edit-filename').value, filename = base + state.editExtension;
  if (!base.trim() || base.startsWith('.') || /[/\\\x00-\x1f\x7f]/.test(base)) throw new Error('Název souboru nesmí být prázdný, začínat tečkou ani obsahovat lomítka nebo řídicí znaky.');
  if (new TextEncoder().encode(filename).byteLength > 240) throw new Error('Název souboru je moc dlouhý. Zkrať ho prosím.');
  return filename;
}
function editVideo(video) {
  state.editId = video.id; const parts = filenameParts(video.filename); state.editExtension = parts.extension;
  $('edit-title').value = video.title || ''; $('edit-filename').value = parts.base; $('edit-filename-extension').textContent = parts.extension; $('edit-filename-extension').hidden = !parts.extension;
  $('edit-filename-help').textContent = `${parts.extension ? `Přípona ${parts.extension} zůstane zachovaná. ` : ''}Název v knihovně může být jiný.`;
  $('edit-description').value = video.description || ''; $('edit-error').textContent = ''; $('edit-dialog').showModal();
}
$('close-edit').addEventListener('click', () => $('edit-dialog').close());
$('edit-form').addEventListener('submit', async (event) => {
  event.preventDefault(); const submit = event.target.querySelector('[type=submit]'); submit.disabled = true; $('edit-error').textContent = '';
  try { const filename = editedFilename(); await api(`/api/videos/${encodeURIComponent(state.editId)}`, { method: 'PATCH', json: { title: $('edit-title').value.trim(), description: $('edit-description').value.trim(), filename } }); $('edit-dialog').close(); await loadCatalog(); toast('Změny jsou uložené.'); }
  catch (error) { $('edit-error').textContent = error.message; }
  finally { submit.disabled = false; }
});
async function prepareVideo(video) { try { await api(`/api/videos/${encodeURIComponent(video.id)}/prepare`, { method: 'POST', json: {} }); await loadCatalog(); toast('Video je ve frontě. Příprava může u velkého originálu trvat déle.'); } catch (error) { toast(error.message); } }
async function deleteVideo(video) { if (!confirm(`Opravdu smazat video „${video.title || video.filename}“? Soubor se odstraní z knihovny.`)) return; try { await api(`/api/videos/${encodeURIComponent(video.id)}`, { method: 'DELETE' }); await loadCatalog(); toast('Video je smazané.'); } catch (error) { toast(error.message); } }
function renderAdmin() {
  const panel = node('section', 'admin-panel'); panel.id = 'upload-panel'; const heading = node('div', 'panel-heading'); const intro = node('div'); intro.append(node('h2', '', 'Další příběh do knihovny'), node('p', '', 'Vyber soubor a přidej mu název. Velká videa jsou vítaná.')); heading.append(intro);
  if (state.storage && state.storage.free !== undefined) heading.append(node('span', 'storage', `${size(state.storage.free)} volného místa`)); panel.append(heading);
  const photosPicker = node('input', 'file-picker'); photosPicker.type = 'file'; photosPicker.accept = 'video/*'; photosPicker.id = 'video-file-photos'; photosPicker.setAttribute('aria-label', 'Vybrat video z Fotek'); photosPicker.tabIndex = -1;
  const picker = node('input', 'file-picker'); picker.type = 'file'; picker.accept = '.mp4,.mov,.m4v,.webm,.mkv,.avi,.mts,.m2ts,video/quicktime,video/mp4,video/webm,application/octet-stream'; picker.id = 'video-file'; picker.setAttribute('aria-label', 'Vybrat video ze Souborů'); picker.tabIndex = -1;
  [photosPicker, picker].forEach(input => { input.addEventListener('change', () => { const file = input.files[0]; if (file) selectFile(file); input.value = ''; }); input.addEventListener('cancel', () => { if (state.pendingSelection) toast('Výběr je zavřený. Rozpracované video i vybraný přenos zůstávají uložené.'); }); });
  const drop = button('', 'dropzone', () => picker.click()); drop.id = 'video-dropzone'; drop.append(icon('upload')); const dropText = node('strong'); dropText.append(document.createTextNode('Přetáhni video sem '), node('em', '', 'nebo vyber soubor')); drop.append(dropText, node('span', '', 'MP4, MOV i další videa. Nahraješ i velké soubory.')); ['dragenter', 'dragover'].forEach(event => drop.addEventListener(event, (e) => { e.preventDefault(); if (!state.preparingFile) drop.classList.add('dragover'); })); ['dragleave', 'drop'].forEach(event => drop.addEventListener(event, (e) => { e.preventDefault(); drop.classList.remove('dragover'); })); drop.addEventListener('drop', e => { if (state.preparingFile) return; const file = e.dataTransfer.files[0]; if (file) selectFile(file); }); panel.append(photosPicker, picker, drop);
  const sourceChoices = node('div', 'upload-source-choices');
  const photosButton = button('', 'button upload-source-button', () => photosPicker.click()); photosButton.id = 'pick-photos'; photosButton.append(icon('film'), node('span', '', 'Vybrat z Fotek'));
  const filesButton = button('', 'button upload-source-button', () => picker.click()); filesButton.id = 'pick-files'; filesButton.append(icon('upload'), node('span', '', 'Vybrat ze Souborů')); sourceChoices.append(photosButton, filesButton); panel.append(sourceChoices);
  const resumeChoice = node('div', 'resume-choice hidden'); resumeChoice.id = 'resume-choice'; const resumeText = node('p'); resumeText.id = 'resume-choice-text'; resumeChoice.append(resumeText, button('Vybrat jiné video', 'small-link', clearPendingSelection)); panel.append(resumeChoice);
  const pickerStatus = node('p', 'picker-status'); pickerStatus.id = 'picker-status'; pickerStatus.setAttribute('role', 'status'); panel.append(pickerStatus);
  panel.append(node('p', 'phone-upload-note', 'Na iPhonu nech Safari otevřené a telefon odemčený. Po přerušení znovu vyber stejný původní soubor. Výběr zdroje se může lišit podle systému.'));
  const originalHint = node('details', 'upload-original-help'); originalHint.append(node('summary', '', 'Video z iPhonu a původní kvalita'), node('p', '', 'MOV i HEVC můžeš nahrát. Pokud už máš původní MOV uložený v Souborech, vyber ho odtud. Telefon může přípravu velkého videa ve výběru chvíli zpracovávat.'));
  panel.append(originalHint);
  const awakeLabel = node('label', 'keep-awake-control'); const awakeCheck = node('input'); awakeCheck.type = 'checkbox'; awakeCheck.id = 'keep-awake'; awakeCheck.checked = state.keepAwake; awakeCheck.addEventListener('change', () => { state.keepAwake = awakeCheck.checked; updateScreenWakeLock(); }); const awakeText = node('span'); awakeText.append(node('span', '', 'Během nahrávání neuspávat displej')); const awakeStatus = node('small', '', 'Podle možností prohlížeče.'); awakeStatus.id = 'screen-awake-status'; awakeText.append(awakeStatus); awakeLabel.append(awakeCheck, awakeText); panel.append(awakeLabel);
  const selected = node('p', 'file-selected hidden'); selected.id = 'selected-file'; panel.append(selected);
  const fields = node('div', 'upload-fields'); const titleLabel = node('label', '', 'Název videa'); const titleInput = node('input'); titleInput.id = 'upload-title'; titleInput.placeholder = 'Třeba: Léto u moře'; titleInput.maxLength = 180; titleLabel.append(titleInput); const descriptionLabel = node('label', '', 'Krátký popis · nepovinné'); const descriptionInput = node('textarea'); descriptionInput.id = 'upload-description'; descriptionInput.placeholder = 'Pár slov pro naše diváky'; descriptionInput.maxLength = 3000; descriptionInput.rows = 1; descriptionLabel.append(descriptionInput); fields.append(titleLabel, descriptionLabel); panel.append(fields);
  const submitRow = node('div', 'upload-submit-row'); submitRow.append(node('p', 'upload-note', 'Přenos můžeš přerušit a navázat stejným souborem. Originál zachováme; u velkých videí příprava snadné verze potrvá déle.')); const submit = button('Nahrát video', 'button primary', startSelectedUpload); submit.id = 'start-upload'; submit.disabled = true; submit.prepend(icon('upload')); submitRow.append(submit); panel.append(submitRow);
  const progress = node('div', 'upload-live hidden'); progress.id = 'upload-live'; const nameRow = node('div', 'upload-name-row'); const name = node('span', 'upload-name'); name.id = 'upload-name'; const percent = node('span', 'upload-percent'); percent.id = 'upload-percent'; nameRow.append(name, percent); const track = node('div', 'progress-track'); track.setAttribute('role', 'progressbar'); track.setAttribute('aria-label', 'Průběh nahrávání'); track.setAttribute('aria-valuemin', '0'); track.setAttribute('aria-valuemax', '100'); track.id = 'progress-track'; const bar = node('div', 'progress-bar'); bar.id = 'upload-bar'; track.append(bar); const detail = node('div', 'upload-detail-row'); const stats = node('span', 'upload-stats'); stats.id = 'upload-stats'; const controls = node('div', 'upload-controls'); const pause = button('Pozastavit', 'small-link', toggleUpload); pause.id = 'pause-upload'; const cancel = button('Zrušit', 'small-link danger', cancelActiveUpload); cancel.id = 'cancel-upload'; controls.append(pause, cancel); detail.append(stats, controls); progress.append(nameRow, track, detail); panel.append(progress);
  const error = node('p', 'form-error'); error.id = 'upload-error'; error.setAttribute('role', 'alert'); panel.append(error);
  const pending = node('div', 'pending-list'); state.uploads.filter(item => item.id !== state.upload?.id).forEach(item => { const row = node('div', 'pending-item'); const info = node('div'); info.append(node('div', '', item.filename), node('p', '', `Rozpracované · ${size(item.offset)} z ${size(item.size)}`)); const actions = node('div', 'pending-actions'); actions.append(button('Pokračovat', 'small-link', () => { if (state.preparingFile) return; if (state.upload && !state.upload.paused) { toast('Nejdřív pozastav aktuální nahrávání.'); return; } state.pendingSelection = item; updatePendingSelection(); $('resume-choice').scrollIntoView({ behavior: 'smooth', block: 'nearest' }); toast('Vyber původní soubor pomocí Fotek nebo Souborů.'); }), button('Zrušit', 'small-link danger', () => cancelPending(item))); row.append(info, actions); pending.append(row); }); panel.append(pending);
  if (state.upload) { titleInput.value = state.upload.title || ''; descriptionInput.value = state.upload.description || ''; selected.textContent = `${state.upload.file.name} · ${size(state.upload.file.size)}`; selected.classList.remove('hidden'); }
  queueMicrotask(() => { updatePendingSelection(); updatePickerControls(); updateScreenWakeStatus(); });
  return panel;
}
function shouldKeepScreenAwake() { return state.keepAwake && !document.hidden && !!state.upload?.running && !state.upload.paused; }
function updateScreenWakeStatus() {
  const status = $('screen-awake-status'); if (!status) return;
  status.textContent = screenWakeLock && !screenWakeLock.released ? 'Uspání displeje je při tomto nahrávání blokované.' : 'Podle možností prohlížeče. Nech stránku otevřenou.';
}
async function releaseScreenWakeLock() {
  ++screenWakeLockRevision; const lock = screenWakeLock; screenWakeLock = null; updateScreenWakeStatus();
  if (lock) { try { await lock.release(); } catch (_) {} }
}
async function updateScreenWakeLock() {
  if (!shouldKeepScreenAwake() || !navigator.wakeLock?.request) { releaseScreenWakeLock(); return; }
  if (screenWakeLock && !screenWakeLock.released || screenWakeLockRequest) return;
  const revision = ++screenWakeLockRevision; let request;
  try { request = navigator.wakeLock.request('screen'); } catch (_) { updateScreenWakeStatus(); return; }
  screenWakeLockRequest = request;
  try {
    const lock = await request;
    if (revision !== screenWakeLockRevision || !shouldKeepScreenAwake()) { try { await lock.release(); } catch (_) {} return; }
    screenWakeLock = lock;
    lock.addEventListener('release', () => { if (screenWakeLock === lock) screenWakeLock = null; updateScreenWakeStatus(); });
  } catch (_) {}
  finally {
    if (screenWakeLockRequest === request) screenWakeLockRequest = null;
    updateScreenWakeStatus();
    if (revision !== screenWakeLockRevision && shouldKeepScreenAwake()) updateScreenWakeLock();
  }
}
function updatePendingSelection() {
  const choice = $('resume-choice'); if (!choice) return;
  choice.classList.toggle('hidden', !state.pendingSelection);
  if (state.pendingSelection) $('resume-choice-text').textContent = `Pokračování: ${state.pendingSelection.filename}. Nahoře vyber znovu stejný původní soubor.`;
}
function clearPendingSelection() {
  ++state.selectionRevision; state.preparingFile = false; state.pendingSelection = null;
  if ($('picker-status')) $('picker-status').textContent = '';
  updatePendingSelection(); updatePickerControls();
}
function updatePickerControls() {
  const busy = state.preparingFile || !!state.upload?.running;
  ['pick-photos', 'pick-files', 'video-dropzone'].forEach(id => { if ($(id)) $(id).disabled = busy; });
  if ($('start-upload')) {
    const upload = state.upload; $('start-upload').disabled = state.preparingFile || !upload || upload.running || !!upload.completing;
    $('start-upload').textContent = upload?.running ? (upload.completing ? 'Připravujeme…' : upload.paused ? 'Pozastavujeme…' : 'Nahráváme…') : upload?.id ? 'Pokračovat v nahrávání' : 'Nahrát video';
  }
  if ($('pause-upload')) $('pause-upload').disabled = state.preparingFile || !!state.upload?.completing || !!(state.upload?.running && state.upload.paused);
  if ($('cancel-upload')) $('cancel-upload').disabled = state.preparingFile || !!state.upload?.completing;
}
async function contentFingerprint(file) {
  if (!globalThis.crypto?.subtle) throw new Error('Pro ověření souboru otevři tuto stránku v aktuálním prohlížeči přes zabezpečenou adresu.');
  const sampleSize = Math.min(1024 * 1024, file.size), prefix = new TextEncoder().encode(`${file.size}:`);
  const [first, last] = await Promise.all([file.slice(0, sampleSize).arrayBuffer(), file.slice(file.size - sampleSize, file.size).arrayBuffer()]);
  const sample = new Uint8Array(prefix.length + first.byteLength + last.byteLength); sample.set(prefix); sample.set(new Uint8Array(first), prefix.length); sample.set(new Uint8Array(last), prefix.length + first.byteLength);
  const digest = await globalThis.crypto.subtle.digest('SHA-256', sample);
  return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('');
}
async function selectFile(file) {
  if (state.role !== 'admin' || !state.adminPage || state.preparingFile) return;
  if (state.upload && !state.upload.paused) { toast('Nejdřív pozastav aktuální nahrávání.'); return; }
  if (file.size < 1) { toast('Soubor je prázdný. Vyber jiné video.'); return; }
  if (file.size > 100 * 1024 ** 3) { toast('Jedno video může mít nejvýše 100 GiB.'); return; }
  const pending = state.pendingSelection, revision = ++state.selectionRevision;
  if (pending && (file.name !== pending.filename || file.size !== Number(pending.size))) { toast('Tenhle soubor neodpovídá rozpracovanému videu. Vyber prosím původní soubor.'); return; }
  state.preparingFile = true; if ($('picker-status')) $('picker-status').textContent = 'Ověřujeme soubor…'; updatePickerControls();
  try {
    const fingerprint = await contentFingerprint(file); if (revision !== state.selectionRevision) return;
    if (pending) {
      const saved = remembered(pending.id), expected = pending.content_fingerprint || saved?.content_fingerprint;
      if (expected && expected !== fingerprint) throw new Error('Obsah souboru neodpovídá rozpracovanému videu. Vyber původní soubor.');
      if (!expected) {
        const previousModified = Number(saved?.lastModified || pending.last_modified || 0);
        if (!(previousModified > 0) || previousModified !== Number(file.lastModified)) throw new Error('Tento starší přenos nejde spolehlivě ověřit. Zvol „Vybrat jiné video“ a nahraj ho znovu.');
      }
    }
    state.upload = { file, contentFingerprint: fingerprint, id: pending?.id || null, offset: pending?.offset || 0, paused: true, running: false, title: pending?.title || file.name.replace(/\.[^.]+$/, '').replace(/[_]/g, ' '), description: '', startingOffset: 0, started: 0, status: pending ? 'Připraveno pokračovat' : 'Připraveno k nahrání' };
    state.pendingSelection = null; $('upload-title').value = state.upload.title; $('upload-description').value = ''; $('selected-file').textContent = `${file.name} · ${size(file.size)}`; $('selected-file').classList.remove('hidden'); $('start-upload').textContent = pending ? 'Pokračovat v nahrávání' : 'Nahrát video'; $('upload-error').textContent = ''; $('picker-status').textContent = 'Soubor je připravený.';
    updatePendingSelection(); updateUploadProgress();
  } catch (error) { if (revision === state.selectionRevision) { if ($('picker-status')) $('picker-status').textContent = error.message; toast(error.message); } }
  finally { if (revision === state.selectionRevision) { state.preparingFile = false; updatePickerControls(); } }
}
async function startSelectedUpload() {
  const upload = state.upload; if (!upload || upload.running || state.preparingFile) return;
  upload.title = $('upload-title').value.trim() || upload.file.name.replace(/\.[^.]+$/, ''); upload.description = $('upload-description').value.trim(); upload.paused = false; upload.status = 'Připojujeme se…'; $('upload-error').textContent = ''; await runUpload(upload);
}
function updateUploadProgress() {
  updateScreenWakeLock(); updatePickerControls(); const upload = state.upload; if (!upload || !$('upload-live')) return; $('upload-live').classList.remove('hidden'); $('upload-name').textContent = upload.file.name;
  const percent = Math.min(100, upload.offset / upload.file.size * 100); $('upload-percent').textContent = `${percent.toLocaleString('cs-CZ', { maximumFractionDigits: 1 })} %`; $('upload-bar').style.width = `${percent}%`; $('progress-track').setAttribute('aria-valuenow', String(Math.round(percent)));
  const elapsed = (Date.now() - upload.started) / 1000; const speed = elapsed > 0 && upload.started ? (upload.offset - upload.startingOffset) / elapsed : 0; const remain = speed > 0 ? eta((upload.file.size - upload.offset) / speed) : ''; const parts = [upload.status, `${size(upload.offset)} / ${size(upload.file.size)}`]; if (upload.running && !upload.paused && speed > 0) parts.push(`${size(speed)}/s`, remain ? `zbývá ${remain}` : ''); $('upload-stats').textContent = parts.filter(Boolean).join(' · ');
  $('pause-upload').textContent = upload.paused ? 'Pokračovat' : 'Pozastavit'; $('pause-upload').disabled = state.preparingFile || (upload.running && upload.paused) || !!upload.completing; $('cancel-upload').disabled = state.preparingFile || !!upload.completing; $('pause-upload').hidden = !upload.id && !upload.running; $('start-upload').disabled = state.preparingFile || upload.running || !!upload.completing; $('video-dropzone').disabled = upload.running || state.preparingFile; $('upload-title').disabled = !!upload.id; $('upload-description').disabled = !!upload.id;
}
const sleep = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
async function runUpload(upload) {
  if (upload.running || state.upload !== upload) return; upload.running = true; upload.paused = false; upload.started = Date.now(); upload.startingOffset = upload.offset; updateUploadProgress();
  try {
    let record;
    if (upload.id && upload.offset >= upload.file.size) record = { id: upload.id, offset: upload.offset };
    else if (upload.id) record = await api(`/api/uploads/${encodeURIComponent(upload.id)}`);
    else record = await api('/api/uploads', { method: 'POST', json: { filename: upload.file.name, size: upload.file.size, title: upload.title, description: upload.description, last_modified: upload.file.lastModified, content_fingerprint: upload.contentFingerprint } });
    if (record.content_fingerprint && record.content_fingerprint !== upload.contentFingerprint) throw new Error('Rozpracovaný přenos neodpovídá vybranému souboru.');
    upload.id = record.id; upload.offset = Number(record.offset || 0); upload.startingOffset = upload.offset; remember(upload); upload.status = 'Nahráváme'; updateUploadProgress();
    const chunkSize = Math.min(Number(record.chunk_size) || 8388608, 8388608);
    while (upload.offset < upload.file.size && !upload.paused && state.upload === upload) {
      let delivered = false;
      for (let attempt = 0; attempt < 5 && !delivered && !upload.paused; attempt++) {
        try {
          if (attempt > 0) { upload.status = 'Obnovujeme spojení…'; updateUploadProgress(); await sleep(Math.min(8000, 1000 * (2 ** (attempt - 1)))); if (upload.paused) break; const check = await api(`/api/uploads/${encodeURIComponent(upload.id)}`); upload.offset = Number(check.offset || 0); if (upload.offset >= upload.file.size) { delivered = true; break; } }
          upload.controller = new AbortController(); const start = upload.offset; const chunk = upload.file.slice(start, Math.min(start + chunkSize, upload.file.size));
          const result = await api(`/api/uploads/${encodeURIComponent(upload.id)}`, { method: 'PUT', headers: { 'Upload-Offset': String(start), 'Content-Type': 'application/octet-stream' }, body: chunk, signal: upload.controller.signal });
          const next = Number(result.offset); if (!Number.isFinite(next) || next <= start || next > upload.file.size) throw new Error('Server nepotvrdil přijetí souboru. Zkus přenos obnovit.'); upload.offset = next; upload.status = 'Nahráváme'; delivered = true; updateUploadProgress();
        } catch (error) { if (upload.paused || error.name === 'AbortError') break; if (error.status && error.status !== 409 && (error.status < 500 || error.status === 507)) throw error; if (attempt === 4) throw error; }
      }
    }
    if (upload.paused || state.upload !== upload) { upload.status = 'Pozastaveno'; return; }
    upload.completing = true; upload.status = 'Připravujeme video do knihovny…'; updateUploadProgress(); const completed = await api(`/api/uploads/${encodeURIComponent(upload.id)}/complete`, { method: 'POST' }); forget(upload.id); state.upload = null; await loadCatalog(); toast(['queued', 'processing'].includes(processingStatus(completed.video || {})) ? 'Originál je uložený. Teď připravujeme verzi pro snadné přehrávání; u velkých videí to může trvat déle.' : 'Hotovo. Video je uložené v knihovně.');
  } catch (error) { upload.paused = true; upload.status = 'Přenos přerušen'; if ($('upload-error')) $('upload-error').textContent = `${error.message} Nahraná část zůstává uložená. Zkus pokračovat.`; }
  finally { upload.running = false; upload.completing = false; upload.controller = null; updateScreenWakeLock(); if (state.upload === upload) { if (upload.paused && upload.status === 'Nahráváme') upload.status = 'Pozastaveno'; updateUploadProgress(); } }
}
function toggleUpload() { const upload = state.upload; if (!upload || state.preparingFile) return; if (upload.paused) { if (!upload.id) { startSelectedUpload(); return; } upload.status = 'Připojujeme se…'; runUpload(upload); } else { upload.paused = true; upload.status = 'Pozastavujeme…'; upload.controller?.abort(); updateUploadProgress(); } }
async function cancelActiveUpload() { const upload = state.upload; if (!upload || state.preparingFile || !confirm(`Zrušit nahrávání „${upload.file.name}“ a odstranit uloženou část?`)) return; upload.paused = true; upload.controller?.abort(); releaseScreenWakeLock(); if (upload.running) { for (let wait = 0; wait < 30 && upload.running; wait++) await sleep(100); } if (upload.running) { toast('Přenos se ještě zastavuje. Zkus to za chvíli znovu.'); return; } try { if (upload.id) await api(`/api/uploads/${encodeURIComponent(upload.id)}`, { method: 'DELETE' }); forget(upload.id); state.upload = null; await loadCatalog(); toast('Nahrávání je zrušené.'); } catch (error) { toast(error.message); } }
async function cancelPending(upload) { if (!confirm(`Odstranit rozpracovaný přenos „${upload.filename}“?`)) return; if (state.pendingSelection?.id === upload.id) clearPendingSelection(); try { await api(`/api/uploads/${encodeURIComponent(upload.id)}`, { method: 'DELETE' }); forget(upload.id); await loadCatalog(); } catch (error) { toast(error.message); } }
window.addEventListener('beforeunload', event => { if (state.upload?.running && !state.upload.paused) { event.preventDefault(); event.returnValue = ''; } });
function openRequestedVideo() {
  if (state.adminPage) return;
  const identifier = state.requestedVideoId || state.scopedVideoId; state.requestedVideoId = null;
  if (!identifier) return;
  const video = state.videos.find(item => item.id === identifier);
  if (video) openVideo(video); else toast('Tohle video není dostupné. Odkaz mohl být zrušený.');
}
(async () => {
  try {
    applySession(await api('/api/session'));
    let shareError = '';
    if (initialShareToken !== null && !state.adminPage) {
      try {
        const redeemed = await api('/api/share/redeem', { method: 'POST', json: { token: initialShareToken } });
        state.requestedVideoId = redeemed.video_id || state.requestedVideoId;
        applySession(await api('/api/session'));
      } catch (_) { shareError = 'Tenhle odkaz není platný nebo už byl zrušený. Můžeš se přihlásit heslem.'; }
      finally { initialShareToken = null; }
    } else initialShareToken = null;
    if (!state.role || (state.adminPage && state.role !== 'admin')) renderLogin(shareError);
    else { await loadCatalog(); if (shareError) toast(shareError); else openRequestedVideo(); }
  } catch (_) { initialShareToken = null; renderLogin('Kino se teď nepodařilo načíst. Obnov prosím stránku.'); }
})();
