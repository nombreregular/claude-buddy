"""hermes-plugin/claude-buddy: Hermes' dangerous-command approvals answered on the screen.

The HTTP client is exercised against the buddy's own HermesServer (test_hermes.py's rig); the Hermes
side (ApprovalRequest, ctx) is stood in for, since Hermes itself is not installed here."""
import asyncio
import importlib
import importlib.util
import logging
import sys
import threading
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_hermes import TOKEN, hermes  # noqa: E402
from test_ui import Rig, run  # noqa: E402

PLUGIN_DIR = Path(__file__).resolve().parent.parent / "hermes-plugin" / "claude-buddy"


def _load_plugin():
    """Import the plugin package from its directory, the way Hermes would (a package named after it)."""
    name = "claude_buddy_plugin"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, PLUGIN_DIR / "__init__.py",
                                                  submodule_search_locations=[str(PLUGIN_DIR)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


plugin = _load_plugin()
client = importlib.import_module("claude_buddy_plugin.client")


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path / "state")


@pytest.fixture
def token_file(tmp_path):
    path = tmp_path / "claude-buddy.token"
    path.write_text(TOKEN + "\n")
    path.chmod(0o600)
    return path


def settings(server, token_file, timeout=30):
    return client.Settings(url=f"http://127.0.0.1:{server.port}", token_file=str(token_file), timeout=timeout)


async def in_thread(fn, *args, **kw):
    return await asyncio.get_running_loop().run_in_executor(None, lambda: fn(*args, **kw))


async def on_screen(rig, limit=5.0):
    end = time.monotonic() + limit
    while time.monotonic() < end:
        if rig.view()["approvals"]:
            return rig.view()["approvals"][0]
        await asyncio.sleep(0.05)
    raise AssertionError("the request never reached the screen")


class FakeRequest:
    """What hermes_cli.approval_transport.ApprovalRequest gives a transport."""

    def __init__(self, command="rm -rf /tmp/x", description="Clean the scratch dir", timeout_seconds=300.0,
                 allowed_choices=("once", "session", "always", "deny")):
        self.request_id, self.digest = "req-1", "digest-1"
        self.command, self.description = command, description
        self.timeout_seconds, self.allowed_choices = timeout_seconds, allowed_choices

    def respond(self, choice):
        return ("decision", self.request_id, self.digest, choice)


class FakeCtx:
    def __init__(self, config):
        self.config, self.transports = config, {}

    def register_approval_transport(self, name, present_fn):
        self.transports[name] = present_fn

    def get_config(self, key, default=None):
        return self.config.get(key, default)


# --- the client against the real listener -----------------------------------------------


@pytest.mark.parametrize("tap", ["once", "deny"])
def test_a_tap_on_the_screen_is_what_hermes_gets(rig, token_file, tap):
    async def scenario():
        async with hermes(rig) as server:
            asking = asyncio.ensure_future(in_thread(client.ask_buddy, settings(server, token_file),
                                                     "sudo systemctl restart pihole-FTL", "Restart the DNS"))
            shown = await on_screen(rig)
            assert shown["source"] == "hermes" and shown["tool"] == "terminal"
            assert shown["hint"] == "sudo systemctl restart pihole-FTL\nRestart the DNS"
            assert shown["key"].startswith("hermes:h-")
            assert rig.hub.decide(shown["key"], tap)
            return await asyncio.wait_for(asking, 5)

    assert run(scenario) == tap


def test_with_no_tap_in_time_it_gives_up_and_the_screen_lets_go(rig, token_file, caplog):
    caplog.set_level(logging.INFO)

    async def scenario():
        async with hermes(rig) as server:
            started = time.monotonic()
            asking = asyncio.ensure_future(in_thread(client.ask_buddy, settings(server, token_file), "ls",
                                                     deadline=time.monotonic() + 1.5))
            key = (await on_screen(rig))["key"]
            outcome = await asyncio.wait_for(asking, 10)
            return outcome, time.monotonic() - started, server.record(key.split(":", 1)[1]).state, rig.view()["approvals"]

    outcome, took, state, approvals = run(scenario)
    assert outcome is None and 1.0 <= took < 6
    assert state == "withdrawn" and approvals == []
    assert TOKEN not in caplog.text


def test_cancelling_takes_the_request_off_the_screen(rig, token_file):
    async def scenario():
        async with hermes(rig) as server:
            cancelled = threading.Event()
            asking = asyncio.ensure_future(in_thread(client.ask_buddy, settings(server, token_file), "ls",
                                                     cancelled=cancelled))
            key = (await on_screen(rig))["key"]
            await asyncio.sleep(0.2)
            cancelled.set()
            outcome = await asyncio.wait_for(asking, 40)  # the current ?wait= poll has to come back first
            return outcome, server.record(key.split(":", 1)[1]).state, rig.view()["approvals"]

    outcome, state, approvals = run(scenario)
    assert outcome is None and state == "withdrawn" and approvals == []


def test_with_the_buddy_off_it_answers_at_once(token_file, caplog):
    caplog.set_level(logging.WARNING)
    started = time.monotonic()
    out = client.ask_buddy(client.Settings(url="http://127.0.0.1:9", token_file=str(token_file), timeout=60), "ls")
    assert out is None and time.monotonic() - started < 5
    assert "asking elsewhere" in caplog.text and TOKEN not in caplog.text


@pytest.mark.parametrize("problem", ["missing", "readable", "short", "wrong", "dir"])
def test_without_a_good_token_nothing_reaches_the_screen(rig, token_file, problem, caplog):
    caplog.set_level(logging.WARNING)
    if problem == "missing":
        token_file.unlink()
    elif problem == "readable":
        token_file.chmod(0o644)
    elif problem == "short":
        token_file.write_text("short")
    elif problem == "wrong":
        token_file.write_text("x" * 32)
    else:
        token_file.unlink()
        token_file.mkdir()

    async def scenario():
        async with hermes(rig) as server:
            out = await in_thread(client.ask_buddy, settings(server, token_file), "ls")
            return out, rig.view()["approvals"]

    out, approvals = run(scenario)
    assert out is None and approvals == []
    assert TOKEN not in caplog.text and "x" * 32 not in caplog.text


@pytest.mark.parametrize("url", ["https://buddy.local:8766", "buddy.local:8766", "http://buddy.local:8766/approval",
                                 "http://buddy.local:8766/?x=1", ""])
def test_a_url_that_is_not_host_and_port_is_refused(token_file, url):
    assert client.ask_buddy(client.Settings(url=url, token_file=str(token_file)), "ls") is None


def test_the_hint_is_the_command_and_hermes_words_about_it():
    assert client.hint_for("rm -rf x", "Remove x") == "rm -rf x\nRemove x"
    assert client.hint_for("rm -rf x", "rm -rf x") == "rm -rf x"  # Hermes often repeats the command
    assert client.hint_for("rm -rf x", "") == "rm -rf x"
    assert client.hint_for("", "only words") == "only words"
    assert client.hint_for("a\r\nb", "") == "a\nb"
    long = client.hint_for("x" * 2000, "y")
    assert len(long) == client.MAX_HINT_CHARS and long.endswith("…")


def test_settings_come_from_the_plugin_config_with_fallbacks():
    get = lambda key, default=None: {"url": "http://10.0.0.2:8766", "timeout": "45"}.get(key, default)  # noqa: E731
    s = client.Settings.from_mapping(get)
    assert s.url == "http://10.0.0.2:8766" and s.timeout == 45.0 and s.token_file == client.DEFAULT_TOKEN_FILE
    s = client.Settings.from_mapping(lambda key, default=None: {"timeout": "soon", "url": ""}.get(key, default))
    assert s.url == client.DEFAULT_URL and s.timeout == client.DEFAULT_TIMEOUT_S
    assert client.Settings.from_mapping(lambda k, d=None: {"timeout": 1}.get(k, d)).timeout == client.MIN_TIMEOUT_S
    assert client.Settings.from_mapping(lambda k, d=None: {"timeout": 10**9}.get(k, d)).timeout == client.MAX_TIMEOUT_S


# --- the plugin around it ------------------------------------------------------------------


def test_register_offers_the_buddy_transport_and_a_tap_becomes_hermes_decision(rig, token_file):
    async def scenario():
        async with hermes(rig) as server:
            ctx = FakeCtx({"url": f"http://127.0.0.1:{server.port}", "token_file": str(token_file), "timeout": 30})
            plugin.register(ctx)
            assert list(ctx.transports) == ["buddy"]
            present = ctx.transports["buddy"]
            request = FakeRequest(command="sudo reboot", description="Reboot the Pi")
            presenting = asyncio.ensure_future(in_thread(present, request))
            shown = await on_screen(rig)
            assert shown["hint"] == "sudo reboot\nReboot the Pi" and shown["source"] == "hermes"
            rig.hub.decide(shown["key"], "deny")
            return await asyncio.wait_for(presenting, 5)

    assert run(scenario) == ("decision", "req-1", "digest-1", "deny")


def test_when_the_screen_cannot_answer_the_plugin_raises_so_hermes_asks_elsewhere(token_file):
    ctx = FakeCtx({"url": "http://127.0.0.1:9", "token_file": str(token_file)})
    plugin.register(ctx)
    with pytest.raises(client.Unavailable):
        ctx.transports["buddy"](FakeRequest())


def test_the_plugin_answers_before_hermes_own_deadline(rig, token_file):
    async def scenario():
        async with hermes(rig) as server:
            ctx = FakeCtx({"url": f"http://127.0.0.1:{server.port}", "token_file": str(token_file), "timeout": 600})
            plugin.register(ctx)
            started = time.monotonic()
            presenting = asyncio.ensure_future(in_thread(ctx.transports["buddy"], FakeRequest(timeout_seconds=6.5)))
            await on_screen(rig)
            with pytest.raises(client.Unavailable):
                await asyncio.wait_for(presenting, 10)
            return time.monotonic() - started, rig.view()["approvals"]

    took, approvals = run(scenario)
    assert took < 6.5 and approvals == []  # gave up with the host margin to spare, and cleaned the screen


def test_the_plugin_is_standard_library_only():
    for path in PLUGIN_DIR.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.startswith(("import ", "from ")) and not line.startswith("from ."):
                module = line.split()[1].split(".")[0]
                assert module in sys.stdlib_module_names, f"{path.name}: {line}"
    assert (PLUGIN_DIR / "plugin.yaml").is_file()
