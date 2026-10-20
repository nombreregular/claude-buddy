"""Hermes notices on the screen: the "Avisos" group at the top of the thread list, its rows, the detail with Listo, the 4 s of attention a new
notice gives Buddy, and everything it must leave alone (a permission, the dim night screen, the arming, the text of the status).

The browser tests run the page for real in a headless Chromium (they skip when there is none), with the same battery as test_page_tasks.py:
the self-test is injected into a temporary COPY of ui/index.html, inside the page's own script, so it can hand render() any view it likes
(the shape the daemon sends in `view.notices`); nothing is ever written to the repo. The page runs on virtual time. Every deadline the page
keeps on performance.now() (the 350 ms guard of Volver and Listo, the night wake) is read from a clock the TEST owns: a timer moves it to its
due time just before it runs, so how fast the machine is (a Pi's Chromium spends tens of ms opening a detail) never decides a guard. The
network is the test's too: every fetch is remembered and what it answers is chosen."""
import asyncio
import html
import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from page_runner import attr, dump, needs_chromium  # noqa: E402
from test_page_tasks import FINISH, OLD_DEMOS, PRELUDE, _css, _page, _rule, assert_all, battery  # noqa: E402
from test_ui import PAGE, Rig, heartbeat, run, serving  # noqa: E402

NOTICE_DEMOS = ("notices", "notices-pack", "notices-only")


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path / "state")


# --- what every browser test below starts from -------------------------------------------------------------------------

HELPERS = r"""
  /* One clock for the page's deadlines and its timers, owned by the test (Date.now() is not used: the Pi's Chromium does not move it with
     virtual time). Every timer the page arms is told apart by what it runs, so "one timer" can be counted. */
  var clock = 1000;
  performance.now = function () { return clock; };
  var live = {}, realSet = window.setTimeout, realClear = window.clearTimeout;
  window.setTimeout = function (fn, ms) {
    var due = clock + (Number(ms) || 0);
    var id = realSet(function () { clock = Math.max(clock, due); delete live[id]; return fn.apply(this, arguments); }, ms);
    live[id] = fn === closeTask ? 'detail' : fn === paintDim ? 'dim' : String(fn).indexOf('clearTouch') >= 0 ? 'touch' : 'other';
    return id;
  };
  window.clearTimeout = function (id) { delete live[id]; return realClear(id); };
  function liveOf(kind) { return Object.keys(live).filter(function (k) { return live[k] === kind; }).length; }
  /* the network is the test's: every call is remembered and what it answers is chosen */
  var calls = [], fetchMode = 'ok', unhandled = 0, pending = [];
  window.addEventListener('unhandledrejection', function () { unhandled += 1; });
  window.fetch = function (url, opt) {
    calls.push({ url: String(url), method: opt && opt.method, headers: opt && opt.headers, body: opt && opt.body });
    if (fetchMode === 'throw') throw new Error('no fetch');
    if (fetchMode === 'reject') return Promise.reject(new Error('down'));
    if (fetchMode === 'hang') return new Promise(function (resolve) { pending.push(resolve); });
    if (fetchMode === 'busy') return Promise.resolve({ ok: false, status: 429, json: function () { return Promise.resolve({}); } });
    return Promise.resolve({ ok: true, status: 200, json: function () { return Promise.resolve({ ok: true, state: 'dismissed' }); } });
  };
  function dismisses() { return calls.filter(function (c) { return c.url === '/dismiss'; }); }
  function wakes() { return calls.filter(function (c) { return c.url === '/wake'; }); }
  function decides() { return calls.filter(function (c) { return c.url === '/decide'; }); }
  /* the page with no daemon: its stream closed, the views put by hand */
  function goLive() { if (es) { es.close(); es.onerror = null; } setOffline(false); }
  /* a page that has just loaded: nothing seen, nothing hidden, no first view yet */
  function reborn() { clearTouch(); closeTask(); nightReset(); noticeReset(); rank.clear(); grank.clear(); rankSeq = 0; grankSeq = 0; view = null; render(); }
  function screenOf() { return document.body.getAttribute('data-screen'); }
  function mood() { return document.body.getAttribute('data-mood'); }
  function middle(el) { var r = el.getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height / 2]; }
  function at(X, Y) { var sr = $('stage').getBoundingClientRect(), k = sr.width / 800; return [sr.left + X * k, sr.top + Y * k]; }
  /* a click the way a finger makes one: on whatever is really on top at that point */
  function tapAt(x, y) {
    var t = document.elementFromPoint(x, y);
    t.dispatchEvent(new MouseEvent('click', { bubbles: true, clientX: x, clientY: y }));
    return t;
  }
  function tapOn(el) { var c = middle(el); return tapAt(c[0], c[1]); }
  function later(ms, fn) { step(ms, fn); }
  /* a view as the daemon sends it (busy base, a Mac that is linked), with what the test says on top */
  function V(o) {
    var v = clone(DEMOS.busy);
    Object.keys(o || {}).forEach(function (k) { v[k] = o[k]; });
    return v;
  }
  function put(v) { view = v; render(); return v; }
  /* a notice as the daemon sends it */
  function N(id, o) {
    var n = { id: id, title: 'Aviso ' + id, kind: 'info', source: 'hermes', age: 5, rev: 1 };
    Object.keys(o || {}).forEach(function (k) { n[k] = o[k]; });
    return n;
  }
  function nrows() { return kids().filter(function (el) { return el.classList.contains('notice'); }); }
  function nrow(id) { return nrows().filter(function (el) { return el.getAttribute('data-n') === id; })[0]; }
  function ncap() { return kids().filter(function (el) { return el.hasAttribute('data-notes'); })[0]; }
  function bell(li) { return li.firstChild.children[0]; }
  function ttl(li) { return li.firstChild.children[1]; }
  function okMark(li) { return li.firstChild.children[2]; }
  function warnMark(li) { return li.firstChild.children[3]; }
  function agel(li) { return li.firstChild.children[4]; }
  /* the whole list, in order: the notices' caption, notice rows, thread captions and rows, the "+K más" line */
  function nseq() {
    return kids().map(function (el) {
      return el.hasAttribute('data-notes') ? 'notes' : el.classList.contains('notice') ? 'n:' + el.getAttribute('data-n')
        : el.classList.contains('grp') ? 'g:' + el.getAttribute('data-g') : el.hasAttribute('data-more') ? 'more' : el.getAttribute('data-id');
    }).join('|');
  }
  /* everything on the screen that is the server's word */
  function truth() {
    return JSON.stringify(['mood', 'n-total', 'n-running', 'n-waiting', 'f-tokens', 'f-ok', 'f-no', 'pet-level', 'pet-name', 'pet-owner']
      .map(function (id) { return $(id).textContent; }).concat(document.body.getAttribute('data-real')));
  }
  function tv() { return $('task-view'); }
  function pingMark() { return $('ping').style.getPropertyValue('--px') + '|' + $('ping').style.getPropertyValue('--py'); }
  function bubbleBox() { return px($('feed')); }
  /* is any of el's text cut? (Nunito Sans letters stick out of their line by about 3 px, so a line more is "more than half a line more") */
  function clipped(el) { return el.scrollHeight > el.clientHeight + 11 || el.scrollWidth > el.clientWidth + 1; }
  var D = $('dim'), Z = $('petzone');
  function dark() { return D.classList.contains('on') && D.getBoundingClientRect().width > 0; }
"""


def run_page(tmp_path, body, query="live=1", budget=4000):
    return battery(tmp_path, HELPERS + body, query=query, budget=budget)


# --- static: what the page's own source promises ----------------------------------------------------------------------


def _notice_block():
    page = _page()
    start = page.index("/* ---------------- avisos")
    return page[start:page.index("function renderDown(v) {", start)]


def _notice_code():
    """The notice block without its comments (prose may name what the code does not do)."""
    return re.sub(r"//[^\n]*", "", re.sub(r"/\*.*?\*/", "", _notice_block(), flags=re.S))


def test_this_file_has_no_em_dash():
    assert chr(0x2014) not in Path(__file__).read_text(encoding="utf-8")


def test_the_notice_numbers_are_what_the_contract_says():
    block, page = _notice_block(), _page()
    assert "var NOTICE_MAX = 3;" in block and "var NOTICE_ATTN_MS = 4000;" in block
    assert "setTouch('attention', NOTICE_ATTN_MS)" in block  # the existing touch override, not a second mechanism
    assert "var TAP_GUARD_MS = 350;" in page and "var DETAIL_MS = 25000;" in page and "var ARM_MS = 600;" in page  # the others' numbers
    assert "performance.now() - openedAt < TAP_GUARD_MS" in block  # Listo has the same guard as Volver
    assert "function backTask() { if (performance.now() - openedAt >= TAP_GUARD_MS) closeTask(); }" in page  # and Volver is as it was


def test_the_notice_code_keeps_text_as_text_ids_as_map_keys_and_stays_out_of_the_arming():
    code = _notice_code()
    for name in ("holdUntil", "ARM_MS", "decide", "paintAsk", "armTimer", "sending", "curKey", "touchMood", "touchTimer", "touchTaps"):
        assert not re.search(rf"\b{name}\b", code), f"the notice code touches {name}"
    for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "createElementNS", "querySelector(", "querySelectorAll(", "eval(",
                "localStorage", "sessionStorage", "indexedDB", "document.cookie", "XMLHttpRequest", "sendBeacon", "EventSource", "location"):
        assert bad not in code, bad  # an id is never a selector and nothing is stored or opened
    assert not re.search(r"\.(?:textContent|innerText)\s*=[^=]", code)  # everything goes through setText
    assert "Object.create(null)" not in code and not re.search(r"\b(?:noticeHidden|noticeRank|noticeSeen|all|live|ids)\s*=\s*\{\}", code)
    for state in ("noticeHidden", "noticeRank", "noticeSeen"):
        assert re.search(rf"var {state} = new Map\(\)", _notice_block()), state  # ids come from outside: "constructor" and "__proto__" are fine
    assert "setTimeout(" not in code  # the one timer of the attention is the touch code's, armed by setTouch


def test_listo_posts_to_dismiss_once_with_the_token_and_says_nothing_in_a_preview():
    code = _notice_code()
    assert code.count("fetch(") == 1 and "fetch('/dismiss', { method: 'POST'" in code
    assert "'Content-Type': 'application/json', 'X-Buddy-Token': TOKEN" in code and "body: JSON.stringify({ id: id })" in code
    assert re.search(r"function sendDismiss\(id\) \{\n\s+if \(DEMO\) return;", code)  # the preview talks to nobody
    assert "/decide" not in code and "/wake" not in code  # a notice can never approve or deny anything


def test_the_notice_touch_rule_and_the_render_hooks_are_where_the_contract_wants_them():
    page = _page()
    assert "touchMood === 'dizzy' || touchMood === 'attention' || curMood === 'attention'" in page  # a displayed attention is not for playing
    assert re.search(r"paintMood\(\);\n\s+watchNotices\(v, screen, mood\);", page)  # after the pack and the mood, before the bar
    assert re.search(r"return v\.linked \|\| noticesOf\(v\)\.length \? 'main' : 'wait';", page)
    assert re.search(r"if \(!v\.ble\.ready && !noticesOf\(v\)\.length\) return 'down';\n\s+return v\.linked", page)  # a live notice outranks "down" too
    assert "if (screen === 'ask' || mood === 'attention') clearTouch();" in page  # a permission cancels the notice attention at once, same line
    assert "paintNotice(notes);" in page and "orderNotes(noticesOf(v))" in page
    assert "if (!tasks.length && !notes.length) {" in page  # the protocol lines never overwrite a list that only has notices
    assert page.count("stableOrder(shownTasks(") == 3  # the one list every thread caller shares
    assert "nightReset(); rank.clear();" in page  # (night's own check) and the preview reset sits next to it
    assert re.search(r"grankSeq = 0;[^\n]*\n\s+noticeReset\(\);\n\s+render\(\);", page)


def test_notice_rows_reuse_the_thread_row_and_follow_the_design_rules():
    css = _css()
    for selector in ("#feed-list li.notice", "li.notice .trow b", ".trow .bell", ".trow .wn", "li.notice.ok .trow", "li.notice.warn .trow",
                     ".tv-done", "#task-view.notice .tv-done", "#task-view.notice.tall .tv-name"):
        assert "opacity" not in _rule(css, selector), selector  # colour, never dimming
    assert "height:44px" in _rule(css, ".tv-done") and "height:44px" in _rule(css, ".trow")  # the same finger-sized pill, no second row design
    assert "height" not in _rule(css, "li.notice .trow b")
    assert "var(--ok) 34%" in _rule(css, "li.notice.ok .trow") and "var(--ask) 38%" in _rule(css, "li.notice.warn .trow")
    assert "var(--accent) 22%" in _rule(css, ".trow")  # info is the thread row's own working tint: no rule of its own
    assert "li.notice.info" not in css and ".notice.info" not in css
    for found in re.finditer(r"transition:([^;}]*)", _rule(css, ".tv-done")):
        assert set(re.findall(r"(?:^|,)\s*([a-z-]+)", found.group(1))) <= {"transform"}
    assert re.search(r"li\.notice\.ok \.trow \.ck,li\.notice\.warn \.trow \.wn\{display:block\}", css)  # a mark that is not a colour
    assert "display:none" in _rule(css, ".trow .wn") and "display:none" in _rule(css, ".tv-done") and "display:none" in _rule(css, ".tv-na")
    assert "white-space:pre-line" in _rule(css, "#tv-said")  # the body keeps its line breaks, as text
    page = _page()
    assert '<template id="note-proto">' in page and 'id="tv-done"' in page and 'id="tv-na"' in page
    head = page[page.index('<div class="tv-head">'):page.index('<div class="tv-sub">')]
    assert head.index('id="tv-name"') < head.index('id="tv-done"') < head.index('id="tv-back"')  # Volver is the last of the head: where it is in a thread's detail
    template = page[page.index('<template id="note-proto">'):page.index("</template>", page.index('<template id="note-proto">'))]
    assert 'class="bell"' in template and 'stroke="currentColor"' in template and "<svg" in template  # an inline bell that takes the text colour
    assert "innerHTML" not in page[page.index("<script>"):]


def test_the_notice_demos_exist_and_the_base_view_carries_an_empty_list():
    page = _page()
    for name in NOTICE_DEMOS:
        assert re.search(rf"^\s+(?:'{name}'|{name}): mk\(", page, re.M), name
    assert "approvals: [], notices: [], clock: '10:42'" in page  # the daemon's view always has the list
    assert "&open=" in page or "query.get('open')" in page


# --- the list: the group at the top, its rows, the headline -----------------------------------------------------------------

LIST_JS = r"""
  ok('the page started as a demo with two notices and threads', DEMOS.notices.notices.length === 2 && DEMOS.notices.tasks.length === 5);
  ok('the caption leads the list, then the two notices, then the thread groups', nseq() === 'notes|n:n-reunion|n:n-correo|g:Acme Studio|f1|f2|g:Buddy|b1|b2|g:Terminal|t1', nseq());
  ok('the caption says Avisos and how many, plain like the thread captions', ncap().textContent === 'Avisos · 2' && ncap().classList.contains('grp')
    && Math.abs(px(ncap()).h - 16) < .6 && ncap().children.length === 0 && cs(ncap()).fontSize === '14px' && cs(ncap()).fontWeight === '700');
  ok('it sits 18 px in, like the row text', Math.abs(px(ncap()).x - px(nrows()[0]).x - 18) < 1);
  ok('the headline is still the threads', $('feed-msg').textContent === '3 tareas en marcha', $('feed-msg').textContent);
  ok('every notice row is a 44 px button, 384 px wide', nrows().every(function (li) {
    var r = px(li.firstChild); return li.firstChild.tagName === 'BUTTON' && Math.abs(r.h - 44) < .6 && Math.abs(r.w - 384) < 1;
  }));
  ok('a notice row is not a thread row (the thread code never sees it) and a thread row is not a notice', nrows().every(function (li) { return !li.classList.contains('task') && !li.hasAttribute('data-id'); })
    && rowsOnly().length === 5 && rowsOnly().every(function (li) { return !li.classList.contains('notice'); }));
  var r1 = nrow('n-reunion'), r2 = nrow('n-correo');
  ok('the row text is the title, the right side is how long ago (m:ss)', ttl(r1).textContent === 'Reunión con Ana en 10 min' && agel(r1).textContent === '1:35' && agel(r2).textContent === '3:00');
  ok('each row has a small bell drawn with the text colour', nrows().every(function (li) {
    var b = bell(li), path = b.querySelector('path');
    return b.tagName.toLowerCase() === 'svg' && px(b).w >= 14 && px(b).w <= 20 && path.getAttribute('stroke') === 'currentColor' && cs(path).stroke !== 'none';
  }));
  ok('the title is bold, takes the row and gives way with an ellipsis', cs(ttl(r1)).fontWeight === '800' && cs(ttl(r1)).textOverflow === 'ellipsis' && cs(ttl(r1)).flexGrow === '1');
  ok('the age is never clipped and the row does not overflow', nrows().every(function (li) {
    var btn = li.firstChild, i = agel(li), chev = btn.children[5];
    return btn.scrollWidth <= btn.clientWidth && px(i).x + px(i).w <= px(chev).x + .6 && i.scrollWidth <= i.clientWidth;
  }));
  ok('a warn row says it with a mark, an info row has none', cs(warnMark(r1)).display === 'block' && cs(okMark(r1)).display === 'none'
    && cs(warnMark(r2)).display === 'none' && cs(okMark(r2)).display === 'none');
  ok('info is the working-thread tint', cs(r2.firstChild).backgroundColor === cs(row('f1').firstChild).backgroundColor);
  ok('warn is not the info tint, and both are real', cs(r1.firstChild).backgroundColor !== cs(r2.firstChild).backgroundColor
    && rgba(cs(r1.firstChild).backgroundColor).a > .2 && rgba(cs(r2.firstChild).backgroundColor).a > .1);
  ok('nothing is dimmed', nrows().every(function (li) { return cs(li).opacity === '1' && cs(li.firstChild).opacity === '1'; }));
  ok('the list scrolls as before: 219 px window, and the notices add a caption and two rows to its height', Math.abs(px(L()).h - 219) < .6
    && L().scrollHeight === 44 * 7 + 10 * (7 + 4 - 1) + 10 * 4 + 18, L().scrollHeight);
  var pillsY = px(document.querySelector('.pills')).y;
  ok('the bubble is as tall as ever', Math.abs(px($('feed')).h - 284) < .6);
  ok('a tap on the middle of every visible notice row lands on that row', nrows().every(function (li) {
    var r = li.getBoundingClientRect(); var hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    return hit && hit.closest('li.notice') === li;
  }));
  ok('tapping the caption opens nothing', (function () { tap(ncap()); return !reading(); })());
  tap(row('f1').firstChild);
  ok('a thread row still opens its own detail, with no notice dressing', reading() && $('tv-name').textContent === 'Catálogo de la tienda web'
    && !tv().classList.contains('notice') && cs($('tv-done')).display === 'none' && cs($('tv-na')).display === 'none');
  closeTask();
  /* the list gets the group back when the notices come back, and gives it up when they go */
  show(function (v) { v.notices = []; });
  ok('without notices there is no caption and no row, and the threads are where they were', nseq() === 'g:Acme Studio|f1|f2|g:Buddy|b1|b2|g:Terminal|t1' && ncap() === undefined);
  ok('and nothing below the bubble moved, with the group or without it', Math.abs(px(document.querySelector('.pills')).y - pillsY) < .6 && Math.abs(px($('feed')).h - 284) < .6);
  show(function (v) { v.notices = [N('x1', { title: 'Uno' })]; });
  ok('one notice is "Avisos · 1"', nseq() === 'notes|n:x1|g:Acme Studio|f1|f2|g:Buddy|b1|b2|g:Terminal|t1' && ncap().textContent === 'Avisos · 1', nseq());
"""


@needs_chromium
def test_the_group_sits_at_the_top_of_the_thread_list_with_its_rows(tmp_path):
    assert_all(battery(tmp_path, HELPERS + LIST_JS, query="demo=notices", budget=3000), 20)


KINDS_JS = r"""
  var tanda = nrow('n-tanda'), reunion = nrow('n-reunion'), correo = nrow('n-correo');
  ok('three kinds, three rows, in the order the daemon sent them', nseq() === 'notes|n:n-tanda|n:n-reunion|n:n-correo', nseq());
  ok('an ok row has a check mark and a warn row an exclamation, an info row neither', cs(okMark(tanda)).display === 'block' && cs(warnMark(tanda)).display === 'none'
    && cs(warnMark(reunion)).display === 'block' && cs(okMark(reunion)).display === 'none'
    && cs(okMark(correo)).display === 'none' && cs(warnMark(correo)).display === 'none');
  ok('the marks are 14 px and come before the age, the colour is never the only cue', [[tanda, okMark(tanda)], [reunion, warnMark(reunion)]].every(function (p) {
    return Math.abs(px(p[1]).w - 14) < .6 && px(p[1]).x + px(p[1]).w <= px(agel(p[0])).x + .6;
  }));
  var tint = function (li) { return rgba(cs(li.firstChild).backgroundColor); };
  ok('ok is green, warn is amber, info is the mood accent at the thread row 22%', tint(tanda).g > tint(tanda).r && tint(tanda).g > tint(tanda).b
    && tint(reunion).r > tint(reunion).g && tint(reunion).g > tint(reunion).b && tint(reunion).b < 140
    && Math.abs(tint(correo).a - .22) < .02 && Math.abs(tint(correo).r - 255) < 2 && Math.abs(tint(correo).g - 158) < 2);
  ok('the three tints are three different colours', new Set([cs(tanda.firstChild).backgroundColor, cs(reunion.firstChild).backgroundColor, cs(correo.firstChild).backgroundColor]).size === 3);
  ok('the green is the page ok and the amber is the page ask', Math.abs(tint(tanda).r - 124) < 2 && Math.abs(tint(tanda).g - 227) < 2 && Math.abs(tint(reunion).r - 255) < 2 && Math.abs(tint(reunion).g - 207) < 2);
  var bubble = cs($('feed')).backgroundColor;
  ok('titles read on every tint (7:1) on paper', nrows().every(function (li) { return contrast(cs(ttl(li)).color, over(tint(li), rgba(bubble))) >= 7; }),
    nrows().map(function (li) { return contrast(cs(ttl(li)).color, over(tint(li), rgba(bubble))).toFixed(1); }).join(','));
  ok('the age, the marks and the bell read on every tint (4.5:1)', nrows().every(function (li) {
    var base = over(tint(li), rgba(bubble));
    return contrast(cs(agel(li)).color, base) >= 4.5 && contrast(cs(bell(li)).color, base) >= 4.5 && contrast(cs(okMark(li)).color, base) >= 4.5 && contrast(cs(warnMark(li)).color, base) >= 4.5;
  }));
  ok('the caption reads on paper (4.5:1)', contrast(cs(ncap()).color, bubble) >= 4.5);
"""


@needs_chromium
def test_each_kind_has_its_tint_and_a_mark_and_all_of_them_read(tmp_path):
    assert_all(battery(tmp_path, HELPERS + KINDS_JS, query="demo=notices-only", budget=3000), 9)


NOTES_ONLY_JS = r"""
  ok('with no threads the headline counts the notices', $('feed-msg').textContent === '3 avisos' && cs($('feed-msg')).display !== 'none', $('feed-msg').textContent);
  ok('the empty state does not speak and the list is the notices alone', cs($('feed-empty')).display === 'none' && cs($('feed-detail')).display === 'none'
    && nseq() === 'notes|n:n-tanda|n:n-reunion|n:n-correo' && L().classList.contains('tasks'), nseq());
  ok('there is no stray line in the list (nothing without a key)', kids().every(function (el) { return el.hasAttribute('data-notes') || el.hasAttribute('data-n'); }));
  ok('the counters are the daemon\'s word and say zero', $('n-total').textContent === '0' && $('n-running').textContent === '0');
  show(function (v) { v.notices = [v.notices[0]]; });
  ok('one notice: "Un aviso"', $('feed-msg').textContent === 'Un aviso' && nseq() === 'notes|n:n-tanda', $('feed-msg').textContent);
  show(function (v) { v.notices = []; });
  feedKeep = { msg: '', entries: [], at: 0 };
  show(function (v) { v.sessions.msg = ''; v.sessions.entries = []; v.sessions.total = 0; v.sessions.running = 0; });
  ok('with none the empty state is back and the list is clean', cs($('feed-empty')).display !== 'none' && $('feed-empty').textContent === 'Acá espero.' && kids().length === 0, kids().length);
  show(function (v) { v.notices = [N('a'), N('b')]; });
  ok('and the notices come back as a list again', $('feed-msg').textContent === '2 avisos' && nseq() === 'notes|n:a|n:b' && cs($('feed-empty')).display === 'none');
  /* threads and notices: the headline stays the threads', the notices stay on top */
  show(function (v) { v.tasks = [T('t1', { title: 'Hilo', group: 'Buddy' })]; v.working_total = 1; });
  ok('with a thread again the headline is the thread\'s and the notices still lead', $('feed-msg').textContent === 'Una tarea en marcha' && nseq() === 'notes|n:a|n:b|g:Buddy|t1', nseq());
  show(function (v) { v.working_total = 0; });
  ok('a notice never counts as work', $('feed-msg').textContent === 'Nadie trabaja ahora.');
"""


@needs_chromium
def test_the_headline_counts_the_notices_only_when_there_are_no_threads(tmp_path):
    assert_all(battery(tmp_path, HELPERS + NOTES_ONLY_JS, query="demo=notices-only", budget=3000), 9)


ORDER_JS = r"""
  goLive();
  put(V({ notices: [N('c'), N('b'), N('a')] }));
  var nodes = kids();
  ok('first paint: the daemon\'s order (newest first)', nseq() === 'notes|n:c|n:b|n:a', nseq());
  show(function (v) { v.notices.reverse(); });
  ok('the daemon flipping its order moves nothing', nseq() === 'notes|n:c|n:b|n:a', nseq());
  show(function (v) { v.notices.forEach(function (n) { n.age += 30; }); });
  ok('time passing moves nothing and reuses every node', nseq() === 'notes|n:c|n:b|n:a' && kids().every(function (el, i) { return el === nodes[i]; }));
  ok('and the ages moved', agel(nrow('c')).textContent === '0:35');
  show(function (v) { v.notices.forEach(function (n) { if (n.id === 'b') { n.title = 'Otro texto'; n.kind = 'warn'; n.rev += 1; } }); });
  ok('a changed notice (same id, rev up) stays where it was and takes the new text and tint', nseq() === 'notes|n:c|n:b|n:a' && ttl(nrow('b')).textContent === 'Otro texto'
    && nrow('b').classList.contains('warn') && nrow('b') === nodes[2], nseq());
  show(function (v) {
    var by = function (id) { return v.notices.filter(function (n) { return n.id === id; })[0]; };
    v.notices = [N('d'), by('c'), by('b')];
  });
  ok('the daemon evicts the oldest (a) for a fourth (d): the oldest goes, the newcomer joins the end', nseq() === 'notes|n:c|n:b|n:d', nseq());
  ok('the caption still counts what is drawn', ncap().textContent === 'Avisos · 3');
  ok('the survivors are the same elements', nrow('c') === nodes[1] && nrow('b') === nodes[2]);
  show(function (v) { v.notices = v.notices.filter(function (n) { return n.id !== 'c'; }); });
  ok('a notice that goes shifts nobody else', nseq() === 'notes|n:b|n:d', nseq());
  show(function (v) { v.notices.push(N('c')); });
  ok('and one that comes back has lost its seat: last', nseq() === 'notes|n:b|n:d|n:c', nseq());
  /* the page does not trust the cap: four sent, three drawn */
  show(function (v) { v.notices = [N('p'), N('q'), N('r'), N('s')]; });
  ok('a daemon that sends four gets three drawn (the first three), and "Avisos · 3"', nseq() === 'notes|n:p|n:q|n:r' && ncap().textContent === 'Avisos · 3', nseq());
  /* the rows never made the thread list jump: the threads below keep their order */
  show(function (v) { v.tasks = [T('x1', { title: 'Uno', group: 'G' }), T('x2', { title: 'Dos', group: 'G' })]; v.notices = [N('p')]; });
  var before = nseq();
  show(function (v) { v.notices = [N('p'), N('q')]; });
  ok('a notice arriving does not reorder the threads', nseq() === 'notes|n:p|n:q|g:G|x1|x2' && before === 'notes|n:p|g:G|x1|x2', before + ' -> ' + nseq());
"""


@needs_chromium
def test_notice_rows_keep_their_place_and_are_reused(tmp_path):
    assert_all(run_page(tmp_path, ORDER_JS), 12)


HOSTILE_JS = r"""
  goLive();
  var evil = '<img src=x onerror="window.pwned=1"><b>x</b>';
  var threw = '';
  try {
    put(V({ notices: [N('constructor', { title: evil, body: evil + '\n' + evil, source: evil, kind: 'constructor' }),
                      N('__proto__', { title: '__proto__', kind: 'toString', source: 'hasOwnProperty' }),
                      N('toString', { title: 'x', body: '<scr' + 'ipt>window.pwned=2</scr' + 'ipt>' })] }));   // (split: a literal end tag would close the page's own script)
  } catch (e) { threw = e.message; }
  ok('hostile ids, titles, sources and kinds render without an exception', threw === '' && nrows().length === 3, threw);
  ok('the title and the age are literal text, no element came out', ttl(nrow('constructor')).textContent === evil && nrow('constructor').querySelectorAll('img, script').length === 0
    && L().querySelector('img') === null && window.pwned === undefined);
  ok('a kind that is not ok or warn is info, whatever it says', !nrow('constructor').classList.contains('ok') && !nrow('constructor').classList.contains('warn')
    && !nrow('__proto__').classList.contains('ok') && !nrow('__proto__').classList.contains('warn') && !nrow('toString').classList.contains('warn'));
  ok('names that poison plain objects are fine', ['constructor', '__proto__', 'toString'].every(function (id) { return !!nrow(id); }) && ttl(nrow('__proto__')).textContent === '__proto__');
  tap(nrow('constructor').firstChild);
  ok('the detail shows the same as text', reading() && $('tv-name').textContent === evil && $('tv-name').children.length === 0 && $('tv-did').textContent === 'de ' + evil
    && $('tv-did').children.length === 0 && $('tv-text').children.length === 0 && $('tv-text').textContent.indexOf('<img') === 0
    && document.querySelectorAll('#feed img, #feed script, #feed b b').length === 0 && window.pwned === undefined);
  ok('a body with line breaks keeps them as text', $('tv-text').textContent.indexOf('\n') > 0 && cs($('tv-said')).whiteSpace === 'pre-line');
  closeTask();
  /* a daemon that sends things that are not what it should: nothing breaks, nothing half-draws */
  var odd = [undefined, null, 'abc', 5, {}, [null, 3, 'x', [], { id: 7 }, { id: '' }, { title: 'sin id' }], [N('a'), N('a', { title: 'repetido' })],
             [N('z', { title: 7, body: 8, source: 9, age: 'abc', rev: null }), N('y', { title: '', source: '', age: -5, rev: -1 }), N('w', { age: 1e12, rev: 1.9 })]];
  odd.forEach(function (list, k) {
    var thrown = '';
    try { show(function (v) { v.notices = list; }); } catch (e) { thrown = e.message; }
    ok('notices = ' + JSON.stringify(list) + ' draws without an exception', thrown === '', thrown);
  });
  ok('a duplicated id is drawn once, with its first text', (function () { show(function (v) { v.notices = [N('a'), N('a', { title: 'repetido' })]; }); return nrows().length === 1 && ttl(nrow('a')).textContent === 'Aviso a'; })());
  show(function (v) { v.notices = [N('z', { title: 7, body: 8, source: 9, age: 'abc', rev: null }), N('y', { title: '', source: '', age: -5, rev: -1 }), N('w', { age: 1e12, rev: 1.9 })]; });
  ok('a title that is not text or is empty is "Aviso", an age that is not a number is 0:00', ttl(nrow('z')).textContent === 'Aviso' && ttl(nrow('y')).textContent === 'Aviso'
    && agel(nrow('z')).textContent === '0:00' && agel(nrow('y')).textContent === '0:00');
  ok('the biggest age is still whole and not clipped', nrows().every(function (li) { var btn = li.firstChild; return btn.scrollWidth <= btn.clientWidth && agel(li).scrollWidth <= agel(li).clientWidth; }));
  tap(nrow('z').firstChild);
  ok('a notice with a source that is not text says hermes, one with a body that is not text says there is no more', reading() && $('tv-did').textContent === 'de hermes'
    && $('tv-text').textContent === 'Sin más detalle.' && $('tv-said').classList.contains('none'), $('tv-did').textContent);
"""


@needs_chromium
def test_hostile_ids_text_kinds_and_numbers_are_only_text_and_never_break_the_screen(tmp_path):
    assert_all(run_page(tmp_path, HOSTILE_JS), 15)


# --- the detail: open, read, Volver, the guards ----------------------------------------------------------------------------

DETAIL_JS = r"""
  goLive();
  put(V({ notices: [N('n1', { title: 'Tanda ok', body: 'Las cuatro corridas pasaron.\nEl resumen quedó en la bitácora.', kind: 'ok', age: 75 }),
                    N('n2', { title: 'Reunión', age: 3 })] }));
  var pillsY = px(document.querySelector('.pills')).y, feedH = bubbleBox().h;
  tap(nrow('n1').firstChild);
  ok('a tap opens the detail of that notice, the thread detail\'s own view', reading() && tv().classList.contains('notice') && cs(tv()).display === 'flex'
    && cs(L()).display === 'none' && cs($('feed-msg')).display === 'none');
  ok('the title is the headline of the detail', $('tv-name').textContent === 'Tanda ok' && cs($('tv-name')).fontSize === '30px' && !tv().classList.contains('long'));
  ok('the sub line reads "Aviso · de hermes · hace 1:15"', $('tv-age').textContent === 'Aviso' && $('tv-did').textContent === 'de hermes' && $('tv-na').textContent === 'hace 1:15'
    && cs($('tv-na')).display === 'block');
  ok('the dots between them are drawn by CSS and are not text', getComputedStyle($('tv-age'), '::after').content.indexOf('·') >= 0 && getComputedStyle($('tv-na'), '::before').content.indexOf('·') >= 0);
  ok('the three parts sit on one line, in that order, none clipped', (function () {
    var a = px($('tv-age')), d = px($('tv-did')), n = px($('tv-na'));
    return Math.abs(a.y - d.y) < 1 && Math.abs(d.y - n.y) < 1 && a.x + a.w < d.x && d.x + d.w < n.x && $('tv-did').scrollWidth <= $('tv-did').clientWidth && $('tv-na').scrollWidth <= $('tv-na').clientWidth;
  })());
  ok('the body is text with its line break, in full ink and with no quotes', $('tv-text').textContent === 'Las cuatro corridas pasaron.\nEl resumen quedó en la bitácora.' && $('tv-pre').textContent === ''
    && cs($('tv-said')).whiteSpace === 'pre-line' && !$('tv-said').classList.contains('none')
    && getComputedStyle($('tv-text'), '::before').content.indexOf('“') < 0 && getComputedStyle($('tv-text'), '::after').content.indexOf('”') < 0);
  ok('Volver and Listo are 44 px pills side by side at the top right, Volver last (Listo before it)', (function () {
    var b = px($('tv-back')), d = px($('tv-done'));
    return $('tv-done').tagName === 'BUTTON' && Math.abs(b.h - 44) < .6 && Math.abs(d.h - 44) < .6 && Math.abs(b.y - d.y) < .6 && d.x + d.w <= b.x + .6 && b.x - (d.x + d.w) <= 12
      && b.w >= 96 && d.w >= 80 && b.x + b.w <= px($('feed')).x + 432 - 24 + .6;
  })());
  ok('Volver sits exactly where it sits in a thread\'s detail, so a finger that goes to its usual place never lands on Listo', (function () {
    var here = px($('tv-back')), c = middle($('tv-back')), hit = document.elementFromPoint(c[0], c[1]);
    closeTask();
    put(V({ notices: [N('n1', { title: 'Tanda ok' })], tasks: [T('f1', { title: 'Un hilo', group: 'Buddy', tool: 'Bash', what: 'ls' })] }));
    tap(row('f1').firstChild);
    var there = px($('tv-back')), t = middle($('tv-back'));
    var same = Math.abs(here.x - there.x) < .6 && Math.abs(here.y - there.y) < .6 && Math.abs(here.w - there.w) < .6 && Math.abs(here.h - there.h) < .6;
    closeTask();
    put(V({ notices: [N('n1', { title: 'Tanda ok', body: 'Las cuatro corridas pasaron.\nEl resumen quedó en la bitácora.', kind: 'ok', age: 75 }), N('n2', { title: 'Reunión', age: 3 })] }));
    tap(nrow('n1').firstChild);
    var onTop = document.elementFromPoint(t[0], t[1]);
    return same && !!hit.closest('#tv-back') && !!onTop.closest('#tv-back') && !onTop.closest('#tv-done');
  })());
  ok('Listo says Listo and is a green pill with dark text', $('tv-done').textContent.trim() === 'Listo' && rgba(cs($('tv-done')).backgroundColor).g > 200 && contrast(cs($('tv-done')).color, cs($('tv-done')).backgroundColor) >= 7);
  ok('the bubble kept its height and nothing outside it moved', Math.abs(bubbleBox().h - feedH) < .6 && Math.abs(px(document.querySelector('.pills')).y - pillsY) < .6);
  ok('the detail is as tall as a thread\'s and the text has room', Math.abs(px(tv()).h - 246) < 1 && px($('tv-said')).h >= 100);
  tap($('tv-said')); tap($('tv-text')); tap($('tv-name')); tap(tv()); tap($('tv-na'));
  ok('touching the text never closes the detail', reading());
  tap($('tv-back'));
  ok('Volver right after opening is a bounce and is ignored (350 ms)', reading());
  later(200, function () { tap($('tv-back')); ok('still ignored at 200 ms', reading() && dismisses().length === 0); });
  later(200, function () { tap($('tv-back')); ok('Volver works after the guard and the notice is still in the list', !reading() && !tv().classList.contains('notice') && cs(L()).display !== 'none'
    && !!nrow('n1') && dismisses().length === 0 && !tv().classList.contains('long') && !tv().classList.contains('tall')); });
  /* a notice with no body and a notice whose title needs the other layouts */
  later(10, function () { tap(nrow('n2').firstChild);
    ok('no body: "Sin más detalle." in the quiet colour and no quotes', $('tv-text').textContent === 'Sin más detalle.' && $('tv-said').classList.contains('none')
      && $('tv-na').textContent === 'hace 0:03'); });
  later(400, function () { tap($('tv-back')); });
  /* the page's own clock keeps the age going, and a repaint from the daemon updates what is open */
  later(10, function () {
    tap(nrow('n1').firstChild);
    show(function (v) { v.notices[0].age = 80; v.notices[0].body = 'Cambió.'; v.notices[0].title = 'Otro título'; v.notices[0].rev = 2; });
    ok('a changed notice repaints the open detail in place (title, body, age)', $('tv-name').textContent === 'Otro título' && $('tv-text').textContent === 'Cambió.' && $('tv-na').textContent === 'hace 1:20');
  });
  /* the notice leaves the board (expired, or Hermes withdrew it) while it is open: what was last known stays, its clock keeps counting */
  later(10, function () {
    show(function (v) { v.notices = [v.notices[1]]; });
    ok('the open detail survives its notice leaving, with the last text', reading() && $('tv-name').textContent === 'Otro título' && nrow('n1') === undefined);
  });
  later(5000, function () { ok('and its age kept counting by itself', /^hace 1:(2[4-9]|3[0-2])$/.test($('tv-na').textContent), $('tv-na').textContent); });
  later(400, function () { tap($('tv-back')); ok('Volver closes the snapshot', !reading()); });
  /* 25 s after the LAST touch it closes by itself, and the notice stays in the list */
  later(10, function () { tap(nrow('n2').firstChild); });
  later(20000, function () { $('tv-said').dispatchEvent(new Event('pointerdown', { bubbles: true })); ok('still open at 20 s', reading()); });
  later(24000, function () { ok('still open 24 s after the last touch', reading()); });
  later(2000, function () { ok('closed by itself 26 s after the last touch; the notice is still there and nothing was sent', !reading() && !!nrow('n2') && dismisses().length === 0
    && !tv().classList.contains('notice')); });
  later(10, function () { tap(nrow('n2').firstChild); });
  later(24000, function () { $('tv-said').dispatchEvent(new Event('scroll')); });
  later(24000, function () { ok('a scroll in the text counts as a touch', reading()); });
  later(2000, function () { ok('and then it closes', !reading()); });
"""


@needs_chromium
def test_the_detail_reads_a_notice_and_keeps_the_threads_guards_and_clock(tmp_path):
    assert_all(run_page(tmp_path, DETAIL_JS, budget=160000), 24)


TITLES_JS = r"""
  goLive();
  var titles = ['Listo', 'La tanda terminó', 'Reunión con Ana en 10 min', 'Resumen de la tanda nocturna de corridas del repositorio',
                'Resumen de la tanda nocturna de corridas del repositorio ok!', 'Ana escribió un mensaje largo sobre la factura de octubre y el cambio'.slice(0, 60)];
  titles.forEach(function (title) {
    put(V({ notices: [N('t', { title: title, body: 'Las cuatro corridas pasaron.\nEl resumen quedó en la bitácora.\nFalta la revisión de Ana.\nY el correo.' })] }));
    closeTask(); tap(nrow('t').firstChild);
    var nm = $('tv-name'), tier = tv().classList.contains('tall') ? 'tall' : tv().classList.contains('long') ? 'long' : 'one line';
    var inside = function (el) { var r = px(el), f = px($('feed')); return r.x >= f.x - .6 && r.x + r.w <= f.x + 432 - 24 + .6 && r.y >= f.y - .6; };
    ok(title.length + ' letters (' + tier + '): the whole title shows, nothing is cut', !clipped(nm)
      && cs(nm).fontSize === (tier === 'one line' ? '30px' : tier === 'long' ? '21px' : '19px'), tier + ' ' + nm.scrollHeight + '/' + nm.clientHeight);
    ok(title.length + ' letters: the buttons stay at 44 px, inside the bubble, at the top, and the title does not run under them', Math.abs(px($('tv-back')).h - 44) < .6 && Math.abs(px($('tv-done')).h - 44) < .6
      && inside($('tv-back')) && inside($('tv-done')) && px(nm).x + px(nm).w <= px($('tv-done')).x + .6 && Math.abs(px($('tv-done')).y - px($('tv-back')).y) < .6);
    ok(title.length + ' letters: the sub line is below the title and the text still has room (100 px)', px($('tv-na')).y >= px(nm).y + px(nm).h - 1 && px($('tv-said')).h >= 100
      && Math.abs(px(tv()).h - 246) < 1 && px($('tv-said')).y + px($('tv-said')).h <= px($('feed')).y + 284 + .6, px($('tv-said')).h);
    ok(title.length + ' letters: the row of the same notice does not overflow', (function () { closeTask(); var btn = nrow('t').firstChild; return btn.scrollWidth <= btn.clientWidth && Math.abs(px(btn).h - 44) < .6; })());
  });
  /* the longest source the daemon allows (24 letters) and the widest letters: the age is still read whole */
  put(V({ notices: [N('t', { title: 'WWWWWWWWWWWWWWWWWWWW', source: 'WWWWWWWWWWWWWWWWWWWWWWWW', body: 'x' })] }));
  tap(nrow('t').firstChild);
  ok('a 24 letter source gives way with an ellipsis and the age is never clipped', $('tv-na').scrollWidth <= $('tv-na').clientWidth && px($('tv-na')).x + px($('tv-na')).w <= px($('feed')).x + 432 - 24 + .6
    && $('tv-did').scrollWidth >= $('tv-did').clientWidth, px($('tv-na')).x + ' ' + px($('tv-na')).w);
  /* a thread's detail after a notice's is the thread's again, measured again */
  closeTask();
  put(V({ notices: [N('t', { title: 'Hola' })], tasks: [T('f1', { title: 'Hola', group: 'Buddy', tool: 'Bash', what: 'ls' })] }));
  tap(nrow('t').firstChild); closeTask(); tap(row('f1').firstChild);
  ok('a thread detail after a notice has no notice about it: no Listo, no third part, the thread\'s own sub line', reading() && !tv().classList.contains('notice') && cs($('tv-done')).display === 'none'
    && cs($('tv-na')).display === 'none' && $('tv-age').textContent === 'hace 0:01' && $('tv-did').textContent === 'Buddy · Bash ls' && Math.abs(px($('tv-back')).h - 44) < .6);
"""


@needs_chromium
def test_a_title_up_to_60_letters_is_never_cut_whatever_the_room_the_buttons_leave(tmp_path):
    assert_all(run_page(tmp_path, TITLES_JS), 26)


GUARDS_JS = r"""
  goLive();
  put(V({ notices: [N('g1', { title: 'Uno' }), N('g2', { title: 'Dos' })] }));
  tap(nrow('g1').firstChild);
  tap($('tv-done'));
  ok('Listo right after opening is a bounce: nothing is sent, the row stays, the detail stays', reading() && dismisses().length === 0 && !!nrow('g1'));
  later(200, function () { tap($('tv-done')); ok('still ignored at 200 ms', reading() && dismisses().length === 0 && !!nrow('g1')); });
  later(200, function () {
    tap($('tv-done'));
    ok('after the guard Listo sends one POST /dismiss and the row is gone at once', dismisses().length === 1 && !nrow('g1') && !!nrow('g2') && !reading());
    var c = dismisses()[0];
    ok('it is a POST with the token and a JSON body with the id', c.method === 'POST' && c.headers['Content-Type'] === 'application/json' && c.headers['X-Buddy-Token'] === TOKEN
      && JSON.stringify(JSON.parse(c.body)) === '{"id":"g1"}' && Object.keys(JSON.parse(c.body)).length === 1, c.body);
    ok('back on the list, with the other notice and the caption counting one', nseq() === 'notes|n:g2' && ncap().textContent === 'Avisos · 1' && cs(L()).display !== 'none', nseq());
    tap($('tv-done'));
    ok('a second Listo on a closed detail does nothing', dismisses().length === 1);
  });
  /* the daemon, not yet knowing, still lists it: it stays hidden; once the daemon drops it, the hiding is forgotten */
  later(10, function () {
    show(function (v) { v.notices = [N('g1', { title: 'Uno' }), N('g2', { title: 'Dos' })]; });
    ok('a view that still has the dismissed notice does not bring it back', !nrow('g1') && ncap().textContent === 'Avisos · 1' && noticeHidden.has('g1'));
    show(function (v) { v.notices = [N('g2', { title: 'Dos' })]; });
    ok('the daemon agrees: it is forgotten, not hidden any more', !noticeHidden.has('g1') && !nrow('g1'));
    show(function (v) { v.notices = [N('g1', { title: 'Uno otra vez' }), N('g2', { title: 'Dos' })]; });
    ok('the same id sent again later is a notice like any other', !!nrow('g1') && ttl(nrow('g1')).textContent === 'Uno otra vez');
  });
  /* the notice that comes back changed while it is hidden shows again (a new rev is news) */
  later(400, function () {
    tap(nrow('g1').firstChild);
  });
  later(400, function () {
    tap($('tv-done'));
    ok('dismissed again', !nrow('g1') && dismisses().length === 2);
    show(function (v) { v.notices = [N('g1', { title: 'Uno cambió', rev: 2 }), N('g2', { title: 'Dos' })]; });
    ok('a changed one (rev 2) while it was hidden comes back', !!nrow('g1') && ttl(nrow('g1')).textContent === 'Uno cambió' && !noticeHidden.has('g1'));
  });
  /* Listo never decides anything */
  later(10, function () { ok('nothing that Listo does is a /decide or a /wake', decides().length === 0 && wakes().length === 0 && calls.every(function (c) { return c.url === '/dismiss'; })); });
"""


@needs_chromium
def test_listo_waits_out_the_guard_posts_once_and_removes_the_row_at_once(tmp_path):
    assert_all(run_page(tmp_path, GUARDS_JS, budget=8000), 12)


LISTO_NET_JS = r"""
  goLive();
  var live1 = V({ notices: [N('k1', { title: 'Uno' }), N('k2', { title: 'Dos' }), N('k3', { title: 'Tres' })] });
  put(live1);
  /* an answer that never comes: the row is gone anyway (nothing waits) and the page is not stuck */
  fetchMode = 'hang';
  later(10, function () { tap(nrow('k1').firstChild); });
  later(400, function () {
    tap($('tv-done'));
    ok('with no answer yet the row is already gone and the list is back', dismisses().length === 1 && !nrow('k1') && !reading() && pending.length === 1);
    ok('and the page took no decision: one POST, to /dismiss, with the right id', JSON.parse(dismisses()[0].body).id === 'k1');
    pending[0]({ ok: true, status: 200 });
  });
  /* a refusal (rate limit, wrong token) or no connection: the notice is back, because it is what really happened */
  later(10, function () { fetchMode = 'busy'; tap(nrow('k2').firstChild); });
  later(400, function () { tap($('tv-done')); ok('removed at once, even if the daemon is about to say no', !nrow('k2')); });
  later(100, function () { ok('a 429 brings the row back and the notice is still the daemon\'s', !!nrow('k2') && !noticeHidden.has('k2') && ncap().textContent === 'Avisos · 2'); });
  later(10, function () { fetchMode = 'reject'; tap(nrow('k2').firstChild); });
  later(400, function () { tap($('tv-done')); });
  later(100, function () { ok('no connection brings it back too, with no unhandled rejection', !!nrow('k2') && unhandled === 0, unhandled); });
  later(10, function () { fetchMode = 'throw'; tap(nrow('k2').firstChild); });
  later(400, function () { tap($('tv-done')); ok('a fetch that throws at once brings it back at once', !!nrow('k2') && !reading()); });
  /* an answer that says ok changes nothing the daemon's next view does not say */
  later(10, function () { fetchMode = 'ok'; tap(nrow('k3').firstChild); });
  later(400, function () { tap($('tv-done')); });
  later(100, function () { ok('an ok answer leaves it gone', !nrow('k3') && noticeHidden.has('k3') && unhandled === 0); });
"""


@needs_chromium
def test_listo_never_waits_and_a_failed_dismiss_brings_the_row_back(tmp_path):
    assert_all(run_page(tmp_path, LISTO_NET_JS, budget=8000), 7)


LISTO_DEMO_JS = r"""
  ok('in a preview Listo removes the row and talks to nobody', (function () {
    tap(nrow('n-reunion').firstChild); return reading();
  })());
  later(400, function () {
    tap($('tv-done'));
    ok('gone, no network', !nrow('n-reunion') && !!nrow('n-correo') && !reading() && calls.length === 0 && ncap().textContent === 'Avisos · 1');
  });
  later(10, function () { demoShow('notices'); ok('and the next preview starts with every notice back', !!nrow('n-reunion') && !!nrow('n-correo') && ncap().textContent === 'Avisos · 2'); });
"""


@needs_chromium
def test_in_a_preview_listo_is_local_and_the_next_preview_starts_clean(tmp_path):
    assert_all(battery(tmp_path, HELPERS + LISTO_DEMO_JS, query="demo=notices", budget=3000), 3)


# --- the attention: a new or changed notice, and everything that stops it -------------------------------------------------

ATTN_JS = r"""
  goLive();
  var base = V({ mood: 'idle', notices: [N('a'), N('b')] });
  put(base);
  ok('the first view after a load never animates, even with notices already there', touchMood === '' && mood() === 'idle' && liveOf('touch') === 0, mood());
  show(function (v) { v.notices.forEach(function (n) { n.age += 1; }); });
  ok('time passing is not news', touchMood === '' && liveOf('touch') === 0);
  var said = truth(), pill = $('mood').textContent;
  show(function (v) { v.notices.unshift(N('c')); });
  ok('a new notice (an id never seen) makes Buddy show attention', touchMood === 'attention' && mood() === 'attention', mood());
  ok('through the touch machinery: one timer, armed once', liveOf('touch') === 1, liveOf('touch'));
  ok('the status text is untouched: the pill, the counters and the real mood say what the server says', truth() === said && $('mood').textContent === pill && document.body.getAttribute('data-real') === 'idle'
    && curMood === 'idle');
  ok('Buddy really changed look (the attention mouth is the one showing)', getComputedStyle(document.querySelector('#pet .m-attn')).display !== 'none' && getComputedStyle(document.querySelector('#pet .m-idle')).display === 'none');
  later(3000, function () { ok('still attentive 3 s later', mood() === 'attention' && touchMood === 'attention'); });
  later(3000, function () { ok('and back to the server mood after 4 s, the timer gone', touchMood === '' && mood() === 'idle' && liveOf('touch') === 0, mood()); });
  /* a changed notice (rev up) */
  later(10, function () { show(function (v) { v.notices[1].title = 'Cambiado'; v.notices[1].rev = 2; }); ok('a changed notice (rev up) animates too', touchMood === 'attention' && liveOf('touch') === 1); });
  later(6000, function () { ok('and ends', touchMood === '' && liveOf('touch') === 0); });
  later(10, function () { show(function (v) { v.notices[1].title = 'Otra vez'; }); ok('a text that changes with the same rev is not news', touchMood === ''); });
  later(10, function () { show(function (v) { v.notices[1].rev = 1; }); ok('a rev that goes down is not news either', touchMood === ''); });
  /* two at once, and a second arrival inside the 4 s restarts the 4 s (one timer) */
  later(10, function () { show(function (v) { v.notices = [N('p'), N('q'), N('c')]; }); ok('two arriving in one view are one animation and one timer', touchMood === 'attention' && liveOf('touch') === 1); });
  later(3000, function () { show(function (v) { v.notices = [N('r'), N('p'), N('q')]; }); ok('a third arrival restarts the 4 s with no second timer', touchMood === 'attention' && liveOf('touch') === 1); });
  later(3000, function () { ok('3 s after the restart it is still on', touchMood === 'attention'); });
  later(3000, function () { ok('and 6 s after it is over', touchMood === '' && liveOf('touch') === 0); });
  /* a notice that goes and comes back with the same id is new again */
  later(10, function () { show(function (v) { v.notices = []; }); ok('losing notices animates nothing', touchMood === ''); });
  later(10, function () { show(function (v) { v.notices = [N('p')]; }); ok('the same id after it left is a new notice', touchMood === 'attention'); });
  later(6000, function () { ok('over', touchMood === ''); });
  /* the server's own attention: Buddy is already attentive, nothing to override */
  later(10, function () { show(function (v) { v.mood = 'attention'; v.sessions.waiting = 1; v.notices = [N('p'), N('z')]; }); ok('when the server mood is attention the notice sets no override', touchMood === '' && liveOf('touch') === 0 && mood() === 'attention'); });
  later(10, function () { show(function (v) { v.mood = 'idle'; v.sessions.waiting = 0; }); });
  /* the page's own touches: attention is not for playing */
  later(10, function () { show(function (v) { v.notices.push(N('t1')); v.notices.shift(); }); ok('armed again', touchMood === 'attention'); });
  later(100, function () {
    var before = pingMark();
    tapOn(Z);
    ok('a tap on Buddy while he is attentive for a notice does not play: no heart, same timer, the finger gets the echo', touchMood === 'attention' && mood() === 'attention' && liveOf('touch') === 1
      && pingMark() !== before && touchTaps.length === 0, touchMood);
  });
  later(6000, function () { ok('and it still ends on time', touchMood === '' && mood() === 'idle'); });
  /* a heart from a tap gives way to a notice */
  later(10, function () { tapOn(Z); ok('a tap gives a heart', touchMood === 'heart'); show(function (v) { v.notices.push(N('t2')); v.notices.shift(); }); ok('a notice takes over from the heart', touchMood === 'attention' && liveOf('touch') === 1); });
  later(6000, function () { ok('and the server mood is what is left', touchMood === '' && mood() === 'idle' && liveOf('touch') === 0); });
"""


@needs_chromium
def test_a_new_or_changed_notice_makes_buddy_attentive_for_4_s_and_never_on_a_first_view(tmp_path):
    assert_all(run_page(tmp_path, ATTN_JS, budget=80000), 24)


ATTN_BLOCKED_JS = r"""
  goLive();
  /* a permission wins: the notice arrives with it, or while the animation runs */
  put(V({ mood: 'idle', notices: [N('a')] }));
  show(function (v) { v.mood = 'attention'; v.approvals = [{ key: 'claude:r1', tool: 'Bash', hint: 'ls', source: 'claude' }]; v.notices.unshift(N('b')); });
  ok('a notice that arrives with a permission does not animate: the permission is the screen', screenOf() === 'ask' && touchMood === '' && liveOf('touch') === 0);
  ok('the list is not shown and the notice waits', cs($('s-main')).display === 'none' && cs($('s-ask')).display !== 'none');
  show(function (v) { v.mood = 'idle'; v.approvals = []; });
  ok('when the permission is decided the notice is there, still not an animation (it was seen)', screenOf() === 'main' && !!nrow('b') && touchMood === '', touchMood);
  show(function (v) { v.notices.unshift(N('c')); });
  ok('armed again', touchMood === 'attention' && liveOf('touch') === 1);
  show(function (v) { v.mood = 'attention'; v.approvals = [{ key: 'claude:r2', tool: 'Edit', hint: 'x.py', source: 'claude' }]; });
  ok('a permission cancels the notice attention at once, with its timer', touchMood === '' && liveOf('touch') === 0 && screenOf() === 'ask');
  ok('and the buttons are asleep for their own 600 ms, untouched by any of this', $('btns').className.indexOf('hold') >= 0);
  later(700, function () { ok('and awake after', $('btns').className.indexOf('hold') < 0); show(function (v) { v.mood = 'idle'; v.approvals = []; }); });
  /* the detail of a notice with a permission arriving */
  later(10, function () {
    show(function (v) { v.notices = [N('c'), N('d')]; });
    tap(nrow('d').firstChild);
    ok('open', reading() && tv().classList.contains('notice'));
    show(function (v) { v.mood = 'attention'; v.approvals = [{ key: 'claude:r3', tool: 'Bash', hint: 'ls', source: 'claude' }]; });
    ok('a permission closes the detail and the ask screen is the screen', !reading() && screenOf() === 'ask' && !tv().classList.contains('notice') && openNoteId === '');
    tap($('tv-done'));
    ok('a late tap on Listo does nothing', dismisses().length === 0 && decides().length === 0);
    tap(nrow('d').firstChild);
    ok('nor does a late tap on the row (the click that follows a finger that was already down)', !reading() && screenOf() === 'ask');
  });
  later(700, function () {
    show(function (v) { v.mood = 'idle'; v.approvals = []; });
    ok('when it is decided the list is back, not a stale detail', screenOf() === 'main' && !reading() && cs(L()).display !== 'none' && cs(tv()).display === 'none' && !!nrow('d'));
  });
  /* the screen guard on its own: a view with a permission whose mood is not "attention" (the daemon always says attention, the page does not rely on it) */
  later(10, function () {
    show(function (v) { v.mood = 'idle'; v.approvals = [{ key: 'claude:r5', tool: 'Bash', hint: 'ls', source: 'claude' }]; v.notices = [N('zz')].concat(v.notices); v.notices.pop(); });
    ok('a notice that arrives with a permission does not animate even if the view\'s mood is not attention', screenOf() === 'ask' && touchMood === '' && liveOf('touch') === 0);
    show(function (v) { v.approvals = []; });
  });
  /* a permission that is on screen while a notice changes: nothing animates, the notice waits */
  later(10, function () {
    show(function (v) { v.mood = 'attention'; v.approvals = [{ key: 'claude:r4', tool: 'Bash', hint: 'ls', source: 'claude' }]; });
    show(function (v) { var c = v.notices.filter(function (n) { return n.id === 'c'; })[0]; c.rev = 9; c.title = 'Cambió con un permiso'; });
    ok('a notice that changes under a permission does not animate', touchMood === '' && liveOf('touch') === 0);
    show(function (v) { v.mood = 'idle'; v.approvals = []; });
    ok('and is as the daemon said when the permission is gone', ttl(nrow('c')).textContent === 'Cambió con un permiso' && touchMood === '');
  });
"""


@needs_chromium
def test_a_permission_wins_over_a_notice_in_the_list_the_detail_and_the_attention(tmp_path):
    assert_all(run_page(tmp_path, ATTN_BLOCKED_JS, budget=8000), 15)


ATTN_HIDDEN_JS = r"""
  goLive();
  /* the passkey is on screen: the list is not, so a notice that arrives waits, unseen, and announces itself when the list is back */
  put(V({ mood: 'idle', notices: [N('a')] }));
  show(function (v) { v.pairing = { code: '482197', entered: 0 }; });
  ok('the pairing screen is up', screenOf() === 'pair');
  show(function (v) { v.notices.unshift(N('b')); });
  ok('a notice that arrives under the pairing code animates nothing: Buddy is not attentive to a list nobody can see', screenOf() === 'pair' && touchMood === '' && liveOf('touch') === 0 && mood() === 'idle', mood());
  show(function (v) { v.pairing = null; });
  ok('when the list is back the notice announces itself: its news was not used up', screenOf() === 'main' && !!nrow('b') && touchMood === 'attention' && liveOf('touch') === 1, touchMood);
  later(6000, function () { ok('and it ends on time', touchMood === '' && liveOf('touch') === 0 && mood() === 'idle'); });
  /* a notice that was seen stays seen through a pairing, and a changed one (rev up) under the code is news when the list returns */
  later(10, function () {
    show(function (v) { v.pairing = { code: '482197', entered: 0 }; });
    show(function (v) { v.pairing = null; });
    ok('a pairing with nothing new animates nothing when it ends', screenOf() === 'main' && touchMood === '');
    show(function (v) { v.pairing = { code: '482197', entered: 0 }; });
    show(function (v) { v.notices[0].title = 'Cambiado'; v.notices[0].rev = 2; });
    ok('a change under the code animates nothing yet', touchMood === '' && liveOf('touch') === 0);
    show(function (v) { v.pairing = null; });
    ok('and is announced when the list is back', touchMood === 'attention' && liveOf('touch') === 1);
  });
  later(6000, function () { ok('over', touchMood === ''); });
  /* the notices that were already there when the page loaded are no news after the pairing either */
  later(10, function () {
    reborn();
    put(V({ mood: 'idle', pairing: { code: '482197', entered: 0 }, notices: [N('a')] }));
    show(function (v) { v.pairing = null; });
    ok('a first view under the pairing code is a first view: nothing animates when it ends', screenOf() === 'main' && !!nrow('a') && touchMood === '' && liveOf('touch') === 0);
  });
  /* a Bluetooth that is down: the notice is on the main screen, so it is news right away */
  later(10, function () {
    reborn();
    put(V({ mood: 'idle', notices: [] }));
    show(function (v) { v.ble.ready = false; v.ble.reason = 'rfkill'; });
    ok('the Bluetooth error screen is up', screenOf() === 'down');
    show(function (v) { v.notices = [N('x')]; });
    ok('a notice that arrives with Bluetooth down is on the screen and makes Buddy attentive, like any other', screenOf() === 'main' && !!nrow('x') && touchMood === 'attention' && mood() === 'attention', screenOf() + ' ' + mood());
  });
  later(6000, function () { ok('and it ends', touchMood === '' && mood() === 'idle'); });
"""


@needs_chromium
def test_a_notice_does_not_animate_buddy_under_the_pairing_code_and_does_when_the_list_is_back(tmp_path):
    assert_all(run_page(tmp_path, ATTN_HIDDEN_JS, budget=40000), 11)


ATTN_DARK_JS = r"""
  goLive();
  var night = function (o) { return V(Object.assign({ night: true, dim: true, backlight: true, mood: 'idle' }, o)); };
  put(night({ notices: [] }));
  ok('it is dark: the layer is on and catches taps', D.classList.contains('on') && cs(D).pointerEvents === 'auto');
  show(function (v) { v.notices = [N('a')]; });
  ok('a notice that arrives while the screen is dark does not animate', touchMood === '' && liveOf('touch') === 0 && mood() === 'idle');
  ok('it waits in the list for when the screen wakes', !!nrow('a') && ncap().textContent === 'Avisos · 1');
  show(function (v) { v.dim = false; });
  ok('and when it later wakes (the daemon says so) it does not animate either', touchMood === '' && !D.classList.contains('dark'));
  show(function (v) { v.dim = true; v.notices = [N('a'), N('b')]; });
  ok('dark again: a second one does not animate', touchMood === '');
  /* a tap on #dim wakes the page locally for 30 s: the screen is visible, so a notice is news */
  tapOn(D);
  ok('a tap on the dark screen wakes it (the layer consumed it) and sent only a /wake', nightAwakeUntil > clock && wakes().length === 1 && dismisses().length === 0 && !reading());
  show(function (v) { v.notices.push(N('c')); v.notices.shift(); });
  ok('awake by a tap, a notice that arrives is news', touchMood === 'attention', touchMood);
  later(6000, function () { ok('and it ends', touchMood === ''); });
  /* the daemon wakes it for the notice (wake: true): the view that wakes carries the notice with dim already false */
  later(10, function () {
    reborn();
    put(night({ notices: [] }));
    show(function (v) { v.dim = false; v.notices = [N('w')]; });
    ok('a notice that comes with the wake (dim false in the same view) is news', touchMood === 'attention');
  });
  later(6000, function () { ok('over', touchMood === ''); });
"""


@needs_chromium
def test_a_notice_that_arrives_while_the_screen_is_dark_does_not_animate_when_it_wakes(tmp_path):
    assert_all(run_page(tmp_path, ATTN_DARK_JS, budget=30000), 10)


DIM_TOUCH_JS = r"""
  goLive();
  put(V({ night: true, dim: true, backlight: true, mood: 'idle', notices: [N('a', { title: 'Uno' }), N('b', { title: 'Dos' })], tasks: [T('t1', { title: 'Hilo' })] }));
  ok('dark: every row of the list, the caption and Buddy are under the layer', [nrow('a').firstChild, nrow('b').firstChild, ncap(), row('t1').firstChild].every(function (el) { return document.elementFromPoint.apply(document, middle(el)) === D; }));
  /* a click that slips through anyway (a synthetic one on the row itself) opens nothing and sends nothing */
  tap(nrow('a').firstChild);
  ok('a click that reaches a notice row while dark opens nothing', !reading() && openNoteId === '');
  openNotice('a');
  ok('not even by the function itself', !reading() && openNoteId === '');
  /* wake it, open one, and let it go dark under the open detail */
  tapOn(D);
  ok('awake by a tap', !D.classList.contains('dark') || nightAwakeUntil > clock);
  later(1000, function () {
    ok('the layer lets go of the screen after its guard', !D.classList.contains('on'));
    tap(nrow('a').firstChild);
    ok('awake: the row opens its detail', reading() && openNoteId === 'a');
  });
  later(500, function () {
    show(function (v) { v.dim = true; });
    nightAwakeUntil = 0; paintDim();
    ok('the daemon says dim and the local wake is over: the layer is back over the detail', D.classList.contains('on'));
    tap($('tv-done'));
    ok('a Listo that slips through a dark screen sends nothing and hides nothing', dismisses().length === 0 && !!nrow('a') && reading());
  });
"""


@needs_chromium
def test_while_the_screen_is_dim_nothing_about_a_notice_answers_a_touch(tmp_path):
    assert_all(run_page(tmp_path, DIM_TOUCH_JS, budget=8000), 6)


# --- the screen picked: neither the Mac asleep nor a Bluetooth that is down may hide a notice -----------------------------------

SCREENS_JS = r"""
  goLive();
  put(V({ linked: false, mood: 'sleep', sessions: { total: 0, running: 0, waiting: 0, msg: '', entries: [], tokens_today: 0 } }));
  ok('a Mac that is not linked and no notice: the "waiting" screen, as always', screenOf() === 'wait');
  put(V({ linked: false, mood: 'sleep', notices: [N('a', { title: 'Reunión en 10 min', kind: 'warn' })], sessions: { total: 0, running: 0, waiting: 0, msg: '', entries: [], tokens_today: 0 } }));
  ok('with a notice the main screen shows it (Hermes does not need the Mac)', screenOf() === 'main' && !!nrow('a') && $('feed-msg').textContent === 'Un aviso' && curMood === 'sleep', screenOf());
  ok('the status is as honest as ever: the pill says sleeping, no counters invented', $('mood').textContent === 'Durmiendo' && $('n-total').textContent === '0');
  tap(nrow('a').firstChild);
  ok('and it opens and reads like any other', reading() && $('tv-name').textContent === 'Reunión en 10 min');
  closeTask();
  show(function (v) { v.notices = []; });
  ok('when it is gone the waiting screen is back', screenOf() === 'wait' && !reading());
  show(function (v) { v.notices = [N('a')]; v.ble.ready = false; v.ble.reason = 'el adaptador hci0 desapareció'; });
  ok('a Bluetooth that is down does not hide a notice either (Hermes talks over the network): the list is on screen', screenOf() === 'main' && !!nrow('a')
    && $('feed-msg').textContent === 'Un aviso' && cs($('s-main')).display !== 'none' && cs($('s-down')).display === 'none', screenOf());
  ok('and the fault is still said, in red, in the top bar', $('net').getAttribute('data-state') === 'down');
  tap(nrow('a').firstChild);
  ok('the notice opens and reads like any other', reading() && $('tv-name').textContent === 'Aviso a');
  closeTask();
  show(function (v) { v.notices = []; });
  ok('with no notice left the Bluetooth error screen is back', screenOf() === 'down' && cs($('s-down')).display !== 'none' && !reading());
  show(function (v) { v.notices = [N('a')]; v.ble.ready = true; v.ble.reason = ''; v.pairing = { code: '482197', entered: 0 }; });
  ok('a pairing code is on screen before anything else', screenOf() === 'pair');
  show(function (v) { v.pairing = null; v.approvals = [{ key: 'claude:r1', tool: 'Bash', hint: 'ls', source: 'claude' }]; });
  ok('a permission, before anything', screenOf() === 'ask');
  show(function (v) { v.approvals = []; });
  ok('and the notice is there when they are done', screenOf() === 'main' && !!nrow('a'));
  /* the old screens do not change when there are no notices (an old daemon sends no list at all) */
  put(V({ linked: false, mood: 'sleep', notices: undefined }));
  ok('a daemon that sends no notices field is a daemon with none', screenOf() === 'wait');
"""


@needs_chromium
def test_a_notice_is_not_hidden_behind_the_waiting_or_the_bluetooth_screen_but_pair_and_ask_still_win(tmp_path):
    assert_all(run_page(tmp_path, SCREENS_JS, budget=3000), 13)


# --- pack mode, the demos and the old demos ------------------------------------------------------------------------------------

PACK_JS = r"""
  ok('pack mode is on and the notices are drawn', document.body.classList.contains('pack') && nrows().length === 3 && ncap().textContent === 'Avisos · 3');
  PACK_CHECKS
"""

PACK_CHECKS = r"""
  var bubble = cs($('feed')).backgroundColor;
  var tint = function (li) { return rgba(cs(li.firstChild).backgroundColor); };
  ok('the three kinds are still three tints, all real', new Set(nrows().map(function (li) { return cs(li.firstChild).backgroundColor; })).size >= 2 && nrows().every(function (li) { return tint(li).a > .15; })
    && cs(nrow('n-tanda').firstChild).backgroundColor !== cs(nrow('n-reunion').firstChild).backgroundColor);
  ok('titles read on the pack bubble (7:1)', nrows().every(function (li) { return contrast(cs(ttl(li)).color, over(tint(li), rgba(bubble))) >= 7; }),
    nrows().map(function (li) { return contrast(cs(ttl(li)).color, over(tint(li), rgba(bubble))).toFixed(1); }).join(','));
  ok('age, bell and marks read (4.5:1)', nrows().every(function (li) {
    var base = over(tint(li), rgba(bubble));
    return contrast(cs(agel(li)).color, base) >= 4.5 && contrast(cs(bell(li)).color, base) >= 4.5 && contrast(cs(okMark(li)).color, base) >= 4.5 && contrast(cs(warnMark(li)).color, base) >= 4.5;
  }));
  ok('the caption reads (4.5:1)', contrast(cs(ncap()).color, bubble) >= 4.5);
  tap(nrow('n-tanda').firstChild);
  ok('the detail opens in pack mode', reading() && Math.abs(px($('tv-back')).h - 44) < .6 && Math.abs(px($('tv-done')).h - 44) < .6);
  ok('Volver is visible against the bubble', rgba(cs($('tv-back')).backgroundColor).a > .1 && contrast(cs($('tv-back')).color, over(rgba(cs($('tv-back')).backgroundColor), rgba(bubble))) >= 4.5);
  ok('Listo keeps dark text on its own green (7:1), whatever the pack says the ink is', contrast(cs($('tv-done')).color, cs($('tv-done')).backgroundColor) >= 7, contrast(cs($('tv-done')).color, cs($('tv-done')).backgroundColor));
  ok('the body, the sub line and the title read on the bubble (7:1, 4.5:1, 7:1)', contrast(cs($('tv-text')).color, bubble) >= 7 && contrast(cs($('tv-did')).color, bubble) >= 4.5 && contrast(cs($('tv-name')).color, bubble) >= 7);
  ok('the quiet body of a notice with no more to say reads too', (function () { closeTask(); tap(nrow('n-reunion').firstChild); return contrast(cs($('tv-text')).color, bubble) >= 4.5; })());
"""


@needs_chromium
def test_pack_mode_keeps_the_notices_and_their_detail_legible(tmp_path):
    assert_all(battery(tmp_path, HELPERS + PACK_JS.replace("PACK_CHECKS", PACK_CHECKS), query="demo=notices-pack", budget=3000), 9)


# the pack that is really on the Pi (characters/buddy-pixel): its own colours, not the demo's black and white
REAL_PACK_JS = r"""
  goLive();
  put(V({ mood: 'idle', character: { name: 'buddy', rev: 'abcdef012345', render: 'pixel', states: {}, colors: { bg: '#0E0D14', text: '#F3EEF7', textDim: '#9A93A4', body: '#FF9E7A' } },
          notices: [N('n-tanda', { title: 'La tanda terminó', kind: 'ok', body: 'x' }), N('n-reunion', { title: 'Reunión con Ana en 10 min', kind: 'warn' }), N('n-correo', { title: 'Ana escribió' })] }));
  ok('the real pack is on and the notices are drawn', document.body.classList.contains('pack') && nrows().length === 3);
  PACK_CHECKS
"""


@needs_chromium
def test_the_pack_on_the_pi_keeps_the_notices_and_their_detail_legible_too(tmp_path):
    assert_all(run_page(tmp_path, REAL_PACK_JS.replace("PACK_CHECKS", PACK_CHECKS), budget=3000), 9)


ALL_NOTICE_DEMOS_JS = r"""
  var seen = 0;
  ['notices', 'notices-pack', 'notices-only'].forEach(function (name) {
    try {
      demoShow(name);
      var v = DEMOS[name], n = v.notices.length;
      ok(name + ': renders the main screen and does not animate on its first view', screenOf() === 'main' && touchMood === '' && liveOf('touch') === 0, screenOf());
      ok(name + ': the caption counts the notices and every one has its row', ncap().textContent === 'Avisos · ' + n && nrows().length === n);
      var extra = (v.tasks ? v.tasks.length : 0) > 0;
      var R = rowsOnly().length + n, G = captions().length;
      ok(name + ': the list measures what the formula says (44R + 10(R+G-1) + 10G + 18)', L().scrollHeight === 44 * R + 10 * (R + G - 1) + 10 * G + 18, L().scrollHeight);
      nrows().forEach(function (li) {
        var label = ttl(li).textContent;
        L().scrollTop = 0;
        tap(li.firstChild);
        var nm = $('tv-name');
        var sound = reading() && nm.textContent === label && !clipped(nm) && Math.abs(px($('tv-back')).h - 44) < .6 && Math.abs(px($('tv-done')).h - 44) < .6
          && px($('tv-said')).h >= 100 && Math.abs(px(tv()).h - 246) < 1 && px($('tv-done')).x + px($('tv-done')).w <= px($('feed')).x + 432 - 24 + .6;
        ok(name + ': the detail of "' + label + '" is sound', sound, px(nm).h + ' ' + px($('tv-said')).h + ' ' + px(tv()).h);
        closeTask();
        seen += 1;
      });
      if (extra) { tap(rowsOnly()[0].firstChild); ok(name + ': a thread still opens its own detail after all that', reading() && !tv().classList.contains('notice')); closeTask(); }
    } catch (e) { ok(name + ' threw: ' + e.message, false); }
  });
  ok('every notice row was opened', seen === 8, seen);
  /* the cycle: each preview starts clean and never animates its first view, whatever the one before did */
  demoShow('notices-only'); tap(nrow('n-correo').firstChild); tapOn(Z);
  demoShow('notices');
  ok('the next preview has no detail and no animation left from the last', !reading() && touchMood === '' && !tv().classList.contains('notice') && nrows().length === 2);
  ok('all three are in the cycle', ['notices', 'notices-pack', 'notices-only'].every(function (nme) { return demoNames.indexOf(nme) >= 0; }));
"""


@needs_chromium
def test_every_notice_demo_measures_right_and_every_row_opens_a_sound_detail(tmp_path):
    assert_all(battery(tmp_path, HELPERS + ALL_NOTICE_DEMOS_JS, query="demo=notices", budget=4000), 20)


OLD_DEMOS_JS = r"""
  var expected = EXPECTED;
  Object.keys(expected).forEach(function (name) {
    try {
      demoShow(name);
      ok(name + ': the same screen as before (' + expected[name] + ')', screenOf() === expected[name], screenOf());
      ok(name + ': no notice caption, no notice row, nothing animates', ncap() === undefined && nrows().length === 0 && touchMood === '' && !tv().classList.contains('notice')
        && cs($('tv-done')).display === 'none');
    } catch (e) { ok(name + ' threw: ' + e.message, false); }
  });
  demoShow('threads');
  ok('the thread list is exactly what it was: three captions, five rows and nothing else', nseq() === 'g:Acme Studio|f1|f2|g:Buddy|b1|b2|g:Terminal|t1' && $('feed-msg').textContent === '3 tareas en marcha');
"""


@needs_chromium
def test_every_demo_the_page_had_before_the_notices_renders_as_it_did(tmp_path):
    body = HELPERS + OLD_DEMOS_JS.replace("EXPECTED", json.dumps(OLD_DEMOS))
    assert_all(battery(tmp_path, body, query="demo=idle", budget=3000), 2 * len(OLD_DEMOS) + 1)


@needs_chromium
def test_the_notice_demos_render_without_a_daemon():
    dom = dump(f"{PAGE.as_uri()}?demo=notices&tag=0")
    assert attr(dom, "data-screen") == "main"
    listing = re.search(r'<ul id="feed-list"[^>]*>(.*?)</ul>', dom, re.S).group(1)
    assert re.search(r'<li class="grp" data-notes="">Avisos [^<0-9]{1,8} 2</li>', listing)  # "Avisos · 2", first
    assert listing.index('data-notes=""') < listing.index('data-n="n-reunion"') < listing.index('data-n="n-correo"') < listing.index('data-g="Acme Studio"')
    assert len(re.findall(r'<li class="notice', listing)) == 2 and len(re.findall(r'<li class="task', listing)) == 5
    assert "Reuni" in listing and "Ana escribi" in listing and "1:35" in listing and "3:00" in listing
    assert re.search(r'id="feed-msg"[^>]*>3 tareas en marcha<', dom)
    assert not re.search(r'<body[^>]*\bclass="[^"]*\bpack\b', dom)
    dom = dump(f"{PAGE.as_uri()}?demo=notices-only&tag=0")
    assert attr(dom, "data-screen") == "main"
    assert re.search(r'id="feed-msg"[^>]*>3 avisos<', dom)
    listing = re.search(r'<ul id="feed-list"[^>]*>(.*?)</ul>', dom, re.S).group(1)
    assert len(re.findall(r'<li class="notice', listing)) == 3 and 'class="task' not in listing and "data-id" not in listing
    assert re.search(r'<li class="notice ok" data-n="n-tanda">', listing) and re.search(r'<li class="notice warn" data-n="n-reunion">', listing)
    assert re.search(r'<li class="notice" data-n="n-correo">', listing)
    dom = dump(f"{PAGE.as_uri()}?demo=notices-pack&tag=0")
    assert attr(dom, "data-screen") == "main" and re.search(r'<body[^>]*\bclass="[^"]*\bpack\b', dom)
    assert len(re.findall(r'<li class="notice', re.search(r'<ul id="feed-list"[^>]*>(.*?)</ul>', dom, re.S).group(1))) == 3


@needs_chromium
def test_the_open_flag_opens_a_notice_by_id_or_by_row_and_a_thread_after_the_notices():
    dom = dump(f"{PAGE.as_uri()}?demo=notices&open=n-correo&tag=0")
    assert re.search(r'<div id="feed" class="[^"]*\breading\b', dom) and re.search(r'<div id="task-view" class="[^"]*\bnotice\b', dom)
    assert re.search(r'<div class="tv-name" id="tv-name">Ana escribi', dom)
    assert re.search(r'id="tv-age">Aviso<', dom) and re.search(r'id="tv-did">de hermes<', dom) and re.search(r'id="tv-na">hace 3:00<', dom)
    assert "Pregunta si el logo" in dom
    dom = dump(f"{PAGE.as_uri()}?demo=notices&open=0&tag=0")
    assert re.search(r'<div class="tv-name" id="tv-name">Reuni', dom)  # the first row of the list is the first notice
    dom = dump(f"{PAGE.as_uri()}?demo=notices&open=2&tag=0")
    assert not re.search(r'<div id="task-view" class="[^"]*\bnotice\b', dom)
    assert re.search(r'<div class="tv-name" id="tv-name">Cat[^<]*web<', dom)  # row 2 is the first thread: the two notices come first
    dom = dump(f"{PAGE.as_uri()}?demo=notices-only&open=1&tag=0")
    assert re.search(r'<div class="tv-name" id="tv-name">Reuni', dom)
    dom = dump(f"{PAGE.as_uri()}?demo=notices-only&open=9&tag=0")
    assert not re.search(r'<div id="feed" class="[^"]*\breading\b', dom)
    dom = dump(f"{PAGE.as_uri()}?demo=threads&open=1&tag=0")
    assert re.search(r'<div class="tv-name" id="tv-name">Respaldos del servidor<', dom)  # a demo with no notices counts rows as it always did


# --- the real data path: the board, the view, the page and POST /dismiss -----------------------------------------------------


def _live_page(tmp_path, body):
    """A copy of the page with a self-test injected, to be SERVED by a real UIServer (page=...), so its fetches reach the real daemon code."""
    page = PAGE.read_text(encoding="utf-8")
    tail = "})();\n</script>"
    assert page.count(tail) == 1
    copy = tmp_path / "live-page.html"
    copy.write_text(page.replace(tail, PRELUDE + body + FINISH + tail), encoding="utf-8")
    return copy


def _results(dom):
    found = re.search(r'<pre id="selftest">(.*?)</pre>', dom, re.S)
    assert found, "the self-test never reported; the DOM ended with:\n" + dom[-700:]
    return [tuple(item) for item in json.loads(html.unescape(found.group(1)))]


@needs_chromium
def test_a_live_screen_draws_the_board_the_way_the_view_sends_it(rig):
    async def scenario():
        rig.rx(heartbeat())
        board = rig.core.notices
        board.add(title="La tanda terminó", body="Pasaron las cuatro.\nQuedó el resumen.", kind="ok", notice_id="tanda")
        rig.t += 60
        board.add(title="<b>Ana</b> escribió", kind="warn", source="server", notice_id="ana")
        rig.t += 5
        async with serving(rig) as server:
            return await asyncio.get_running_loop().run_in_executor(None, dump, server.url + "/?poll=300", 2500)

    dom = run(scenario)
    assert attr(dom, "data-screen") == "main"
    listing = re.search(r'<ul id="feed-list"[^>]*>(.*?)</ul>', dom, re.S).group(1)
    seen = re.findall(r'<li class="([^"]*)" data-(?:g|id|n|notes)="([^"]*)"', listing)
    assert [name for _, name in seen] == ["", "ana", "tanda"]  # the caption, then the daemon's order: the one sent last first
    assert re.search(r'<li class="grp" data-notes="">Avisos [^<0-9]{1,8} 2</li>', listing)
    classes = dict((name, cls.split()) for cls, name in seen)
    assert classes["ana"] == ["notice", "warn"] and classes["tanda"] == ["notice", "ok"]
    assert "&lt;b&gt;Ana&lt;/b&gt; escribi" in listing and "<b>Ana</b>" not in listing  # a title is text
    assert "<i>0:05</i>" in listing and "<i>1:05</i>" in listing  # how long ago each one was sent
    assert re.search(r'id="feed-msg"[^>]*>2 avisos<', dom)


@needs_chromium
def test_a_live_screen_with_no_mac_still_shows_a_notice_and_goes_back_to_waiting_without_one(rig):
    async def scenario():
        async with serving(rig) as server:
            loop = asyncio.get_running_loop()
            first = await loop.run_in_executor(None, dump, server.url + "/?poll=300", 2500)
            rig.core.notices.add(title="Reunión en 10 min", kind="warn", notice_id="m1")
            second = await loop.run_in_executor(None, dump, server.url + "/?poll=300", 2500)
            rig.core.notices.withdraw("m1")
            third = await loop.run_in_executor(None, dump, server.url + "/?poll=300", 2500)
            return first, second, third

    first, second, third = run(scenario)
    assert attr(first, "data-screen") == "wait" and attr(third, "data-screen") == "wait"  # the Mac is not linked: the instructions, as always
    assert attr(second, "data-screen") == "main" and 'data-n="m1"' in second and re.search(r'id="feed-msg"[^>]*>Un aviso<', second)
    assert "data-n=" not in re.sub(r"<template.*?</template>", "", third, flags=re.S)


LIVE_LISTO_JS = r"""
  function L() { return $('feed-list'); }
  function nrow(id) { return Array.prototype.slice.call(L().children).filter(function (el) { return el.getAttribute('data-n') === id; })[0]; }
  /* the guard of Listo is judged on performance.now(): the test moves that clock by hand (a slow Chromium would otherwise decide it) */
  var realNow = performance.now.bind(performance), skew = 0;
  performance.now = function () { return realNow() + skew; };
  step(1200, function () {
    ok('the page got both notices from the real daemon, through its own poll', !!nrow('tanda') && !!nrow('ana') && L().querySelector('[data-notes]').textContent === 'Avisos · 2');
    tap(nrow('tanda').firstChild);
    ok('a tap opens it, with what the board holds', reading() && $('tv-name').textContent === 'La tanda terminó' && $('tv-text').textContent === 'Pasaron las cuatro.\nQuedó el resumen.');
  });
  step(600, function () {
    skew += 500;
    tap($('tv-done'));
    ok('Listo takes the row away at once', !nrow('tanda') && !!nrow('ana') && !$('feed').classList.contains('reading'));
  });
  step(2000, function () {
    ok('and two seconds of the real daemon later it still agrees: gone, and the page has forgotten hiding it', !nrow('tanda') && !!nrow('ana') && noticeHidden.size === 0);
    ok('the daemon\'s own view no longer lists it', view.notices.length === 1 && view.notices[0].id === 'ana', JSON.stringify(view.notices));
  });
  function reading() { return $('feed').classList.contains('reading'); }
"""


@needs_chromium
def test_listo_on_a_live_screen_reaches_the_real_dismiss_route_and_the_board_agrees(rig, tmp_path):
    async def scenario():
        rig.rx(heartbeat())
        rig.core.notices.add(title="La tanda terminó", body="Pasaron las cuatro.\nQuedó el resumen.", kind="ok", notice_id="tanda")
        rig.core.notices.add(title="Ana escribió", notice_id="ana")
        page = _live_page(tmp_path, LIVE_LISTO_JS)
        async with serving(rig, page=page) as server:
            dom = await asyncio.get_running_loop().run_in_executor(None, dump, server.url + "/?poll=300", 6000)
        return dom

    results = _results(run(scenario))
    assert len(results) >= 5
    assert not [(name, extra) for name, good, extra in results if not good], results
    assert rig.core.notices.state("tanda") == "dismissed" and rig.core.notices.state("ana") == "shown"
