"""Languages of the buddy: Spanish (the default, with voseo) and English. Pure standard library.

The screen's own texts live in the page (the I18N table of ui/index.html). This module is for the human strings the
DAEMON puts into the view that the page shows as they come, and for the switch itself:

    LANGS, DEFAULT_LANG   the two languages and the one used when nothing says otherwise
    parse_lang(text)      the strict reading of `--lang` / $BUDDY_LANG (ValueError for anything but es or en)
    normalize(value)      the lenient one, for code that must never fail: anything unknown is "es"
    t(key, lang, **p)     one daemon string in a language, with {placeholders} filled
    ble_reason(text, lang)  the Bluetooth "not ready" reason the radio produced (always Spanish, see below) in `lang`

The Bluetooth reasons are written in Spanish by buddy_bluez.py, which is not translated at the source: they are
recognised here by their Spanish template and swapped for the English one at the moment the view is built. A reason
nobody here recognises (a new message, or text from the system such as an exception) is passed on untouched, and in
Spanish nothing is ever changed: the view stays byte for byte what it always was.

Terminal and journal lines are not part of this: they stay in Spanish whatever the language of the screen.
"""
import re
from typing import Dict, List, Pattern, Tuple

LANGS = ("es", "en")
DEFAULT_LANG = "es"
ENV_VAR = "BUDDY_LANG"

# key -> text in each language. {name} marks a value that is filled in. The Spanish ble.* texts are the exact
# templates buddy_bluez.py writes (that is how a reason is recognised); the English ones say the same.
TABLE: Dict[str, Dict[str, str]] = {
    "es": {
        "ble.dbus_connect": "no puedo conectar al bus D-Bus del sistema: {detail}",
        "ble.bluetoothd_down": "bluetoothd no está corriendo (sudo systemctl enable --now bluetooth)",
        "ble.dbus_denied": "D-Bus no me deja hablar con BlueZ. Probá con sudo (sudo .venv/bin/python buddy.py ...). "
                           "En sistemas con la política vieja de D-Bus ayuda estar en el grupo bluetooth "
                           "(sudo usermod -aG bluetooth $USER y volver a entrar)",
        "ble.rejected": "BlueZ rechazó un pedido: {kind}: {text}",
        "ble.no_answer": "BlueZ no contestó ({error})",
        "ble.adapter_missing_hint": "no encuentro el adaptador {adapter} (¿rfkill unblock bluetooth? ¿el dongle está enchufado?)",
        "ble.adapter_no_peripheral": "el adaptador {adapter} no ofrece {iface}: no puede actuar como periférico BLE",
        "ble.stopped_answering": "BlueZ dejó de contestar",
        "ble.adapter_gone": "el adaptador {adapter} desapareció (¿se desenchufó?)",
        "ble.bluetoothd_stopped": "bluetoothd se detuvo",
        "ble.adapter_missing": "no encuentro el adaptador {adapter}",
    },
    "en": {
        "ble.dbus_connect": "cannot connect to the system D-Bus: {detail}",
        "ble.bluetoothd_down": "bluetoothd is not running (sudo systemctl enable --now bluetooth)",
        "ble.dbus_denied": "D-Bus will not let me talk to BlueZ. Try with sudo (sudo .venv/bin/python buddy.py ...). "
                           "On systems with the old D-Bus policy it helps to be in the bluetooth group "
                           "(sudo usermod -aG bluetooth $USER and log in again)",
        "ble.rejected": "BlueZ rejected a request: {kind}: {text}",
        "ble.no_answer": "BlueZ did not answer ({error})",
        "ble.adapter_missing_hint": "cannot find adapter {adapter} (rfkill unblock bluetooth? is the dongle plugged in?)",
        "ble.adapter_no_peripheral": "adapter {adapter} does not offer {iface}: it cannot act as a BLE peripheral",
        "ble.stopped_answering": "BlueZ stopped answering",
        "ble.adapter_gone": "adapter {adapter} disappeared (was it unplugged?)",
        "ble.bluetoothd_stopped": "bluetoothd stopped",
        "ble.adapter_missing": "cannot find adapter {adapter}",
    },
}

_FIELD = re.compile(r"\{(\w+)\}")


def parse_lang(text: str) -> str:
    """`--lang` and $BUDDY_LANG: "es" or "en" (any case, spaces around are fine). ValueError for anything else."""
    value = str(text).strip().lower()
    if value not in LANGS:
        raise ValueError(f"{text!r} no es un idioma: usá es o en")
    return value


def normalize(value: object) -> str:
    """A language for code that must not fail: "en" when it says so, "es" for everything else."""
    return "en" if isinstance(value, str) and value.strip().lower() == "en" else DEFAULT_LANG


def t(key: str, lang: str = DEFAULT_LANG, **params: object) -> str:
    """The daemon string `key` in `lang` (Spanish when that language has no such key), placeholders filled.
    KeyError for a key that does not exist in Spanish either: a typo should be loud."""
    table = TABLE.get(normalize(lang), TABLE[DEFAULT_LANG])
    text = table[key] if key in table else TABLE[DEFAULT_LANG][key]
    return _FIELD.sub(lambda m: str(params[m.group(1)]) if m.group(1) in params else m.group(0), text)


def _template(spanish: str) -> Pattern[str]:
    """A regex that matches `spanish` with every {name} standing for what was filled in there."""
    out: List[str] = []
    last = 0
    for found in _FIELD.finditer(spanish):
        out.append(re.escape(spanish[last:found.start()]))
        name = found.group(1)
        out.append(r"(?P<%s>\S+)" % name if name in ("adapter", "iface") else r"(?P<%s>.*)" % name)
        last = found.end()
    out.append(re.escape(spanish[last:]))
    return re.compile("".join(out), re.DOTALL)


# longest literal text first, so "no encuentro el adaptador X (...)" is tried before the bare "no encuentro el adaptador X"
_REASONS: List[Tuple[str, Pattern[str]]] = sorted(
    ((key, _template(text)) for key, text in TABLE["es"].items() if key.startswith("ble.")),
    key=lambda item: -len(_FIELD.sub("", TABLE["es"][item[0]])),
)


def ble_reason(text: str, lang: str = DEFAULT_LANG) -> str:
    """The reason Bluetooth is not ready, in `lang`. Spanish (the radio's own wording) and anything not recognised
    come back exactly as given."""
    if normalize(lang) == DEFAULT_LANG or not text:
        return text
    for key, pattern in _REASONS:
        found = pattern.fullmatch(text)
        if found:
            return t(key, lang, **found.groupdict())
    return text
