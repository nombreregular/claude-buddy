"""Night mode on the page: the dim layer that catches the waking tap, the two ways the screen goes dark (the panel backlight does it, or
the page darkens itself), the 30 s of local wake and the 350 ms guard that follows it, and the one rule above all: a permission is
never dim and never covered.

The browser tests run the page for real in a headless Chromium (they skip when there is none), with the same battery as
test_page_tasks.py: the self-test is injected into a temporary COPY of ui/index.html, inside the page's own script, and the page runs
on virtual time, so 30 s of local wake is seen in a fraction of a second. The page keeps its night deadlines on performance.now(); the
test owns that clock and moves it by timer (nowMs()), so a deadline and the timer that fires at it always agree on every Chromium.
Nothing is ever written to the repo."""
import asyncio
import inspect
import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from buddy_ui import UIHub  # noqa: E402
from page_runner import attr, dump, needs_chromium  # noqa: E402
from test_page_tasks import OLD_DEMOS, _css, _page, _rule, assert_all, battery  # noqa: E402
from test_ui import PAGE, Rig, heartbeat, run, serving  # noqa: E402

NIGHT_DEMOS = ("night", "night-overlay", "night-ask")
DEMO_SCREENS = {**OLD_DEMOS, "runs": "main", "runs1": "main", "runs-pack": "main", "night": "main", "night-overlay": "main", "night-ask": "ask"}


# --- what every browser test below starts from -------------------------------------------------------------------------

HELPERS = r"""
  /* One clock for the page's deadlines and its timers, owned by the test: a timer moves it to its due time just before it runs, so a
     deadline and the timer that fires at it always agree. (Date.now() is not used: the Pi's Chromium does not move it with virtual time.) */
  var clock = 1000;
  performance.now = function () { return clock; };
  function nowMs() { return clock; }
  var D = $('dim'), Z = $('petzone');
  function screenOf() { return document.body.getAttribute('data-screen'); }
  function mood() { return document.body.getAttribute('data-mood'); }
  function on() { return D.classList.contains('on'); }
  function dark() { return D.classList.contains('dark'); }
  function pe() { return cs(D).pointerEvents; }
  function op() { return Number(cs(D).opacity); }
  function middle(el) { var r = el.getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height / 2]; }
  function at(X, Y) { var sr = $('stage').getBoundingClientRect(), k = sr.width / 800; return [sr.left + X * k, sr.top + Y * k]; }
  function lookAt(el) { var c = middle(el); return document.elementFromPoint(c[0], c[1]); }
  /* a click the way a finger makes one: on whatever is really on top at that point, with its coordinates */
  function tapAt(x, y) {
    var t = document.elementFromPoint(x, y);
    t.dispatchEvent(new MouseEvent('click', { bubbles: true, clientX: x, clientY: y }));
    return t;
  }
  function tapOn(el) { var c = middle(el); return tapAt(c[0], c[1]); }
  function pingMark() { return $('ping').style.getPropertyValue('--px') + '|' + $('ping').style.getPropertyValue('--py'); }
  /* everything on the screen that is the server's word */
  function truth() {
    return JSON.stringify(['mood', 'feed-msg', 'n-total', 'n-running', 'n-waiting', 'f-tokens', 'f-ok', 'f-no', 'pet-level', 'pet-name', 'pet-owner']
      .map(function (id) { return $(id).textContent; }));
  }
  /* the night timer: told apart from every other timer by what it runs */
  var live = {}, realSet = window.setTimeout, realClear = window.clearTimeout;
  window.setTimeout = function (fn, ms) {
    var due = clock + (Number(ms) || 0);
    var id = realSet(function () { clock = Math.max(clock, due); delete live[id]; return fn.apply(this, arguments); }, ms);
    if (fn === paintDim) live[id] = true;
    return id;
  };
  window.clearTimeout = function (id) { delete live[id]; return realClear(id); };
  function liveDim() { return Object.keys(live).length; }
  /* the network is the test's: every call is remembered, and what it answers is chosen */
  var calls = [], fetchMode = 'ok', unhandled = 0;
  window.addEventListener('unhandledrejection', function () { unhandled += 1; });
  window.fetch = function (url, opt) {
    calls.push({ url: String(url), method: opt && opt.method, headers: opt && opt.headers, body: opt && opt.body });
    if (fetchMode === 'throw') throw new Error('no fetch');
    if (fetchMode === 'reject') return Promise.reject(new Error('down'));
    if (fetchMode === 'forbidden') return Promise.resolve({ ok: false, status: 403, json: function () { return Promise.resolve({}); } });
    return Promise.resolve({ ok: true, status: 200, json: function () { return Promise.resolve({ ok: true, dim: false }); } });
  };
  function wakes() { return calls.filter(function (c) { return c.url === '/wake'; }); }
  function decides() { return calls.filter(function (c) { return c.url === '/decide'; }); }
  /* the page with no daemon: its stream closed, the views put by hand */
  function goLive() { if (es) { es.close(); es.onerror = null; } setOffline(false); }
  function put(name, mut) { var v = clone(DEMOS[name]); if (mut) mut(v); nightReset(); view = v; render(); return v; }
  function redark() { nightReset(); render(); }
  var ASK = { key: 'claude:t1', tool: 'Bash', hint: 'ls', source: 'claude' };
"""


def run_page(tmp_path, body, query="demo=night", budget=3000):
    return battery(tmp_path, HELPERS + body, query=query, budget=budget)


# --- static: what the page's own source promises --------------------------------------------------------------------


def _night_block():
    page = _page()
    start = page.index("/* ---------------- modo noche")
    return page[start:page.index("/* ---------------- vista previa sin buddy", start)]


def _night_code():
    """The night block without its comments (prose may name the events it does not use)."""
    return re.sub(r"//[^\n]*", "", re.sub(r"/\*.*?\*/", "", _night_block(), flags=re.S))


def test_the_night_numbers_are_what_the_contract_says():
    block = _night_block()
    assert "var NIGHT_AWAKE_MS = 30000;" in block  # the daemon counts the same 30 s (AWAKE_S)
    assert "var NIGHT_GUARD_MS = 350;" in block
    page = _page()
    assert "var ARM_MS = 600;" in page  # the approve buttons' arming and Volver's guard are somebody else's numbers
    assert "var TAP_GUARD_MS = 350;" in page


def test_the_dim_layer_is_one_inert_element_on_top_of_the_stage():
    css, page = _css(), _page()
    base = _rule(css, "#dim")
    for declaration in ("position:absolute", "left:0", "top:0", "right:0", "bottom:0", "background:#000", "opacity:0", "pointer-events:none",
                        "transition:opacity .3s"):
        assert declaration in base, declaration
    top = int(re.search(r"z-index:(\d+)", base).group(1))
    others = [int(n) for n in re.findall(r"z-index:(\d+)", css.replace(base, ""))]
    assert others and top > max(others)  # above the toast, the offline notice, the flash and the demo tag
    assert page.count('id="dim"') == 1 and '<div id="dim" aria-hidden="true"></div>' in page
    stage = page[page.index('<div id="stage">'):page.index("<script>")]
    assert stage.index('<div id="ping"') < stage.index('<div id="dim"') < stage.index('<template id="row-proto">')  # inside the stage, after the rest


def test_the_two_ways_of_going_dark_and_the_permission_override_are_in_the_css():
    css = _css()
    assert _rule(css, "#dim.on").strip() == "pointer-events:auto"  # catching taps is the only thing "on" does
    dark = _rule(css, "#dim.dark")
    assert "opacity:.78" in dark and "transition:opacity .6s" in dark  # black at 78%, darkening in 600 ms
    ask = _rule(css, "body[data-screen=ask] #dim")
    assert "opacity:0" in ask and "pointer-events:none" in ask and "transition:opacity 0s" in ask  # at once, whatever the classes say
    for selector in ("#dim", "#dim.on", "#dim.dark", "body[data-screen=ask] #dim"):
        for found in re.finditer(r"transition:([^;}]*)", _rule(css, selector)):
            assert set(re.findall(r"(?:^|,)\s*([a-z-]+)", found.group(1))) == {"opacity"}, selector  # the Pi 4 only gets opacity


def test_the_night_code_has_one_timer_one_post_no_markup_and_stays_out_of_the_arming_and_the_touch_code():
    block = _night_code()
    for name in ("holdUntil", "ARM_MS", "decide", "paintAsk", "armTimer", "sending", "curKey", "openTask", "closeTask", "backTask", "clearTouch",
                 "touchMood", "touchBuddy", "petzone", "location"):
        assert not re.search(rf"\b{name}\b", block), f"the night code touches {name}"
    assert not re.search(r"\becho\b", block)  # the waking tap leaves no ping
    for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "textContent", "innerText", "createElement",
                "localStorage", "sessionStorage", "indexedDB", "document.cookie", "XMLHttpRequest", "sendBeacon", "EventSource"):
        assert bad not in block, f"the night code uses {bad}"
    assert block.count("setTimeout(") == 1  # one timer
    assert re.search(r"clearTimeout\(nightTimer\);\n\s+nightTimer = 0;", block)  # and it is always cleared before the next is armed
    assert block.count("fetch(") == 1 and "fetch('/wake', { method: 'POST'" in block
    assert re.search(r"function sendWake\(\) \{\n\s+if \(DEMO\) return;", block)  # the preview talks to nobody
    assert "'X-Buddy-Token': TOKEN" in block and ".catch(function () {" in block  # same token as /decide; a failure is silent


def test_the_waking_tap_is_a_click_on_the_dim_layer_and_nothing_else():
    block = _night_code()
    assert "$('dim').addEventListener('click', function (e) {" in block
    assert re.search(r"function \(e\) \{\n\s+e\.stopPropagation\(\);", block)  # the stage's ping never sees it
    assert len(re.findall(r"addEventListener\(", block)) == 3  # the click, a finger resting on the layer (below), and the in-use notice
    for early in ("mousedown", "touchstart", "pointerup", "pointercancel", "mouseup", "touchend"):
        assert early not in block, early  # a click, so the same gesture cannot also reach what is underneath
    assert "view.dim === true" in block and "view.backlight === true" in block  # booleans only: anything else is not "dim"
    # the one early event there is only makes the guard last: it cannot wake the screen, tell the daemon or touch the arming
    press = re.search(r"\$\('dim'\)\.addEventListener\('pointerdown', function \(\) \{(.*?)\n  \}\);", block, re.S).group(1)
    for wakes in ("sendWake", "nightAwakeUntil", "fetch", "stopPropagation", "classList"):
        assert wakes not in press, wakes
    assert block.count("pointerdown") == 2 and "var NIGHT_PRESS_MS = 600;" in block  # the second is the in-use notice (see test_a_touch_on_the_awake_night_screen...)


def test_render_repaints_the_dim_layer_on_every_view_and_when_there_is_none():
    page = _page()
    assert re.search(r"setAttr\(b, 'data-real', 'sleep'\); paintDim\(\); return; \}", page)
    assert re.search(r"Object\.keys\(sending\)\.forEach\(function \(k\) \{ if \(!live\[k\]\) delete sending\[k\]; \}\);\n\s+paintDim\(\);\n  \}", page)
    assert "nightReset(); rank.clear();" in page  # every preview starts without the last one's wake


def test_the_demos_carry_the_daemons_two_flags():
    page = _page()
    assert "mood: 'idle', night: false, dim: false, backlight: false," in page  # the base of every demo, like the daemon's view
    assert re.search(r"night: mk\(\{ night: true, dim: true, backlight: true,", page)
    assert re.search(r"'night-overlay': mk\(\{ night: true, dim: true, backlight: false,", page)
    assert re.search(r"'night-ask': mk\(\{ night: true, dim: true, backlight: false,.*?approvals: \[", page, re.S)


def test_this_file_has_no_em_dash():
    assert chr(0x2014) not in Path(__file__).read_text(encoding="utf-8")


# --- the layer: inert by day, over everything at night, in both modes ----------------------------------------------------

LAYER_JS = r"""
  var NIGHT = ['night', 'night-overlay'], seen = 0;
  var five = [at(2, 2), at(797, 2), at(2, 477), at(797, 477), at(400, 240)];
  demoNames.forEach(function (name) {
    demoShow(name);
    var v = DEMOS[name], night = NIGHT.indexOf(name) >= 0;
    if (v) ok(name + ': the view always says dim and backlight, as booleans', typeof v.dim === 'boolean' && typeof v.backlight === 'boolean');
    if (name === 'boot') { ok('boot: inert', !on() && !dark() && pe() === 'none' && op() === 0); return; }
    if (night) {
      var loose = [Z, document.querySelector('#s-main .col'), $('plate'), $('clock'), $('feed-list'), $('mood')].filter(function (el) { return lookAt(el) !== D; });
      ok(name + ': it catches taps over Buddy, the column, the plate, the clock and the list', on() && pe() === 'auto' && loose.length === 0,
        loose.map(function (el) { return el.id || el.className; }).join(','));
      ok(name + ': the four corners and the middle of the stage are its', five.every(function (p) { return document.elementFromPoint(p[0], p[1]) === D; }));
    } else {
      ok(name + ': inert (no class, no taps, see-through)', !on() && !dark() && pe() === 'none' && op() === 0, D.className + ' ' + pe() + ' ' + op());
      ok(name + ': nothing the page shows is ever under it', five.every(function (p) { return document.elementFromPoint(p[0], p[1]) !== D; }));
    }
    seen += 1;
  });
  ok('all the demos were seen', seen === demoNames.length - 1 && seen >= 32, seen);
  var r = px(D), g = px($('stage'));
  ok('it is the whole stage, 800 x 480', Math.abs(r.x - g.x) < .6 && Math.abs(r.y - g.y) < .6 && Math.abs(r.w - 800) < .6 && Math.abs(r.h - 480) < .6, JSON.stringify([r, g]));
  var reach = Array.prototype.slice.call($('stage').querySelectorAll('*')).filter(function (el) { return el !== D && (Number(cs(el).zIndex) || 0) >= Number(cs(D).zIndex); });
  ok('it is a child of the stage and no other element reaches its z-index', D.parentNode === $('stage') && Number(cs(D).zIndex) > 0 && reach.length === 0,
    reach.map(function (el) { return el.id || el.className; }).join(','));
  /* the two ways of going dark (the headless page draws no frames, so a transition is read as the animation it started and finished by hand) */
  demoShow('night-ask');
  demoShow('night-overlay');
  var fade = D.getAnimations();
  ok('overlay: it darkens itself with one opacity transition of 600 ms to 78%', dark() && fade.length === 1 && fade[0].transitionProperty === 'opacity'
    && fade[0].effect.getTiming().duration === 600 && fade[0].effect.getKeyframes().pop().opacity === '0.78', fade.length);
  fade.forEach(function (a) { a.finish(); });
  ok('overlay: black at 78% once it settles, and still catching taps', Math.abs(op() - .78) < .01 && pe() === 'auto' && on());
  demoShow('night-ask');
  demoShow('night');
  ok('backlight: #dim stays see-through, the panel does the dimming (no double darkening)', on() && !dark() && op() === 0 && pe() === 'auto' && D.getAnimations().length === 0);
"""


@needs_chromium
def test_the_dim_layer_is_inert_by_day_covers_everything_at_night_and_darkens_only_without_a_backlight(tmp_path):
    assert_all(run_page(tmp_path, LAYER_JS, query="demo=idle", budget=4000), 80)


# --- the waking tap: consumed, and the daemon is told once --------------------------------------------------------------

WAKING_JS = r"""
  goLive();
  var token = TOKEN;
  ok('the page has the token it uses for /decide', typeof token === 'string' && token.length > 0);
  put('threads', function (v) { v.dim = true; v.backlight = true; v.mood = 'idle'; });
  var targets = [['Buddy', function () { return middle(Z); }], ['a thread row', function () { return middle(row('f1')); }], ['the mood pill', function () { return middle($('mood')); }],
    ['the clock', function () { return middle($('clock')); }], ['a dead part of the screen', function () { return at(300, 300); }]];
  var base = truth();
  targets.forEach(function (t) {
    redark();
    var before = wakes().length, p = t[1]();
    ok(t[0] + ': the screen is dark first', on() && pe() === 'auto');
    var hit = tapAt(p[0], p[1]);
    ok(t[0] + ': the tap lands on #dim', hit === D);
    ok(t[0] + ': nothing underneath reacted (no row, no heart, no squeeze, no ping, nothing sent to decide)', !reading() && touchMood === '' && touchTimer === 0
      && pingMark() === '|' && !$('ping').classList.contains('on') && !$('pet-svg').classList.contains('poke') && truth() === base
      && Object.keys(sending).length === 0 && decides().length === 0 && !$('flash').classList.contains('on'));
    ok(t[0] + ': it asked the daemon once', wakes().length === before + 1);
  });
  var w = wakes()[0];
  ok('the POST is /wake with the same token and JSON headers as /decide and an empty object', w.method === 'POST' && w.headers['X-Buddy-Token'] === token
    && w.headers['Content-Type'] === 'application/json' && w.body === '{}', JSON.stringify(w));
  /* Volver: with a detail open, the waking tap on Volver does not close it (and it is a live button once awake) */
  redark();
  openTask('f1');
  step(400, function () {
    ok('a detail is open', reading());
    redark();
    var n = wakes().length, v = tapOn($('tv-back'));
    ok('the waking tap on Volver lands on #dim and the detail stays open', v === D && reading() && wakes().length === n + 1);
    var again = tapOn($('tv-back'));
    ok('a second tap on Volver in the 350 ms after is swallowed too', again === D && reading() && wakes().length === n + 1);
  });
  step(500, function () {
    ok('350 ms later #dim is inert', !on() && pe() === 'none');
    var live = tapOn($('tv-back'));
    ok('and Volver is a live button again: it closes the detail', live !== D && !!live.closest('#tv-back') && !reading());
  });
  /* the daemon is not there, or says no: nothing breaks, nothing is shown, the screen still wakes (locally) */
  ['reject', 'throw', 'forbidden'].forEach(function (mode) {
    step(100, function () {
      fetchMode = mode;
      redark();
      var n = wakes().length;
      tapOn(Z);
      ok(mode + ': the POST was tried once and the screen is awake anyway', wakes().length === n + 1 && nightAwakeUntil > performance.now() + 29000 && !dark());
    });
    step(100, function () { ok(mode + ': nothing was said about it (no toast, no unhandled rejection)', unhandled === 0 && $('toast').className === ''); });
  });
  step(100, function () { fetchMode = 'ok'; });
"""


@needs_chromium
def test_the_waking_tap_is_consumed_by_the_dim_layer_and_asks_the_daemon_once(tmp_path):
    assert_all(run_page(tmp_path, WAKING_JS, query="live=1", budget=6000), 30)


CLICK_ONLY_JS = r"""
  goLive();
  put('threads', function (v) { v.dim = true; v.backlight = false; });
  ['pointerdown', 'pointerup', 'mousedown', 'mouseup', 'touchstart', 'touchend', 'dblclick', 'contextmenu'].forEach(function (kind) {
    var e = new Event(kind, { bubbles: true, cancelable: true });
    D.dispatchEvent(e);
  });
  ok('only a click wakes it: the other events of a gesture do nothing', dark() && on() && wakes().length === 0);
  tapOn(D);
  ok('a click does', !dark() && wakes().length === 1);
"""


@needs_chromium
def test_only_the_click_of_a_tap_wakes_the_screen_never_its_first_events(tmp_path):
    assert_all(run_page(tmp_path, CLICK_ONLY_JS, query="live=1", budget=2000), 2)


STRADDLE_JS = r"""
  /* A second press that starts inside the guard and lifts after it ended: Chromium gives the click to whatever is under the finger when it
     lifts, so #dim has to stay on until that press is over (the page cannot see the lift, so it holds on for a whole tap from where it began). */
  goLive();
  put('threads', function (v) { v.dim = true; v.backlight = true; });
  var t0 = 0;
  function ms() { return nowMs() - t0; }
  function press() { D.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, pointerType: 'touch' })); }
  step(10, function () { redark(); t0 = nowMs(); tapOn(Z); ok('woken: the guard is on', on() && pe() === 'auto' && !dark()); });
  step(290, function () {
    ok('300 ms in, a finger rests on #dim, inside the guard', ms() >= 280 && ms() < 340 && on(), ms());
    press();
    ok('still one timer', liveDim() === 1);
  });
  step(160, function () {
    ok('460 ms in, 110 ms past the plain guard: #dim still catches taps (the press began inside it)', ms() > 440 && on() && pe() === 'auto' && lookAt(row('f1')) === D);
    ok('and a click on a row there lands on #dim, not on the row', tapOn(row('f1')) === D && !reading() && touchMood === '');
    ok('and nothing more was sent to the daemon', wakes().length === 1);
  });
  step(1200, function () {   // (slack for a slow Pi, see LOCAL_JS)
    ok('well past 600 ms after the press began: #dim is inert again', ms() > 930 && !on() && pe() === 'none', ms());
    ok('and the row is live', tapOn(row('f1')) !== D && reading());
    closeTask();
    ok('no timer is left but the one for the end of the local wake', liveDim() === 1);
  });
  /* only a press that begins inside the first guard lengthens it: being impatient does not keep the screen blocked */
  step(100, function () { redark(); t0 = nowMs(); tapOn(Z); });
  step(100, function () { press(); ok('a press 100 ms in', on()); });
  step(300, function () { press(); ok('a second press at 400 ms, past the plain guard, is swallowed too but adds nothing', ms() > 380 && on()); });
  step(1000, function () { ok('well past 700 ms: only the first press counted, so #dim is inert again', ms() > 730 && !on() && pe() === 'none', ms()); });
  step(100, function () {
    press();
    ok('a press on an inert #dim (no real finger can) lengthens nothing', !on() && pe() === 'none');
    ok('the daemon was asked twice in all, once per waking tap', wakes().length === 2, wakes().length);
  });
  /* a press never wakes: it is the click that does, and it only counts when the screen is dark */
  step(100, function () {
    redark();
    var n = wakes().length;
    press();
    ok('a finger on the dark screen wakes nothing and asks for nothing', dimNow(performance.now()) && on() && wakes().length === n && nightAwakeUntil === 0);
  });
"""


@needs_chromium
def test_a_press_that_starts_inside_the_guard_and_lifts_after_it_is_still_swallowed(tmp_path):
    assert_all(run_page(tmp_path, STRADDLE_JS, query="live=1", budget=9000), 13)


# --- the local wake: 30 s, the 350 ms guard, and the one timer ----------------------------------------------------------------

LOCAL_JS = r"""
  /* no daemon in the preview: the tap wakes the page by itself and nothing is sent anywhere */
  var t0 = 0, mode = MODE;
  function ms() { return nowMs() - t0; }
  step(10, function () {
    ok('it starts dark', on() && pe() === 'auto' && dark() === (mode === 'overlay'));
    ok('and the dim timer is not needed yet', liveDim() === 0);
    D.getAnimations().forEach(function (a) { a.finish(); });   // (no frames are drawn here: the darkening that began at load is settled by hand)
    t0 = nowMs();
    var hit = tapOn(Z);
    ok('the waking tap lands on #dim', hit === D);
    var fade = D.getAnimations();
    ok('it stops darkening at once (an opacity fade of 300 ms, none in backlight mode) but keeps catching taps', !dark() && on() && pe() === 'auto' && cs(D).transitionDuration === '0.3s'
      && (mode === 'overlay' ? fade.length === 1 && fade[0].transitionProperty === 'opacity' && fade[0].effect.getTiming().duration === 300 : fade.length === 0), fade.length);
    ok('the daemon was not called from a preview', calls.length === 0, calls.length);
    ok('one timer, for the end of the guard', liveDim() === 1);
    ok('nothing underneath reacted', touchMood === '' && !reading() && pingMark() === '|');
  });
  step(250, function () {
    ok('250 ms in, a finger over a row still finds #dim', ms() >= 240 && ms() < 340 && lookAt(row('b1')) === D);
    var hit = tapOn(row('b1'));
    ok('a second tap in the guard lands on #dim and opens nothing', hit === D && !reading() && touchMood === '');
    ok('still one timer', liveDim() === 1);
  });
  step(900, function () {   // (slack: on the Pi a heavy step delays the page's timers in virtual time, so "over" is asserted well after 350 ms)
    D.getAnimations().forEach(function (a) { a.finish(); });
    ok('well past 350 ms, the guard is over: #dim is inert and see-through', ms() > 400 && !on() && pe() === 'none' && !dark() && op() === 0);
    var hit = tapOn(row('b1'));
    ok('the next tap reaches the row', hit !== D && reading());
    closeTask();
    ok('one timer: the end of the local wake', liveDim() === 1);
  });
  step(27500, function () {
    D.getAnimations().forEach(function (a) { a.finish(); });
    ok('about 28.7 s in, the screen is still awake', ms() > 28000 && ms() < 30000 && !on() && !dark() && pe() === 'none');
    ok('and still one timer', liveDim() === 1);
  });
  step(2800, function () {
    ok('past 30 s the server still says night, so it is dark again', ms() > 30060 && on() && pe() === 'auto' && dark() === (mode === 'overlay'), ms());
    var back = D.getAnimations();
    ok('the darkening takes its 600 ms when it comes back', mode !== 'overlay' || (back.length === 1 && back[0].effect.getTiming().duration === 600), back.length);
    ok('and no timer is left', liveDim() === 0);
  });
"""


@needs_chromium
@pytest.mark.parametrize("demo,mode", [("night-overlay", "overlay"), ("night", "backlight")])
def test_the_local_wake_lasts_30_s_the_guard_350_ms_and_one_timer_runs_it(tmp_path, demo, mode):
    assert_all(run_page(tmp_path, LOCAL_JS.replace("MODE", json.dumps(mode)), query=f"demo={demo}", budget=40000), 14)


# --- the daemon is the boss: its flag wakes, darkens, and takes the local wake away ---------------------------------------------

SERVER_JS = r"""
  goLive();
  put('threads', function (v) { v.dim = true; v.backlight = false; });
  var t0 = 0;
  function ms() { return nowMs() - t0; }
  step(10, function () {
    ok('dark', dark() && on());
    show(function (v) { v.dim = false; });
    ok('dim:false from the daemon wakes it at once, with nothing local', !dark() && nightAwakeUntil === 0 && cs(D).transitionDuration === '0.3s');
    ok('and #dim catches taps for the 350 ms after, over a row too', on() && lookAt(row('f1')) === D && tapOn(row('f1')) === D && !reading());
    ok('one timer, for that guard', liveDim() === 1);
  });
  step(1000, function () {
    ok('then it is inert and the page is awake with no timer at all', !on() && !dark() && pe() === 'none' && liveDim() === 0, JSON.stringify([on(), dark(), pe(), liveDim(), clock, nightGuardUntil, nightAwakeUntil, view && view.dim]));
    ok('a row is live', tapOn(row('f1')) !== D && reading());
    closeTask();
    show(function (v) { v.dim = true; });
    ok('dim:true again darkens at once (nothing local is left)', dark() && on() && pe() === 'auto');
  });
  /* the daemon confirming a tap takes the local wake with it: no leftover seconds */
  step(100, function () {
    t0 = nowMs();
    tapOn(Z);
    ok('a tap wakes it locally for 30 s', !dark() && nightAwakeUntil > performance.now() + 29000);
    show(function (v) { v.dim = false; });
    ok('the daemon says awake: the local wake is gone, the daemon owns it', nightAwakeUntil === 0 && !dark());
  });
  step(5000, function () {
    show(function (v) { v.dim = true; });
    ok('and when the daemon says dim again, it is dark at once, 5 s into the old local wake', dark() && on());
  });
  /* a tap whose POST is lost: the daemon keeps saying dim; the local wake holds for the full 30 s, whatever the heartbeats say */
  step(100, function () {
    fetchMode = 'reject';
    tapOn(Z);
    t0 = nowMs();
    ok('awake locally again', !dark());
  });
  [1000, 9000, 18000].forEach(function (gap) {
    step(gap, function () {
      show(function (v) { v.dim = true; v.sessions.msg = 'm' + gap; });
      ok('a heartbeat at ' + Math.round(ms() / 1000) + ' s still says dim and the screen stays awake', !dark() && !on() && pe() === 'none', ms());
      ok('with one timer for the end of the wake', liveDim() === 1);
    });
  });
  step(1700, function () { ok('about 29.8 s in: awake', ms() > 29500 && ms() < 30000 && !dark() && !on()); });
  step(2500, function () {
    ok('past 30 s: dark again, from the timer alone', ms() > 30100 && dark() && on() && pe() === 'auto', ms());
    ok('nothing left running', liveDim() === 0);
    ok('the wake was asked for once per tap, never repeated by heartbeats', wakes().length === 2, wakes().length);
  });
"""


@needs_chromium
def test_the_daemons_dim_flag_wakes_and_darkens_and_a_lost_wake_still_holds_30_s(tmp_path):
    assert_all(run_page(tmp_path, SERVER_JS, query="live=1", budget=60000), 21)


TIMERS_JS = r"""
  goLive();
  var most = 0;
  function note() { most = Math.max(most, liveDim()); }
  put('threads', function (v) { v.dim = true; v.backlight = false; });
  /* every kind of change in a row, with a heartbeat after each: there is never more than one timer */
  for (var i = 0; i < 6; i++) {
    tapOn(Z); note();
    show(function (v) { v.sessions.msg = 'beat'; }); note();
    show(function (v) { v.dim = false; }); note();
    show(function (v) { v.dim = true; }); note();
    show(function (v) { v.approvals = [ASK]; v.mood = 'attention'; }); note();
    show(function (v) { v.approvals = []; v.mood = 'idle'; }); note();
  }
  ok('one dim timer at the most, whatever came in (and the probe does see it)', most === 1, most);
  put('night-ask', function (v) { v.dim = false; });
  nightReset();
  ok('a reset leaves no timer', liveDim() === 0);
"""


@needs_chromium
def test_there_is_never_more_than_one_dim_timer(tmp_path):
    assert_all(run_page(tmp_path, TIMERS_JS, query="live=1", budget=2000), 2)


# --- a permission is never dim and never covered --------------------------------------------------------------------------------

ASK_JS = r"""
  goLive();
  /* the flag says night and no backlight (the page would darken itself): under a permission it still does nothing */
  put('approval', function (v) { v.dim = true; v.backlight = false; });
  ok('a permission with dim:true: no class, no taps, no darkness, at once', screenOf() === 'ask' && !on() && !dark() && pe() === 'none' && op() === 0
    && cs(D).transitionDuration === '0s');
  ok('the buttons are what a finger finds', lookAt($('b-ok')).closest('#b-ok') === $('b-ok') && lookAt($('b-no')).closest('#b-no') === $('b-no'));
  ok('Buddy is not covered either, and a tap there wakes nothing and asks nothing', lookAt(Z) === Z && tapOn(Z) === Z && wakes().length === 0);
  /* dark first, then a permission arrives while the flag is still true */
  put('threads', function (v) { v.dim = true; v.backlight = false; });
  ok('dark first', dark() && on());
  var t0 = nowMs();
  show(function (v) { v.approvals = [ASK]; v.mood = 'attention'; });
  ok('the permission uncovers the screen at once, with no fade', screenOf() === 'ask' && !on() && !dark() && pe() === 'none' && op() === 0, D.className + ' ' + op());
  ok('its buttons are armed as always: asleep for 600 ms', $('btns').className.indexOf('hold') >= 0);
  var hit = tapOn($('b-ok'));
  ok('a finger that was on the dark screen and now lands on Aprobar decides nothing', hit !== D && Object.keys(sending).length === 0 && decides().length === 0
    && $('btns').className.indexOf('hold') >= 0);
  tapOn($('b-no'));
  ok('nor on Denegar', Object.keys(sending).length === 0 && decides().length === 0);
  ok('the waking tap code did not run for it', wakes().length === 0);
  step(300, function () {
    ok('still asleep at 300 ms', nowMs() - t0 >= 280 && $('btns').className.indexOf('hold') >= 0);
    tapOn($('b-ok'));
    ok('and still nothing decided', decides().length === 0 && Object.keys(sending).length === 0);
  });
  step(450, function () {
    ok('awake at 750 ms', nowMs() - t0 >= 700 && $('btns').className.indexOf('hold') < 0);
    var hit2 = tapOn($('b-ok'));
    ok('then Aprobar decides, once, through /decide with the token', hit2 !== D && decides().length === 1 && decides()[0].headers['X-Buddy-Token'] === TOKEN
      && JSON.parse(decides()[0].body).decision === 'once', JSON.stringify(decides()));
    ok('and the wake POST never happened', wakes().length === 0);
  });
  /* the daemon's own shape: dim:false with a permission, the same screen */
  step(1200, function () {
    put('threads', function (v) { v.dim = true; v.backlight = false; });
    ok('dark again', dark());
    show(function (v) { v.dim = false; v.approvals = [{ key: 'claude:t2', tool: 'Edit', hint: 'x.py', source: 'claude' }]; v.mood = 'attention'; });
    ok('dim:false and a permission: bright, armed, nothing in the way', screenOf() === 'ask' && !on() && !dark() && $('btns').className.indexOf('hold') >= 0);
  });
  /* the permission is answered: the daemon keeps the screen awake 30 s more (dim:false) and the page just shows the list */
  step(800, function () {
    show(function (v) { v.approvals = []; v.mood = 'idle'; v.dim = false; });
    ok('answered: the list is back, and it is not dark or covered', screenOf() === 'main' && !dark() && !on() && pe() === 'none');
  });
  step(100, function () {
    show(function (v) { v.dim = true; });
    ok('when the daemon finally says dim, it darkens', dark() && on());
  });
"""


@needs_chromium
def test_a_permission_is_never_dim_never_covered_and_keeps_its_600_ms(tmp_path):
    assert_all(run_page(tmp_path, ASK_JS, query="live=1", budget=6000), 18)


# --- the demos ----------------------------------------------------------------------------------------------------------------

DEMOS_JS = r"""
  var expect = __SCREENS__, kinds = { night: 'backlight', 'night-overlay': 'overlay', 'night-ask': 'ask' };
  Object.keys(expect).forEach(function (name) {
    ok(name + ': the demo is there, in the cycle', Object.prototype.hasOwnProperty.call(DEMOS, name) && demoNames.indexOf(name) >= 0);
    try {
      demoShow(name);
      ok(name + ': it picks the screen it always did (' + expect[name] + ')', screenOf() === expect[name], screenOf());
    } catch (e) { ok(name + ' threw: ' + e.message, false); }
  });
  demoShow('night');
  ok('night is dim with the backlight doing it: nothing to see but a layer that catches taps', DEMOS.night.dim === true && DEMOS.night.backlight === true && on() && !dark());
  demoShow('night-overlay');
  ok('night-overlay is dim with the page darkening itself', DEMOS['night-overlay'].dim === true && DEMOS['night-overlay'].backlight === false && on() && dark());
  demoShow('night-ask');
  ok('night-ask: night and a permission, the flag still set, and the screen is bright and the buttons are live-looking',
    DEMOS['night-ask'].dim === true && screenOf() === 'ask' && !on() && !dark() && op() === 0 && $('b-ok').getBoundingClientRect().width > 100);
  /* a wake in one preview is not inherited by the next (the cycle moves on every 4.5 s) */
  demoShow('night-overlay');
  tapOn(D);
  ok('awake after a tap', !dark() && nightAwakeUntil > 0);
  demoShow('night-overlay');
  ok('and the next preview starts dark again, with no wake and no timer', dark() && on() && nightAwakeUntil === 0 && liveDim() === 0);
  demoShow('idle');
  ok('a day preview afterwards is inert', !on() && !dark() && pe() === 'none');
"""


@needs_chromium
def test_every_demo_still_renders_its_screen_and_the_night_demos_are_in_the_cycle(tmp_path):
    body = DEMOS_JS.replace("__SCREENS__", json.dumps(DEMO_SCREENS))
    assert_all(run_page(tmp_path, body, query="demo=idle", budget=3000), 2 * len(DEMO_SCREENS) + 6)


@needs_chromium
@pytest.mark.parametrize("demo,screen,on,dark", [("night", "main", True, False), ("night-overlay", "main", True, True), ("night-ask", "ask", False, False),
                                                   ("idle", "main", False, False), ("approval", "ask", False, False)])
def test_the_night_demos_render_without_a_daemon(demo, screen, on, dark):
    dom = dump(f"{PAGE.as_uri()}?demo={demo}&tag=0", budget=2500)
    assert attr(dom, "data-screen") == screen
    names = _dim_classes(dom)
    assert ("on" in names) is on and ("dark" in names) is dark, names


def _dim_classes(dom):
    """The classes #dim wears in a dumped DOM."""
    tag = re.search(r'<div id="dim"[^>]*>', dom)
    assert tag, "no #dim in the DOM"
    found = re.search(r'class="([^"]*)"', tag.group(0))
    return set(found.group(1).split()) if found else set()


# --- hostile data: text stays text, and odd flags are not "dim" --------------------------------------------------------------

HOSTILE_JS = r"""
  goLive();
  var X = '<img src=x onerror="window.__pwn=1"><script>window.__pwn=2<\/script>';
  put('threads', function (v) {
    v.dim = true; v.backlight = false;
    v.sessions.msg = X; v.pet.name = X; v.pet.owner = X; v.ble.name = X;
    v.tasks[0].title = X; v.tasks[0].said = X; v.tasks[0].group = X; v.tasks[1].project = X; v.tasks[1].tool = X; v.tasks[1].what = X;
  });
  ok('hostile text with the screen dark: still dark, still covered', dark() && on() && screenOf() === 'main');
  ok('it is text: no element came out of it and nothing ran', document.querySelectorAll('img[src="x"]').length === 0 && window.__pwn === undefined
    && document.querySelectorAll('#stage script').length === 0);
  ok('and it is there as text', $('pet-name').textContent === X && $('feed-list').textContent.indexOf(X) >= 0 && $('net-name').textContent === X);
  tapOn(Z);
  ok('waking it with that on screen changes nothing about it', !dark() && $('pet-name').textContent === X && document.querySelectorAll('img[src="x"]').length === 0);
  put('approval', function (v) { v.dim = true; v.approvals[0].hint = X; v.approvals[0].tool = X; });
  ok('a hostile permission under dim:true is shown as text and not covered', screenOf() === 'ask' && !on() && $('hint').textContent === X && $('tool').textContent.indexOf(X) >= 0
    && document.querySelectorAll('img[src="x"]').length === 0 && window.__pwn === undefined);
  /* only a real true is "dim", and only a real true is "the backlight does it" */
  [['true'], [1], ['yes'], [{}], [[true]], [null], ['<b>'], ['false'], [0]].forEach(function (odd) {
    put('threads', function (v) { v.dim = odd[0]; v.backlight = true; });
    ok('dim = ' + JSON.stringify(odd[0]) + ' is not night', !on() && !dark() && pe() === 'none', D.className);
  });
  [['true'], [1], [{}], ['yes'], [null]].forEach(function (odd) {
    put('threads', function (v) { v.dim = true; v.backlight = odd[0]; });
    ok('backlight = ' + JSON.stringify(odd[0]) + ' is not a backlight: the page darkens itself', on() && dark());
  });
  put('threads', function (v) { delete v.dim; delete v.backlight; });
  ok('a view from a daemon that knows nothing of night is a day view', !on() && !dark() && pe() === 'none');
  view = null; render();
  ok('no view at all: boot, inert', screenOf() === 'boot' && !on() && !dark());
"""


@needs_chromium
def test_hostile_data_stays_text_and_only_a_real_true_is_night(tmp_path):
    assert_all(run_page(tmp_path, HOSTILE_JS, query="live=1", budget=2000), 21)


OFFLINE_JS = r"""
  goLive();
  put('threads', function (v) { v.dim = true; v.backlight = true; });
  $('offline').className = 'on';
  $('toast').className = 'on';
  $('flash').className = 'flash on ok';
  var mid = at(400, 240), low = at(400, 450);
  ok('over the offline notice, the toast and the flash: #dim is still on top', document.elementFromPoint(mid[0], mid[1]) === D && document.elementFromPoint(low[0], low[1]) === D
    && cs($('offline')).display === 'flex');
  put('approval', function (v) { v.dim = true; });
  ok('and under a permission it is on top of nothing, the notices included', document.elementFromPoint(mid[0], mid[1]) !== D && document.elementFromPoint(low[0], low[1]) !== D);
"""


@needs_chromium
def test_the_dim_layer_is_above_the_notices_and_still_never_above_a_permission(tmp_path):
    assert_all(run_page(tmp_path, OFFLINE_JS, query="live=1", budget=1500), 2)


# --- the real daemon's view ----------------------------------------------------------------------------------------------------


def _night_rig(tmp_path, night):
    """A rig whose hub is in night mode (no backlight, so the page has to darken itself)."""
    rig = Rig(tmp_path / "state")
    rig.hub.unbind()
    rig.hub = UIHub(night=night)
    rig.hub.bind(rig.core, rig.radio)
    return rig


needs_night_hub = pytest.mark.skipif("night" not in inspect.signature(UIHub.__init__).parameters, reason="the daemon has no night mode yet")


@needs_chromium
@needs_night_hub
def test_the_page_goes_dark_on_the_real_daemons_night_view_and_stays_bright_under_a_permission(tmp_path):
    async def scenario():
        rig = _night_rig(tmp_path, (0, 0))  # start == end: always night
        rig.rx(heartbeat())
        loop = asyncio.get_running_loop()
        async with serving(rig) as server:
            night = await loop.run_in_executor(None, dump, server.url + "/?poll=300&tag=0")
            rig.rx(heartbeat(waiting=1, prompt={"id": "r1", "tool": "Bash", "hint": "ls"}))
            asked = await loop.run_in_executor(None, dump, server.url + "/?poll=300&tag=0")
        rig.hub.close()
        return night, asked

    night, asked = run(scenario)
    assert attr(night, "data-screen") == "main"
    assert _dim_classes(night) == {"on", "dark"}  # no backlight in this rig: the page darkens itself
    assert attr(asked, "data-screen") == "ask"
    assert not _dim_classes(asked) & {"on", "dark"}


@needs_chromium
@needs_night_hub
def test_a_daemon_without_night_mode_leaves_the_page_exactly_as_it_was(tmp_path):
    async def scenario():
        rig = Rig(tmp_path / "state")
        rig.rx(heartbeat())
        async with serving(rig) as server:
            return await asyncio.get_running_loop().run_in_executor(None, dump, server.url + "/?poll=300&tag=0")

    dom = run(scenario)
    assert attr(dom, "data-screen") == "main"
    assert not _dim_classes(dom) & {"on", "dark"}


KEEP_JS = r"""
  goLive();
  function press(el) { el.dispatchEvent(new Event('pointerdown', { bubbles: true })); }
  put('night', function (v) { v.dim = false; });   // night window, and awake: the daemon says dim false, night true
  var base = wakes().length;
  press($('feed-list'));
  ok('a touch on the awake night screen tells the daemon it is in use', wakes().length === base + 1 && !dark());
  press($('feed-list')); press(Z); press($('plate'));
  ok('and no more than once every 5 s', wakes().length === base + 1);
  step(5100, function () {
    press($('feed-list'));
    ok('after the 5 s it tells it again', wakes().length === base + 2);
    put('night', function (v) { v.dim = true; });   // dark: the waking tap is the click on #dim, not this
    press(D);
    ok('while it is dark the press says nothing (the click will)', wakes().length === base + 2 && on());
    put('night', function (v) { v.dim = false; v.night = false; });   // by day
    press($('feed-list'));
    ok('by day it never says anything', wakes().length === base + 2);
    put('night-ask', function (v) { v.dim = false; });   // a permission: the daemon already keeps it awake
    press($('feed-list'));
    ok('under a permission it says nothing', wakes().length === base + 2);
  });
  step(6000, function () {
    put('night', function (v) { v.dim = false; });
    fetchMode = 'reject';
    press($('feed-list'));
    ok('a lost notice breaks nothing and is not shown', wakes().length === base + 3 && unhandled === 0 && !dark());
    fetchMode = 'ok';
  });
"""


@needs_chromium
def test_a_touch_on_the_awake_night_screen_tells_the_daemon_it_is_still_in_use(tmp_path):
    assert_all(run_page(tmp_path, KEEP_JS, query="live=1", budget=14000), 7)
