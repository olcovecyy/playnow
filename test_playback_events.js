'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const watch = require('./app/static/watchtime.js');
const source = fs.readFileSync(require.resolve('./app/static/app.js'), 'utf8');
const trackerSource = source.slice(source.indexOf('function playbackIsPresented('), source.indexOf('function processingStatus('));

function playerHarness() {
  const player = Object.assign(new EventTarget(), { currentTime: 0, playbackRate: 1, paused: false, seeking: false, readyState: 4, ended: false });
  const dialog = { open: true }, timers = new Map(), requests = [], accepted = [];
  let now = 0, timerId = 0;
  const context = {
    Event, AbortController, PlayNowWatchTime: watch, state: { role: 'viewer' }, playerVideoId: 'video',
    playbackTracker: null, playbackGeneration: 0, engagementEpoch: 1, engagementControllers: new Set(),
    document: { hidden: false, pictureInPictureElement: null }, performance: { now: () => now },
    $: id => id === 'video-player' ? player : dialog,
    setInterval: callback => { const id = ++timerId; timers.set(id, callback); return id; }, clearInterval: id => timers.delete(id),
    engagementReadContext: () => ({ epoch: context.engagementEpoch }),
    receiveEngagement: (id, value) => accepted.push({ id, value }), refreshEngagementSession() {},
    api: async (path, options) => {
      requests.push({ path, options });
      return path.endsWith('/playback') ? { playback_id: `ticket-${requests.length}`, minimum_seconds: 10 } : { qualified: true, engagement: { views: 1 } };
    }
  };
  vm.createContext(context); vm.runInContext(trackerSource, context);
  const settle = () => new Promise(resolve => setImmediate(resolve));
  return {
    context, player, requests, accepted,
    async start() { context.beginPlaybackTracking({ id: 'video' }); player.dispatchEvent(new Event('playing')); await settle(); },
    async advance(seconds, mediaRate = player.playbackRate, eventsOnly = false) {
      for (let n = 0; n < seconds * 4; n++) {
        now += 250; player.currentTime += .25 * mediaRate;
        if (eventsOnly) player.dispatchEvent(new Event('timeupdate'));
        else [...timers.values()].forEach(callback => callback());
        await settle();
      }
    },
    dispatch(name) { player.dispatchEvent(new Event(name)); },
    get seconds() { return context.playbackTracker.clock.seconds; },
    get starts() { return requests.filter(request => request.path.endsWith('/playback')).length; },
    get qualifications() { return requests.filter(request => !request.path.endsWith('/playback')).length; }
  };
}

test('network stalled does not stop credit for a video still playing from its buffer', async () => {
  const h = playerHarness(); await h.start(); await h.advance(4);
  h.dispatch('stalled'); await h.advance(6);
  assert.equal(h.seconds, 10); assert.equal(h.starts, 1); assert.equal(h.qualifications, 1);
});

test('network stalled with no media progress adds no time, then buffered playback can resume', async () => {
  const h = playerHarness(); await h.start(); await h.advance(4);
  h.dispatch('stalled'); await h.advance(20, 0);
  assert.equal(h.seconds, 4); assert.equal(h.qualifications, 0);
  await h.advance(6); assert.equal(h.seconds, 10); assert.equal(h.qualifications, 1);
});

test('actual waiting and paused time are excluded, while resume keeps the same ticket', async () => {
  const h = playerHarness(); await h.start(); await h.advance(4);
  h.player.readyState = 2; h.dispatch('waiting'); await h.advance(20, 0);
  h.player.paused = true; h.dispatch('pause'); await h.advance(20, 0);
  assert.equal(h.seconds, 4); assert.equal(h.qualifications, 0);
  h.player.paused = false; h.player.readyState = 4; h.dispatch('playing'); await h.advance(6);
  assert.equal(h.seconds, 10); assert.equal(h.starts, 1); assert.equal(h.qualifications, 1);
});

test('seek and source switches preserve played time without crediting jumps or a duplicate view', async () => {
  const h = playerHarness(); await h.start(); await h.advance(4);
  h.player.seeking = true; h.dispatch('seeking'); h.player.currentTime = 120; await h.advance(1, 0);
  h.player.seeking = false; h.dispatch('seeked'); await h.advance(2);
  h.context.suspendPlaybackTracking(); h.dispatch('emptied'); h.player.currentTime = 300; await h.advance(1, 0);
  h.dispatch('playing'); await h.advance(4); await h.advance(5);
  assert.equal(h.seconds, 10); assert.equal(h.starts, 1); assert.equal(h.qualifications, 1);
});

test('media timeupdate events can qualify real playback when the interval timer is throttled', async () => {
  const h = playerHarness(); await h.start(); await h.advance(9.75, 1, true);
  assert.equal(h.qualifications, 0);
  await h.advance(.25, 1, true);
  assert.equal(h.seconds, 10); assert.equal(h.qualifications, 1);
});

test('a hidden ordinary tab stays excluded but actual PiP and wireless playback can qualify', async () => {
  const ordinary = playerHarness(); ordinary.context.document.hidden = true;
  await ordinary.start(); await ordinary.advance(12);
  assert.equal(ordinary.seconds, 0); assert.equal(ordinary.starts, 0);
  for (const presentation of ['standard-pip', 'webkit-pip', 'airplay', 'remote']) {
    const h = playerHarness(); h.context.document.hidden = true;
    if (presentation === 'standard-pip') h.context.document.pictureInPictureElement = h.player;
    if (presentation === 'webkit-pip') h.player.webkitPresentationMode = 'picture-in-picture';
    if (presentation === 'airplay') h.player.webkitCurrentPlaybackTargetIsWireless = true;
    if (presentation === 'remote') h.player.remote = { state: 'connected' };
    await h.start(); await h.advance(10, 1, true);
    assert.equal(h.seconds, 10, presentation); assert.equal(h.qualifications, 1, presentation);
  }
});

test('double speed still needs ten actual seconds through player event handlers', async () => {
  const h = playerHarness(); h.player.playbackRate = 2; await h.start(); await h.advance(5);
  assert.equal(h.seconds, 5); assert.equal(h.qualifications, 0);
  await h.advance(5); assert.equal(h.seconds, 10); assert.equal(h.qualifications, 1);
});

test('a late qualification from a closed player cannot qualify the reopened player', async () => {
  const h = playerHarness(), originalApi = h.context.api;
  let releaseOld, firstQualification = true;
  h.context.api = async (path, options) => {
    const response = await originalApi(path, options);
    if (!path.endsWith('/playback') && firstQualification) {
      firstQualification = false;
      return new Promise(resolve => { releaseOld = resolve; });
    }
    return response;
  };
  await h.start(); await h.advance(10); assert.equal(h.qualifications, 1);
  h.context.stopPlaybackTracking(); h.player.currentTime = 0;
  await h.start(); releaseOld({ qualified: true, engagement: { views: 99 } });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.context.playbackTracker.qualified, false); assert.equal(h.accepted.length, 0);
  await h.advance(10);
  assert.equal(h.starts, 2); assert.equal(h.qualifications, 2); assert.equal(h.accepted.length, 1);
  assert.equal(h.accepted[0].value.views, 1);
});

test('a server timing retry retains the ticket and stops after one qualified view', async () => {
  const h = playerHarness(), originalApi = h.context.api;
  let attempt = 0;
  h.context.api = async (path, options) => {
    const response = await originalApi(path, options);
    if (!path.endsWith('/playback') && ++attempt === 1) return { qualified: false, retry_after: .25, engagement: { views: 0 } };
    return response;
  };
  await h.start(); await h.advance(10);
  assert.equal(h.context.playbackTracker.qualified, false); assert.equal(h.qualifications, 1);
  await h.advance(.25); assert.equal(h.qualifications, 1);
  await h.advance(.25); await h.advance(10);
  assert.equal(h.starts, 1); assert.equal(h.qualifications, 2);
  assert.equal(h.accepted.filter(result => result.value.views === 1).length, 1);
});
