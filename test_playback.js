'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const watch = require('./app/static/watchtime.js');
function sample(clock, now, media, extras = {}) {
  return watch.sample(clock, { nowMs: now * 1000, mediaTime: media, playbackRate: 1, playing: true, paused: false, seeking: false, readyState: 4, ended: false, visible: true, ...extras });
}
function play(clock, seconds, { start = 0, media = start, rate = 1 } = {}) {
  for (let i = 0; i <= seconds * 4; i += 1) sample(clock, start + i / 4, media + i / 4 * rate, { playbackRate: rate });
}

test('view becomes eligible only after ten actual seconds', () => {
  const clock = watch.create(); play(clock, 9.75);
  assert.equal(watch.reached(clock), false);
  sample(clock, 10, 10);
  assert.equal(watch.reached(clock), true);
  assert.equal(clock.seconds, 10);
});

test('starting a video at a late timestamp does not create watch time', () => {
  const clock = watch.create(); sample(clock, 0, 120);
  assert.equal(clock.seconds, 0);
  play(clock, 5, { start: 0, media: 120 });
  assert.equal(clock.seconds, 5); assert.equal(watch.reached(clock), false);
});

test('a forward seek never turns skipped footage into a view', () => {
  const clock = watch.create(); play(clock, 2);
  sample(clock, 2.25, 122.25);
  assert.equal(clock.seconds, 2);
  sample(clock, 2.5, 122.5);
  assert.equal(clock.seconds, 2.25);
  assert.equal(watch.reached(clock), false);
});

test('paused time is excluded and elapsed playback survives resume', () => {
  const clock = watch.create(); play(clock, 6);
  sample(clock, 6.25, 6, { paused: true });
  sample(clock, 60, 6, { paused: true });
  assert.equal(clock.seconds, 6);
  play(clock, 4, { start: 60, media: 6 });
  assert.equal(clock.seconds, 10);
});

test('seeking and buffer stalls reset the baseline without losing credited time', () => {
  const clock = watch.create(); play(clock, 4);
  sample(clock, 4.25, 50, { seeking: true });
  sample(clock, 40, 50, { readyState: 2 });
  play(clock, 2, { start: 40, media: 50 });
  assert.equal(clock.seconds, 6); assert.equal(watch.reached(clock), false);
});

test('double speed still requires ten seconds of real time', () => {
  const clock = watch.create(); play(clock, 5, { rate: 2 });
  assert.equal(clock.seconds, 5); assert.equal(watch.reached(clock), false);
  play(clock, 5, { start: 5, media: 10, rate: 2 });
  assert.equal(clock.seconds, 10); assert.equal(watch.reached(clock), true);
});

test('changing playback rate resets the baseline before accumulating again', () => {
  const clock = watch.create(); play(clock, 4);
  sample(clock, 4.25, 4.5, { playbackRate: 2 });
  assert.equal(clock.seconds, 4);
  sample(clock, 4.5, 5, { playbackRate: 2 });
  assert.equal(clock.seconds, 4.25);
});

test('a quality switch preserves time without crediting the source timestamp jump', () => {
  const clock = watch.create(); play(clock, 7);
  watch.reset(clock); assert.equal(clock.seconds, 7);
  play(clock, 3, { start: 12, media: 200 });
  assert.equal(clock.seconds, 10); assert.equal(watch.reached(clock), true);
});

test('hidden tabs and long throttled intervals do not manufacture playback time', () => {
  const clock = watch.create(); play(clock, 3);
  sample(clock, 3.25, 3.25, { visible: false });
  sample(clock, 40, 40, { visible: false });
  sample(clock, 50, 50);
  sample(clock, 80, 80);
  assert.equal(clock.seconds, 3);
  sample(clock, 80.25, 80.25);
  assert.equal(clock.seconds, 3.25);
});

test('a stopped or ended video does not accrue wall time', () => {
  const clock = watch.create(); play(clock, 2);
  sample(clock, 3, 3, { playing: false });
  sample(clock, 4, 4, { ended: true });
  assert.equal(clock.seconds, 2);
});

test('invalid clocks and unadvancing media do not qualify a view', () => {
  const clock = watch.create(); sample(clock, 0, 0);
  sample(clock, 1, 0); sample(clock, NaN, 20); sample(clock, 3, Infinity);
  sample(clock, 4, 4, { playbackRate: 0 });
  assert.equal(clock.seconds, 0); assert.equal(watch.reached(clock), false);
});
