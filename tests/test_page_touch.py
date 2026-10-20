"""Buddy under the finger: the touch zone, the moods a tap gives, the ping on dead parts of the screen and the pack images that follow `rev`.

Most of these run the page for real in a headless Chromium (they skip when there is none), with the same battery as
test_page_tasks.py: the self-test is injected into a temporary COPY of ui/index.html, inside the page's own script, and the
page runs on virtual time, so a 2.5 s heart or a 3 s dizzy is seen in a fraction of a second. The one clock the page reads
for the five-taps window (performance.now) is taken over by the test and moved by hand next to the virtual timers, so nothing
here depends on how fast the machine is. Nothing is ever written to the repo."""
import asyncio
import base64
import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from page_runner import attr, dump, needs_chromium  # noqa: E402
from test_page_tasks import OLD_DEMOS, _css, _page, _rule, assert_all, battery  # noqa: E402
from test_ui import Rig, heartbeat, run, serving  # noqa: E402

DEMO_SCREENS = dict(OLD_DEMOS, runs="main", runs1="main", **{"runs-pack": "main"})
GIF = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")  # a real 1x1 GIF


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path / "state")


# --- what every browser test below starts from -------------------------------------------------------------------------

HELPERS = r"""
  /* The page keeps its tap window on performance.now(): the test owns that clock and moves it next to the virtual timers. */
  var clock = 1000;
  performance.now = function () { return clock; };
  function later(ms, fn) { step(ms, function () { clock += ms; fn(); }); }
  var Z = $('petzone');
  function mood() { return document.body.getAttribute('data-mood'); }
  function screenOf() { return document.body.getAttribute('data-screen'); }
  function gifOn() { var on = $('gif').querySelector('img.on'); return on ? on.getAttribute('data-state') : ''; }
  function accent() { return cs(document.body).getPropertyValue('--accent').trim().toLowerCase(); }
  function shown(sel) { var el = document.querySelector(sel); return !!el && cs(el).display !== 'none'; }
  /* everything on the screen that is the server's word: the pill, the headline, the counters, the level and the name */
  function truth() {
    return JSON.stringify(['mood', 'feed-msg', 'n-total', 'n-running', 'n-waiting', 'f-tokens', 'f-ok', 'f-no', 'pet-level', 'pet-name', 'pet-owner']
      .map(function (id) { return $(id).textContent; }).concat($('xp').style.transform));
  }
  /* every timer the touch code arms is told apart by what it runs (it clears the touch mood and paints) */
  var live = {}, realSet = window.setTimeout, realClear = window.clearTimeout;
  window.setTimeout = function (fn, ms) {
    var id = realSet(function () { delete live[id]; return fn.apply(this, arguments); }, ms);
    live[id] = String(fn).indexOf('clearTouch') >= 0 ? 'touch' : 'other';
    return id;
  };
  window.clearTimeout = function (id) { delete live[id]; return realClear(id); };
  function liveTouch() { return Object.keys(live).filter(function (k) { return live[k] === 'touch'; }).length; }
  /* stage pixels, whatever the scale */
  function st(el) {
    var sr = $('stage').getBoundingClientRect(), k = sr.width / 800, r = el.getBoundingClientRect();
    return { x: (r.left - sr.left) / k, y: (r.top - sr.top) / k, w: r.width / k, h: r.height / k };
  }
  function hits(a, b) { return a.x < b.x + b.w - .5 && b.x < a.x + a.w - .5 && a.y < b.y + b.h - .5 && b.y < a.y + a.h - .5; }
  function middle(el) { var r = el.getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height / 2]; }
  function at(X, Y) { var sr = $('stage').getBoundingClientRect(), k = sr.width / 800; return [sr.left + X * k, sr.top + Y * k]; }
  /* a click the way a finger makes one: on whatever is really under that point, with its coordinates */
  function tapAt(x, y) {
    var t = document.elementFromPoint(x, y);
    t.dispatchEvent(new MouseEvent('click', { bubbles: true, clientX: x, clientY: y }));
    return t;
  }
  function taps(n) { for (var i = 0; i < n; i++) tap(Z); }
  /* the ping: where the last one was put, and whether one is playing */
  function pingMark() { return $('ping').style.getPropertyValue('--px') + '|' + $('ping').style.getPropertyValue('--py'); }
  function pingAnim() { return $('ping').getAnimations()[0] || null; }
  /* Pack images, under the test's control: a src is not fetched, it is remembered, and a load or an error is something the test fires.
     That makes "the new images have not arrived yet" and "the last one arrived" exact moments, not a race with the machine. */
  var held = [];
  function holdImages() {
    var realSetAttribute = Element.prototype.setAttribute;
    Element.prototype.setAttribute = function (name, value) {
      if (this.tagName === 'IMG' && name === 'src') { held.push({ img: this, url: String(value) }); return; }
      return realSetAttribute.apply(this, arguments);
    };
  }
  function batch() { return held.splice(0); }                      // the loads started since the last call
  function fire(entry, kind) { entry.img.dispatchEvent(new Event(kind === 'error' ? 'error' : 'load')); }   // (forEach passes an index as `kind`)
"""

SCREEN_DEMOS = ("idle", "tasks1", "tasks6", "longsaid", "idle-pack", "threads", "threads-long", "threads8", "threads-pack",
                "runs", "runs1", "runs-pack", "waiting", "starting", "notready", "pairing", "pairing-half", "busy", "attention",
                "celebrate", "dizzy", "heart", "asleep")
ASK_DEMOS = ("approval", "approval-long", "approval-queue", "approval-hermes", "approval-cli", "approval-cli-always")


def run_page(tmp_path, body, query="demo=idle", budget=6000):
    return battery(tmp_path, HELPERS + body, query=query, budget=budget)


# --- static: what the page's own source promises --------------------------------------------------------------------


def _touch_block():
    page = _page()
    start = page.index("/* ---------------- Buddy al tacto")
    return page[start:page.index("/* ---------------- la línea con el daemon", start)]


def test_the_touch_numbers_are_what_the_contract_says():
    block = _touch_block()
    for line in ("var WAKE_MS = 2500;", "var HEART_MS = 2500;", "var DIZZY_MS = 3000;", "var TAP_WINDOW_MS = 2000;", "var TAPS_TO_DIZZY = 5;"):
        assert line in block, line
    assert "var ARM_MS = 600;" in _page()  # the approve buttons' own arming is somebody else's number
    assert "$('petzone').addEventListener('click', touchBuddy);" in block  # a click: a drag is not a tap


def test_the_touch_code_stays_out_of_the_arming_the_network_the_storage_and_the_markup():
    block = _touch_block()
    for name in ("holdUntil", "ARM_MS", "decide", "paintAsk", "armTimer", "sending", "curKey", "openTask", "closeTask", "backTask"):
        assert not re.search(rf"\b{name}\b", block), f"the touch code touches {name}"
    for bad in ("fetch(", "XMLHttpRequest", "sendBeacon", "EventSource", "localStorage", "sessionStorage", "indexedDB", "document.cookie",
                "innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "createElement", "textContent", "innerText"):
        assert bad not in block, f"the touch code uses {bad}"  # page-only: no server, no saving, and it writes no text
    assert block.count("setTimeout(") == 1  # one timer, and it is always cleared before the next is armed
    assert re.search(r"function setTouch\(mood, ms\) \{\n\s+clearTimeout\(touchTimer\);", block)


def test_the_effective_mood_is_the_only_thing_that_reaches_the_look_and_the_pill_keeps_the_servers_word():
    page = _page()
    assert "function shownMood() { return touchMood || curMood; }" in page
    assert re.search(r"setAttr\(document\.body, 'data-mood', m\);", page)
    assert re.search(r"renderPlate\(v, mood\);", page)  # the pill is painted from the server's mood, never the shown one
    assert re.search(r"if \(screen === 'ask' \|\| mood === 'attention'\) clearTouch\(\);", page)
    assert not re.search(r"setText\(\$\('mood'\), MOODS\[shownMood", page)


def test_the_zone_and_the_ping_follow_the_design_rules():
    css, page = _css(), _page()
    zone = _rule(css, "#petzone")
    for declaration in ("position:absolute", "left:74px", "top:140px", "width:192px", "height:196px", "z-index:3"):
        assert declaration in zone, declaration
    assert "display:none" in _rule(css, "body[data-screen=boot] #petzone")
    # under a permission the zone stays, inert: a wide finger just below Buddy's feet must land on it and not on Denegar
    assert not re.search(r"data-screen=ask\]\s*#petzone", css)
    ping = _rule(css, "#ping")
    for declaration in ("width:56px", "height:56px", "border-radius:50%", "opacity:0", "pointer-events:none", "color-mix(in srgb,var(--accent) 25%,transparent)"):
        assert declaration in ping, declaration
    assert "left" not in re.sub(r"left:0", "", ping) and "animation" not in ping
    assert "animation:ping .14s" in _rule(css, "#ping.on")
    assert not re.search(r"forwards|both|infinite|backwards", _rule(css, "#ping.on"))  # it goes back to invisible by itself and plays once
    assert '<div id="ping" aria-hidden="true"></div>' in page and '<div id="petzone" aria-hidden="true"></div>' in page
    assert page.count('id="ping"') == 1  # one element, reused
    # the squish of a tap is transform only
    keyframes = re.search(r"@keyframes poke\{(.*?)\}\s*\n", css).group(1)
    assert set(re.findall(r"([a-z-]+)\s*:", keyframes)) == {"transform"}


def test_the_demo_packs_carry_a_rev_like_the_daemons():
    page = _page()
    assert re.search(r"var PACK = \{ name: 'bufo', rev: '[0-9a-f]{12}',", page)
    for demo in ("idle-pack", "threads-pack", "runs-pack"):
        assert re.search(rf"'{demo}': mk\(.*PACK", page), demo  # every demo that has a pack draws this one


def test_the_pack_urls_are_built_in_one_place_and_the_rev_is_encoded():
    page = _page()
    assert page.count("function packUrl(c, file)") == 1
    assert "'?v=' + encodeURIComponent(c.rev)" in page
    assert "typeof c.rev === 'string' && c.rev" in page
    start = page.index("function loadPack(c)")
    block = page[start:page.index("function showGif(mood)", start)]
    assert "packSeq" in block and "packWait" in block and "removeChild(box.firstChild)" in block
    assert not re.search(r"\.src\s*=[^=]", page)  # the URL goes through setAttribute, the one spelling the "loads nothing from outside" test lets through


def test_this_file_has_no_em_dash():
    assert chr(0x2014) not in Path(__file__).read_text(encoding="utf-8")


# --- the zone: where it is, what it covers and what it leaves alone ------------------------------------------------------

ZONE_JS = r"""
  var checked = 0;
  var names = __NAMES__, asks = __ASKS__;
  names.forEach(function (name) {
    demoShow(name);
    var zone = st(Z), pack = document.body.classList.contains('pack'), fig = pack ? st($('gif')) : st($('pet-svg'));
    ok(name + ': at least 120 x 120 stage pixels (192 x 196)', zone.w >= 120 && zone.h >= 120 && Math.abs(zone.w - 192) < 1 && Math.abs(zone.h - 196) < 1, JSON.stringify(zone));
    ok(name + ': centred on the figure, in ' + (pack ? 'pack' : 'SVG') + ' mode', Math.abs(zone.x + zone.w / 2 - fig.x - fig.w / 2) <= 8 && Math.abs(zone.y + zone.h / 2 - fig.y - fig.h / 2) <= 8,
       JSON.stringify([zone, fig]));
    ok(name + ': it covers the figure', zone.x <= fig.x + 8 && zone.y <= fig.y + 8 && zone.x + zone.w >= fig.x + fig.w - 8 && zone.y + zone.h >= fig.y + fig.h - 8);
    var clash = Array.prototype.slice.call(document.querySelectorAll('#plate, #mood, #top, .col, .what, .hint-wrap, .btns, #always-note, button, .pill'))
      .filter(function (el) { return el.getClientRects().length > 0 && hits(zone, st(el)); });
    ok(name + ': it overlaps no plate, mood pill, bar, right column or control', clash.length === 0, clash.map(function (el) { return el.id || el.className; }).join(','));
    var c = middle(Z), r = Z.getBoundingClientRect();
    ok(name + ': a finger on its centre and its corners finds the zone', [[c[0], c[1]], [r.left + 3, r.top + 3], [r.right - 3, r.top + 3], [r.left + 3, r.bottom - 3], [r.right - 3, r.bottom - 3]]
      .every(function (p) { return document.elementFromPoint(p[0], p[1]) === Z; }));
    checked += 1;
  });
  asks.forEach(function (name) {
    demoShow(name);
    /* The zone stays under a permission, inert: a finger is wider than its tip, and the browser sends a tap that lands a few pixels
       below Buddy's feet to the nearest control, which is Denegar. The zone is what claims that tap (touchBuddy ignores it there). */
    var zone = st(Z);
    ok(name + ': the zone is still there under a permission (inert, it only claims the tap)', screenOf() === 'ask' && cs(Z).display !== 'none' && zone.w >= 120 && zone.h >= 120, JSON.stringify(zone));
    /* (the stack of the tool name, its age and the "en cola" pill is static text that can reach down into the top of the zone when the
       queue is long: it is not a control and not a text that scrolls, and the zone is transparent, so nothing is lost) */
    var clash = Array.prototype.slice.call(document.querySelectorAll('#plate, #mood, #top, .hint-wrap, .hint, .btns, #always-note, button'))
      .filter(function (el) { return el.getClientRects().length > 0 && hits(zone, st(el)); });
    ok(name + ': there it overlaps no plate, bar, scrolling text or control either', clash.length === 0, clash.map(function (el) { return el.id || el.className; }).join(','));
    var band = [[170, 330], [170, 334], [90, 332], [250, 332]].map(function (p) { return at(p[0], p[1]); });
    ok(name + ': a finger on the band just below Buddy\'s feet finds the zone and not a button', band.every(function (p) { return document.elementFromPoint(p[0], p[1]) === Z; }));
    checked += 1;
  });
  demoShow('boot');
  ok('boot: no zone either', screenOf() === 'boot' && cs(Z).display === 'none');
  ok('every screen was looked at', checked === names.length + asks.length, checked);
"""


@needs_chromium
def test_the_zone_sits_on_the_figure_in_both_modes_and_covers_no_control(tmp_path):
    body = ZONE_JS.replace("__NAMES__", json.dumps(SCREEN_DEMOS)).replace("__ASKS__", json.dumps(ASK_DEMOS))
    assert_all(run_page(tmp_path, body, budget=5000), 135)


# --- a tap: heart for 2.5 s, restarted by the next, and the server mood is what comes back --------------------------------

HEART_JS = r"""
  var base = truth();
  ok('it starts as the server says', mood() === 'idle' && touchMood === '' && liveTouch() === 0 && $('mood').textContent === 'Tranquilo');
  tap(Z);
  ok('a tap makes Buddy heart', mood() === 'heart' && touchMood === 'heart' && screenOf() === 'main');
  ok('the SVG wears the heart look', shown('#pet .e-heart') && !shown('#pet .e-open') && shown('#pet .fx-heart') && accent() === '#ff8fa3', accent());
  ok('it lands like jelly and is squeezed by the finger', document.querySelector('#pet .jelly').classList.contains('land') && $('pet-svg').classList.contains('poke')
    && cs($('pet-svg')).animationName === 'poke');
  ok('the pill, the headline and the counters keep the servers word', truth() === base && $('mood').textContent === 'Tranquilo');
  ok('one timer', liveTouch() === 1);
  later(2400, function () { ok('still heart at 2400 ms', mood() === 'heart' && liveTouch() === 1); });
  later(200, function () {
    ok('and back to the server mood at 2600 ms', mood() === 'idle' && touchMood === '' && liveTouch() === 0 && shown('#pet .e-open') && truth() === base);
    tap(Z);                                                          // the restart
  });
  later(2000, function () { ok('2 s into the second heart', mood() === 'heart'); tap(Z); });
  later(1000, function () { ok('a tap while it is on restarts the 2500 ms (it would have ended at 2500)', mood() === 'heart' && liveTouch() === 1); });
  later(1400, function () { ok('and it is still on at 2400 ms after the last tap', mood() === 'heart' && liveTouch() === 1); });
  later(200, function () { ok('then it ends', mood() === 'idle' && touchMood === '' && liveTouch() === 0 && truth() === base); });
  /* a lone tap long after the last one is a heart, never a dizzy: the window only counts the last 2 s */
  later(5000, function () { tap(Z); ok('a lone tap is a heart', mood() === 'heart'); });
  later(2600, function () { ok('and it ends again', mood() === 'idle' && liveTouch() === 0); });
"""


@needs_chromium
def test_a_tap_hearts_buddy_for_2500_ms_a_second_tap_restarts_it_and_the_server_mood_returns(tmp_path):
    assert_all(run_page(tmp_path, HEART_JS, budget=20000), 14)


SLEEP_JS = r"""
  var base = truth();
  ok('asleep: closed eyes and the pill says so', mood() === 'sleep' && $('mood').textContent === 'Durmiendo' && shown('#pet .e-sleep') && !shown('#pet .e-open'));
  tap(Z);
  ok('the first tap wakes Buddy: idle, eyes open', mood() === 'idle' && touchMood === 'idle' && shown('#pet .e-open') && !shown('#pet .e-sleep'));
  ok('the wake is a hop (jelly landing) and the status still says it is asleep', document.querySelector('#pet .jelly').classList.contains('land') && truth() === base);
  later(1000, function () { tap(Z); ok('a tap in the awake window is a heart tap', mood() === 'heart' && touchMood === 'heart'); });
  later(2400, function () { ok('the heart ran its own 2500 ms from the second tap', mood() === 'heart'); });
  later(200, function () { ok('then asleep again', mood() === 'sleep' && touchMood === '' && liveTouch() === 0 && truth() === base); tap(Z); });
  later(2400, function () { ok('a wake with no second tap lasts 2500 ms', mood() === 'idle' && liveTouch() === 1); });
  later(200, function () { ok('and then Buddy sleeps', mood() === 'sleep' && liveTouch() === 0 && truth() === base); });
  /* the screens that make Buddy sleep by themselves wake the same way */
  later(10, function () {
    ['waiting', 'starting', 'notready', 'pairing', 'pairing-half'].forEach(function (name) {
      demoShow(name);
      var before = truth();
      ok(name + ': asleep', mood() === 'sleep');
      tap(Z);
      ok(name + ': the first tap wakes it', mood() === 'idle' && touchMood === 'idle' && truth() === before);
      tap(Z);
      ok(name + ': the second is a heart', mood() === 'heart');
    });
  });
"""


@needs_chromium
def test_a_sleeping_buddy_is_woken_by_the_first_tap_and_hearted_by_the_next(tmp_path):
    assert_all(run_page(tmp_path, SLEEP_JS, query="demo=asleep", budget=9000), 23)


DIZZY_JS = r"""
  var base = truth();
  taps(4);
  ok('four taps are hearts', mood() === 'heart' && touchMood === 'heart');
  tap(Z);
  ok('the fifth within 2 s makes Buddy dizzy', mood() === 'dizzy' && touchMood === 'dizzy' && shown('#pet .e-dizzy') && shown('#pet .fx-dizzy') && accent() === '#a9b8cf', accent());
  ok('the status still tells the truth', truth() === base && $('mood').textContent === 'Tranquilo');
  ok('one timer and a clean counter', liveTouch() === 1 && touchTaps.length === 0);
  var id = touchTimer;
  later(1000, function () {
    taps(3);
    ok('taps while dizzy are ignored: same mood, same timer, nothing counted, no squeeze', mood() === 'dizzy' && touchTimer === id && touchTaps.length === 0 && liveTouch() === 1);
    /* ignored is not mute: the finger is felt (the plain ping), and still nothing counts */
    var zc = middle(Z), mark = pingMark();
    var landed = tapAt(zc[0], zc[1]);
    ok('a tap in the cooldown answers with a ping and changes nothing else', landed === Z && pingMark() !== mark && pingAnim() !== null && mood() === 'dizzy' && touchTimer === id && touchTaps.length === 0
      && liveTouch() === 1 && truth() === base);
  });
  later(1900, function () { ok('still dizzy at 2900 ms', mood() === 'dizzy' && liveTouch() === 1); });
  later(200, function () {
    ok('over at 3000 ms (the taps did not extend it), and the counter is clean', mood() === 'idle' && touchMood === '' && liveTouch() === 0 && touchTaps.length === 0 && truth() === base);
    taps(4);
    ok('after the cooldown four taps are hearts again (the counter reset)', mood() === 'heart' && touchMood === 'heart');
    tap(Z);
    ok('and the fifth makes it dizzy again', mood() === 'dizzy');
  });
  later(3100, function () { ok('and it comes back', mood() === 'idle' && liveTouch() === 0); });
"""


@needs_chromium
def test_five_taps_in_two_seconds_make_buddy_dizzy_for_3_s_and_ignore_taps_until_it_ends(tmp_path):
    assert_all(run_page(tmp_path, DIZZY_JS, budget=12000), 11)


WINDOW_JS = r"""
  /* taps at 0, 600, 1200, 1800 and 2400 ms: the window is 2000 ms and it slides, so the fifth finds only four in it */
  tap(Z);
  later(600, function () { tap(Z); });
  later(600, function () { tap(Z); });
  later(600, function () { tap(Z); });
  later(600, function () { tap(Z); ok('the first tap has left the window by 2400 ms: still a heart', mood() === 'heart' && touchTaps.length === 4, touchTaps.length); });
  later(100, function () { tap(Z); ok('at 2500 ms five taps are inside 2000 ms: dizzy', mood() === 'dizzy', mood()); });
  later(3100, function () { ok('over', mood() === 'idle' && liveTouch() === 0); });
  /* taps 2100 ms apart (inside the 2500 ms heart) never make it dizzy, however many */
  [1, 2, 3, 4, 5, 6, 7].forEach(function () {
    later(2100, function () { tap(Z); ok('a spaced tap is a heart', mood() === 'heart' && touchTaps.length <= 2, touchTaps.length); });
  });
  /* exactly inside: five taps over 2000 ms on the dot count */
  later(2600, function () { ok('idle between the series', mood() === 'idle'); tap(Z); });
  [1, 2, 3, 4].forEach(function (i) { later(500, function () { tap(Z); if (i === 4) ok('five taps spanning exactly 2000 ms are dizzy', mood() === 'dizzy', mood()); }); });
"""


@needs_chromium
def test_the_five_taps_window_slides_and_spaced_taps_never_make_buddy_dizzy(tmp_path):
    assert_all(run_page(tmp_path, WINDOW_JS, budget=40000), 12)


# --- the things Buddy is not to be played with: attention, a permission, boot ------------------------------------------------

ATTENTION_JS = r"""
  ok('attention on the main screen', mood() === 'attention' && screenOf() === 'main');
  var base = truth();
  tap(Z); tap(Z);
  ok('it needs you: a tap does nothing, arms nothing and does not squeeze', mood() === 'attention' && touchMood === '' && liveTouch() === 0 && !$('pet-svg').classList.contains('poke') && truth() === base);
  /* ... but the finger is felt: the same 140 ms ping as on any dead part of the screen, at the touch point (Buddy does not play, it does not stay mute) */
  var zc = middle(Z), before = pingMark();
  var landed = tapAt(zc[0], zc[1]), after = pingMark(), a1 = pingAnim();
  ok('a tap on Buddy while it needs you answers with a ping and nothing else', landed === Z && a1 !== null && a1.animationName === 'ping' && after !== before
    && mood() === 'attention' && touchMood === '' && liveTouch() === 0 && !$('pet-svg').classList.contains('poke') && !$('gif').classList.contains('poke') && truth() === base);
  var zr = Z.getBoundingClientRect();
  tapAt(zr.left + 20, zr.top + 20);
  ok('and it follows the finger (a new ping, another place)', pingMark() !== after && pingAnim() !== a1 && document.querySelectorAll('#ping').length === 1);
  /* the server turns to attention while a heart is on */
  show(function (v) { v.mood = 'idle'; });
  tap(Z);
  ok('a heart', mood() === 'heart' && liveTouch() === 1);
  show(function (v) { v.mood = 'attention'; });
  ok('attention wins at once: no override, no timer', mood() === 'attention' && touchMood === '' && touchTimer === 0 && liveTouch() === 0 && $('mood').textContent === 'Te necesita');
  later(3000, function () { ok('and nothing comes back later', mood() === 'attention' && touchMood === ''); });
"""


@needs_chromium
def test_a_buddy_that_needs_you_does_not_play_and_attention_ends_a_touch_mood_at_once(tmp_path):
    assert_all(run_page(tmp_path, ATTENTION_JS, query="demo=attention", budget=5000), 7)


ASK_JS = r"""
  ok('a permission is up and the zone is still there, inert (it claims a tap that lands just below Buddy)', screenOf() === 'ask' && mood() === 'attention' && cs(Z).display !== 'none');
  tap(Z); taps(6);
  ok('a tap on the touch zone does nothing there (even a click that reaches it)', mood() === 'attention' && touchMood === '' && liveTouch() === 0 && !$('pet-svg').classList.contains('poke'));
  var zc = middle(Z), hit = tapAt(zc[0], zc[1]);
  ok('and it is silent: no squeeze, no ping, nothing for a wide finger to confuse with a button', hit === Z && pingAnim() === null && pingMark() === '|' && !$('pet-svg').classList.contains('poke') && !$('gif').classList.contains('poke'));
  var low = at(170, 332), under = tapAt(low[0], low[1]);
  ok('a tap on the band just below Buddy\'s feet is the zone\'s: it decides nothing and the buttons stay asleep', under === Z && $('btns').className.indexOf('hold') >= 0 && touchMood === '' && sending && Object.keys(sending).length === 0);
  ok('the buttons are still asleep for their own 600 ms', $('btns').className.indexOf('hold') >= 0);
  /* a oneshot heart from the server is no reason to play either */
  show(function (v) { v.mood = 'heart'; });
  tap(Z);
  ok('not even when the server itself says heart', screenOf() === 'ask' && touchMood === '' && liveTouch() === 0);
"""


@needs_chromium
def test_nothing_responds_on_a_permission(tmp_path):
    assert_all(run_page(tmp_path, ASK_JS, query="demo=approval", budget=3000), 6)


BOOT_JS = r"""
  ok('boot', screenOf() === 'boot' && cs(Z).display === 'none');
  tap(Z); taps(6);
  ok('nothing responds before the first state', touchMood === '' && liveTouch() === 0 && mood() === 'sleep');
"""


@needs_chromium
def test_nothing_responds_before_the_first_state(tmp_path):
    assert_all(run_page(tmp_path, BOOT_JS, query="demo=boot", budget=3000), 2)


APPROVAL_JS = r"""
  var ask = { key: 'claude:t1', tool: 'Bash', hint: 'ls', source: 'claude' }, ask2 = { key: 'claude:t2', tool: 'Edit', hint: 'x.py', source: 'claude' };
  /* an approval arrives in the middle of a heart */
  tap(Z);
  ok('a heart is on', mood() === 'heart' && liveTouch() === 1);
  show(function (v) { v.approvals = [ask]; v.mood = 'attention'; });
  ok('the approval ends it at once: the ask screen, attention, no override, no timer', screenOf() === 'ask' && mood() === 'attention' && touchMood === '' && touchTimer === 0 && liveTouch() === 0);
  ok('and its buttons are asleep for the usual 600 ms', $('btns').className.indexOf('hold') >= 0);
  tap(Z);
  ok('taps do not play on the ask screen', touchMood === '' && liveTouch() === 0);
  later(700, function () { ok('then the buttons wake as always', $('btns').className.indexOf('hold') < 0 && mood() === 'attention'); });
  later(3000, function () {
    ok('and no old heart came back', mood() === 'attention' && touchMood === '' && liveTouch() === 0);
    show(function (v) { v.approvals = []; v.mood = 'idle'; });
    ok('decided: the main screen and the server mood', screenOf() === 'main' && mood() === 'idle');
    tap(Z);
    ok('and Buddy plays again', mood() === 'heart');
    taps(4);
    ok('even dizzy', mood() === 'dizzy' && liveTouch() === 1);
  });
  /* the same in the middle of a dizzy */
  later(500, function () {
    show(function (v) { v.approvals = [ask2]; v.mood = 'attention'; });
    ok('an approval ends a dizzy at once too', screenOf() === 'ask' && mood() === 'attention' && touchMood === '' && touchTimer === 0 && liveTouch() === 0);
  });
  later(4000, function () { ok('and nothing is left over', mood() === 'attention' && liveTouch() === 0 && touchTaps.length === 0); });
"""


@needs_chromium
def test_an_approval_arriving_mid_override_ends_it_at_once_and_leaves_no_timer_and_the_600_ms_alone(tmp_path):
    assert_all(run_page(tmp_path, APPROVAL_JS, budget=12000), 11)


LATE_MOOD_JS = r"""
  /* the daemon always puts attention with a pending request, but the page must not depend on it: a permission on screen ends every touch mood */
  var ask = { key: 'claude:t9', tool: 'Bash', hint: 'ls', source: 'claude' };
  tap(Z);
  ok('a heart is on', mood() === 'heart' && liveTouch() === 1);
  show(function (v) { v.approvals = [ask]; });
  ok('a permission with the server mood still idle ends it at once: the server mood shows, no timer', screenOf() === 'ask' && touchMood === '' && touchTimer === 0 && liveTouch() === 0 && mood() === 'idle');
  show(function (v) { v.approvals = []; });
  taps(5);
  ok('a dizzy is on', mood() === 'dizzy' && liveTouch() === 1);
  show(function (v) { v.approvals = [ask]; });
  ok('and a permission ends it just the same', screenOf() === 'ask' && touchMood === '' && touchTimer === 0 && liveTouch() === 0 && mood() === 'idle');
  later(4000, function () { ok('nothing comes back later', mood() === 'idle' && touchMood === '' && liveTouch() === 0); });
"""


@needs_chromium
def test_a_permission_ends_a_touch_mood_even_when_the_servers_mood_is_not_attention(tmp_path):
    assert_all(run_page(tmp_path, LATE_MOOD_JS, budget=6000), 5)


# --- the server keeps talking while Buddy is touched --------------------------------------------------------------------------

SERVER_JS = r"""
  var base = truth();
  tap(Z);
  var id = touchTimer;
  /* the page re-renders on every state the server pushes: that must not reset a heart, move its timer or touch its look */
  for (var i = 0; i < 20; i++) show(function (v) { v.sessions.tokens_today += 1; });
  ok('twenty renders later the heart is where it was, with the same timer', mood() === 'heart' && touchTimer === id && liveTouch() === 1);
  /* a new server mood does not cancel it, but the pill tells the new truth */
  show(function (v) { v.mood = 'busy'; });
  ok('a new server mood does not cancel a heart; the pill follows the server', mood() === 'heart' && touchTimer === id && $('mood').textContent === 'Trabajando');
  later(2600, function () {
    ok('when it ends the CURRENT server mood shows, not the one at the tap', mood() === 'busy' && liveTouch() === 0 && $('mood').textContent === 'Trabajando');
    show(function (v) { v.mood = 'idle'; });
    taps(5);
    ok('a dizzy', mood() === 'dizzy');
    show(function (v) { v.mood = 'celebrate'; });
    ok('does not give way to a new server mood either', mood() === 'dizzy' && $('mood').textContent === '¡Terminó!');
    show(function (v) { v.linked = false; });
    ok('nor to the link dropping (the screen asleep): the pill says asleep, Buddy is dizzy', screenOf() === 'wait' && mood() === 'dizzy' && $('mood').textContent === 'Durmiendo');
  });
  later(3100, function () { ok('and at the end it is what the screen says now: asleep', mood() === 'sleep' && liveTouch() === 0); });
  /* nothing was sent anywhere, nothing was kept */
  later(10, function () {
    var asked = 0, kept = 0, realSetItem = Storage.prototype.setItem;
    window.fetch = function () { asked += 1; return Promise.reject(new Error('no')); };
    Storage.prototype.setItem = function () { kept += 1; };
    demoShow('idle');
    taps(5);
    tapAt.apply(null, at(300, 300));
    ok('touching Buddy or the dead screen asks the server for nothing and keeps nothing', mood() === 'dizzy' && asked === 0 && kept === 0, asked + '/' + kept);
    Storage.prototype.setItem = realSetItem;
  });
"""


@needs_chromium
def test_the_servers_stream_does_not_reset_a_touch_mood_and_the_end_shows_the_current_server_mood(tmp_path):
    assert_all(run_page(tmp_path, SERVER_JS, budget=12000), 8)


CYCLE_JS = r"""
  /* the preview cycle moves on every 4.5 s: a touch mood must not leak from one demo into the next, nor leave a timer */
  tap(Z);
  ok('the first demo of the cycle is boot: nothing responds', screenOf() === 'boot' && touchMood === '');
  later(8800, function () {
    ok('the second demo is on (the page starting up)', screenOf() === 'down');
    tap(Z);
    ok('Buddy wakes there', mood() === 'idle' && liveTouch() === 1);
  });
  later(300, function () { ok('the cycle moved on at 9 s: no touch mood and no timer left', screenOf() === 'down' && touchMood === '' && liveTouch() === 0 && mood() === 'sleep'); });
"""


@needs_chromium
def test_the_demo_cycle_carries_no_touch_mood_from_one_demo_to_the_next(tmp_path):
    assert_all(run_page(tmp_path, CYCLE_JS, query="demo=cycle", budget=11000), 4)


# --- pack mode: the same rules, the GIF of the effective mood ------------------------------------------------------------------

PACK_JS = r"""
  var ALL = { sleep: ['sleep.gif'], idle: ['idle_0.gif', 'idle_1.gif'], busy: ['busy.gif'], attention: ['attention.gif'], celebrate: ['celebrate.gif'],
              dizzy: ['dizzy.gif'], heart: ['heart.gif'] };
  var ACC = { sleep: '#A78BFA', idle: '#FF9E7A', busy: '#4FD1C5', attention: '#FFCF5C', celebrate: '#8BE76A', dizzy: '#A9B8CF', heart: '#FF8FA3' };
  function pack(states, rev) {
    return { name: 'bufo', rev: rev, colors: { bg: '#0E0D14', text: '#F3EEF7', textDim: '#9A93A4', body: '#FF9E7A' }, accents: ACC, render: 'pixel', states: states };
  }
  holdImages();
  show(function (v) { v.character = pack(ALL, '0123456789ab'); });
  var base = truth();
  ok('pack mode: the pack is drawn and the SVG is not', document.body.classList.contains('pack') && cs($('pet')).display === 'none' && cs($('gif')).display === 'flex');
  ok('idle follows the server', mood() === 'idle' && gifOn() === 'idle');
  ok('a finger on the GIF finds the zone', document.elementFromPoint.apply(document, middle($('gif'))) === Z);
  tap(Z);
  ok('a tap shows the heart GIF with the heart accent', mood() === 'heart' && gifOn() === 'heart' && document.body.style.getPropertyValue('--accent') === '#FF8FA3');
  ok('the GIF is squeezed by the finger and the status keeps its word', $('gif').classList.contains('poke') && cs($('gif')).animationName === 'poke' && truth() === base);
  later(2600, function () {
    ok('back to the server mood GIF', mood() === 'idle' && gifOn() === 'idle' && document.body.style.getPropertyValue('--accent') === '#FF9E7A');
    taps(5);
    ok('five taps: the dizzy GIF', mood() === 'dizzy' && gifOn() === 'dizzy' && document.body.style.getPropertyValue('--accent') === '#A9B8CF');
  });
  later(3100, function () {
    ok('and back again', mood() === 'idle' && gifOn() === 'idle');
    show(function (v) { v.mood = 'sleep'; });
    ok('asleep in pack mode', gifOn() === 'sleep');
    tap(Z);
    ok('the first tap wakes it: the idle GIF', mood() === 'idle' && gifOn() === 'idle' && $('mood').textContent === 'Durmiendo');
    tap(Z);
    ok('then hearts', gifOn() === 'heart');
  });
  /* a pack that lacks the states falls back exactly as before: idle if it has one, else the first state */
  later(2600, function () {
    batch();
    show(function (v) { v.mood = 'idle'; v.character = pack({ sleep: ['sleep.gif'], idle: ['idle_0.gif'], busy: ['busy.gif'] }, 'aaaaaaaaaaaa'); });
    batch().forEach(fire);                                           // the new pack arrives
    ok('a new pack is swapped in', packWait === false && $('gif').children.length === 3);
    ok('no heart and no dizzy GIF: the idle one', gifOn() === 'idle');
    tap(Z);
    ok('a tap on a pack without heart shows idle', mood() === 'heart' && gifOn() === 'idle');
    taps(4);
    ok('and without dizzy', mood() === 'dizzy' && gifOn() === 'idle');
  });
  later(3100, function () {
    show(function (v) { v.mood = 'busy'; v.character = pack({ sleep: ['sleep.gif'], busy: ['busy.gif'] }, 'bbbbbbbbbbbb'); });
    batch().forEach(fire);
    ok('a pack with neither idle, heart nor dizzy: the server mood GIF', gifOn() === 'busy');
    tap(Z);
    ok('a heart tap falls back to its first state (as the old code did)', mood() === 'heart' && gifOn() === 'sleep');
  });
"""


@needs_chromium
def test_pack_mode_shows_the_gif_of_the_effective_mood_and_falls_back_like_before(tmp_path):
    assert_all(run_page(tmp_path, PACK_JS, query="demo=idle-pack", budget=16000), 16)


ROTATION_JS = r"""
  /* a pack with two idles rotates them every 12 s: the rotation must repaint the mood Buddy is showing, not the server's */
  show(function (v) {
    v.character = { name: 'bufo', rev: '0123456789ab', colors: { bg: '#000000', text: '#ffffff', textDim: '#bdbdbd', body: '#7fd27f' },
                    states: { sleep: ['sleep.gif'], idle: ['idle_0.gif', 'idle_1.gif'], heart: ['heart.gif'] } };
  });
  ok('idle_0 shows first', gifOn() === 'idle' && $('gif').querySelector('img.on').getAttribute('data-i') === '0');
  later(11500, function () { tap(Z); ok('a heart at 11.5 s', gifOn() === 'heart'); });
  later(1000, function () {
    ok('the 12 s rotation fired, and it did not undo the heart', gifIdx >= 1 && gifOn() === 'heart', gifIdx + ' ' + gifOn());
  });
  later(1600, function () {
    var on = $('gif').querySelector('img.on');
    ok('the heart is over and the idle in turn shows', mood() === 'idle' && on.getAttribute('data-state') === 'idle' && on.getAttribute('data-i') === String(gifIdx % 2), gifIdx);
  });
"""


@needs_chromium
def test_the_idle_rotation_of_a_pack_does_not_undo_a_touch_mood(tmp_path):
    assert_all(run_page(tmp_path, ROTATION_JS, query="demo=idle-pack", budget=15000), 4)


# --- the colour of a touch mood belongs to Buddy; what the screen reports keeps the server's -------------------------------------

ACCENT_JS = r"""
  /* the pill, the counters, the net pill, the level bar and the rows of working threads are the server's word: their colour too */
  var chromeSel = '#mood, #xp, .xptrack, #net, .pill, .trow, .tv-back, .card, .steps li';
  function chrome() {
    return JSON.stringify(Array.prototype.slice.call(document.querySelectorAll(chromeSel)).map(function (el) {
      return cs(el).backgroundColor + (el.matches('.steps li') ? '/' + getComputedStyle(el, '::before').backgroundColor : '');   // the number discs are a ::before
    }));
  }
  function glow() { return cs($('glow')).backgroundImage; }
  var BUSY = 'rgb(79, 209, 197)', CELEBRATE = 'rgb(139, 231, 106)', HEART = 'rgb(255, 143, 163)';
  ok('the server says working: the pill is teal and there are working rows', mood() === 'busy' && cs($('mood')).backgroundColor === BUSY && document.querySelectorAll('.trow').length >= 3);
  var base = chrome(), glowBusy = glow(), words = truth();
  tap(Z);
  ok('Buddy turns heart: its own accent is pink and so is its glow', mood() === 'heart' && accent() === '#ff8fa3' && glow() !== glowBusy && glow().indexOf(HEART) >= 0, glow());
  ok('but the pill, the rows, the level bar and the net pill keep the colour of the server mood', chrome() === base && cs($('mood')).backgroundColor === BUSY && truth() === words);
  /* a new server mood in the middle of the heart: the screen follows the server at once, Buddy keeps its heart */
  show(function (v) { v.mood = 'celebrate'; });
  ok('a server mood in the middle of a heart recolours the screen and not Buddy', mood() === 'heart' && accent() === '#ff8fa3' && cs($('mood')).backgroundColor === CELEBRATE && chrome() !== base);
  var celebrating = chrome();
  later(2600, function () {
    ok('when the heart ends Buddy joins the screen: one colour again', mood() === 'celebrate' && accent() === '#8be76a' && chrome() === celebrating && cs($('mood')).backgroundColor === CELEBRATE);
    taps(5);
    ok('a dizzy does not touch the status colours either', mood() === 'dizzy' && accent() === '#a9b8cf' && chrome() === celebrating);
  });
  later(3100, function () { ok('and after it nothing was left behind', mood() === 'celebrate' && chrome() === celebrating); });
  /* the screens that make Buddy sleep by themselves: touching it wakes it, the screen still reports sleep */
  later(10, function () {
    demoShow('waiting');
    var asleep = chrome();
    tap(Z);
    ok('woken on the waiting screen: Buddy is idle-coloured, the screen stays the colour of the sleep it reports', mood() === 'idle' && accent() === '#ff9e7a' && chrome() === asleep);
  });
"""


@needs_chromium
def test_a_touch_mood_recolours_buddy_and_not_the_pill_the_rows_or_the_bar_in_svg_mode(tmp_path):
    assert_all(run_page(tmp_path, ACCENT_JS, query="demo=threads", budget=8000), 8)


ACCENT_PACK_JS = r"""
  var ALL = { sleep: ['sleep.gif'], idle: ['idle_0.gif'], busy: ['busy.gif'], attention: ['attention.gif'], celebrate: ['celebrate.gif'], dizzy: ['dizzy.gif'], heart: ['heart.gif'] };
  var ACC = { sleep: '#A78BFA', idle: '#FF9E7A', busy: '#4FD1C5', attention: '#FFCF5C', celebrate: '#8BE76A', dizzy: '#A9B8CF', heart: '#FF8FA3' };
  holdImages();
  show(function (v) {
    v.mood = 'busy';
    v.character = { name: 'bufo', rev: '0123456789ab', colors: { bg: '#0E0D14', text: '#F3EEF7', textDim: '#9A93A4', body: '#FF9E7A' }, accents: ACC, render: 'pixel', states: ALL };
  });
  batch().forEach(fire);
  var chromeSel = '#mood, #xp, .xptrack, #net, .pill, .trow, .tv-back';
  function chrome() { return JSON.stringify(Array.prototype.slice.call(document.querySelectorAll(chromeSel)).map(function (el) { return cs(el).backgroundColor; })); }
  var base = chrome();
  ok('pack mode, working: Buddy and the pill both wear the pack\'s busy accent', gifOn() === 'busy' && accent() === '#4fd1c5' && cs($('mood')).backgroundColor === 'rgb(79, 209, 197)');
  tap(Z);
  ok('a tap: the heart GIF and the heart accent for Buddy', gifOn() === 'heart' && accent() === '#ff8fa3');
  ok('the status colours stay the pack\'s busy accent', chrome() === base && cs($('mood')).backgroundColor === 'rgb(79, 209, 197)');
  show(function (v) { v.mood = 'celebrate'; });
  ok('a server mood in the middle of the heart: the screen goes to the pack\'s celebrate accent, Buddy keeps the heart', accent() === '#ff8fa3' && cs($('mood')).backgroundColor === 'rgb(139, 231, 106)');
  later(2600, function () {
    ok('then they agree again', gifOn() === 'celebrate' && accent() === '#8be76a' && cs($('mood')).backgroundColor === 'rgb(139, 231, 106)');
  });
  /* a pack without per-mood accents: one colour for everything, touched or not */
  later(10, function () {
    show(function (v) { v.mood = 'idle'; v.character = { name: 'bufo', rev: 'aaaaaaaaaaaa', colors: { bg: '#000000', text: '#ffffff', textDim: '#bdbdbd', body: '#7fd27f' }, states: ALL }; });
    batch().forEach(fire);
    var plain = chrome();
    tap(Z);
    ok('a pack with only a body colour: the pill and Buddy keep it, touched or not', accent() === '#7fd27f' && chrome() === plain && cs($('mood')).backgroundColor === 'rgb(127, 210, 127)');
  });
  /* the pack goes away: the page's own colours return, for Buddy and for the screen */
  later(10, function () {
    show(function (v) { v.character = null; });
    ok('no pack: no inline colours left over', document.body.style.getPropertyValue('--accent') === '' && document.body.style.getPropertyValue('--accent-real') === '');
  });
"""


@needs_chromium
def test_a_touch_mood_recolours_buddy_and_not_the_status_colours_in_pack_mode(tmp_path):
    assert_all(run_page(tmp_path, ACCENT_PACK_JS, query="demo=threads-pack", budget=8000), 7)


# --- the squeeze of a tap: only on the figure that is there, and gone when it ends --------------------------------------------------

POKE_JS = r"""
  function poking(id) { return $(id).classList.contains('poke'); }
  function running(id) { return $(id).getAnimations().filter(function (a) { return a.animationName === 'poke'; }).length; }
  function end(el, name, kind) { el.dispatchEvent(new AnimationEvent(kind || 'animationend', { animationName: name, bubbles: true })); }
  /* SVG mode: the hidden pack figure is not squeezed behind the scenes */
  ok('SVG mode', !document.body.classList.contains('pack'));
  tap(Z);
  ok('only the figure that is on screen is squeezed', poking('pet-svg') && !poking('gif') && running('pet-svg') === 1);
  /* it ends with its own animation, not with the others that end inside the same figure (the jelly landing, the hearts) */
  end(document.querySelector('#pet .jelly'), 'poke');
  ok('an animation ending deeper inside the figure does not clear it, even if it is also called poke', poking('pet-svg'));
  end($('pet-svg'), 'land');
  ok('another animation of the figure itself does not either', poking('pet-svg'));
  end($('pet-svg'), 'poke');
  ok('its own end clears it, so it cannot replay later', !poking('pet-svg') && running('pet-svg') === 0);
  tap(Z);
  ok('and the next tap squeezes again', poking('pet-svg') && running('pet-svg') === 1);
  end($('pet-svg'), 'poke', 'animationcancel');
  ok('a cancelled squeeze (the figure got hidden in the middle of it) clears it as well', !poking('pet-svg'));

  /* a pack arrives right after a tap: the pack figure does not play a squeeze nobody asked for */
  tap(Z);
  show(function (v) { v.character = { name: 'bufo', rev: '0123456789ab', colors: { bg: '#000000', text: '#ffffff', textDim: '#bdbdbd', body: '#7fd27f' }, states: { idle: ['idle_0.gif'], heart: ['heart.gif'] } }; });
  ok('pack mode now', document.body.classList.contains('pack') && cs($('gif')).display === 'flex');
  ok('the pack figure shows up without a squeeze it was never given', !poking('gif') && running('gif') === 0);

  /* pack mode: now it is the pack figure that is squeezed, and the SVG that is left alone */
  end($('pet-svg'), 'poke');
  tap(Z);
  ok('in pack mode only the GIF is squeezed', poking('gif') && !poking('pet-svg') && running('gif') === 1 && running('pet-svg') === 0);
  show(function (v) { v.character = null; });
  ok('the pack goes away: the SVG figure comes back without a squeeze it never got', !document.body.classList.contains('pack') && !poking('pet-svg') && running('pet-svg') === 0);
"""


@needs_chromium
def test_a_tap_squeezes_only_the_visible_figure_and_the_squeeze_cleans_up_after_itself(tmp_path):
    assert_all(run_page(tmp_path, POKE_JS, query="demo=idle", budget=3000), 11)


# --- the ping: a dead part of the screen answers a tap ----------------------------------------------------------------------
# (A page dumped without a screen never starts its animations: the clock of a CSS animation waits for a frame. So these look at what
# a ping does the moment it is made: where it is, which animation it got and that it is a new one. The 140 ms itself is the CSS.)

PING_JS = r"""
  var ping = $('ping');
  var dead = [[300, 300], [200, 100]];
  var restore = $('stage').style.getPropertyValue('--s');
  var count = document.getElementsByTagName('*').length, kidsOf = $('stage').children.length;
  function centreNow() { var r = ping.getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height / 2]; }
  function cur() { return ping.getAnimations()[0] || null; }
  ok('the ping starts invisible, no touch target and hidden from readers', cs(ping).opacity === '0' && cs(ping).pointerEvents === 'none' && ping.getAttribute('aria-hidden') === 'true'
    && cur() === null);
  var last = null;
  [0.62, 1, 1.3].forEach(function (s, si) {
    dead.forEach(function (d, di) {
      later(si === 0 && di === 0 ? 10 : 100, function () {
        $('stage').style.setProperty('--s', String(s));              // the stage is scaled to fit the real screen: any scale must work
        var p = at(d[0], d[1]), target = tapAt(p[0], p[1]);
        var c = centreNow(), a = cur();
        ok('scale ' + s + ' at (' + d + '): the tap landed on a dead part', !target.closest(LIVE_TOUCH), target.id || target.className || target.tagName);
        ok('scale ' + s + ' at (' + d + '): the ping is centred under the finger', Math.abs(c[0] - p[0]) < 1 && Math.abs(c[1] - p[1]) < 1, JSON.stringify([c, p]));
        ok('scale ' + s + ' at (' + d + '): a new 140 ms animation of transform and opacity only', a !== null && a !== last && a.animationName === 'ping' && a.effect.getTiming().duration === 140
          && a.effect.getKeyframes().every(function (f) { return Object.keys(f).every(function (k) { return ['offset', 'easing', 'composite', 'computedOffset', 'transform', 'opacity'].indexOf(k) >= 0; }); }));
        ok('scale ' + s + ' at (' + d + '): it does not catch the next tap', document.elementFromPoint(p[0], p[1]) === target);
        last = a;
      });
    });
  });
  later(100, function () {
    ok('it is a 25% accent circle of 56 stage pixels', Math.abs(rgba(cs(ping).backgroundColor).a - .25) < .02 && cs(ping).borderRadius === '50%' && cs(ping).width === '56px' && cs(ping).height === '56px', cs(ping).backgroundColor);
    ok('still exactly one ping and no node was added by all those taps', document.querySelectorAll('#ping').length === 1 && document.getElementsByTagName('*').length === count && $('stage').children.length === kidsOf);
    var p2 = at(200, 100);
    tapAt(p2[0], p2[1]);
    var again = cur(), c = centreNow(), p3 = at(300, 300);
    tapAt(p3[0], p3[1]);
    var c3 = centreNow();
    ok('a tap right after another restarts it where the finger is now: a fresh animation each time, the same element', cur() !== again && cur() !== null && Math.abs(c3[0] - p3[0]) < 1 && Math.abs(c3[1] - p3[1]) < 1
      && Math.abs(c[0] - p2[0]) < 1 && document.querySelectorAll('#ping').length === 1);
    $('stage').style.setProperty('--s', restore);
  });
"""


@needs_chromium
def test_the_ping_lands_under_the_finger_at_any_scale_and_is_one_reused_circle(tmp_path):
    assert_all(run_page(tmp_path, PING_JS, budget=4000), 28)


CONTROLS_JS = r"""
  var ping = $('ping');
  function cur() { return ping.getAnimations()[0] || null; }
  function stamp() { return ping.style.getPropertyValue('--px') + '|' + ping.style.getPropertyValue('--py'); }
  var p = at(300, 300), dead = tapAt(p[0], p[1]);
  var mark = stamp(), anim = cur();
  ok('a dead tap makes a ping (the control for the rest)', anim !== null && mark !== '|', mark);
  later(100, function () {
    /* a caption, Buddy, a row, the detail text and Volver answer for themselves: the ping is not made for any of them */
    var cap = captions()[0], c1 = middle(cap);
    tapAt(c1[0], c1[1]);
    ok('a caption is part of the list: no ping', stamp() === mark && cur() === anim);
    var zc = middle(Z);
    var z = tapAt(zc[0], zc[1]);
    ok('Buddy\'s zone: no ping, and Buddy answered', z === Z && stamp() === mark && cur() === anim && mood() === 'heart');
    var rb = row('f1').firstChild, rc = middle(rb);
    var hit = tapAt(rc[0], rc[1]);
    ok('a row is a button: no ping, and it opened the detail', hit.closest('button') === rb && stamp() === mark && cur() === anim && reading());
    var sc = middle($('tv-said'));
    tapAt(sc[0], sc[1]);
    ok('the detail text scrolls: no ping, the detail stays', stamp() === mark && cur() === anim && reading());
    var vc = middle($('tv-back'));
    tapAt(vc[0], vc[1]);
    ok('Volver is a button: no ping (its 350 ms guard is its own business)', stamp() === mark && cur() === anim && reading());
    var nc = middle($('tv-name'));
    tapAt(nc[0], nc[1]);
    ok('the title is plain text: it answers with a ping and never closes the detail', stamp() !== mark && cur() !== anim && cur() !== null && reading());
    mark = stamp(); anim = cur();
  });
  later(400, function () {
    var vc = middle($('tv-back'));
    tapAt(vc[0], vc[1]);
    ok('Volver closes after its guard, with no ping', !reading() && stamp() === mark && cur() === anim);
    var mc = middle($('feed-msg'));
    tapAt(mc[0], mc[1]);
    ok('the headline is plain text: ping', stamp() !== mark && cur() !== anim);
  });
"""


@needs_chromium
def test_the_ping_never_shows_on_a_button_a_row_a_scrolling_text_or_buddy(tmp_path):
    assert_all(run_page(tmp_path, CONTROLS_JS, query="demo=threads", budget=4000), 9)


ASK_PING_JS = r"""
  var ping = $('ping');
  function cur() { return ping.getAnimations()[0] || null; }
  function stamp() { return ping.style.getPropertyValue('--px') + '|' + ping.style.getPropertyValue('--py'); }
  ok('a permission is up and its buttons are asleep', screenOf() === 'ask' && $('btns').className.indexOf('hold') >= 0 && cur() === null);
  /* the buttons and the hint first, while they are asleep, so a tap decides nothing in the demo */
  [$('b-no'), $('b-ok'), $('hint')].forEach(function (el) {
    var c = middle(el);
    tapAt(c[0], c[1]);
    ok((el.id || el.className) + ': no ping', cur() === null && stamp() === '|');
  });
  var z = at(170, 300), tz = tapAt(z[0], z[1]);
  ok('Buddy\'s place is the inert zone: it claims the tap and stays mute', tz === Z && cur() === null && stamp() === '|');
  var p = at(300, 250), t = tapAt(p[0], p[1]);
  ok('a dead part of the ask screen (between the zone and the bubble) answers with a ping, and Buddy stays as it is', !t.closest('#petzone, button, .hint') && cur() !== null && mood() === 'attention' && touchMood === '');
  var first = cur();
  var q = at(170, 100), t2 = tapAt(q[0], q[1]);
  ok('so does the plain text', cur() !== null && cur() !== first && !t2.closest('button'));
"""


@needs_chromium
def test_the_ping_also_answers_on_the_dead_parts_of_a_permission_but_not_on_its_buttons(tmp_path):
    assert_all(run_page(tmp_path, ASK_PING_JS, query="demo=approval", budget=3000), 7)


# --- the pack images follow the rev -----------------------------------------------------------------------------------------

REV_JS = r"""
  holdImages();
  var FILES = { sleep: ['sleep.gif'], idle: ['idle_0.gif', 'idle_1.gif'], heart: ['heart.gif'] };
  var LIST = ['/character/bufo/sleep.gif', '/character/bufo/idle_0.gif', '/character/bufo/idle_1.gif', '/character/bufo/heart.gif'];
  function pack(rev) {
    var c = { name: 'bufo', colors: { bg: '#000000', text: '#ffffff', textDim: '#bdbdbd', body: '#7fd27f' }, states: FILES };
    if (rev !== undefined) c.rev = rev;
    return c;
  }
  function box() { return Array.prototype.slice.call($('gif').children); }
  function urls(list) { return list.map(function (e) { return e.url; }).sort(); }
  function suffixed(rev) { return LIST.map(function (u) { return u + (rev ? '?v=' + rev : ''); }).sort(); }
  function same(a, b) { return a.length === b.length && a.every(function (x, i) { return x === b[i]; }); }
  function showing() { return $('gif').querySelectorAll('img.on'); }

  /* without a rev the URLs are what they always were, and the first set (nothing on show yet) is built at once */
  show(function (v) { v.character = pack(undefined); });
  var first = batch();
  ok('no rev, no query: the URLs are exactly the old ones', same(urls(first), suffixed('')), urls(first).join(' '));
  ok('the first set is on screen at once, one showing', box().length === 4 && packWait === false && showing().length === 1 && gifOn() === 'idle');
  var plain = box();

  /* the daemon starts sending a rev: the new set loads aside and the plain one stays on show until ALL of it has arrived */
  show(function (v) { v.character = pack('0123456789ab'); });
  var one = batch();
  ok('every new URL carries ?v=<rev>', same(urls(one), suffixed('0123456789ab')), urls(one).join(' '));
  ok('meanwhile the old images stay connected and one is showing: no empty frame', packWait === true && box().length === 4 && box().every(function (im, i) { return im === plain[i] && im.isConnected; })
    && showing().length === 1 && plain.indexOf(showing()[0]) >= 0);
  fire(one[0]); fire(one[1]); fire(one[2]);
  ok('three of four have arrived: nothing has moved', packWait === true && box().every(function (im, i) { return im === plain[i]; }) && showing().length === 1);
  show(function (v) { v.mood = 'busy'; v.sessions.msg = 'otra cosa'; });
  ok('a render while they load neither swaps nor loads again', packWait === true && box().every(function (im, i) { return im === plain[i]; }) && held.length === 0);
  fire(one[3], 'error');                                             // a file that is gone settles too: the old set must not hang on for ever
  ok('the last one settled (an error counts): swapped in one go, the old ones discarded', packWait === false && box().length === 4
    && box().every(function (im) { return one.some(function (e) { return e.img === im; }); }) && plain.every(function (im) { return !im.isConnected; }));
  ok('and exactly one is showing again (busy has no GIF in this pack: the idle one)', showing().length === 1 && gifOn() === 'idle');
  show(function (v) { v.mood = 'idle'; });

  /* the same rev loads nothing */
  var current = box();
  show(function (v) { v.sessions.tokens_today += 1; });
  ok('the same rev reloads nothing: the very same elements, no new load', held.length === 0 && box().every(function (im, i) { return im === current[i]; }) && packWait === false);

  /* a rev that changes twice before the first set arrived: the first one is dropped, and arriving late it moves nothing */
  show(function (v) { v.character = pack('111111111111'); });
  var stale = batch();
  show(function (v) { v.character = pack('222222222222'); });
  var latest = batch();
  ok('the second load replaced the first one', same(urls(latest), suffixed('222222222222')) && packWait === true);
  latest.forEach(fire);
  var settled = box();
  ok('only the last set is on screen', same(box().map(function (im) { return latest.filter(function (e) { return e.img === im; })[0].url; }).sort(), suffixed('222222222222')) && packWait === false);
  stale.forEach(fire);
  ok('the first set, arriving late, changes nothing', box().every(function (im, i) { return im === settled[i]; }) && packWait === false && showing().length === 1
    && stale.every(function (e) { return !e.img.isConnected; }));

  /* the pack goes away while a newer set was still loading: nothing comes back from the dead */
  show(function (v) { v.character = pack('333333333333'); });
  var doomed = batch();
  show(function (v) { v.character = null; });
  ok('no pack: the SVG Buddy again, no images, nothing waiting', !document.body.classList.contains('pack') && box().length === 0 && packWait === false);
  doomed.forEach(fire);
  ok('and the late set never arrives', box().length === 0 && !document.body.classList.contains('pack') && packWait === false);
  /* a pack back after that starts clean */
  show(function (v) { v.character = pack('444444444444'); });
  ok('a pack back is built at once, with its rev', box().length === 4 && packWait === false && same(urls(batch()), suffixed('444444444444')));
"""


@needs_chromium
def test_pack_urls_carry_the_rev_and_a_new_rev_swaps_the_images_without_an_empty_frame(tmp_path):
    assert_all(run_page(tmp_path, REV_JS, budget=3000), 15)


WAIT_JS = r"""
  /* While a new set of images loads aside, the old set is what is on screen. A mood change in that wait (a heart that ends, a
     permission that arrives) must still show the right one of the images that are there, with the usual fallback. */
  var ALL = { sleep: ['sleep.gif'], idle: ['idle_0.gif', 'idle_1.gif'], busy: ['busy.gif'], attention: ['attention.gif'], celebrate: ['celebrate.gif'],
              dizzy: ['dizzy.gif'], heart: ['heart.gif'] };
  var ask = { key: 'claude:w1', tool: 'Bash', hint: 'ls', source: 'claude' };
  function pack(states, rev) {
    return { name: 'bufo', rev: rev, colors: { bg: '#0E0D14', text: '#F3EEF7', textDim: '#9A93A4', body: '#FF9E7A' }, states: states };
  }
  function showingNow() { return $('gif').querySelectorAll('img.on').length; }
  holdImages();
  show(function (v) { v.character = pack(ALL, 'aaaaaaaaaaaa'); });
  batch().forEach(fire);
  ok('the first pack is settled and shows idle', packWait === false && gifOn() === 'idle' && showingNow() === 1);

  /* a heart is on when the new set starts to load, and ends while it is still loading */
  tap(Z);
  ok('a heart', gifOn() === 'heart');
  show(function (v) { v.character = pack(ALL, 'bbbbbbbbbbbb'); });
  var next = batch();
  ok('the swap is pending and the old heart image stays on show', packWait === true && gifOn() === 'heart' && showingNow() === 1);
  later(2600, function () {
    ok('the heart ended while the swap was pending: the image follows the server mood again (no stale heart)', packWait === true && mood() === 'idle' && gifOn() === 'idle' && showingNow() === 1, gifOn());
    next.forEach(fire);
    ok('the swap lands on the same image, the old ones discarded', packWait === false && gifOn() === 'idle' && showingNow() === 1);
  });

  /* a permission arrives while a swap is pending: the ask screen shows attention, not the image of the mood before */
  later(10, function () {
    show(function (v) { v.character = pack(ALL, 'cccccccccccc'); });
    next = batch();
    ok('a new swap is pending', packWait === true && gifOn() === 'idle');
    show(function (v) { v.approvals = [ask]; v.mood = 'attention'; });
    ok('the ask screen shows the attention image already', screenOf() === 'ask' && packWait === true && gifOn() === 'attention' && showingNow() === 1, gifOn());
    show(function (v) { v.approvals = []; v.mood = 'busy'; });
    ok('decided: the busy image, still from the old set', screenOf() === 'main' && packWait === true && gifOn() === 'busy' && showingNow() === 1, gifOn());
    next.forEach(fire);
    ok('and the swap keeps it', packWait === false && gifOn() === 'busy' && showingNow() === 1);
  });

  /* the same fallback as without a wait: the mood, else idle, else the first state that is there */
  later(10, function () {
    show(function (v) { v.mood = 'idle'; v.character = pack({ sleep: ['sleep.gif'], idle: ['idle_0.gif'], busy: ['busy.gif'] }, 'dddddddddddd'); });
    batch().forEach(fire);
    show(function (v) { v.character = pack({ sleep: ['sleep.gif'], idle: ['idle_0.gif'], busy: ['busy.gif'] }, 'eeeeeeeeeeee'); });
    next = batch();
    ok('a pack without attention, swap pending', packWait === true && gifOn() === 'idle');
    show(function (v) { v.approvals = [ask]; v.mood = 'attention'; });
    ok('the old set has no attention image: idle, as the code always did', packWait === true && gifOn() === 'idle' && showingNow() === 1);
    show(function (v) { v.approvals = []; v.mood = 'busy'; });
    ok('busy is there', gifOn() === 'busy');
    next.forEach(fire);
  });
  later(10, function () {
    show(function (v) { v.mood = 'busy'; v.character = pack({ sleep: ['sleep.gif'], busy: ['busy.gif'] }, 'ffffffffffff'); });
    batch().forEach(fire);
    show(function (v) { v.character = pack({ sleep: ['sleep.gif'], busy: ['busy.gif'] }, 'gggggggggggg'); });
    next = batch();
    show(function (v) { v.mood = 'celebrate'; });
    ok('no celebrate and no idle in the old set: the first state that is there', packWait === true && gifOn() === 'sleep' && showingNow() === 1, gifOn());
    next.forEach(fire);
    ok('and the swap agrees', packWait === false && gifOn() === 'sleep' && showingNow() === 1);
  });
"""


@needs_chromium
def test_a_mood_change_while_a_pack_swap_is_pending_shows_the_right_image_of_the_old_set(tmp_path):
    assert_all(run_page(tmp_path, WAIT_JS, query="demo=idle-pack", budget=4500), 14)


HOSTILE_JS = r"""
  var evil = '<img src=x onerror="window.pwned=1"><b>x</b>';
  var weird = { name: evil, rev: evil + '"\' ?&#', colors: { bg: '#000000', text: '#ffffff', textDim: '#bdbdbd', body: '#7fd27f' }, states: { idle: [evil], heart: [evil + '2'], constructor: ['c.gif'] } };
  show(function (v) {
    v.pet.name = evil; v.pet.owner = evil; v.ble.name = evil; v.sessions.msg = evil; v.sessions.entries = [evil];
    v.tasks = [T('e1', { title: evil, group: evil, project: evil, tool: evil, what: evil, said: evil, state: 'idle' })];
    v.character = weird;
  });
  tap(Z);
  ok('Buddy plays with hostile text on the screen', mood() === 'heart');
  var imgs = Array.prototype.slice.call(document.querySelectorAll('img'));
  ok('only the pack images exist and no markup was made of any text', imgs.length === 3 && imgs.every(function (im) { return $('gif').contains(im); }) && window.pwned === undefined
    && $('pet-name').children.length === 0 && $('mood').children.length === 0 && $('feed-msg').children.length === 0);
  ok('the rev and the names are encoded in the URLs', imgs.every(function (im) { return /^\/character\/[^<>" ]+\/[^<>" ]+\?v=[^<>" &#]+$/.test(im.getAttribute('src')); }), imgs.map(function (im) { return im.getAttribute('src'); }).join(' '));
  ok('the name is literal text', $('pet-name').textContent === evil && $('pet-owner').textContent === 'de ' + evil);
"""


@needs_chromium
def test_hostile_data_stays_text_while_buddy_is_touched_and_the_rev_is_encoded(tmp_path):
    assert_all(run_page(tmp_path, HOSTILE_JS, budget=3000), 4)


ODD_REV_JS = r"""
  var results = [];
  [12345, { a: 1 }, ['x'], true, null, ''].forEach(function (odd) {
    show(function (v) { v.character = null; });
    show(function (v) { v.character = { name: 'bufo', rev: odd, colors: {}, states: { idle: ['a.gif'] } }; });
    results.push(JSON.stringify(odd) + ' -> ' + $('gif').firstChild.getAttribute('src'));
  });
  ok('a rev that is not a non-empty text adds no query', results.every(function (r) { return /-> \/character\/bufo\/a\.gif$/.test(r); }), results.join(' | '));
"""


@needs_chromium
def test_a_rev_that_is_not_a_text_is_not_used(tmp_path):
    assert_all(run_page(tmp_path, ODD_REV_JS, budget=2000), 1)


# --- every demo still renders, and every one can be touched ----------------------------------------------------------------------

DEMOS_JS = r"""
  var expect = __SCREENS__, seen = 0;
  Object.keys(expect).forEach(function (name) {
    ok(name + ' is still a demo', Object.prototype.hasOwnProperty.call(DEMOS, name), name);
  });
  demoNames.forEach(function (name) {
    try {
      demoShow(name);
      var want = expect[name], screen = screenOf(), v = DEMOS[name];
      if (want !== undefined) ok(name + ': the same screen as before', screen === want, screen + ' vs ' + want);
      ok(name + ': renders', !!screen, screen);
      var before = truth();
      tap(Z);
      var plays = screen !== 'ask' && screen !== 'boot' && !(v && v.mood === 'attention');
      ok(name + ': a tap ' + (plays ? 'changes Buddy' : 'changes nothing'), plays ? touchMood !== '' : touchMood === '', touchMood);
      ok(name + ': and the status is the same after it', screen === 'boot' || truth() === before);
      clearTouch();
      seen += 1;
    } catch (e) { ok(name + ' threw: ' + e.message, false); }
  });
  ok('all the demos were seen', seen === demoNames.length && seen >= 30, seen);
"""


@needs_chromium
def test_every_demo_still_renders_its_screen_and_can_be_touched(tmp_path):
    body = DEMOS_JS.replace("__SCREENS__", json.dumps(DEMO_SCREENS))
    assert_all(run_page(tmp_path, body, budget=4000), 151)


# --- the pack on a real server: the daemon's own rev in the URLs --------------------------------------------------


def _install_pack(rig, name="bufo"):
    """A pack on disk the way the core leaves it, with real (tiny) GIFs."""
    folder = rig.core.state_dir / "characters" / name
    folder.mkdir(parents=True)
    manifest = {"name": name, "colors": {"body": "#6B8E23", "bg": "#000000", "text": "#FFFFFF", "textDim": "#808080"},
                "states": {"sleep": "sleep.gif", "idle": ["idle_0.gif", "idle_1.gif"]}}
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    for fname in ("sleep.gif", "idle_0.gif", "idle_1.gif"):
        (folder / fname).write_bytes(GIF)
    rig.core.state.character = name
    return folder


@needs_chromium
def test_the_page_loads_the_images_under_the_rev_the_daemon_sent(rig):
    async def scenario():
        _install_pack(rig)
        rig.rx(heartbeat())
        async with serving(rig) as server:
            return await asyncio.get_running_loop().run_in_executor(None, dump, server.url + "/?poll=300")

    dom = run(scenario)
    rev = rig.view()["character"]["rev"]
    assert re.fullmatch(r"[0-9a-f]{12}", rev)
    assert attr(dom, "data-screen") == "main"
    for fname in ("sleep.gif", "idle_0.gif", "idle_1.gif"):
        assert f'src="/character/bufo/{fname}?v={rev}"' in dom, fname
    assert re.search(r'<img[^>]*class="on"[^>]*data-state="idle"', dom) or re.search(r'<img[^>]*data-state="idle"[^>]*class="on"', dom)
