"""The thread list on the screen: the page's rows, group captions, children, detail and its rules, and the runs
(the census) that sit under "Corridas" beside the threads.

Most of these run the page for real in a headless Chromium (they skip when there is none). Page self-tests are
injected into a temporary COPY of ui/index.html, inside the page's own script, so they can call render() with any
view they like; nothing is ever written to the repo. The page runs on virtual time, so a 25 s auto close is seen in a
fraction of a second."""
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
from test_ui import PAGE, Rig, heartbeat, run, serving  # noqa: E402

DEMO_THREADS = ("idle", "tasks1", "tasks6", "longsaid", "idle-pack", "threads", "threads-long", "threads8", "threads-pack")
DEMO_RUNS = ("runs", "runs1", "runs-pack")
# every demo the page had before the runs came; each must still be there and still pick the same screen
OLD_DEMOS = {
    "boot": "boot", "starting": "down", "notready": "down", "waiting": "wait", "pairing": "pair", "pairing-half": "pair",
    "idle": "main", "tasks1": "main", "tasks6": "main", "longsaid": "main", "idle-pack": "main", "threads": "main",
    "threads-long": "main", "threads8": "main", "threads-pack": "main", "busy": "main", "attention": "main",
    "celebrate": "main", "dizzy": "main", "heart": "main", "asleep": "main", "approval": "ask", "approval-long": "ask",
    "approval-queue": "ask", "approval-hermes": "ask", "approval-cli": "ask", "approval-cli-always": "ask",
}


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path / "state")


# --- the self-test harness --------------------------------------------------------------------------------------

PRELUDE = r"""
(function () {
  var res = [];
  function ok(name, cond, extra) { res.push([name, !!cond, extra === undefined ? '' : String(extra)]); }
  function clone(o) { return JSON.parse(JSON.stringify(o)); }
  function tap(el) { el.dispatchEvent(new MouseEvent('click', { bubbles: true })); }
  var S = $('stage').getBoundingClientRect().width / 800;          // the stage scale: sizes below are in stage pixels
  function px(el) { var r = el.getBoundingClientRect(); return { w: r.width / S, h: r.height / S, x: r.left / S, y: r.top / S }; }
  function L() { return $('feed-list'); }
  function kids() { return Array.prototype.slice.call(L().children); }
  function seq() {                                                  // the "+K más" line is "more"; its text is checked on its own
    return kids().map(function (el) {
      return el.classList.contains('grp') ? 'g:' + el.getAttribute('data-g') : el.hasAttribute('data-more') ? 'more' : el.getAttribute('data-id');
    }).join('|');
  }
  function row(id) { return kids().filter(function (el) { return el.getAttribute('data-id') === id; })[0]; }
  function rowsOnly() { return kids().filter(function (el) { return el.classList.contains('task'); }); }
  function captions() { return kids().filter(function (el) { return el.classList.contains('grp'); }); }
  function show(mut) { var v = clone(view); mut(v); view = v; render(); return v; }
  function fresh() { rank.clear(); grank.clear(); rankSeq = 0; grankSeq = 0; closeTask(); }
  function reading() { return $('feed').classList.contains('reading'); }
  function T(id, o) {
    var t = { id: id, project: 'p-' + id, tool: '', what: '', state: 'working', age: 1 };
    Object.keys(o || {}).forEach(function (k) { t[k] = o[k]; });
    return t;
  }
  function CR(id, o) {                                              // a census row as the server sends it: title = project = the name, age = idle (0 when unknown)
    var t = { id: id, project: 'n-' + id, title: 'n-' + id, group: 'Corridas', tool: '', what: '', state: 'working', age: 0, source: 'census', elapsed: 100 };
    Object.keys(o || {}).forEach(function (k) { t[k] = o[k]; });
    if (typeof t.idle === 'number') t.age = t.idle;
    return t;
  }
  function names() { return captions().map(function (c) { return c.textContent; }).join('|'); }
  function tailLine() { var all = kids(); var last = all[all.length - 1]; return last && last.hasAttribute('data-more') ? last : null; }
  function cs(el) { return getComputedStyle(el); }
  function rgba(s) {                                                // "rgb(1, 2, 3)", "rgba(..)" or "color(srgb 0.1 0.2 0.3 / 0.5)"
    var n = (s.match(/[\d.]+(?:e-?\d+)?/g) || []).map(Number);
    var unit = /^color\(/.test(s) ? 255 : 1;
    return { r: n[0] * unit, g: n[1] * unit, b: n[2] * unit, a: n.length > 3 ? n[3] : 1 };
  }
  function over(fg, bg) { return { r: fg.r * fg.a + bg.r * (1 - fg.a), g: fg.g * fg.a + bg.g * (1 - fg.a), b: fg.b * fg.a + bg.b * (1 - fg.a), a: 1 }; }
  function lum(c) {
    var f = function (v) { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b);
  }
  function colour(x) { return typeof x === 'string' ? rgba(x) : x; }
  function contrast(fg, bg) {                                       // WCAG ratio of fg over bg (CSS colours or {r,g,b,a}; a see-through bg counts over black)
    var base = over(colour(bg), { r: 0, g: 0, b: 0, a: 1 }), top = over(colour(fg), base);
    var a = lum(top), b = lum(base);
    return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
  }
  var Q = [];
  function step(ms, fn) { Q.push([ms, fn]); }
  function go() {
    var at = 0;
    Q.forEach(function (s) {
      at += s[0];
      setTimeout(function () { try { s[1](); } catch (e) { ok('a step threw: ' + e.message, false); } }, at);
    });
    setTimeout(function () {
      var pre = document.createElement('pre'); pre.id = 'selftest'; pre.textContent = JSON.stringify(res); document.body.appendChild(pre);
    }, at + 50);
  }
  try {
"""

FINISH = r"""
  } catch (e) { ok('the self-test body threw: ' + e.message, false); }
  go();
})();
"""


def battery(tmp_path, body, query="demo=threads", budget=3000):
    """Run `body` (JS, with the helpers above) inside a copy of the page loaded with `query`; return [(check, ok, extra)]."""
    page = PAGE.read_text(encoding="utf-8")
    tail = "})();\n</script>"
    assert page.count(tail) == 1, "the page's script no longer ends the way the harness expects"
    copy = tmp_path / "index.html"
    copy.write_text(page.replace(tail, PRELUDE + body + FINISH + tail), encoding="utf-8")
    dom = dump(f"{copy.as_uri()}?{query}&tag=0", budget=budget, timeout=90)
    found = re.search(r'<pre id="selftest">(.*?)</pre>', dom, re.S)
    assert found, "the self-test never reported; the DOM ended with:\n" + dom[-700:]
    return [tuple(item) for item in json.loads(html.unescape(found.group(1)))]


def failures(results):
    return [(name, extra) for name, good, extra in results if not good]


def assert_all(results, expect_at_least=1):
    assert len(results) >= expect_at_least, f"only {len(results)} checks ran"
    assert not failures(results), "failed checks (name, detail): " + json.dumps(failures(results), ensure_ascii=False, indent=1)


# --- static: what the page's own source promises ----------------------------------------------------------------


def _page():
    return PAGE.read_text(encoding="utf-8")


def _css():
    page = _page()
    return page[page.index("<style>"):page.index("</style>")]


def _rule(css, selector):
    """The declarations of the first rule whose selector list is exactly `selector`."""
    found = re.search(r"(?:^|\})[ \t]*" + re.escape(selector) + r"\s*\{([^}]*)\}", css, re.M)
    assert found, f"no CSS rule for {selector}"
    return found.group(1)


def test_task_rows_and_volver_are_finger_sized():
    css = _css()
    assert re.search(r"height:44px", _rule(css, ".trow"))
    assert re.search(r"height:44px", _rule(css, ".tv-back"))
    assert re.search(r"height:44px", _rule(css, ".tv-head"))
    assert "gap:10px" in _rule(css, "#feed-list.tasks")  # the gap is what keeps two 44 px targets apart


def test_the_list_scrolls_inside_the_bubble_and_never_chains_or_snaps():
    css = _css()
    for selector in ("#feed-list.tasks", "#tv-said"):
        assert "overscroll-behavior:contain" in _rule(css, selector), selector
        assert "overflow-y:auto" in _rule(css, selector), selector
    assert "scroll-snap" not in _page()  # a snap point fights a finger that stopped on purpose
    assert "max-height:219px" in _rule(css, "#feed-list.tasks")
    # a bar a finger can grab (16 px, the same in the detail), always drawn while the list overflows, its width reserved so rows
    # never change width; the stripe that shows is the 12 px of thumb inside 2 px of transparent border
    assert "::-webkit-scrollbar{width:0}" not in css
    for selector in ("#feed-list.tasks", "#tv-said"):
        assert "width:16px" in _rule(css, selector + "::-webkit-scrollbar"), selector
        assert "scrollbar-gutter:stable" in _rule(css, selector), selector
        thumb = _rule(css, selector + "::-webkit-scrollbar-thumb")
        assert "rgba(27,22,38,.25)" in thumb and "border:2px solid transparent" in thumb and "min-height:56px" in thumb, selector
        assert "var(--bone) 30%" in _rule(css, "body.pack " + selector + "::-webkit-scrollbar-thumb"), selector
        assert "var(--bone) 8%" in _rule(css, "body.pack " + selector + "::-webkit-scrollbar-track"), selector
    assert "linear-gradient(to bottom,#000 calc(100% - 16px),transparent)" in _rule(css, "#feed-list.tasks")  # no pill ends flat on the edge
    assert "#feed{height:284px" in css  # the bubble itself never grows with the list


def test_rows_captions_and_children_use_colour_and_not_opacity_or_lines():
    css = _css()
    for selector in (".trow", "li.task.idle .trow", "#feed-list li.task", "#feed-list li.task.kid", "#feed-list li.grp",
                     "li.task.titled .trow b", ".tv-back"):
        assert "opacity" not in _rule(css, selector), selector
    caption = _rule(css, "#feed-list li.grp")
    assert "text-transform" not in caption and "letter-spacing" not in caption and "border" not in caption
    assert "font-size:14px" in caption and "font-weight:700" in caption and "color:var(--ink-2)" in caption
    assert "margin:0 0 -6px 18px" in caption
    assert "margin-left:18px" in _rule(css, "#feed-list li.task.kid")
    assert "var(--accent) 22%" in _rule(css, ".trow")  # working: the mood's colour
    assert "var(--ink) 7%" in _rule(css, "li.task.idle .trow")  # finished and waiting: neutral
    assert "78%" in _rule(css, ".trow")  # secondary text is ink at 78%, never an opacity


def test_the_reading_rules_hide_everything_else_in_the_bubble():
    css = _css()
    assert "#feed.reading>*{display:none}" in css  # also hides #feed-empty and #feed-detail
    assert "#feed.reading>#task-view{display:flex}" in css
    page = _page()
    feed = page[page.index('<div id="feed"'):page.index('<div class="pills">')]
    assert feed.index('id="feed-detail"') < feed.index('id="task-view"')  # the detail is the last child of #feed
    assert 'id="tv-back"' in feed and '<template id="row-proto">' in page


def test_the_tap_guard_and_the_auto_close_are_what_the_design_says():
    page = _page()
    assert re.search(r"var TAP_GUARD_MS = 350;", page)
    assert re.search(r"var DETAIL_MS = 25000;", page)
    assert "var ARM_MS = 600;" in page  # the approve buttons' own arming is somebody else's number
    assert "function backTask() { if (performance.now() - openedAt >= TAP_GUARD_MS) closeTask(); }" in page
    assert "$('tv-back').addEventListener('click', backTask);" in page
    assert not re.search(r"\$\('tv-(?:said|name|did|age)'\)\.addEventListener\('click'", page)  # text never closes the detail
    assert not re.search(r"\$\('task-view'\)\.addEventListener\('click'", page)


def test_an_approval_closes_the_detail_and_the_task_code_never_touches_the_arming():
    page = _page()
    assert re.search(r"var screen = pickScreen\(v\);\n\s+if \(screen !== 'main'\) closeTask\(\);", page)
    start = page.index("/* ---------------- hilos:")
    end = page.index("function renderMain(v) {")
    block = page[start:end]
    for name in ("holdUntil", "ARM_MS", "decide", "paintAsk", "armTimer", "sending", "curKey"):
        assert not re.search(rf"\b{name}\b", block), f"the thread code touches {name}"
    tick = page[page.index("/* el reloj y la edad del pedido avanzan solos */"):]
    tick = tick[:tick.index("}, 1000);")]
    assert "paintDetail(stableOrder(" in tick  # the open detail's age keeps running without a new push


def test_the_thread_code_keeps_text_as_text_and_names_as_map_keys():
    page = _page()
    start = page.index("/* ---------------- hilos:")
    block = page[start:page.index("function renderMain(v) {")]
    for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "createElementNS", "querySelector(", "querySelectorAll(", "eval("):
        assert bad not in block, bad  # a group name or an id is never a selector and never markup
    assert "var rank = new Map()" in block and "var grank = new Map()" in block
    assert "Object.create(null)" not in block and not re.search(r"\b(?:have|rank|grank|at|byId)\s*=\s*\{\}", block)
    assert not re.search(r"\.(?:textContent|innerText)\s*=[^=]", block)  # everything goes through setText


def test_the_page_has_no_em_dash_anywhere():
    dash = chr(0x2014)
    assert dash not in _page()
    assert dash not in Path(__file__).read_text(encoding="utf-8")


def test_the_runs_rows_and_the_more_line_follow_the_design_rules():
    css = _css()
    for selector in ("li.task.quiet .trow span", "li.task.quiet .trow b", "#feed-list li.more"):
        assert "opacity" not in _rule(css, selector), selector  # colour, never dimming
    more = _rule(css, "#feed-list li.more")
    for declaration in ("margin-left:18px", "font-size:14px", "line-height:16px", "font-weight:700", "color:var(--ink-2)"):
        assert declaration in more, declaration  # a caption-sized line, aligned with the row text
    assert "text-transform" not in more and "letter-spacing" not in more and "border" not in more and "background" not in more
    quiet = _rule(css, "li.task.quiet .trow span")
    assert "flex:none" in quiet and "color:var(--ink)" in quiet  # the silence notice is read whole, in full ink
    assert "min-width:0" in _rule(css, "li.task.quiet .trow b")  # it is the name that gives way


def test_the_run_numbers_and_the_one_list_every_caller_shares():
    page = _page()
    assert "var MAX_RUNS_DRAWN = 5;" in page and "var QUIET_S = 120;" in page
    # arrange() keeps a rank per id and per group and forgets whoever is not in the list it is given: every caller must
    # hand it the same capped list, or a row cut by the page would lose its seat on one call and get it back on the next
    calls = re.findall(r"stableOrder\((?!tasks\))(?!shownTasks\()", page)
    assert calls == [], calls
    assert page.count("stableOrder(shownTasks(") == 3 and "arrange(tasks)" in page
    assert re.search(r"var shown = shownTasks\(v\), tasks = shown\.tasks;", page)
    assert "whole(v.tasks_more) + shown.cut" in page and "typeof v.working_total === 'number'" in page


def test_every_thread_demo_exists_and_the_cycle_still_lists_them():
    page = _page()
    for name in DEMO_THREADS + DEMO_RUNS:
        assert re.search(rf"^\s+(?:'{name}'|{name}): mk\(", page, re.M), name
    assert "query.get('open')" in page and "query.get('scroll')" in page  # demo-only flags, read inside the demo branch
    assert page.index("query.get('open')") > page.index("} else if (Object.prototype.hasOwnProperty.call(DEMOS, DEMO)) {")


# --- the list: rows, captions, labels (one page load) -----------------------------------------------------------

LIST_JS = r"""
  var rows = rowsOnly();
  ok('5 rows and 3 captions, in group order', seq() === 'g:Acme Studio|f1|f2|g:Buddy|b1|b2|g:Terminal|t1', seq());
  ok('every row is a 44 px button, 384 px wide (366 when it is a child)', rows.every(function (li) {
    var r = px(li.firstChild);
    return li.firstChild.tagName === 'BUTTON' && Math.abs(r.h - 44) < .6 && Math.abs(r.w - (li.classList.contains('kid') ? 366 : 384)) < 1;
  }));
  ok('a titled row says its title, an untitled one its folder', row('f1').firstChild.children[0].textContent === 'Catálogo de la tienda web'
    && row('t1').firstChild.children[0].textContent === 'api-pagos');
  ok('a titled row hides the tool, an untitled one shows it', cs(row('f1').firstChild.children[1]).display === 'none'
    && row('t1').firstChild.children[1].textContent === 'Bash git pull' && cs(row('t1').firstChild.children[1]).display !== 'none');
  ok('the age is in the row', row('f1').firstChild.children[3].textContent === '0:03');
  ok('a finished row has a check mark before its age, a working row does not (a cue that is not a colour)', (function () {
    var done = row('f2').firstChild.children[2], busy = row('f1').firstChild.children[2];
    return done.classList.contains('ck') && cs(done).display === 'block' && Math.abs(px(done).w - 14) < .6
      && px(done).x + px(done).w <= px(row('f2').firstChild.children[3]).x && cs(busy).display === 'none';
  })());
  ok('working rows and finished rows are not the same tint', cs(row('f1').firstChild).backgroundColor !== cs(row('f2').firstChild).backgroundColor);
  ok('both tints are real', rgba(cs(row('f1').firstChild).backgroundColor).a > .1 && rgba(cs(row('f2').firstChild).backgroundColor).a > .05);
  ok('the finished row is classed idle and no row is dimmed', row('f2').classList.contains('idle') && rows.every(function (li) {
    return cs(li).opacity === '1' && cs(li.firstChild).opacity === '1';
  }));
  ok('the child is indented 18 px and 366 px wide', row('b2').classList.contains('kid') && Math.abs(px(row('b2').firstChild).w - 366) < 1
    && Math.abs(px(row('b2')).x - px(row('b1')).x - 18) < 1 && !row('b1').classList.contains('kid'));
  var caps = captions();
  ok('captions are 16 px tall, plain, not buttons', caps.every(function (c) {
    var st = cs(c);
    return Math.abs(px(c).h - 16) < .6 && c.children.length === 0 && st.textTransform === 'none' && st.letterSpacing === 'normal'
      && st.borderTopWidth === '0px' && st.fontSize === '14px' && st.fontWeight === '700';
  }));
  ok('caption text is the group name, ungrouped is Terminal', caps.map(function (c) { return c.textContent; }).join('|') === 'Acme Studio|Buddy|Terminal');
  ok('a caption sits 18 px in, like the row text', Math.abs(px(caps[0]).x - px(rows[0]).x - 18) < 1);
  ok('the headline counts the working threads', $('feed-msg').textContent === '3 tareas en marcha');
  var R = 5, G = 3;
  ok('scrollHeight follows 44R + 10(R+G-1) + 10G + 18 bleed', L().scrollHeight === 44 * R + 10 * (R + G - 1) + 10 * G + 18, L().scrollHeight);
  ok('the window is 219 px and scrolls', Math.abs(px(L()).h - 219) < .6 && L().scrollHeight > L().clientHeight && cs(L()).overflowY === 'auto');
  ok('no chained scroll, no snap', cs(L()).overscrollBehaviorY === 'contain' && cs(L()).scrollSnapType === 'none');
  ok('the bubble is 284 px tall', Math.abs(px($('feed')).h - 284) < .6);
  ok('a tap on the middle of every visible row lands on that row', rows.filter(function (li) { var r = li.getBoundingClientRect(); return r.bottom <= L().getBoundingClientRect().bottom; }).every(function (li) {
    var r = li.getBoundingClientRect(); var hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    return hit && hit.closest('li.task') === li;
  }));
  ok('tapping a caption opens nothing', (function () { tap(caps[1]); return !reading(); })());
  tap(row('t1').firstChild);
  ok('tapping a row opens its detail', reading() && $('tv-name').textContent === 'api-pagos');
"""


@needs_chromium
def test_the_list_groups_threads_under_plain_captions_with_children_indented(tmp_path):
    assert_all(battery(tmp_path, LIST_JS), 18)


# --- more rows than the window shows: the cue is always there --------------------------------------------------

CUE_JS = r"""
  /* Layouts of group sizes. Whatever the arithmetic of rows and captions does to the window, a list that overflows must
     say so: a 6 px thumb that is always drawn, a gutter that is always reserved (so row widths never depend on the count)
     and a fade, so a row cut by the window never ends flat against the bubble's edge. */
  var layouts = [[1], [2], [3], [4], [5], [6], [8], [1, 1], [2, 1], [1, 2], [2, 2], [3, 3], [1, 3], [2, 2, 2], [1, 1, 1]];
  var overflowed = 0, fitted = 0;
  layouts.forEach(function (groups) {
    fresh();
    var n = 0;
    show(function (v) {
      v.tasks = [];
      groups.forEach(function (count, gi) {
        for (var i = 0; i < count; i++) { n += 1; v.tasks.push(T('x' + n, { group: gi === 0 ? '' : 'G' + gi, title: gi % 2 ? 'Hilo ' + n : undefined, state: n % 2 ? 'working' : 'idle' })); }
      });
    });
    var name = JSON.stringify(groups), box = px(L()), over = L().scrollHeight > L().clientHeight;
    var widths = rowsOnly().map(function (li) { return Math.round(px(li.firstChild).w); });
    ok(name + ': every row is 384 px wide whatever the count', widths.every(function (w) { return w === 384; }), widths.join(','));
    ok(name + ': the scrollbar width is reserved (16 px) whether or not the list overflows', L().offsetWidth - L().clientWidth === 16, L().offsetWidth - L().clientWidth);
    ok(name + ': the window is never taller than 219 px', box.h <= 219.6, box.h);
    if (over) {
      overflowed += 1;
      var thumb = rgba(getComputedStyle(L(), '::-webkit-scrollbar-thumb').backgroundColor);
      ok(name + ': it overflows, so a visible thumb says so', getComputedStyle(L(), '::-webkit-scrollbar').width === '16px' && thumb.a >= .2, JSON.stringify(thumb));
      ok(name + ': a row cut by the window fades out instead of ending flat', /transparent|rgba\(0, 0, 0, 0\)/.test(cs(L()).webkitMaskImage) && /16px/.test(cs(L()).webkitMaskImage), cs(L()).webkitMaskImage);
      var clip = box.y + box.h;
      var cut = rowsOnly().filter(function (li) { var r = px(li); return r.y < clip - .5 && r.y + r.h > clip + .5; });
      ok(name + ': at most one row is cut by the window', cut.length <= 1, cut.length);
    } else { fitted += 1; }
  });
  ok('the layouts cover both a list that fits and one that overflows', overflowed >= 8 && fitted >= 2, overflowed + ' overflow, ' + fitted + ' fit');
  /* the thumb moves with the finger and stays 16 px wide at the end of the list, where the last row clears the corner */
  fresh();
  show(function (v) { v.tasks = [1, 2, 3, 4, 5, 6].map(function (k) { return T('y' + k, { title: 'Hilo ' + k }); }); });
  L().scrollTop = 9999;
  ok('at the end the list reports no more to scroll and the rows are still 384 px wide', L().scrollTop + L().clientHeight >= L().scrollHeight - 1
    && rowsOnly().every(function (li) { return Math.round(px(li.firstChild).w) === 384; }));
"""


@needs_chromium
def test_a_list_taller_than_its_window_always_says_so(tmp_path):
    assert_all(battery(tmp_path, CUE_JS, query="demo=threads", budget=3000), 50)


# --- the tree rules: parents, orphans, cycles, hostile names ----------------------------------------------------

TREE_JS = r"""
  /* a child with a live parent hangs under it, in the PARENT's group, whatever its own group says */
  fresh();
  show(function (v) { v.tasks = [T('k', { parent: 'p', group: 'Otro' }), T('p', { group: 'Uno' })]; });
  ok('child under its live parent, in the parent group', seq() === 'g:Uno|p|k' && row('k').classList.contains('kid'), seq());
  ok('the parent is not indented', !row('p').classList.contains('kid'));
  /* a child whose parent is not on the screen is an ordinary row in its own group */
  fresh();
  show(function (v) { v.tasks = [T('k', { parent: 'ghost', group: 'Solo' }), T('p', { group: 'Uno' })]; });
  ok('orphan stands alone in its own group', seq() === 'g:Solo|k|g:Uno|p' && !row('k').classList.contains('kid'), seq());
  /* a grandchild hangs under the topmost parent, one indent level only */
  fresh();
  show(function (v) { v.tasks = [T('c', { parent: 'b', group: 'G' }), T('b', { parent: 'a', group: 'G' }), T('a', { group: 'G' })]; });
  ok('grandchild under the root, both indented once', seq() === 'g:G|a|c|b' && row('c').classList.contains('kid') && row('b').classList.contains('kid')
    && Math.abs(px(row('c')).x - px(row('b')).x) < .5, seq());
  /* a cycle and a thread that is its own parent hang nothing and hang nobody */
  fresh();
  show(function (v) { v.tasks = [T('x', { parent: 'y', group: 'G' }), T('y', { parent: 'x', group: 'G' }), T('z', { parent: 'z', group: 'G' })]; });
  ok('a cycle and a self parent are plain rows', rowsOnly().length === 3 && rowsOnly().every(function (li) { return !li.classList.contains('kid'); }), seq());
  /* a chain longer than the 4 hops we follow: nothing hangs, every thread shows once */
  fresh();
  show(function (v) {
    v.tasks = [1, 2, 3, 4, 5, 6].map(function (n) { return T('n' + n, { parent: n > 1 ? 'n' + (n - 1) : undefined, group: 'G' }); }).reverse();
  });
  ok('a long chain still shows every thread once', rowsOnly().length === 6 && seq().split('|').length === 7, seq());
  ok('the sixth link, too deep to resolve, is a plain row', !row('n6').classList.contains('kid') && row('n5').classList.contains('kid'));
  /* no group is Terminal, and a group literally called Terminal is the same section */
  fresh();
  show(function (v) { v.tasks = [T('a'), T('b', { group: 'Terminal' }), T('c', { group: 'Buddy' })]; });
  ok('without a group a thread is under Terminal', seq() === 'g:Terminal|a|b|g:Buddy|c', seq());
  /* names that are valid ids and groups but poison plain objects, and markup: all of it is only text */
  fresh();
  var threw = '';
  try {
    show(function (v) {
      v.tasks = [T('constructor', { group: '__proto__', title: '<img src=x onerror=1>' }),
                 T('__proto__', { group: 'constructor', title: 'x' }),
                 T('toString', { group: '<img src=x onerror=1>' }), T('hasOwnProperty', { group: 'hasOwnProperty', parent: 'toString' }),
                 T('valueOf', { group: '' }), T('k1', { group: 'x', parent: '__proto__' })];
    });
  } catch (e) { threw = e.message; }
  ok('hostile ids and groups render without an exception', threw === '', threw);
  ok('every hostile thread has its row, its caption and its parent',
    seq() === 'g:__proto__|constructor|g:constructor|__proto__|k1|g:<img src=x onerror=1>|toString|hasOwnProperty|g:Terminal|valueOf', seq());
  ok('the names are literal text', captions().map(function (c) { return c.textContent; }).join('|') === '__proto__|constructor|<img src=x onerror=1>|Terminal'
    && captions().map(function (c) { return c.getAttribute('data-g'); }).indexOf('__proto__') === 0);
  ok('nothing was parsed as markup', L().querySelector('img') === null && row('constructor').firstChild.children[0].textContent === '<img src=x onerror=1>');
  ok('children of hostile parents hang under them', row('k1').classList.contains('kid') && row('hasOwnProperty').classList.contains('kid'));
  var before = kids();
  show(function (v) { v.tasks.forEach(function (t) { t.age += 1; }); });
  ok('a second render reuses every hostile row', kids().every(function (el, i) { return el === before[i]; }) && kids().length === before.length);
  show(function (v) { v.tasks = v.tasks.filter(function (t) { return t.id === 'valueOf'; }); });
  ok('they leave cleanly too', seq() === 'g:Terminal|valueOf', seq());
"""


@needs_chromium
def test_children_orphans_cycles_and_hostile_names(tmp_path):
    assert_all(battery(tmp_path, TREE_JS), 14)


# --- stable order -----------------------------------------------------------------------------------------------

ORDER_JS = r"""
  var base = seq();
  var nodes = kids();
  ok('first paint: the server order, working first, groups by first sight', base === 'g:Acme Studio|f1|f2|g:Buddy|b1|b2|g:Terminal|t1', base);
  show(function (v) { v.tasks.reverse(); });
  ok('the server flipping its order moves nothing', seq() === base, seq());
  show(function (v) { v.tasks.forEach(function (t) { if (t.id === 'f1') t.state = 'idle'; if (t.id === 'f2') t.state = 'working'; }); });
  ok('finishing and resuming change the tint, not the place', seq() === base && row('f1').classList.contains('idle') && !row('f2').classList.contains('idle'), seq());
  show(function (v) { v.tasks.forEach(function (t) { t.age += 30; t.what = 'otra cosa'; }); });
  ok('time and text change nothing either', seq() === base);
  ok('the same DOM nodes, in the same places', kids().every(function (el, i) { return el === nodes[i]; }));
  show(function (v) { v.tasks.unshift(T('b3', { title: 'Nuevo en Buddy', group: 'Buddy' })); });
  ok('a new thread joins the end of its group', seq() === 'g:Acme Studio|f1|f2|g:Buddy|b1|b2|b3|g:Terminal|t1', seq());
  show(function (v) { v.tasks.unshift(T('h1', { title: 'Nuevo grupo', group: 'Homelab' })); });
  ok('a new group goes to the end of the list', seq() === 'g:Acme Studio|f1|f2|g:Buddy|b1|b2|b3|g:Terminal|t1|g:Homelab|h1', seq());
  show(function (v) { v.tasks = v.tasks.filter(function (t) { return t.group !== 'Buddy'; }); });
  ok('a group that empties takes its caption with it', seq() === 'g:Acme Studio|f1|f2|g:Terminal|t1|g:Homelab|h1', seq());
  show(function (v) { v.tasks.push(T('b1', { title: 'Hilos', group: 'Buddy' })); });
  ok('and when it comes back it has lost its seat: last', seq() === 'g:Acme Studio|f1|f2|g:Terminal|t1|g:Homelab|h1|g:Buddy|b1', seq());
  show(function (v) { v.tasks = v.tasks.filter(function (t) { return t.id !== 'f1'; }); });
  ok('a row leaving shifts nobody else', seq() === 'g:Acme Studio|f2|g:Terminal|t1|g:Homelab|h1|g:Buddy|b1', seq());
  ok('the work counter follows the working ones', $('feed-msg').textContent.length > 0);
"""


@needs_chromium
def test_order_is_stable_when_state_age_and_server_order_change(tmp_path):
    assert_all(battery(tmp_path, ORDER_JS), 10)


# --- the detail: open, close, the 350 ms guard, the 25 s timer, the snapshot ------------------------------------

DETAIL_JS = r"""
  /* The page's only reads of performance.now() are the 350 ms guard of Volver. The test owns that clock: real timers
     are not a firm base on a slow machine (the Pi 4 spends tens of ms just opening the detail), so the test moves the
     page's clock by hand and the guard is judged without depending on how fast the machine is. */
  var realNow = performance.now.bind(performance), skew = 0;
  performance.now = function () { return realNow() + skew; };
  var pillsY = px(document.querySelector('.pills')).y, feedH = px($('feed')).h;
  var listTop = px(L()).y;
  tap(row('f1').firstChild);                                        // working, has said, long title
  ok('a tap opens the detail', reading());
  ok('the title is the headline and the group leads the sub line', $('tv-name').textContent === 'Catálogo de la tienda web'
    && $('tv-did').textContent === 'Acme Studio · Edit src/Catalogo.tsx' && $('tv-age').textContent === 'hace 0:03');
  ok('a title too long for 30 px drops to two lines of 21 px', $('task-view').classList.contains('long') && px($('tv-name')).h <= 44.6 && cs($('tv-name')).fontSize === '21px');
  ok('a working thread says "Antes dijo:" and the quotes are CSS', $('tv-pre').textContent === 'Antes dijo: '
    && $('tv-text').textContent === 'Cambié el selector de categoría a un menú con búsqueda. Sigo con los estilos de la tarjeta.'
    && getComputedStyle($('tv-text'), '::before').content.indexOf('“') >= 0);
  ok('the list, the headline and the empty state are gone', cs(L()).display === 'none' && cs($('feed-msg')).display === 'none'
    && cs($('feed-empty')).display === 'none' && cs($('feed-detail')).display === 'none' && cs($('task-view')).display === 'flex');
  ok('nothing outside the bubble moved and the bubble kept its height', Math.abs(px(document.querySelector('.pills')).y - pillsY) < .6 && Math.abs(px($('feed')).h - feedH) < .6);
  ok('Volver is a 44 px pill at least 96 wide', $('tv-back').tagName === 'BUTTON' && Math.abs(px($('tv-back')).h - 44) < .6 && px($('tv-back')).w >= 96);
  ok('the text area has room to read', px($('tv-said')).h >= 120);
  tap($('tv-said')); tap($('tv-text')); tap($('tv-name')); tap($('task-view'));
  ok('touching the text never closes the detail', reading());
  tap($('tv-back'));
  ok('Volver right after the opening is a bounce and is ignored (350 ms)', reading());
  skew += 200;                                                      // the page's clock now says about 200 ms since the opening
  tap($('tv-back'));
  ok('still ignored at about 200 ms', reading());
  skew += 200;                                                      // and about 400 ms now, past the guard
  tap($('tv-back'));
  ok('Volver works after the guard', !reading() && cs(L()).display !== 'none' && cs($('feed-msg')).display !== 'none');
  /* the other kinds of thread */
  step(10, function () {
    tap(row('b1').firstChild);
    ok('working without said', $('tv-text').textContent === 'Todavía no dijo nada.' && $('tv-pre').textContent === '' && $('tv-said').classList.contains('none'));
  });
  step(400, function () { tap($('tv-back')); tap(row('f2').firstChild);
    ok('finished without said', $('tv-text').textContent === 'No dejó ningún mensaje.' && $('tv-said').classList.contains('none')
      && $('tv-did').textContent.indexOf('Acme Studio · terminó: Bash aws') === 0 && $('task-view').classList.contains('long'));
  });
  step(400, function () { tap($('tv-back')); tap(row('b2').firstChild);
    ok('finished with said: no label', $('tv-pre').textContent === '' && $('tv-text').textContent.indexOf('Las doce pantallas') === 0);
  });
  step(400, function () { tap($('tv-back')); tap(row('t1').firstChild);
    ok('a terminal thread is headed by its folder, in 30 px when it fits', $('tv-name').textContent === 'api-pagos'
      && $('tv-did').textContent === 'Terminal · Bash git pull');
  });
  step(400, function () { tap($('tv-back')); });
  /* the heartbeat keeps painting the open detail: age in place, a said that arrives, the server order flipping */
  step(10, function () {
    tap(row('b1').firstChild);
    show(function (v) { v.tasks.forEach(function (t) { t.age += 1; if (t.id === 'b1') t.said = 'Texto nuevo.'; }); v.tasks.reverse(); });
    ok('age repainted in place', $('tv-age').textContent === 'hace 0:07');
    ok('a said arriving while open is shown', $('tv-text').textContent === 'Texto nuevo.' && !$('tv-said').classList.contains('none'));
    ok('still the same thread after the server flipped its order', $('tv-name').textContent === 'Hilos en la pantalla del Buddy');
  });
  /* the thread leaves the board while it is open: a snapshot, its age keeps counting */
  step(10, function () {
    show(function (v) { v.tasks = v.tasks.filter(function (t) { return t.id !== 'b1' && t.id !== 'b2'; }); });
    ok('the open detail survives its thread leaving', reading() && $('tv-name').textContent === 'Hilos en la pantalla del Buddy' && row('b1') === undefined);
  });
  step(5000, function () {
    ok('and its age keeps counting up by itself', /^hace 0:(1[1-3])$/.test($('tv-age').textContent), $('tv-age').textContent);
  });
  step(400, function () {
    tap($('tv-back'));
    ok('Volver closes the snapshot', !reading());
    tap(row('f1').firstChild);
  });
  /* 25 s after the LAST touch: a touch at 20 s keeps it open to 45 s */
  step(20000, function () { $('tv-said').dispatchEvent(new Event('pointerdown', { bubbles: true })); ok('still open at 20 s', reading()); });
  step(24000, function () { ok('still open 24 s after the last touch', reading()); });
  step(2000, function () { ok('closed by itself 26 s after the last touch', !reading() && cs(L()).display !== 'none'); });
  step(10, function () {
    tap(row('f1').firstChild);
  });
  step(24000, function () { $('tv-said').dispatchEvent(new Event('scroll')); });
  step(24000, function () { ok('a scroll inside the text counts as a touch', reading()); });
  step(2000, function () { ok('and then it closes', !reading()); });
"""


@needs_chromium
def test_detail_opens_closes_guards_the_bounce_and_follows_its_thread(tmp_path):
    assert_all(battery(tmp_path, DETAIL_JS, budget=130000), 26)


# --- approvals ---------------------------------------------------------------------------------------------------

APPROVAL_JS = r"""
  tap(row('f1').firstChild);
  ok('open', reading());
  show(function (v) { v.approvals = [{ key: 'claude:t1', tool: 'Bash', hint: 'ls', source: 'claude' }]; v.mood = 'attention'; });
  ok('an approval wins: the ask screen, the detail closed', document.body.getAttribute('data-screen') === 'ask' && !reading());
  ok('and its buttons are asleep for the 600 ms', $('btns').className.indexOf('hold') >= 0);
  step(700, function () {
    ok('then awake', $('btns').className.indexOf('hold') < 0);
    show(function (v) { v.approvals = []; v.mood = 'idle'; });
    ok('when it is decided the list is back, not a stale detail', document.body.getAttribute('data-screen') === 'main' && !reading()
      && cs(L()).display !== 'none' && cs($('task-view')).display === 'none');
    tap(row('b2').firstChild);
    ok('and a row opens again', reading() && $('tv-name').textContent === 'Probar la pantalla con Chromium');
  });
  /* an approval that arrives with the finger down on a row: the click that follows opens nothing on the ask screen */
  step(10, function () {
    show(function (v) { v.approvals = [{ key: 'claude:t2', tool: 'Edit', hint: 'x.py', source: 'claude' }]; });
    ok('a second approval closes it again', !reading() && document.body.getAttribute('data-screen') === 'ask');
    tap(row('f1').firstChild);                                      // a late click on a row that is no longer on screen
    ok('the late click does not open a detail over the ask screen', document.body.getAttribute('data-screen') === 'ask'
      && cs($('s-main')).display === 'none');
  });
"""


@needs_chromium
def test_an_approval_arriving_while_the_detail_is_open_wins_and_the_list_returns(tmp_path):
    assert_all(battery(tmp_path, APPROVAL_JS, budget=4000), 8)


# --- entries to tasks and back, tasks vanishing under an open detail ---------------------------------------------

TRANSITION_JS = r"""
  var plain = function () { return kids().filter(function (el) { return !el.hasAttribute('data-id') && !el.hasAttribute('data-g'); }); };
  ok('starts as the protocol lines', kids().length === 4 && plain().length === 4 && !L().classList.contains('tasks'), kids().length);
  view = clone(DEMOS.threads); render();
  ok('threads replace every plain line', kids().length === 8 && plain().length === 0 && L().classList.contains('tasks'), kids().length);
  ok('and the list measures what 5 rows and 3 captions measure', L().scrollHeight === 338, L().scrollHeight);
  view = clone(DEMOS.busy); render();
  ok('back to protocol lines: 4 plain, no rows, no class', kids().length === 4 && plain().length === 4 && !L().classList.contains('tasks')
    && !L().hasAttribute('data-id'), kids().length);
  ok('the headline is the protocol message again', $('feed-msg').textContent === 'Corriendo 2 tareas');
  /* one protocol line that spells the task signature must not be mistaken for the task list */
  view = clone(DEMOS.threads); render();
  show(function (v) { v.tasks = []; v.sessions.msg = 'x'; v.sessions.entries = ['@tasks']; });
  ok('a line that reads @tasks is still a line', kids().length === 1 && kids()[0].textContent === '@tasks' && !L().classList.contains('tasks'), kids().length);
  /* the last thread goes while its detail is open and the protocol message has long expired */
  view = clone(DEMOS.threads); render();
  tap(row('f1').firstChild);
  feedKeep = { msg: '', entries: [], at: 0 };
  show(function (v) { v.tasks = []; v.sessions.msg = ''; v.sessions.entries = []; });
  ok('the detail stays and the empty state does not squeeze it', reading() && cs($('feed-empty')).display === 'none' && cs($('feed-detail')).display === 'none'
    && cs($('feed-msg')).display === 'none' && px($('task-view')).h >= 190 && px($('tv-said')).h >= 120, px($('task-view')).h);
  step(400, function () {
    tap($('tv-back'));
    ok('after Volver the empty state speaks', !reading() && cs($('feed-empty')).display !== 'none' && kids().length === 0);
    view = clone(DEMOS.threads); render();
    ok('and the threads come back as rows', kids().length === 8 && !$('feed').classList.contains('reading'));
  });
"""


@needs_chromium
def test_entries_to_threads_and_back_and_threads_vanishing_under_the_detail(tmp_path):
    assert_all(battery(tmp_path, TRANSITION_JS, query="demo=busy", budget=4000), 8)


# --- the long list scrolls, and a refresh does not move it -------------------------------------------------------

SCROLL_JS = r"""
  var rows = rowsOnly();
  ok('8 rows and 5 captions', rows.length === 8 && captions().length === 5, seq());
  ok('scrollHeight is 540', L().scrollHeight === 44 * 8 + 10 * (8 + 5 - 1) + 10 * 5 + 18, L().scrollHeight);
  L().scrollTop = 120;
  show(function (v) { v.tasks.forEach(function (t) { t.age += 1; }); });
  ok('a refresh keeps the scroll where the finger left it', L().scrollTop === 120, L().scrollTop);
  show(function (v) { v.tasks.reverse(); });
  ok('even when the server reorders', L().scrollTop === 120 && seq().length > 0);
  L().scrollTop = 9999;
  var last = rows[rows.length - 1].getBoundingClientRect(), box = L().getBoundingClientRect();
  ok('at the end the last row clears the corner by the 18 px of bleed', box.bottom - last.bottom >= 17 * S - .6 && box.bottom - last.bottom <= 19 * S + .6, (box.bottom - last.bottom) / S);
  var top = L().scrollTop;
  tap(row('t2').firstChild);
  step(400, function () {
    tap($('tv-back'));
    ok('after reading and coming back the list is where it was', L().scrollTop === top, L().scrollTop + ' vs ' + top);
  });
"""


@needs_chromium
def test_eight_threads_scroll_and_keep_their_place(tmp_path):
    assert_all(battery(tmp_path, SCROLL_JS, query="demo=threads8", budget=3000), 5)


# --- text from sessions stays text in the detail ----------------------------------------------------------------

HOSTILE_DETAIL_JS = r"""
  var evil = '<img src=x onerror="window.pwned=1"><b>x</b>';
  show(function (v) {
    v.tasks = [T('e1', { title: evil, group: evil, project: evil, tool: evil, what: evil, said: evil + '\n' + evil, state: 'idle' })];
  });
  tap(row('e1').firstChild);
  ok('the detail shows markup as text', $('tv-text').children.length === 0 && $('tv-text').textContent.indexOf('<img') === 0
    && $('tv-name').children.length === 0 && $('tv-did').children.length === 0 && $('tv-did').textContent.indexOf(evil) === 0);
  ok('and no element came out of it', document.querySelectorAll('#feed img, #feed b b').length === 0 && window.pwned === undefined);
  ok('the row too', row('e1').firstChild.children[0].textContent === evil && row('e1').querySelectorAll('img').length === 0);
  ok('a said with line breaks keeps them as text', $('tv-text').textContent.indexOf('\n') > 0 && cs($('tv-said')).whiteSpace === 'pre-line');
"""


@needs_chromium
def test_markup_in_a_title_group_or_said_is_only_text(tmp_path):
    assert_all(battery(tmp_path, HOSTILE_DETAIL_JS, budget=3000), 4)


# --- pack mode ---------------------------------------------------------------------------------------------------

PACK_JS = r"""
  ok('pack mode is on', document.body.classList.contains('pack'));
  var bubble = cs($('feed')).backgroundColor;
  var working = row('f1').firstChild, finished = row('f2').firstChild;
  ok('working and finished rows are still two tints', cs(working).backgroundColor !== cs(finished).backgroundColor
    && rgba(cs(working).backgroundColor).a > .1 && rgba(cs(finished).backgroundColor).a > .05);
  ok('row titles read on the pack bubble (7:1)', contrast(cs(working.children[0]).color, bubble) >= 7, contrast(cs(working.children[0]).color, bubble));
  ok('row text reads (4.5:1)', contrast(cs(row('t1').firstChild.children[1]).color, bubble) >= 4.5, contrast(cs(row('t1').firstChild.children[1]).color, bubble));
  ok('captions read (4.5:1)', captions().every(function (c) { return contrast(cs(c).color, bubble) >= 4.5; }), contrast(cs(captions()[0]).color, bubble));
  ok('the rows keep their size and the children their indent', rowsOnly().every(function (li) { return Math.abs(px(li.firstChild).h - 44) < .6; })
    && Math.abs(px(row('b2')).x - px(row('b1')).x - 18) < 1);
  tap(row('b2').firstChild);
  ok('the detail opens in pack mode', reading() && Math.abs(px($('tv-back')).h - 44) < .6);
  ok('Volver is visible against the bubble', rgba(cs($('tv-back')).backgroundColor).a > .1 && contrast(cs($('tv-back')).color, over(rgba(cs($('tv-back')).backgroundColor), rgba(bubble))) >= 4.5);
  ok('the said text reads (7:1)', contrast(cs($('tv-text')).color, bubble) >= 7, contrast(cs($('tv-text')).color, bubble));
  ok('the quiet text reads (4.5:1)', contrast(cs($('tv-did')).color, bubble) >= 4.5, contrast(cs($('tv-did')).color, bubble));
"""


@needs_chromium
def test_pack_mode_keeps_the_list_and_the_detail_legible(tmp_path):
    assert_all(battery(tmp_path, PACK_JS, query="demo=threads-pack", budget=3000), 8)


LIGHT_JS = r"""
  var bubble = cs($('feed')).backgroundColor;
  var rows = rowsOnly();
  ok('row titles read on paper (7:1)', rows.every(function (li) { return contrast(cs(li.firstChild.children[0]).color, over(rgba(cs(li.firstChild).backgroundColor), rgba(bubble))) >= 7; }));
  ok('row text reads on its tint (4.5:1)', rows.every(function (li) { return contrast(cs(li.firstChild).color, over(rgba(cs(li.firstChild).backgroundColor), rgba(bubble))) >= 4.5; }));
  ok('captions read on paper (4.5:1)', captions().every(function (c) { return contrast(cs(c).color, bubble) >= 4.5; }), contrast(cs(captions()[0]).color, bubble));
"""


@needs_chromium
def test_text_on_paper_keeps_its_contrast(tmp_path):
    assert_all(battery(tmp_path, LIGHT_JS, query="demo=threads", budget=3000), 3)


# --- every demo, every row: the list measures what the formula says and every row opens a sound detail -----------

ALL_DEMOS_JS = r"""
  var checked = 0;
  demoNames.forEach(function (name) {
    try {
      demoShow(name);
      var v = DEMOS[name], screen = document.body.getAttribute('data-screen');
      ok(name + ': renders', !!screen && screen !== 'boot' || name === 'boot', screen);
      if (!(v && v.tasks && v.tasks.length && screen === 'main')) return;
      var R = rowsOnly().length, G = captions().length;
      ok(name + ': every thread has its row', R === v.tasks.length, R + ' rows for ' + v.tasks.length);
      var K = v.tasks_more > 0 ? 26 : 0;                              // "+K más": a 16 px line and one more 10 px gap
      var A = (v.notices || []).length;                               // the notices of Hermes are rows too, and their group a caption (counted in G)
      ok(name + ': the list measures 44R + 10(R+G-1) + 10G + 18 (+26 with the "+K más" line; the notices count as rows)', L().scrollHeight === 44 * (R + A) + 10 * (R + A + G - 1) + 10 * G + 18 + K, L().scrollHeight);
      ok(name + ': the headline is one of the three phrases', /^(Nadie trabaja ahora\.|Una tarea en marcha|\d+ tareas en marcha)$/.test($('feed-msg').textContent), $('feed-msg').textContent);
      rowsOnly().forEach(function (li) {
        var label = li.firstChild.children[0].textContent;
        L().scrollTop = 0;
        tap(li.firstChild);
        var fits = px($('tv-name')).h <= 44.6 && ($('task-view').classList.contains('long') || $('tv-name').scrollWidth <= $('tv-name').clientWidth);
        var sound = reading() && $('tv-name').textContent === label && fits && Math.abs(px($('tv-back')).h - 44) < .6
          && px($('tv-said')).h >= 100 && Math.abs(px($('task-view')).h - 246) < 1 && px($('tv-back')).x + px($('tv-back')).w <= px($('feed')).x + 432 - 24 + .6;
        ok(name + ': the detail of "' + label + '" is sound', sound, px($('tv-name')).h + ' ' + px($('tv-said')).h + ' ' + px($('task-view')).h);
        closeTask();
        checked += 1;
      });
    } catch (e) { ok(name + ' threw: ' + e.message, false); }
  });
  ok('many rows were opened', checked >= 40, checked);
"""


@needs_chromium
def test_every_demo_measures_right_and_every_row_opens_a_sound_detail(tmp_path):
    assert_all(battery(tmp_path, ALL_DEMOS_JS, query="demo=idle", budget=4000), 75)


# --- the runs: what the census sees, under "Corridas", beside the threads ------------------------------------------

FMT_JS = r"""
  ok('m:ss is what it always was', fmtAge(0) === '0:00' && fmtAge(3000) === '0:03' && fmtAge(59999) === '0:59' && fmtAge(60000) === '1:00'
    && fmtAge(125000) === '2:05' && fmtAge(1799000) === '29:59' && fmtAge(3599999) === '59:59');
  ok('from the hour on it is h:mm:ss', fmtAge(3600000) === '1:00:00' && fmtAge(3643000) === '1:00:43' && fmtAge(36000000) === '10:00:00'
    && fmtAge(86399000) === '23:59:59' && fmtAge(2678400000) === '744:00:00');
  ok('a negative or odd time is 0:00', fmtAge(-5000) === '0:00' && fmtAge(0.4) === '0:00');
"""


@needs_chromium
def test_fmt_age_teaches_hours_and_keeps_minutes_and_seconds(tmp_path):
    assert_all(battery(tmp_path, FMT_JS, query="demo=idle", budget=2000), 3)


RUNS_JS = r"""
  var runIds = ['run-tienda-web', 'run-api-pagos-sandbox-regional', 'run-docs-sitio', 'run-migracion-correo', 'job-respaldo-nocturno'];
  var isCensus = function (li) { return /^(run|job)-/.test(li.getAttribute('data-id')); };
  var rows = rowsOnly();
  ok('Corridas leads, the threads follow, the last line is the "+K más"', seq() === 'g:Corridas|' + runIds.join('|') + '|g:Buddy|b1|b2|g:Terminal|t1|more', seq());
  ok('the group header counts the 7 alive (census_total), not the 5 drawn', names() === 'Corridas · 7|Buddy|Terminal', names());
  ok('the header is a caption like any other: 16 px, plain, not a button', Math.abs(px(captions()[0]).h - 16) < .6 && captions()[0].children.length === 0
    && captions()[0].getAttribute('data-g') === 'Corridas');
  ok('the headline counts everything that works (working_total 9), not the 7 rows drawn that work', $('feed-msg').textContent === '9 tareas en marcha', $('feed-msg').textContent);
  ok('5 census rows are drawn and 3 threads', rows.filter(isCensus).length === 5 && rows.length === 8);
  var tail = tailLine();
  ok('the last line says how many are not drawn (tasks_more)', tail !== null && tail.textContent === '+3 más' && tail.children.length === 0 && tail.tagName === 'LI');
  ok('it is a quiet 16 px line, 18 px in like the row text, and it is not a button or a row', Math.abs(px(tail).h - 16) < .6 && Math.abs(px(tail).x - px(rows[0]).x - 18) < 1
    && !tail.classList.contains('task') && !tail.hasAttribute('data-id') && tail.querySelector('button') === null);
  ok('tapping it opens nothing', (function () { tap(tail); return !reading(); })());
  ok('the list measures what the rows, captions and the last line add up to', L().scrollHeight === 44 * 8 + 10 * (8 + 3 - 1) + 10 * 3 + 18 + 26, L().scrollHeight);
  ok('every row is a 44 px button, 384 px wide (366 as a child)', rows.every(function (li) {
    var r = px(li.firstChild);
    return li.firstChild.tagName === 'BUTTON' && Math.abs(r.h - 44) < .6 && Math.abs(r.w - (li.classList.contains('kid') ? 366 : 384)) < 1;
  }));
  /* the right of every run row is how long it has been running; the threads keep their age */
  var clock = function (id) { return row(id).firstChild.children[3].textContent; };
  ok('elapsed is h:mm:ss from an hour on and m:ss before', clock('run-tienda-web') === '1:00:43' && clock('run-api-pagos-sandbox-regional') === '28:50'
    && clock('run-docs-sitio') === '1:35' && clock('run-migracion-correo') === '10:12' && clock('job-respaldo-nocturno') === '10:00:00');
  ok('a thread still shows the time since it spoke', clock('b1') === '0:06' && clock('t1') === '0:12');
  ok('a run is never marked as finished', rows.filter(isCensus).every(function (li) { return !li.classList.contains('idle') && cs(li.firstChild.children[2]).display === 'none'; }));
  /* what the middle of a row says */
  var mid = function (id) { return row(id).firstChild.children[1]; };
  ok('a run whose session spoke lately shows what it does, and is not quiet', mid('run-tienda-web').textContent === 'Bash npm run build'
    && !row('run-tienda-web').classList.contains('quiet') && cs(mid('run-tienda-web')).display !== 'none');
  ok('a silent run (idle >= 120) says "callada hace N min" and nothing else', row('run-api-pagos-sandbox-regional').classList.contains('quiet')
    && mid('run-api-pagos-sandbox-regional').textContent === 'callada hace 9 min');
  ok('the silence notice is read whole and the long name is what gets the ellipsis', (function () {
    var b = row('run-api-pagos-sandbox-regional').firstChild.children[0], m = mid('run-api-pagos-sandbox-regional');
    return m.scrollWidth <= m.clientWidth && b.scrollWidth > b.clientWidth;
  })());
  ok('the elapsed time is never clipped, and the row does not overflow', rows.filter(isCensus).every(function (li) {
    var btn = li.firstChild, i = btn.children[3], chev = btn.children[4];
    return btn.scrollWidth <= btn.clientWidth && px(i).x + px(i).w <= px(chev).x + .6 && i.scrollWidth <= i.clientWidth;
  }));
  ok('a run that spoke 31 s ago is not quiet and says nothing; its name takes the room', !row('run-docs-sitio').classList.contains('quiet')
    && mid('run-docs-sitio').textContent === '' && cs(mid('run-docs-sitio')).display === 'none');
  ok('a long name is whole when nothing else is in the row', (function () {
    var b = row('run-migracion-correo').firstChild.children[0];
    return px(b).w > 150 && b.scrollWidth <= b.clientWidth && cs(mid('run-migracion-correo')).display === 'none';
  })());
  ok('a job with no idle is not quiet either', !row('job-respaldo-nocturno').classList.contains('quiet') && mid('job-respaldo-nocturno').textContent === '');
  ok('a thread never says "callada", and its tool and summary stay in the row', !row('t1').classList.contains('quiet') && mid('t1').textContent === 'Bash git pull');
  /* a tap on a run opens the same detail, with the name, the elapsed time and the silence */
  var opened = function (id) {
    closeTask(); tap(row(id).firstChild);
    return { name: $('tv-name').textContent, age: $('tv-age').textContent, did: $('tv-did').textContent, pre: $('tv-pre').textContent,
             text: $('tv-text').textContent, none: $('tv-said').classList.contains('none'), long: $('task-view').classList.contains('long'), on: reading() };
  };
  var d = opened('run-tienda-web');
  ok('a run with its session talking: name, "lleva" elapsed, group and what it does, what it said', d.on && d.name === 'tienda-web' && d.age === 'lleva 1:00:43'
    && d.did === 'Corridas · Bash npm run build' && d.pre === 'Antes dijo: ' && d.text.indexOf('Compilé la tienda') === 0 && !d.none, JSON.stringify(d));
  d = opened('run-api-pagos-sandbox-regional');
  ok('a silent run: elapsed, "callada hace 9 min" and the same in a sentence', d.on && d.age === 'lleva 28:50' && d.did === 'Corridas · callada hace 9 min'
    && d.text === 'Sigue corriendo, pero no escribe nada desde hace 9 min.' && d.none && d.pre === '' && d.long, JSON.stringify(d));
  d = opened('run-docs-sitio');
  ok('a run that spoke lately: when', d.age === 'lleva 1:35' && d.did === 'Corridas · trabajando' && d.text === 'Sigue corriendo. Escribió hace 0:31.' && d.none, JSON.stringify(d));
  d = opened('run-migracion-correo');
  ok('a run with no transcript to look at: only that it is running', d.age === 'lleva 10:12' && d.text === 'Sigue corriendo; no hay más detalle de lo que hace.', JSON.stringify(d));
  d = opened('job-respaldo-nocturno');
  ok('a ten hour job', d.age === 'lleva 10:00:00' && d.name === 'respaldo-nocturno' && d.did === 'Corridas · trabajando', JSON.stringify(d));
  ok('the detail of a run is as tall as any other: 246 px, Volver 44 px', Math.abs(px($('task-view')).h - 246) < 1 && Math.abs(px($('tv-back')).h - 44) < .6);
  closeTask();
  d = opened('b1');
  ok('a thread still says "hace" and its group', d.age === 'hace 0:06' && d.did === 'Buddy · Bash pytest -q' && d.text === 'Todavía no dijo nada.', JSON.stringify(d));
"""


@needs_chromium
def test_runs_sit_under_corridas_with_their_count_their_clock_and_what_is_left_out(tmp_path):
    assert_all(battery(tmp_path, RUNS_JS, query="demo=runs", budget=3000), 28)


RUNS_ORDER_JS = r"""
  var base = seq(), nodes = kids();
  var ys = {};
  rowsOnly().forEach(function (li) { ys[li.getAttribute('data-id')] = px(li).y; });
  show(function (v) { v.tasks.reverse(); });
  ok('the server flipping its order moves nothing', seq() === base, seq());
  show(function (v) {
    v.tasks.forEach(function (t) { if (t.source === 'census') { t.elapsed += 30; if (typeof t.idle === 'number') { t.idle += 30; t.age = t.idle; } } });
  });
  ok('time passing moves nothing and reuses every node', seq() === base && kids().every(function (el, i) { return el === nodes[i]; }));
  ok('the clocks did move', row('run-tienda-web').firstChild.children[3].textContent === '1:01:13' && row('run-docs-sitio').firstChild.children[3].textContent === '2:05');
  ok('a run that was not quiet becomes quiet in place', (function () {
    show(function (v) { v.tasks.forEach(function (t) { if (t.id === 'run-docs-sitio') { t.idle = 400; t.age = 400; } }); });
    return row('run-docs-sitio').classList.contains('quiet') && row('run-docs-sitio').firstChild.children[1].textContent === 'callada hace 6 min' && seq() === base;
  })());
  /* a run ends: only its own row goes */
  show(function (v) { v.tasks = v.tasks.filter(function (t) { return t.id !== 'run-docs-sitio'; }); v.census_total = 6; v.tasks_total = 10; });
  ok('a run that ends takes only its own row; nobody else changes place', seq() === base.replace('run-docs-sitio|', ''), seq());
  ok('and the header follows the count', names() === 'Corridas · 6|Buddy|Terminal', names());
  ok('the rows above it did not move a pixel', ['run-tienda-web', 'run-api-pagos-sandbox-regional'].every(function (id) { return Math.abs(px(row(id)).y - ys[id]) < .6; }));
  ok('the rows after it came up by one row and one gap', Math.abs(ys['run-migracion-correo'] - px(row('run-migracion-correo')).y - 54) < .6);
  ok('the surviving nodes are the same elements', ['run-tienda-web', 'run-migracion-correo', 'b1', 't1'].every(function (id) { return nodes.indexOf(row(id)) >= 0; }));
  /* a run starts: it joins the end of the group, not the top, even when the server sends it first */
  show(function (v) { v.tasks.unshift(CR('run-nueva', { elapsed: 12 })); v.census_total = 7; v.tasks_total = 11; });
  ok('a new run joins the end of its group', seq() === 'g:Corridas|run-tienda-web|run-api-pagos-sandbox-regional|run-migracion-correo|job-respaldo-nocturno|run-nueva|g:Buddy|b1|b2|g:Terminal|t1|more', seq());
  ok('and the header counts it', names() === 'Corridas · 7|Buddy|Terminal', names());
  /* the oldest run ends and comes back: it lost its seat */
  show(function (v) { v.tasks = v.tasks.filter(function (t) { return t.id !== 'run-tienda-web'; }); });
  show(function (v) { v.tasks.push(CR('run-tienda-web', { elapsed: 3700 })); });
  ok('a run that left and came back goes last in its group', seq().indexOf('run-nueva|run-tienda-web|g:Buddy') > 0, seq());
  /* the "+K más" line is one element: it follows its number, goes when there is nothing left out, and comes back last */
  var tail = tailLine();
  show(function (v) { v.tasks_more = 4; });
  ok('the last line updates in place', tailLine() === tail && tail.textContent === '+4 más');
  show(function (v) { v.tasks_more = 0; });
  ok('with nothing left out the line is gone', tailLine() === null && L().querySelector('.more') === null);
  show(function (v) { v.tasks_more = 12; });
  ok('and it comes back at the very end', tailLine() !== null && tailLine().textContent === '+12 más' && seq().slice(-5) === '|more');
  /* every census row leaves: the Corridas caption goes with them, the threads stay */
  show(function (v) { v.tasks = v.tasks.filter(function (t) { return t.source !== 'census'; }); v.census_total = 0; v.tasks_more = 0; v.working_total = 2; });
  ok('without runs there is no Corridas caption', seq() === 'g:Buddy|b1|b2|g:Terminal|t1' && names() === 'Buddy|Terminal', seq());
  ok('and the headline follows working_total', $('feed-msg').textContent === '2 tareas en marcha');
"""


@needs_chromium
def test_run_rows_do_not_jump_when_one_appears_or_disappears(tmp_path):
    assert_all(battery(tmp_path, RUNS_ORDER_JS, query="demo=runs", budget=3000), 17)


RUNS_EDGE_JS = r"""
  /* the page itself draws at most 5 census rows, whatever the server sent: the rest are counted in the last line */
  fresh();
  show(function (v) {
    v.tasks = [1, 2, 3, 4, 5, 6, 7].map(function (n) { return CR('run-r' + n, { elapsed: 9000 - n }); });
    v.census_total = 7; v.tasks_total = 7; v.tasks_more = 0; v.working_total = 7;
  });
  ok('a server that sends 7 census rows gets 5 drawn, the oldest first', seq() === 'g:Corridas|run-r1|run-r2|run-r3|run-r4|run-r5|more', seq());
  ok('the 2 the page left out are in "+2 más"; the header and the headline still say 7', tailLine().textContent === '+2 más' && names() === 'Corridas · 7'
    && $('feed-msg').textContent === '7 tareas en marcha');
  show(function (v) { v.tasks_more = 3; });
  ok('what the server left out is added: "+5 más"', tailLine().textContent === '+5 más');
  show(function (v) { v.tasks.push(T('t1', { group: 'Buddy' })); v.tasks_more = 0; });
  ok('hook rows are not census rows: all of them are drawn', rowsOnly().length === 6 && seq().indexOf('|t1') > 0 && tailLine().textContent === '+2 más', seq());
  tap(row('run-r5').firstChild);
  ok('the fifth census row opens its detail', reading() && $('tv-name').textContent === 'n-run-r5');
  closeTask();
  /* numbers that are not numbers do not break the screen */
  fresh();
  var odd = [[ 'abc', -4, 'x' ], [ null, null, null ], [ 0, 0, 0 ], [ -3, 1e9 + .5, 1.9 ]];
  odd.forEach(function (o, k) {
    var threw = '';
    try {
      show(function (v) { v.tasks = [CR('run-a', {}), CR('run-b', {})]; v.census_total = o[0]; v.tasks_more = o[1]; v.working_total = o[2]; });
    } catch (e) { threw = e.message; }
    ok('odd totals ' + JSON.stringify(o) + ' draw without an exception', threw === '' && rowsOnly().length === 2, threw);
    ok('and the header never says less than the rows drawn', /^Corridas · \d+$/.test(names()) && Number(names().split(' · ')[1]) >= 2, names());
  });
  show(function (v) { v.tasks = [CR('run-a', {})]; v.census_total = 'abc'; v.tasks_more = -4; v.working_total = 'x'; });
  ok('a text for a total is no total: the caption counts the rows, no last line, the headline counts the rows', names() === 'Corridas · 1' && tailLine() === null
    && $('feed-msg').textContent === 'Una tarea en marcha', names() + ' / ' + $('feed-msg').textContent);
  show(function (v) { v.tasks = [CR('run-a', {})]; v.census_total = 1; v.tasks_more = -4; v.working_total = -2; });
  ok('a negative total is nothing: no last line, and nobody works', tailLine() === null && $('feed-msg').textContent === 'Nadie trabaja ahora.', $('feed-msg').textContent);
  show(function (v) { v.tasks = [CR('run-a', {}), CR('run-b', {})]; delete v.census_total; delete v.tasks_more; delete v.working_total; });
  ok('an old daemon with no totals at all: rows are counted, no last line', names() === 'Corridas · 2' && tailLine() === null && $('feed-msg').textContent === '2 tareas en marcha');
  show(function (v) { v.tasks = [CR('run-a', {})]; v.working_total = 0; v.census_total = 1; });
  ok('working_total 0 is believed: nobody works', $('feed-msg').textContent === 'Nadie trabaja ahora.');
  /* elapsed and idle that are not what they should be */
  show(function (v) { v.tasks = [CR('run-a', { elapsed: 'abc' }), CR('run-b', { elapsed: -5 }), CR('run-c', { elapsed: 2678400 }), CR('run-d', {})]; delete v.tasks[3].elapsed; });
  ok('a bad or missing elapsed is 0:00, the biggest is 744:00:00', ['run-a', 'run-b', 'run-d'].every(function (id) { return row(id).firstChild.children[3].textContent === '0:00'; })
    && row('run-c').firstChild.children[3].textContent === '744:00:00');
  ok('the biggest clock is not clipped', (function () { var btn = row('run-c').firstChild; return btn.scrollWidth <= btn.clientWidth && btn.children[3].scrollWidth <= btn.children[3].clientWidth; })());
  show(function (v) {
    v.tasks = [CR('run-a', { idle: -5 }), CR('run-b', { idle: null }), CR('run-c', { idle: 119 }), CR('run-d', { idle: 120 }), CR('run-e', { idle: 7200 })];
  });
  var say = function (id) { return row(id).firstChild.children[1].textContent; };
  ok('idle under 120 s is not silence, 120 s on is', say('run-a') === '' && say('run-b') === '' && say('run-c') === '' && say('run-d') === 'callada hace 2 min', [say('run-a'), say('run-c'), say('run-d')].join('|'));
  show(function (v) { v.tasks.push(CR('run-f', { idle: 200000 })); v.tasks.shift(); });
  ok('a very long silence is said in hours or days, not in thousands of minutes', say('run-e') === 'callada hace 2 h' && say('run-f') === 'callada hace 2 d', say('run-e') + ' | ' + say('run-f'));
  ok('the 120 s mark is a clean line', !row('run-c').classList.contains('quiet') && row('run-d').classList.contains('quiet'));
  show(function (v) { v.tasks = [T('h1', { state: 'idle', age: 10800, idle: 10800, tool: 'Bash', what: 'ls' })]; });
  ok('a thread (not a run) with an "idle" field is never quiet', !row('h1').classList.contains('quiet') && row('h1').firstChild.children[1].textContent === 'terminó: Bash ls');
  /* a census group that is not alone: the count is only claimed where it is known */
  fresh();
  show(function (v) {
    v.tasks = [CR('run-a', {}), T('h1', { group: 'Corridas' }), CR('run-b', { group: 'Otros' }), CR('run-c', { group: 'Otros' })];
    v.census_total = 9;
  });
  ok('a group of runs mixed with threads, or two groups of runs, do not claim the server total', names() === 'Corridas|Otros · 2', names());
  show(function (v) { v.tasks = [CR('run-a', {}), CR('run-b', {}), CR('run-c', { group: 'Otros' }), T('h1', { group: 'Buddy' })]; v.census_total = 9; });
  ok('two groups of only runs count what each draws', names() === 'Corridas · 2|Otros · 1|Buddy', names());
  show(function (v) { v.tasks = [CR('run-a', {}), CR('run-b', {}), T('h1', { group: 'Buddy' })]; v.census_total = 9; });
  ok('one group of only runs claims census_total', names() === 'Corridas · 9|Buddy', names());
"""


@needs_chromium
def test_the_runs_survive_odd_numbers_the_page_cap_and_groups_that_are_not_alone(tmp_path):
    assert_all(battery(tmp_path, RUNS_EDGE_JS, query="demo=runs", budget=3000), 26)


RUNS_HOSTILE_JS = r"""
  var evil = '<img src=x onerror="window.pwned=1"><b>x</b>';
  fresh();
  var threw = '';
  try {
    show(function (v) {
      v.tasks = [CR('run-e', { title: evil, project: evil, group: evil, tool: evil, what: evil, said: evil + '\n' + evil, elapsed: 3700, idle: 500 }),
                 CR('run-constructor', { group: '__proto__', title: 'constructor', project: 'constructor', idle: 20 }),
                 CR('job-__proto__', { group: '__proto__', title: '__proto__', project: '__proto__' }),
                 CR('run-toString', { group: 'constructor', title: 'toString', project: 'toString' })];
      v.census_total = 4; v.tasks_total = 4; v.working_total = 4; v.tasks_more = 0;
    });
  } catch (e) { threw = e.message; }
  ok('hostile run names, groups and ids render without an exception', threw === '' && rowsOnly().length === 4, threw);
  ok('the row, the caption and the silence notice are text', row('run-e').firstChild.children[0].textContent === evil && row('run-e').querySelectorAll('img').length === 0
    && L().querySelector('img') === null && window.pwned === undefined);
  ok('the caption is the group, a dot and the count, all literal', captions()[0].textContent === evil + ' · 1' && captions()[0].getAttribute('data-g') === evil
    && captions().map(function (c) { return c.getAttribute('data-g'); }).join('|') === evil + '|__proto__|constructor', names());
  ok('every group of only runs counts what it draws', names() === evil + ' · 1|__proto__ · 2|constructor · 1', names());
  ok('names that poison plain objects are fine', ['run-constructor', 'job-__proto__', 'run-toString'].every(function (id) { return !!row(id); })
    && row('run-constructor').firstChild.children[0].textContent === 'constructor');
  tap(row('run-e').firstChild);
  ok('the detail shows it as text', reading() && $('tv-name').textContent === evil && $('tv-name').children.length === 0
    && $('tv-did').children.length === 0 && $('tv-did').textContent.indexOf(evil + ' · ') === 0
    && $('tv-text').children.length === 0 && $('tv-text').textContent.indexOf('<img') === 0 && $('tv-age').textContent === 'lleva 1:01:40');
  ok('and no element came out of it', document.querySelectorAll('#feed img, #feed b b').length === 0 && window.pwned === undefined);
  closeTask();
  show(function (v) { v.tasks = v.tasks.filter(function (t) { return t.id === 'run-toString'; }); v.census_total = 1; });
  ok('they leave cleanly', seq() === 'g:constructor|run-toString', seq());
"""


@needs_chromium
def test_a_hostile_run_name_group_or_id_is_only_text(tmp_path):
    assert_all(battery(tmp_path, RUNS_HOSTILE_JS, query="demo=runs", budget=3000), 8)


RUNS_COUNT_JS = r"""
  /* a run leaves the board while its detail is open: what was last known stays and BOTH clocks keep counting */
  fresh();
  show(function (v) {
    v.tasks = [CR('run-a', { elapsed: 3595, idle: 118 })]; v.census_total = 1; v.tasks_more = 0; v.working_total = 1;
  });
  tap(row('run-a').firstChild);
  ok('open: it has run 59:55 and spoke 1:58 ago', reading() && $('tv-age').textContent === 'lleva 59:55' && $('tv-text').textContent === 'Sigue corriendo. Escribió hace 1:58.', $('tv-age').textContent + ' ' + $('tv-text').textContent);
  show(function (v) { v.tasks = []; v.census_total = 0; v.working_total = 0; });
  ok('it leaves and the detail stays', reading() && $('tv-name').textContent === 'n-run-a');
  step(5000, function () {
    ok('5 s later the elapsed time kept counting past the hour', /^lleva (59:5[89]|1:00:0[0-3])$/.test($('tv-age').textContent), $('tv-age').textContent);
    ok('and the silence kept counting too: now it is "callada"', $('tv-did').textContent === 'Corridas · callada hace 2 min' && /^Sigue corriendo, pero no escribe nada desde hace 2 min\.$/.test($('tv-text').textContent),
      $('tv-did').textContent + ' | ' + $('tv-text').textContent);
  });
"""


@needs_chromium
def test_the_detail_of_a_run_that_left_keeps_both_clocks_running(tmp_path):
    assert_all(battery(tmp_path, RUNS_COUNT_JS, query="demo=busy", budget=9000), 4)


RUNS_LIGHT_JS = r"""
  var bubble = cs($('feed')).backgroundColor;
  var rows = rowsOnly();
  ok('run row names read on their tint (7:1)', rows.every(function (li) { return contrast(cs(li.firstChild.children[0]).color, over(rgba(cs(li.firstChild).backgroundColor), rgba(bubble))) >= 7; }));
  ok('what a run does and the silence notice read on their tint (4.5:1 and 7:1)', rows.every(function (li) {
    var tint = over(rgba(cs(li.firstChild).backgroundColor), rgba(bubble)), span = li.firstChild.children[1];
    return cs(span).display === 'none' || contrast(cs(span).color, tint) >= (li.classList.contains('quiet') ? 7 : 4.5);
  }));
  ok('the elapsed time reads (4.5:1)', rows.every(function (li) { return contrast(cs(li.firstChild.children[3]).color, over(rgba(cs(li.firstChild).backgroundColor), rgba(bubble))) >= 4.5; }));
  ok('the header and the "+K más" line read on paper (4.5:1)', contrast(cs(captions()[0]).color, bubble) >= 4.5 && contrast(cs(tailLine()).color, bubble) >= 4.5);
"""


@needs_chromium
def test_run_rows_keep_their_contrast_on_paper(tmp_path):
    assert_all(battery(tmp_path, RUNS_LIGHT_JS, query="demo=runs", budget=3000), 4)


RUNS_PACK_JS = r"""
  var bubble = cs($('feed')).backgroundColor;
  ok('pack mode is on and the runs are drawn', document.body.classList.contains('pack') && rowsOnly().length === 8 && names() === 'Corridas · 7|Buddy|Terminal', names());
  ok('run names read on the pack bubble (7:1)', rowsOnly().every(function (li) { return contrast(cs(li.firstChild.children[0]).color, bubble) >= 7; }));
  ok('the silence notice reads (7:1) and the run text (4.5:1)', contrast(cs(row('run-api-pagos-sandbox-regional').firstChild.children[1]).color, bubble) >= 7
    && contrast(cs(row('run-tienda-web').firstChild.children[1]).color, bubble) >= 4.5);
  ok('the header and "+K más" read (4.5:1)', contrast(cs(captions()[0]).color, bubble) >= 4.5 && contrast(cs(tailLine()).color, bubble) >= 4.5);
  tap(row('run-api-pagos-sandbox-regional').firstChild);
  ok('the detail of a run opens in pack mode and reads (7:1 and 4.5:1)', reading() && contrast(cs($('tv-name')).color, bubble) >= 7 && contrast(cs($('tv-did')).color, bubble) >= 4.5
    && contrast(cs($('tv-text')).color, bubble) >= 4.5);
"""


@needs_chromium
def test_runs_stay_legible_in_pack_mode(tmp_path):
    assert_all(battery(tmp_path, RUNS_PACK_JS, query="demo=runs-pack", budget=3000), 5)


OLD_DEMOS_JS = r"""
  var expected = EXPECTED;
  Object.keys(expected).forEach(function (name) {
    ok(name + ': the demo is still there', Object.prototype.hasOwnProperty.call(DEMOS, name) && demoNames.indexOf(name) >= 0);
    try {
      demoShow(name);
      ok(name + ': it picks the same screen (' + expected[name] + ')', document.body.getAttribute('data-screen') === expected[name], document.body.getAttribute('data-screen'));
    } catch (e) { ok(name + ' threw: ' + e.message, false); }
  });
  ok('the new demos are in the cycle too', ['runs', 'runs1', 'runs-pack'].every(function (n) { return demoNames.indexOf(n) >= 0; }));
"""


@needs_chromium
def test_every_demo_the_page_had_before_the_runs_still_renders_its_screen(tmp_path):
    body = OLD_DEMOS_JS.replace("EXPECTED", json.dumps(OLD_DEMOS))
    assert_all(battery(tmp_path, body, query="demo=idle", budget=3000), 2 * len(OLD_DEMOS))


@needs_chromium
def test_the_runs_demos_render_the_group_and_the_last_line_without_a_daemon():
    dom = dump(f"{PAGE.as_uri()}?demo=runs&tag=0")
    assert attr(dom, "data-screen") == "main"
    listing = re.search(r'<ul id="feed-list"[^>]*>(.*?)</ul>', dom, re.S).group(1)
    assert re.search(r'<li class="grp" data-g="Corridas">Corridas [^<0-9]{1,8} 7</li>', listing)  # "Corridas · 7"
    assert re.search(r'<li class="more" data-more="">\+3 m[^<]{1,4}s</li>', listing) and re.search(r'\+3 m[^<]{1,4}s</li>\s*$', listing)  # "+3 más", last
    assert len(re.findall(r'<li class="task', listing)) == 8
    assert re.search(r'id="feed-msg"[^>]*>9 tareas en marcha<', dom)
    assert "1:00:43" in listing and "10:00:00" in listing and "callada hace 9 min" in listing
    dom = dump(f"{PAGE.as_uri()}?demo=runs1&tag=0")
    assert re.search(r'<li class="grp" data-g="Corridas">Corridas [^<0-9]{1,8} 1</li>', dom) and 'class="more"' not in dom
    assert re.search(r'id="feed-msg"[^>]*>Una tarea en marcha<', dom)
    dom = dump(f"{PAGE.as_uri()}?demo=runs&open=job-respaldo-nocturno&tag=0")
    assert re.search(r'<div class="tv-name" id="tv-name">respaldo-nocturno<', dom)
    assert re.search(r'id="tv-age">lleva 10:00:00<', dom)


# --- the demos, with no daemon ----------------------------------------------------------------------------------


@needs_chromium
@pytest.mark.parametrize("demo,children,captions", [
    ("idle", 5, 1), ("tasks1", 2, 1), ("tasks6", 7, 1), ("longsaid", 2, 1), ("idle-pack", 5, 1),
    ("threads", 8, 3), ("threads-long", 4, 1), ("threads8", 13, 5), ("threads-pack", 8, 3),
    ("runs", 11, 3), ("runs1", 2, 1), ("runs-pack", 11, 3),
])
def test_every_thread_demo_renders_without_a_daemon(demo, children, captions):
    dom = dump(f"{PAGE.as_uri()}?demo={demo}&tag=0")
    assert attr(dom, "data-screen") == "main"
    listing = re.search(r'<ul id="feed-list"[^>]*>(.*?)</ul>', dom, re.S).group(1)
    assert len(re.findall(r'<li class="(?:task|grp)', listing)) == children
    assert len(re.findall(r'<li class="grp"', listing)) == captions
    assert ("pack" in demo) == bool(re.search(r'<body[^>]*\bclass="[^"]*\bpack\b', dom))


@needs_chromium
def test_the_open_and_scroll_flags_work_in_a_demo_only():
    dom = dump(f"{PAGE.as_uri()}?demo=threads&open=1&tag=0")
    assert re.search(r'<div id="feed" class="[^"]*\breading\b', dom)
    assert re.search(r'<div class="tv-name" id="tv-name">Respaldos del servidor<', dom)  # the second row in display order
    dom = dump(f"{PAGE.as_uri()}?demo=threads&open=b1&tag=0")
    assert re.search(r'<div class="tv-name" id="tv-name">Hilos en la pantalla del Buddy<', dom)  # an id works too
    dom = dump(f"{PAGE.as_uri()}?demo=threads&open=9&tag=0")
    assert not re.search(r'<div id="feed" class="[^"]*\breading\b', dom)  # nothing at that place: nothing opens


# --- the real data path: board to view to page ------------------------------------------------------------------


@needs_chromium
def test_a_live_screen_groups_what_the_board_reports(rig):
    async def scenario():
        rig.rx(heartbeat())
        board = rig.core.tasks
        board.update("b1", project="claude-buddy", tool="Bash", what="pytest -q", title="Hilos en la pantalla", group="Buddy")
        rig.t += 1
        board.update("t1", project="api-pagos", tool="Bash", what="git pull")
        rig.t += 1
        board.update("b2", project="claude-buddy", tool="Read", what="ui/index.html", title="Subtarea", group="Buddy", parent="b1")
        rig.t += 1
        board.update("f1", project="~", tool="Edit", what="x.py", title="Bitácora", group="Acme Studio", state="idle", said="Listo.")
        async with serving(rig) as server:
            return await asyncio.get_running_loop().run_in_executor(None, dump, server.url + "/?poll=300", 2500)

    dom = run(scenario)
    assert attr(dom, "data-screen") == "main"
    listing = re.search(r'<ul id="feed-list"[^>]*>(.*?)</ul>', dom, re.S).group(1)
    seen = re.findall(r'<li class="([^"]*)" data-(?:g|id)="([^"]*)"', listing)
    # the server sends working first, newest first (b2, t1, b1), then the finished f1: the page ranks a group by its
    # first member in that order, so Buddy (first seen through its newest thread b2) leads, then Terminal, then Acme
    assert [name for _, name in seen] == ["Buddy", "b1", "b2", "Terminal", "t1", "Acme Studio", "f1"]
    classes = dict((name, cls.split()) for cls, name in seen)
    assert classes["b2"] == ["task", "kid", "titled"] and classes["f1"] == ["task", "idle", "titled"]
    assert classes["t1"] == ["task"] and classes["Buddy"] == ["grp"]
    assert "<b>Hilos en la pantalla</b>" in listing and "<b>api-pagos</b>" in listing
    assert re.search(r'id="feed-msg"[^>]*>3 tareas en marcha<', dom)


@needs_chromium
def test_a_live_screen_draws_the_census_under_corridas(rig):
    async def scenario():
        rig.rx(heartbeat())
        board = rig.core.tasks
        board.update("b1", project="claude-buddy", tool="Bash", what="pytest -q", title="Hilos", group="Buddy")
        rows = [{"id": f"run-r{i}", "name": f"corrida-{i}", "elapsed": 4000 - 100 * i, "idle": 540 if i == 1 else 5} for i in range(7)]
        rows[2]["name"] = "<b>x</b>"
        rows.append({"id": "job-respaldo", "name": "respaldo", "elapsed": 36000})
        board.replace_census("mac", rows, 60)  # 7 runs and a job: the oldest 5 runs are drawn, the other 3 are "+3 más"
        async with serving(rig) as server:
            return await asyncio.get_running_loop().run_in_executor(None, dump, server.url + "/?poll=300", 2500)

    dom = run(scenario)
    assert attr(dom, "data-screen") == "main"
    listing = re.search(r'<ul id="feed-list"[^>]*>(.*?)</ul>', dom, re.S).group(1)
    seen = re.findall(r'<li class="([^"]*)" data-(?:g|id)="([^"]*)"', listing)
    assert [name for _, name in seen] == ["Corridas", "run-r0", "run-r1", "run-r2", "run-r3", "run-r4", "Buddy", "b1"]
    assert re.search(r'data-g="Corridas">Corridas [^<0-9]{1,8} 8</li>', listing)  # the 8 alive, not the 5 drawn
    assert re.search(r'<li class="more" data-more="">\+3 m[^<]{1,4}s</li>\s*$', listing)  # the 3 that do not fit, last
    assert re.search(r'id="feed-msg"[^>]*>9 tareas en marcha<', dom)  # 8 runs and 1 thread work, not the 6 rows drawn
    assert "<span>callada hace 9 min</span>" in listing and "<i>1:06:40</i>" in listing and "<i>1:05:00</i>" in listing
    assert "&lt;b&gt;x&lt;/b&gt;" in listing and "<b>x</b>" not in listing  # a folder name is text
    classes = dict((name, cls.split()) for cls, name in seen)
    assert classes["run-r1"] == ["task", "quiet"] and classes["run-r0"] == ["task", "titled"] and classes["b1"] == ["task", "titled"]


@needs_chromium
def test_a_live_screen_survives_threads_named_constructor_and_proto(rig):
    async def scenario():
        rig.rx(heartbeat())
        rig.core.tasks.update("constructor", project="p", tool="Bash", what="ls", group="__proto__")
        rig.core.tasks.update("__proto__", project="q", tool="Edit", what="a.py", group="constructor", title="<b>x</b>")
        rig.core.tasks.update("kid", project="q", tool="Read", what="b.py", group="__proto__", parent="__proto__")
        async with serving(rig) as server:
            return await asyncio.get_running_loop().run_in_executor(None, dump, server.url + "/?poll=300", 2500)

    dom = run(scenario)
    assert attr(dom, "data-screen") == "main"
    listing = re.search(r'<ul id="feed-list"[^>]*>(.*?)</ul>', dom, re.S).group(1)
    assert 'data-id="constructor"' in listing and 'data-id="__proto__"' in listing
    assert 'data-g="__proto__"' in listing and 'data-g="constructor"' in listing
    assert "&lt;b&gt;x&lt;/b&gt;" in listing and "<b>x</b>" not in listing
    assert 'data-id="kid"' in listing and 'class="task kid"' in listing  # a child of a thread called __proto__ hangs under it
    assert re.search(r'id="feed-msg"[^>]*>3 tareas en marcha<', dom)  # the render got to the end: the screen did not freeze
