"""The two languages of the buddy (es, the default, and en): the page's table, the daemon's few strings, the flag.

The page keeps every text it says in one I18N table (ui/index.html). These tests read that table from the page's source (no
browser needed) and then run the page in a headless Chromium (they skip when there is none) to see every demo in both
languages: no missing-key marker, nothing left in the other language, and nothing that no longer fits the 800x480 screen."""
import ast
import asyncio
import json
import os
import re
import signal
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import buddy  # noqa: E402
import buddy_i18n  # noqa: E402
from buddy_i18n import LANGS, ble_reason  # noqa: E402
from buddy_ui import UIHub  # noqa: E402
from page_runner import attr, dump, needs_chromium  # noqa: E402
from test_page_tasks import assert_all, battery  # noqa: E402
from test_ui import PAGE, Rig, run, serving  # noqa: E402
from ui_client import http  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PAGE_TEXT = PAGE.read_text(encoding="utf-8")

# --- what the Spanish was before there was a table (these are the old texts, word for word) -------------------------

PINNED_ES = {
    'net.open': 'sin cifrado',
    'boot.title': 'Conectando…',
    'boot.note': 'Esperando al buddy.',
    'wait.title': 'Esperando a Claude desktop',
    'wait.s1a': 'En la Mac abrí el menú ',
    'wait.s1b': ' y elegí ',
    'wait.s2a': 'Elegí ',
    'wait.s2b': ' y tocá ',
    'wait.note': 'Si ya lo emparejaste antes, se conecta solo.',
    'down.title.notready': 'Bluetooth no está listo',
    'down.title.starting': 'Iniciando Bluetooth…',
    'down.note.retry': 'El buddy lo reintenta solo.',
    'down.note.starting': 'Un momento.',
    'pair.title': 'Emparejar con Claude desktop',
    'pair.note': 'Escribí este código en la Mac cuando lo pida.',
    'pair.progress': 'Van {n} de {total} dígitos',
    'mood.sleep': 'Durmiendo',
    'mood.idle': 'Tranquilo',
    'mood.busy': 'Trabajando',
    'mood.attention': 'Te necesita',
    'mood.celebrate': '¡Terminó!',
    'mood.dizzy': 'Mareado',
    'mood.heart': 'Contento',
    'plate.owner': 'de {owner}',
    'plate.level': 'Nivel {n}',
    'pill.sessions': 'sesiones',
    'pill.running': 'trabajan',
    'pill.waiting': 'esperan',
    'foot.tokens': 'tokens hoy',
    'foot.approved': 'aprobadas',
    'foot.denied': 'denegadas',
    'feed.nothing': 'Sin novedades por ahora.',
    'feed.idle': 'Nadie trabaja ahora.',
    'feed.tasks.one': 'Una tarea en marcha',
    'feed.tasks.other': '{n} tareas en marcha',
    'feed.notices.one': 'Un aviso',
    'feed.notices.other': '{n} avisos',
    'empty.waiting': 'Te esperan en la Mac.',
    'empty.running': 'Todo en marcha.',
    'empty.calm': 'Todo tranquilo.',
    'empty.here': 'Acá espero.',
    'detail.waiting': 'Una sesión necesita algo que solo se decide allá.',
    'detail.none': 'No hay sesiones abiertas en la Mac.',
    'detail.running.one': 'Una sesión trabaja y nada que decidir.',
    'detail.running.other': '{n} sesiones trabajan y nada que decidir.',
    'detail.open.one': 'Una sesión abierta, ninguna trabajando.',
    'detail.open.other': '{n} sesiones abiertas, ninguna trabajando.',
    'msg.asked': 'Te hizo una pregunta en la Mac',
    'msg.used': 'Usó {tool}',
    'thread.fallback': 'sesión',
    'group.terminal': 'Terminal',
    'group.runs': 'Corridas',
    'group.notices': 'Avisos · {n}',
    'row.done': 'terminó',
    'row.done.with': 'terminó: {what}',
    'row.working': 'trabajando',
    'row.quiet': 'callada hace {span}',
    'row.more': '+{n} más',
    'span.min': '{n} min',
    'span.h': '{n} h',
    'span.d': '{n} d',
    'ago': 'hace {age}',
    'tv.for': 'lleva {age}',
    'tv.before': 'Antes dijo: ',
    'tv.none.idle': 'No dejó ningún mensaje.',
    'tv.none.working': 'Todavía no dijo nada.',
    'run.note.unknown': 'Sigue corriendo; no hay más detalle de lo que hace.',
    'run.note.quiet': 'Sigue corriendo, pero no escribe nada desde hace {span}.',
    'run.note.talking': 'Sigue corriendo. Escribió hace {age}.',
    'tv.done': 'Listo',
    'tv.back': 'Volver',
    'notice.label': 'Aviso',
    'notice.from': 'de {source}',
    'notice.nodetail': 'Sin más detalle.',
    'ask.default': 'Permiso',
    'ask.question': 'Te pregunta algo',
    'ask.note': 'Respondé en la Mac: acá no se puede elegir una opción.',
    'ask.saves': ' guarda ',
    'ask.request': '{who} pide permiso',
    'ask.asks': '{who} pregunta',
    'ask.queue': '+{n} en cola',
    'hint.question': 'La pregunta solo se ve en la Mac.',
    'hint.none': 'Sin detalle.',
    'btn.deny': 'Denegar',
    'btn.once': 'Aprobar una vez',
    'btn.always': 'Siempre',
    'btn.sending': 'Enviando…',
    'flash.denied': 'Denegado',
    'flash.always': 'Aprobado siempre',
    'flash.approved': 'Aprobado',
    'toast.resolved': 'Ya se resolvió en otro lado',
    'toast.reload': 'Recargando la pantalla…',
    'toast.failed': 'No se pudo enviar ({status})',
    'offline.title': 'Sin conexión con el buddy',
    'offline.retry': 'Reintentando…',
}

# the same in both languages on purpose: a proper noun or a unit
SAME_IN_BOTH = {"group.terminal", "span.min", "span.h", "span.d"}
SPANISH_MARKS = re.compile("[áéíóúñ¿¡]")
SPANISH_WORDS = ("hace ", "lleva ", "sesion", "trabaj", "Volver", "Listo", "Aviso", "tareas", "Corridas", "esperan", "Denegar",
                 "Aprobar", "Siempre", "Nadie", "Terminó", "terminó", "Nivel", "Sin ", "Todo ")
PLACEHOLDER = re.compile(r"\{(\w+)\}")


def _read_table():
    """{lang: {key: text}} straight from the page's source."""
    start = PAGE_TEXT.index("var I18N = {")
    end = PAGE_TEXT.index("var lang = 'es'")
    block = PAGE_TEXT[start:end]
    cut = block.index("    en: {")
    pair = re.compile(r"'([\w.]+)':\s*'((?:[^'\\]|\\.)*)'")
    out, dups = {}, []
    for lang, chunk in (("es", block[:cut]), ("en", block[cut:])):
        found = pair.findall(chunk)
        out[lang] = {key: re.sub(r"\\(.)", r"\1", text) for key, text in found}
        dups += [(lang, key) for key in {k for k, _ in found} if [k for k, _ in found].count(key) > 1]
    return out, dups, block


TABLE, DUPLICATES, TABLE_SOURCE = _read_table()
ES, EN = TABLE["es"], TABLE["en"]
CODE = PAGE_TEXT.replace(TABLE_SOURCE, "")  # the page without its table: where the keys are used


def fields(text):
    return set(PLACEHOLDER.findall(text))


# --- the page's table ----------------------------------------------------------------------------------------------


def test_the_table_has_both_languages_and_no_key_twice():
    assert ES and EN and not DUPLICATES
    assert set(ES) == set(EN), (sorted(set(ES) ^ set(EN)))


def test_no_text_is_empty_and_both_languages_use_the_same_placeholders():
    for lang in LANGS:
        for key, text in TABLE[lang].items():
            assert text.strip(), (lang, key)
    for key in ES:
        assert fields(ES[key]) == fields(EN[key]), (key, ES[key], EN[key])


def test_every_key_is_used_and_every_key_the_page_uses_exists():
    for key in ES:
        assert f"'{key}'" in CODE or f'data-i18n="{key}"' in CODE, f"nobody uses {key}"
    prefixes = {key.split(".")[0] for key in ES}
    used = {m for m in re.findall(r"'([a-z]+(?:\.[a-z]+)+)'", CODE) if m.split(".")[0] in prefixes}
    used |= set(re.findall(r'data-i18n="([^"]+)"', PAGE_TEXT))
    assert used and not (used - set(ES)), sorted(used - set(ES))
    # plural pairs come together
    for key in ES:
        if key.endswith(".one"):
            assert key[:-4] + ".other" in ES, key


def test_the_spanish_is_what_it_always_said():
    assert ES == PINNED_ES


def test_the_static_text_in_the_markup_is_the_spanish_one():
    found = re.findall(r'<(\w+)[^>]*\bdata-i18n="([^"]+)"[^>]*>([^<]*)</\1>', PAGE_TEXT)
    assert len(found) >= 25
    for _, key, text in found:
        assert text == ES[key], (key, text)


def test_the_english_reads_as_english_and_differs_from_the_spanish():
    for key in ES:
        assert not SPANISH_MARKS.search(EN[key]), (key, EN[key])
        assert not any(w in EN[key] for w in SPANISH_WORDS), (key, EN[key])
        if key not in SAME_IN_BOTH:
            assert EN[key] != ES[key], key
    for lang in LANGS:
        assert not any("—" in text or "–" in text for text in TABLE[lang].values())  # no dashes: the house rule
    assert {k for k in ES if ES[k] == EN[k]} <= SAME_IN_BOTH


def test_no_text_is_turned_into_markup_and_the_table_is_data():
    assert "innerHTML" not in PAGE_TEXT and "insertAdjacentHTML" not in PAGE_TEXT and "document.write" not in PAGE_TEXT
    assert "<" not in "".join(ES.values()) + "".join(EN.values())  # nothing in the table could even look like markup


def test_the_demos_name_nobody_real():
    low = PAGE_TEXT.lower()
    for word in ("/home/joe", "homelab", "pihole", "home assistant"):  # no real machine, user or service of the author's
        assert word not in low, word
    assert "owner: 'Ana'" in PAGE_TEXT


def test_the_demo_text_exists_in_both_languages():
    start = PAGE_TEXT.index("var DX = {")
    end = PAGE_TEXT.index("}[DEMO_LANG];")
    block = PAGE_TEXT[start:end]
    cut = block.index("    en: {")
    pair = re.compile(r"\b(\w+): '((?:[^'\\]|\\.)*)'")
    es, en = ({k: v for k, v in pair.findall(chunk)} for chunk in (block[:cut], block[cut:]))
    assert es and set(es) == set(en)
    assert all(es[k].strip() and en[k].strip() for k in es)
    assert not SPANISH_MARKS.search(" ".join(en.values()))
    # the demo of "Bluetooth is down" says what the daemon says in each language
    assert ble_reason(es["notReady"], "en") == en["notReady"] and ble_reason(es["notReady"], "es") == es["notReady"]


# --- the daemon's own strings and the switch ------------------------------------------------------------------------


def test_the_daemon_table_is_complete_and_translated():
    es, en = buddy_i18n.TABLE["es"], buddy_i18n.TABLE["en"]
    assert set(es) == set(en) and es
    for key in es:
        assert es[key].strip() and en[key].strip()
        assert fields(es[key]) == fields(en[key]), key
        assert es[key] != en[key], key
        assert not SPANISH_MARKS.search(en[key]), (key, en[key])
        assert "—" not in es[key] + en[key]


SAMPLES = {
    "ble.dbus_connect": {"detail": "[Errno 2] No such file"}, "ble.rejected": {"kind": "org.bluez.Error.Failed", "text": "Operation failed"},
    "ble.no_answer": {"error": "TimeoutError"}, "ble.adapter_missing_hint": {"adapter": "hci0"},
    "ble.adapter_no_peripheral": {"adapter": "hci0", "iface": "org.bluez.GattManager1"}, "ble.adapter_gone": {"adapter": "hci1"},
    "ble.adapter_missing": {"adapter": "hci0"},
}


@pytest.mark.parametrize("key", sorted(k for k in buddy_i18n.TABLE["es"]))
def test_each_bluetooth_reason_is_recognised_and_translated(key):
    params = SAMPLES.get(key, {})
    spanish = buddy_i18n.t(key, "es", **params)
    english = buddy_i18n.t(key, "en", **params)
    assert ble_reason(spanish, "es") == spanish  # Spanish is never touched
    assert ble_reason(spanish, "en") == english != spanish
    assert ble_reason(spanish, "fr") == spanish and ble_reason(spanish, "") == spanish  # an unknown language is Spanish


def test_a_reason_nobody_knows_and_the_wire_words_pass_through():
    for text in ("", "starting", "an exception text we never saw", "no encuentro la luna"):
        assert ble_reason(text, "en") == text
    assert ble_reason("no encuentro el adaptador hci0", "en") == "cannot find adapter hci0"
    assert ble_reason("no encuentro el adaptador hci0 (¿rfkill unblock bluetooth? ¿el dongle está enchufado?)", "en").startswith("cannot find adapter hci0 (rfkill")


def _bluez_reasons():
    """Every reason buddy_bluez.py can put in front of the screen: the text of each TransportError(...) and _not_ready(...) call
    with a literal message (an f-string with its holes filled by a sample)."""
    tree = ast.parse((ROOT / "buddy_bluez.py").read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else ""
        if name not in ("TransportError", "_not_ready"):
            continue
        arg = node.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            out.append(arg.value)
        elif isinstance(arg, ast.JoinedStr):
            out.append("".join(p.value if isinstance(p, ast.Constant) else "hci0" for p in arg.values))
    return out


def test_every_reason_the_radio_can_give_has_an_english_text():
    reasons = _bluez_reasons()
    assert len(reasons) >= 8
    untranslated = [r for r in reasons if ble_reason(r, "en") == r]
    # two of those messages put a whole exception text in the hole ("BlueZ rechazó un pedido: tipo: texto") or text from the
    # operating system ("no puedo conectar ...: {exc}"): they still match their template, the rest is the system's own words
    assert not untranslated, untranslated


def test_the_lang_flag(monkeypatch, capsys):
    monkeypatch.delenv("BUDDY_LANG", raising=False)
    assert buddy.parse_args([]).lang == "es"
    assert buddy.parse_args(["--lang", "es"]).lang == "es"
    assert buddy.parse_args(["--lang", "en"]).lang == "en"
    assert buddy.parse_args(["--lang", "EN"]).lang == "en"
    for bad in ("fr", "", "english", "es,en", "e n"):
        with pytest.raises(SystemExit) as exit_:
            buddy.parse_args(["--lang", bad])
        assert exit_.value.code == 2, bad
    assert "es o en" in capsys.readouterr().err
    with pytest.raises(SystemExit) as exit_:
        buddy.parse_args(["--lang"])  # no value
    assert exit_.value.code == 2


def test_the_environment_is_only_a_fallback(monkeypatch):
    monkeypatch.setenv("BUDDY_LANG", "en")
    assert buddy.parse_args([]).lang == "en"
    assert buddy.parse_args(["--lang", "es"]).lang == "es"  # the flag wins
    assert buddy.parse_args(["--lang", "en"]).lang == "en"
    monkeypatch.setenv("BUDDY_LANG", " ES ")
    assert buddy.parse_args([]).lang == "es"
    monkeypatch.setenv("BUDDY_LANG", "")
    assert buddy.parse_args([]).lang == "es"  # empty is as good as absent
    monkeypatch.setenv("BUDDY_LANG", "klingon")
    with pytest.raises(SystemExit) as exit_:
        buddy.parse_args([])
    assert exit_.value.code == 2  # a wrong value in the environment is as wrong as one on the command line
    assert buddy.parse_args(["--lang", "en"]).lang == "en"  # but a flag that is right never reads it


def test_parse_lang_and_normalize():
    assert [buddy_i18n.parse_lang(x) for x in ("es", " EN ")] == ["es", "en"]
    with pytest.raises(ValueError):
        buddy_i18n.parse_lang("fr")
    assert [buddy_i18n.normalize(x) for x in ("en", " En ", "es", "fr", "", None, 3)] == ["en", "en", "es", "es", "es", "es", "es"]
    assert buddy_i18n.DEFAULT_LANG == "es" and buddy_i18n.LANGS == ("es", "en")
    with pytest.raises(KeyError):
        buddy_i18n.t("no.such.key")


def _hub(rig, **kw):
    hub = UIHub(**kw)
    hub.bind(rig.core, rig.radio)
    return hub


def test_the_view_always_says_its_language(tmp_path):
    rig = Rig(tmp_path / "state")
    assert rig.view()["lang"] == "es"  # the default
    assert _hub(rig, lang="en").view()["lang"] == "en"
    assert _hub(rig, lang="es").view()["lang"] == "es"
    for odd in ("fr", "", None, 7):
        assert _hub(rig, lang=odd).view()["lang"] == "es"  # whatever is not en is es: the key is always there
    rig.hub.refresh()
    assert json.loads(rig.hub.current())["lang"] == "es"


def test_the_default_view_is_what_it_was_plus_the_language(tmp_path):
    rig = Rig(tmp_path / "state")
    rig.radio.ready, rig.radio.not_ready_reason = False, "el adaptador hci0 desapareció (¿se desenchufó?)"
    view = rig.view()
    assert set(view) == {"v", "boot", "lang", "ble", "linked", "pairing", "mood", "pet", "sessions", "approvals", "tasks", "working_total",
                         "tasks_total", "census_total", "tasks_more", "notices", "clock", "character", "night", "dim", "backlight"}
    assert view["ble"]["reason"] == "el adaptador hci0 desapareció (¿se desenchufó?)"  # Spanish: untouched
    assert view["pet"]["owner"] == ""  # no owner made up by default


def test_the_daemon_puts_its_text_in_the_chosen_language(tmp_path):
    rig = Rig(tmp_path / "state")
    en = _hub(rig, lang="en")
    rig.radio.ready = False
    for spanish, english in (("el adaptador hci0 desapareció (¿se desenchufó?)", "adapter hci0 disappeared (was it unplugged?)"),
                             ("bluetoothd se detuvo", "bluetoothd stopped"), ("BlueZ dejó de contestar", "BlueZ stopped answering"),
                             ("starting", "starting"), ("", "")):
        rig.radio.not_ready_reason = spanish
        assert en.view()["ble"]["reason"] == english
        assert rig.hub.view()["ble"]["reason"] == spanish
    rig.radio.ready, rig.radio.not_ready_reason = True, "left over"
    assert en.view()["ble"]["reason"] == ""  # still only while it is down


def test_the_served_page_knows_its_language_before_the_first_state(tmp_path):
    async def scenario():
        out = {}
        for lang in ("en", "es"):
            rig = Rig(tmp_path / lang)
            rig.hub = _hub(rig, lang=lang)
            async with serving(rig) as server:
                _, _, body = await http(server, path="/")
                out[lang] = body.decode("utf-8")
        return out

    pages = run(scenario)
    assert 'name="buddy-lang" content="en"' in pages["en"] and 'name="buddy-lang" content="es"' in pages["es"]
    assert all("__BUDDY_LANG__" not in page and "__BUDDY_TOKEN__" not in page for page in pages.values())


# --- the page in a browser ---------------------------------------------------------------------------------------------

# every demo, in the language the query asks for: no missing-key marker, nothing of the other language and nothing that overflows
EVERY_DEMO_JS = r"""
  var LANG = 'LANGUAGE';
  var SPANISH_MARKS = /[áéíóúñ¿¡]/;
  var SPANISH_WORDS = ['hace ', 'lleva ', 'sesion', 'trabaj', 'Volver', 'Listo', 'Aviso', 'tareas', 'Corridas', 'esperan', 'Denegar', 'Aprobar',
                       'Siempre', 'Nadie', 'Nivel', 'Sin ', 'Todo ', 'Durmiendo', 'Conectando', 'Esperando'];
  var BOX = '.pill, .btn, #mood, .net, .chip, .bubble, .trow, .tv-back, .tv-done, .pl-name, .pl-lvl, .age, #queue, #always-note, .tv-sub, #pair-sub, .note, h1, #flash-t';
  function shown(el) { return !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length); }
  function lines(el) {   // how many lines the text would take with no clamp: a copy of it, as wide as it, with the clamp lifted
    var c = el.cloneNode(true);
    c.removeAttribute('id');
    c.style.cssText = 'position:absolute;visibility:hidden;display:block;-webkit-line-clamp:unset;height:auto;max-height:none;overflow:visible;width:' + el.clientWidth + 'px';
    el.parentNode.appendChild(c);
    var n = Math.round(c.getBoundingClientRect().height / parseFloat(getComputedStyle(c).lineHeight));
    el.parentNode.removeChild(c);
    return n;
  }
  function overflow(tag) {
    var bad = [], r0 = $('stage').getBoundingClientRect();
    Array.prototype.slice.call(document.querySelectorAll(BOX)).forEach(function (el) {
      if (!shown(el)) return;
      var r = el.getBoundingClientRect(), what = tag + ' ' + (el.id || el.className || el.tagName) + ' "' + el.textContent.slice(0, 40) + '"';
      if (el.scrollWidth > el.clientWidth + 1) bad.push('wide ' + what + ' ' + el.scrollWidth + '>' + el.clientWidth);
      var scrolls = el.closest('#feed-list, #tv-said, .hint, .reason');   // what scrolls is allowed to run past the bottom
      if (r.left < r0.left - 1 || r.right > r0.right + 1 || (!scrolls && r.bottom > r0.bottom + 1)) bad.push('outside ' + what);
    });
    ['feed-msg', 'tool'].forEach(function (id) {   // two lines at most: the third would be cut by the clamp
      var f = $(id), n = shown(f) ? lines(f) : 0;
      if (n > 2) bad.push(tag + ' ' + id + ' needs ' + n + ' lines: ' + f.textContent);
    });
    if (shown($('feed')) && $('feed').scrollHeight > $('feed').clientHeight + 1) bad.push(tag + ' the bubble overflows');
    return bad;
  }
  function look(tag) {
    var stage = $('stage'), text = stage.textContent;
    ok(tag + ': no missing-key marker', text.indexOf('⟦') < 0, text.slice(text.indexOf('⟦'), text.indexOf('⟦') + 30));
    ok(tag + ': the document says ' + LANG, document.documentElement.getAttribute('lang') === LANG);
    if (LANG === 'en') {
      var m = text.match(SPANISH_MARKS), w = SPANISH_WORDS.filter(function (x) { return text.indexOf(x) >= 0; });
      ok(tag + ': nothing left in Spanish', !m && !w.length, (m ? m[0] : '') + ' ' + w.join(','));
    }
    var bad = overflow(tag);
    ok(tag + ': nothing overflows', !bad.length, bad.join(' | '));
  }
  demoNames.forEach(function (name) {
    demoShow(name);
    look(name);
    if (view && view.tasks) {   // and every row's detail
      var rows = stableOrder(shownTasks(view).tasks);
      rows.forEach(function (th) { openTask(th.id); look(name + ' > ' + th.id); closeTask(); });
    }
    if (view && view.notices && view.notices.length) {
      orderNotes(noticesOf(view)).forEach(function (n) { openNotice(n.id); look(name + ' > notice ' + n.id); closeTask(); });
    }
  });
  ok('every demo ran', demoNames.length > 30);
"""


def _every_demo(tmp_path, lang):
    results = battery(tmp_path, EVERY_DEMO_JS.replace("LANGUAGE", lang), query=f"demo=idle&lang={lang}", budget=120000)
    assert_all(results, 3 * 30)
    return results


@needs_chromium
@pytest.mark.parametrize("lang", LANGS)
def test_every_demo_and_detail_renders_in_each_language_without_missing_text_or_overflow(tmp_path, lang):
    _every_demo(tmp_path, lang)


SWITCH_JS = r"""
  function txt(id) { return $(id).textContent; }
  function swap(l) { var v = clone(view); v.lang = l; view = v; render(); }
  function main() {
    return { mood: txt('mood'), level: txt('pet-level'), owner: txt('pet-owner'), msg: txt('feed-msg'), pills: txt('t-waiting'),
             foot: document.querySelector('.foot').textContent, tvdone: txt('tv-done').trim(), wait: txt('s-wait'), boot: txt('s-boot') };
  }
  demoShow('threads');
  var es = main();
  ok('the default is Spanish, as always', es.mood === 'Trabajando' && es.level === 'Nivel 3' && es.owner === 'de Ana' && es.msg === '3 tareas en marcha'
    && es.pills === '0esperan' && document.documentElement.getAttribute('lang') === 'es', JSON.stringify(es));
  swap('en');
  var en = main();
  ok('the same view in English repaints the plate, the headline, the pills and the static text', en.mood === 'Working' && en.level === 'Level 3'
    && en.owner === 'belongs to Ana' && en.msg === '3 tasks running' && en.pills === '0waiting' && en.foot.indexOf('tokens today') > 0
    && en.tvdone === 'Done' && en.wait.indexOf('Waiting for Claude desktop') >= 0 && en.boot.indexOf('Connecting') >= 0
    && document.documentElement.getAttribute('lang') === 'en', JSON.stringify(en));
  ok('the rows say it in English too (a finished one, the "+K more" line, the Terminal group)', /finished: Bash aws/.test(txt('feed-list')) && /Terminal/.test(txt('feed-list')));
  swap('es');
  ok('and back to Spanish, exactly as it was', JSON.stringify(main()) === JSON.stringify(es) && /terminó: Bash aws/.test(txt('feed-list')));
  swap('fr');
  ok('a language nobody knows is Spanish', txt('mood') === 'Trabajando' && document.documentElement.getAttribute('lang') === 'es');
  var v0 = clone(view); delete v0.lang; view = v0; render();
  ok('no language at all (an older daemon) is Spanish', txt('mood') === 'Trabajando');
  swap('en');
  // a permission, a notice and a detail open while the language flips
  demoShow('approval-cli-always');
  var ask = { once: txt('b-ok-t'), no: txt('b-no-t'), always: txt('b-always-t'), age: txt('age'), note: txt('always-note') };
  ok('the permission is in Spanish by default', ask.once === 'Aprobar una vez' && ask.no === 'Denegar' && ask.always === 'Siempre' && /^Claude Code pide permiso/.test(ask.age), JSON.stringify(ask));
  swap('en');
  ok('and in English when the language changes under it', txt('b-ok-t') === 'Approve once' && txt('b-no-t') === 'Deny' && txt('b-always-t') === 'Always'
    && /^Claude Code asks permission/.test(txt('age')) && /^Always saves /.test(txt('always-note')), txt('age') + ' ' + txt('always-note'));
  demoShow('notices');
  swap('en');
  openNotice('n-correo');
  ok('a notice detail in English', txt('tv-age') === 'Notice' && txt('tv-did') === 'from hermes' && txt('tv-na') === '3:00 ago', txt('tv-age') + '|' + txt('tv-did') + '|' + txt('tv-na'));
  swap('es');
  ok('and the same detail, repainted in Spanish while it is open', txt('tv-age') === 'Aviso' && txt('tv-did') === 'de hermes' && txt('tv-na') === 'hace 3:00');
"""


@needs_chromium
def test_changing_the_language_at_runtime_repaints_everything(tmp_path):
    assert_all(battery(tmp_path, SWITCH_JS, query="demo=idle", budget=20000), 10)


@needs_chromium
def test_a_demo_asks_for_english_with_lang_en_and_anything_else_is_spanish():
    en = dump(f"{PAGE.as_uri()}?demo=threads&lang=en&tag=0")
    assert re.search(r'<html lang="en"', en) and re.search(r'id="feed-msg"[^>]*>3 tasks running<', en)
    assert re.search(r'id="mood"[^>]*>Working<', en) and "Acme Studio" in en
    for query in ("demo=threads", "demo=threads&lang=es", "demo=threads&lang=fr", "demo=threads&lang="):
        es = dump(f"{PAGE.as_uri()}?{query}&tag=0")
        assert re.search(r'<html lang="es"', es) and re.search(r'id="feed-msg"[^>]*>3 tareas en marcha<', es), query
    boot = dump(f"{PAGE.as_uri()}?demo=boot&lang=en&tag=0")
    assert attr(boot, "data-screen") == "boot" and re.search(r'<h1 data-i18n="boot.title">Connecting…</h1>', boot)
    assert re.search(r'id="mood"[^>]*>Sleeping<', boot) and re.search(r'id="pet-level"[^>]*>Level 0<', boot)


@needs_chromium
def test_the_page_a_live_daemon_serves_speaks_the_daemons_language(tmp_path):
    async def scenario():
        rig = Rig(tmp_path / "state")
        rig.hub = _hub(rig, lang="en")
        rig.radio.ready, rig.radio.not_ready_reason = False, "el adaptador hci0 desapareció (¿se desenchufó?)"
        async with serving(rig) as server:
            return await asyncio.get_running_loop().run_in_executor(None, dump, server.url + "/?poll=300", 2500)

    dom = run(scenario)
    assert re.search(r'<html lang="en"', dom) and attr(dom, "data-screen") == "down"
    assert re.search(r'id="down-title"[^>]*>Bluetooth is not ready<', dom)
    assert re.search(r'id="down-reason"[^>]*>adapter hci0 disappeared \(was it unplugged\?\)<', dom)
    assert re.search(r'id="down-note"[^>]*>The buddy keeps retrying on its own\.<', dom)
    assert not re.search(r'id="down-reason"[^>]*>[^<]*desapareci', dom)


@needs_chromium
def test_the_table_the_tests_read_is_the_one_the_page_runs(tmp_path):
    body = "ok('the page table', JSON.stringify(I18N) === JSON.stringify(EXPECTED), '');".replace("EXPECTED", json.dumps(TABLE, ensure_ascii=False))
    assert_all(battery(tmp_path, body, query="demo=idle"), 1)


# --- the real daemon, in this process, with a stand-in for the radio ---------------------------------------------------


class DownRadio:
    """What run_daemon needs from the Bluetooth transport: one that says it is not ready, in the Spanish buddy_bluez.py uses."""

    ready = False
    device_name = "Claude-1234"
    not_ready_reason = "el adaptador hci0 desapareció (¿se desenchufó?)"
    secure = True
    address = "00:00:00:00:00:00"

    def send(self, message):
        pass

    def unpair(self):
        pass

    def is_secure(self):
        return True

    async def start(self):
        pass

    async def stop(self):
        pass

    async def forget_all(self):
        return 0


def _daemon(tmp_path, monkeypatch, capsys, *extra):
    """Run buddy.run_daemon with `extra` flags until its screen is up; return (/state, /, what it printed)."""
    monkeypatch.setattr(buddy, "make_transport", lambda args, **kw: DownRadio())
    args = buddy.parse_args(["--state-dir", str(tmp_path / "state"), "--no-keys", "--ui-port", "0", "--night", "off", "--backlight", "off", *extra])
    seen = {}

    async def scenario():
        task = asyncio.ensure_future(buddy.run_daemon(args))
        out = ""
        try:
            while "Pantalla en http://127.0.0.1:" not in out:
                out += capsys.readouterr().out
                await asyncio.sleep(0.01)
            port = int(re.search(r"Pantalla en http://127\.0\.0\.1:(\d+)", out).group(1))
            seen["state"] = json.loads((await http(port, path="/state"))[2])
            seen["page"] = (await http(port, path="/"))[2].decode("utf-8")
            os.kill(os.getpid(), signal.SIGTERM)
            assert await asyncio.wait_for(task, 5) == 0
        finally:
            if not task.done():
                task.cancel()
        seen["out"] = out + capsys.readouterr().out

    run(scenario)
    return seen["state"], seen["page"], seen["out"]


def test_the_real_daemon_speaks_english_on_the_screen_and_spanish_in_the_terminal(tmp_path, monkeypatch, capsys):
    state, page, out = _daemon(tmp_path, monkeypatch, capsys, "--lang", "en")
    assert state["lang"] == "en" and state["ble"]["reason"] == "adapter hci0 disappeared (was it unplugged?)"
    assert 'name="buddy-lang" content="en"' in page
    assert "Pantalla en http://127.0.0.1:" in out and "Claude Buddy" in out  # the terminal line is what it always was


def test_the_real_daemon_without_the_flag_is_the_spanish_one(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("BUDDY_LANG", raising=False)
    state, page, _ = _daemon(tmp_path, monkeypatch, capsys)
    assert state["lang"] == "es" and state["ble"]["reason"] == "el adaptador hci0 desapareció (¿se desenchufó?)"
    assert 'name="buddy-lang" content="es"' in page


def test_the_real_daemon_reads_the_environment_when_the_flag_is_absent(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BUDDY_LANG", "en")
    state, _, _ = _daemon(tmp_path, monkeypatch, capsys)
    assert state["lang"] == "en"
