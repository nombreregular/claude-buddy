"""Demo mode: the real screen with a scripted story and invented data, for a video or a screenshot.

    python3 buddy.py demo [--lang en] [--ui-port 8765]

No Bluetooth, no hooks, no Hermes listener, nothing read from or written to the real state directory (a temporary one is made
and removed on the way out). The page, the server, the moods, the night-free thread list, the notices and the permission
buttons are the real ones: a tap on Approve or Deny works and Mochi reacts. Every name in the story is made up.

The story is one loop of `PERIOD_S` seconds: a few threads and two runs work, a notice arrives, a permission request waits
for a tap (it withdraws itself if nobody answers), another notice follows, and the loop starts again.
"""
import asyncio
import json
import shutil
import signal
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from buddy_core import ApprovalBroker, BuddyCore, encode_line
from buddy_ui import UIHub, UIServer

PERIOD_S = 100
CHARACTER_SRC = Path(__file__).resolve().parent / "characters" / "buddy-pixel"
OWNER = "Ana"

PERMISSION_FROM_S = 30
PERMISSION_UNTIL_S = 90
PERMISSION = {"id": "demo_req", "tool": "Bash", "hint": "npm run deploy -- --env production"}

TOOLS = (("Edit", "src/Catalog.tsx"), ("Bash", "pnpm test"), ("Read", "docs/api.md"), ("Edit", "src/Cart.tsx"), ("Bash", "git status --short"))

LINES = {
    "en": {
        "threads": (("Product catalog for the web store", "Acme Studio"), ("Fix refund rounding", "Acme Studio"),
                    ("Write the deploy runbook", "Personal")),
        "said_done": "Done: the rounding is half-even now and the 12 payment tests pass.",
        "said_runbook": "Drafted the runbook: build, smoke test, deploy, rollback. Missing: who to call if the rollback fails.",
        "runs": ("nightly-backup", "site-migration"),
        "notice_ok": ("Deploy finished", "37 of 40 checks passed", "assistant"),
        "notice_warn": ("Meeting in 10 minutes", "", "assistant"),
    },
    "es": {
        "threads": (("Catálogo de la tienda web", "Acme Studio"), ("Arreglar el redondeo de reembolsos", "Acme Studio"),
                    ("Escribir el manual de despliegue", "Personal")),
        "said_done": "Listo: el redondeo ya es half-even y pasan las 12 pruebas de pagos.",
        "said_runbook": "Armé el manual: compilar, prueba de humo, desplegar y volver atrás. Falta: a quién llamar si falla la vuelta.",
        "runs": ("respaldo-nocturno", "migracion-sitio"),
        "notice_ok": ("Terminó el despliegue", "Pasaron 37 de 40 chequeos", "asistente"),
        "notice_warn": ("Reunión en 10 minutos", "", "asistente"),
    },
}


class Radio:
    """What the hub reads from a Bluetooth transport; this one is always up and never sends anything."""

    ready = True
    device_name = "Claude-DEMO"
    not_ready_reason = ""
    secure = True


def story_state(t: float) -> Dict[str, Any]:
    """What the story wants on screen `t` seconds into the loop (a pure function, so it can be tested)."""
    t = t % PERIOD_S
    tool, what = TOOLS[int(t // 4) % len(TOOLS)]
    return {
        "t": t,
        "tool": tool,
        "what": what,
        "refund_working": t < 45,
        "migration": 10 <= t < 70,
        "notice_ok": 14 <= t < 39,
        "notice_warn": 60 <= t < 80,
        "permission": PERMISSION_FROM_S <= t < PERMISSION_UNTIL_S,
    }


class Demo:
    """Drives a real core and hub through the story, one second at a time."""

    def __init__(self, core: BuddyCore, hub: UIHub, lang: str = "en", started: float = 0.0) -> None:
        self.core, self.hub = core, hub
        self.lines = LINES["en" if lang != "es" else "es"]
        self.started = started
        self.asked_cycle = -1  # the loop in which the permission was put on screen
        self.answered_cycle = -1  # the loop in which somebody answered it
        self.notice_sent = {"ok": -1, "warn": -1}
        self.tokens = 48000
        core.feed(encode_line({"cmd": "owner", "name": OWNER}))

    def step(self, now: float) -> None:
        elapsed = now - self.started
        cycle = int(elapsed // PERIOD_S)
        s = story_state(elapsed)
        board = self.core.tasks
        (t1, g1), (t2, g2), (t3, g3) = self.lines["threads"]
        board.update("demo-1", project="web-store", tool=s["tool"], what=s["what"], title=t1, group=g1, state="working")
        board.update("demo-2", project="api-payments", tool="Bash", what="pytest -q", title=t2, group=g2,
                     state="working" if s["refund_working"] else "idle", said=None if s["refund_working"] else self.lines["said_done"])
        board.update("demo-3", project="notes", tool="Read", what="docs/runbook.md", title=t3, group=g3, state="idle",
                     said=self.lines["said_runbook"])
        runs = []
        names = self.lines["runs"]
        runs.append({"id": "run-nightly", "name": names[0], "elapsed": 1500 + int(elapsed), "idle": 20, "state": "working"})
        if s["migration"]:
            runs.append({"id": "run-migration", "name": names[1], "elapsed": 120 + int(s["t"]) * 5, "state": "working"})
        board.replace_census("demo", runs, 10)
        self._notice("ok", s["notice_ok"], self.lines["notice_ok"], "ok", "demo-deploy", cycle)
        self._notice("warn", s["notice_warn"], self.lines["notice_warn"], "warn", "demo-meeting", cycle)

        if s["permission"]:
            if self.asked_cycle == cycle and not self.core.broker.pending():
                self.answered_cycle = cycle  # it was on screen and is gone: somebody tapped Approve or Deny
            want_prompt = self.answered_cycle != cycle
            if want_prompt:
                self.asked_cycle = cycle
        else:
            want_prompt = False
        self.tokens += 37
        running = 1 + (1 if s["refund_working"] else 0) + (1 if s["migration"] else 0)
        heartbeat: Dict[str, Any] = {"total": 5, "running": running, "waiting": 1 if want_prompt else 0, "msg": "working",
                                     "entries": [], "tokens": self.tokens, "tokens_today": self.tokens}
        if want_prompt:
            heartbeat["msg"] = "approve: " + PERMISSION["tool"]
            heartbeat["prompt"] = dict(PERMISSION)
        self.core.feed(encode_line(heartbeat))

    def _notice(self, which: str, active: bool, text: Any, kind: str, notice_id: str, cycle: int) -> None:
        if active and self.notice_sent[which] != cycle:
            self.notice_sent[which] = cycle
            title, body, source = text
            self.core.notices.add(title=title, body=body, kind=kind, ttl=25, source=source, notice_id=notice_id)


async def run_demo(args: Any) -> int:
    """buddy.py demo: serve the real page with the story until stopped."""
    loop = asyncio.get_running_loop()
    tmp = Path(tempfile.mkdtemp(prefix="buddy-demo-"))
    folder = tmp / "characters" / "buddy"
    shutil.copytree(CHARACTER_SRC, folder)
    sent: List[bytes] = []
    core = BuddyCore(tmp, sent.append, broker=ApprovalBroker())
    core.state.character = "buddy"
    hub = UIHub(lang=args.lang or "en")
    hub.bind(core, Radio())
    server = UIServer(hub, port=args.ui_port)
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        await server.start()
        print(f"Demo mode: {server.url} (invented data, nothing is read from or sent to Claude)")
        demo = Demo(core, hub, args.lang or "en", started=loop.time())

        async def ticker() -> None:
            while True:
                demo.step(loop.time())
                core.tick()
                hub.refresh()
                await asyncio.sleep(1.0)

        task = asyncio.ensure_future(ticker())
        try:
            await stop.wait()
        finally:
            task.cancel()
    finally:
        hub.close()
        hub.unbind()
        await server.stop()
        core.close()
        shutil.rmtree(tmp, ignore_errors=True)
    return 0
