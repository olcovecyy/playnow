/* Playback time is credited only while media and wall clocks advance together. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.PlayNowWatchTime = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';
  function create() { return { seconds: 0, lastWall: null, lastMedia: null, lastRate: null, eligible: false }; }
  function reset(clock) { clock.lastWall = null; clock.lastMedia = null; clock.lastRate = null; clock.eligible = false; return clock.seconds; }
  function reached(clock, minimumSeconds = 10) { return Number.isFinite(clock.seconds) && clock.seconds + 1e-8 >= minimumSeconds; }
  function sample(clock, input) {
    const now = Number(input.nowMs), media = Number(input.mediaTime), rate = Number(input.playbackRate);
    const eligible = !!input.playing && !input.paused && !input.seeking && !input.ended && input.visible !== false && Number(input.readyState) >= 3 && Number.isFinite(now) && Number.isFinite(media) && media >= 0 && Number.isFinite(rate) && rate > 0;
    if (!eligible) return reset(clock);
    const previousWall = clock.lastWall, previousMedia = clock.lastMedia, previousRate = clock.lastRate, wasEligible = clock.eligible;
    clock.lastWall = now; clock.lastMedia = media; clock.lastRate = rate; clock.eligible = true;
    if (!wasEligible || previousWall === null || previousMedia === null || previousRate !== rate) return clock.seconds;
    const wall = (now - previousWall) / 1000, progress = media - previousMedia;
    if (wall <= 0 || wall > 2 || progress <= 0) return clock.seconds;
    const expected = wall * rate, tolerance = Math.max(0.12, expected * 0.25);
    if (progress > expected + tolerance) return clock.seconds;
    clock.seconds += Math.min(wall, progress / rate);
    return clock.seconds;
  }
  return Object.freeze({ create, reset, sample, reached });
});
