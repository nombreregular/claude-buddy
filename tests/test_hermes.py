"""Hermes Agent as a second source of approvals: its own listener and token, the same queue as Claude.

The rig, the raw HTTP client and the headless Chromium helpers are the screen's (test_ui.py)."""
import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import signal
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import buddy_hermes  # noqa: E402
from buddy_hermes import HermesServer  # noqa: E402
from buddy_ui import UIServer  # noqa: E402
from test_ui import PAGE, PROMPT, Rig, _attr, _dump, heartbeat, needs_chromium, run  # noqa: E402
from ui_client import Stream, http  # noqa: E402

TOKEN = "hermes-test-token-0123456789"
REQUEST = {"id": "h_1", "tool": "terminal", "hint": "sudo systemctl restart pihole-FTL"}


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path / "state")


@contextlib.asynccontextmanager
async def hermes(rig, token=TOKEN, host="127.0.0.1", **kw):
    server = HermesServer(rig.core.broker, token, host=host, port=0, **kw)
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


@contextlib.asynccontextmanager
async def screen(rig):
    server = UIServer(rig.hub, port=0)
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


def auth(token=TOKEN):
    return {} if token is None else {"Authorization": f"Bearer {token}"}


def submit(server, request=REQUEST, token=TOKEN, **kw):
    headers = {"Content-Type": "application/json", **auth(token), **kw.pop("headers", {})}
    body = kw.pop("body", json.dumps(request).encode())
    return http(server, "POST", "/approval", headers=headers, body=body, **kw)


def poll(server, approval_id, wait=None, token=TOKEN, **kw):
    path = f"/approval/{approval_id}" + ("" if wait is None else f"?wait={wait}")
    return http(server, "GET", path, headers={**auth(token), **kw.pop("headers", {})}, **kw)


def withdraw(server, approval_id, token=TOKEN):
    return http(server, "DELETE", f"/approval/{approval_id}", headers=auth(token))


def tap(screen_server, key, decision):
    """What the page does when a button is tapped."""
    headers = {"Content-Type": "application/json", "X-Buddy-Token": screen_server.token}
    return http(screen_server, "POST", "/decide", headers=headers, body=json.dumps({"key": key, "decision": decision}).encode())


def answered_to_desktop(rig):
    return [m for m in rig.sent if m.get("cmd") == "permission"]


# --- a request from Hermes ---------------------------------------------------------------


def test_a_request_from_hermes_is_queued_and_shown_as_hermes(rig):
    async def scenario():
        async with hermes(rig) as server:
            status, headers, body = await submit(server)
            assert status == 202 and headers["content-type"] == "application/json"
            assert json.loads(body) == {"ok": True, "id": "h_1", "key": "hermes:h_1", "poll": "/approval/h_1",
                                        "ttl": buddy_hermes.DEFAULT_TTL_S}
            assert rig.view()["approvals"] == [
                {"key": "hermes:h_1", "tool": "terminal", "hint": "sudo systemctl restart pihole-FTL", "source": "hermes",
                 "always": ""}
            ]
            status, _, body = await poll(server, "h_1")
            assert status == 200
            assert json.loads(body) == {"id": "h_1", "key": "hermes:h_1", "tool": "terminal", "state": "pending",
                                        "decision": None}
            assert server.pending() == 1

    run(scenario)


def test_a_request_from_hermes_wakes_the_buddy_even_with_the_mac_away(rig):
    async def scenario():
        async with hermes(rig) as server:
            assert rig.view()["linked"] is False and rig.view()["mood"] == "sleep"
            await submit(server)
            assert rig.view()["mood"] == "attention"  # Hermes does not come through the Mac; the screen must still ask
            rig.hub.decide("hermes:h_1", "once")
            assert rig.view()["mood"] == "sleep"

    run(scenario)


def test_the_decision_from_the_screen_reaches_hermes(rig):
    async def scenario():
        async with hermes(rig) as server, screen(rig) as page:
            assert (await submit(server))[0] == 202
            status, _, _ = await tap(page, "hermes:h_1", "once")
            assert status == 200
            status, _, body = await poll(server, "h_1")
            assert status == 200 and json.loads(body)["state"] == "decided" and json.loads(body)["decision"] == "once"
            assert rig.view()["approvals"] == []
            assert answered_to_desktop(rig) == []  # Hermes' request is Hermes' business, the Mac hears nothing
            assert server.pending() == 0

            assert (await submit(server, {"id": "h_2", "tool": "terminal", "hint": "rm -rf /"}))[0] == 202
            assert (await tap(page, "hermes:h_2", "deny"))[0] == 200
            assert json.loads((await poll(server, "h_2"))[2])["decision"] == "deny"
            assert (await tap(page, "hermes:h_2", "once"))[0] == 409  # answered once, like Claude's

    run(scenario)


def test_hermes_can_wait_for_the_answer_instead_of_asking_again_and_again(rig):
    async def scenario():
        async with hermes(rig) as server:
            await submit(server)
            started = time.monotonic()
            waiting = asyncio.ensure_future(poll(server, "h_1", wait=10))
            await asyncio.sleep(0.2)
            assert not waiting.done()  # still pending: the connection is held open
            assert rig.hub.decide("hermes:h_1", "once") is True  # a tap on the screen
            status, _, body = await asyncio.wait_for(waiting, 2)
            assert status == 200 and json.loads(body)["decision"] == "once"
            assert time.monotonic() - started < 3  # it came back as soon as the decision was made, not after 10 s

    run(scenario)


def test_a_wait_that_runs_out_says_still_pending(rig):
    async def scenario():
        async with hermes(rig) as server:
            await submit(server)
            started = time.monotonic()
            status, _, body = await poll(server, "h_1", wait=0.3)
            assert status == 200 and json.loads(body)["state"] == "pending"
            assert time.monotonic() - started >= 0.25
            for bad in ("abc", "-1", "31", "1e9", ""):
                assert (await poll(server, "h_1", wait=bad))[0] == 400, bad
            assert (await poll(server, "h_1", wait=0))[0] == 200  # 0 means answer now

    run(scenario)


def test_a_poll_whose_client_hangs_up_is_let_go(rig):
    async def scenario():
        async with hermes(rig) as server:
            await submit(server)
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            writer.write(f"GET /approval/h_1?wait=30 HTTP/1.1\r\nHost: 127.0.0.1:{server.port}\r\n"
                         f"Authorization: Bearer {TOKEN}\r\n\r\n".encode())
            await writer.drain()
            await asyncio.sleep(0.2)
            assert len(server._tasks) == 1
            writer.close()
            for _ in range(100):
                if not server._tasks:
                    break
                await asyncio.sleep(0.02)
            assert not server._tasks  # not kept 30 s on a dead socket
            assert (await poll(server, "h_1"))[0] == 200

    run(scenario)


def test_hermes_can_withdraw_a_request_it_no_longer_needs(rig):
    async def scenario():
        async with hermes(rig) as server, screen(rig) as page:
            await submit(server)
            assert rig.view()["approvals"]
            status, _, body = await withdraw(server, "h_1")
            assert status == 200 and json.loads(body)["state"] == "withdrawn"
            assert rig.view()["approvals"] == []
            assert json.loads((await poll(server, "h_1"))[2])["state"] == "withdrawn"
            assert (await tap(page, "hermes:h_1", "once"))[0] == 409  # too late for the screen
            assert (await withdraw(server, "h_1"))[0] == 200  # again: harmless
            assert (await withdraw(server, "never"))[0] == 404
            assert (await poll(server, "never"))[0] == 404

    run(scenario)


def test_a_request_nobody_answers_expires_and_leaves_the_screen(rig):
    async def scenario():
        async with hermes(rig) as server:
            status, _, body = await submit(server, {**REQUEST, "ttl": 1})
            assert status == 202 and json.loads(body)["ttl"] == 1
            assert rig.view()["approvals"]
            await asyncio.sleep(1.3)
            assert rig.view()["approvals"] == []
            assert json.loads((await poll(server, "h_1"))[2])["state"] == "expired"
            assert rig.hub.decide("hermes:h_1", "once") is False

    run(scenario)


def test_a_duplicate_id_is_a_conflict_while_it_is_pending(rig):
    async def scenario():
        async with hermes(rig) as server:
            assert (await submit(server))[0] == 202
            status, _, body = await submit(server, {**REQUEST, "hint": "something else"})
            assert status == 409 and json.loads(body)["error"] == "already pending"
            assert [a["hint"] for a in rig.view()["approvals"]] == ["sudo systemctl restart pihole-FTL"]
            rig.hub.decide("hermes:h_1", "deny")
            assert (await submit(server))[0] == 202  # answered: the id can be used again
            assert json.loads((await poll(server, "h_1"))[2])["state"] == "pending"

    run(scenario)


def test_a_claude_code_session_in_a_terminal_comes_as_its_own_source(rig):
    request = {"id": "cli-1", "tool": "Bash", "hint": "git push", "source": "cli"}

    async def scenario():
        async with hermes(rig) as server, screen(rig) as page:
            status, _, body = await submit(server, request)
            assert status == 202 and json.loads(body)["key"] == "cli:cli-1"
            assert rig.view()["approvals"] == [{"key": "cli:cli-1", "tool": "Bash", "hint": "git push", "source": "cli",
                                                "always": ""}]
            assert (await tap(page, "cli:cli-1", "once"))[0] == 200
            assert json.loads((await poll(server, "cli-1"))[2])["decision"] == "once"
            assert answered_to_desktop(rig) == []
            # withdrawn and expired leave the screen whatever the source
            assert (await submit(server, {**request, "id": "cli-2"}))[0] == 202
            assert json.loads((await withdraw(server, "cli-2"))[2])["state"] == "withdrawn"
            assert rig.view()["approvals"] == []
            assert (await submit(server, {**request, "id": "cli-3", "ttl": 1}))[0] == 202
            await asyncio.sleep(1.3)
            assert rig.view()["approvals"] == [] and json.loads((await poll(server, "cli-3"))[2])["state"] == "expired"
            # only the sources this port speaks for: never "claude", whose answers go to the Mac
            for bad in ("claude", "Hermes", "", "cli ", 1, None, ["cli"]):
                assert (await submit(server, {**request, "id": "cli-4", "source": bad}))[0] == 400, bad
            assert server.pending() == 0 and rig.view()["approvals"] == []
            assert (await submit(server, {"id": "h_9"}))[0] == 202
            assert rig.view()["approvals"][0]["source"] == "hermes"  # no source: Hermes, as before

    run(scenario)


def test_only_a_claude_code_session_can_offer_always(rig):
    request = {"id": "cli-1", "tool": "Bash", "hint": "npm run lint", "source": "cli",
               "always": "Bash(npm run *) en este proyecto"}

    async def scenario():
        async with hermes(rig) as server, screen(rig) as page:
            assert (await submit(server, request))[0] == 202
            assert rig.view()["approvals"][0]["always"] == "Bash(npm run *) en este proyecto"
            assert (await tap(page, "cli:cli-1", "always"))[0] == 200
            assert json.loads((await poll(server, "cli-1"))[2])["decision"] == "always"
            # Hermes cannot, and the rule is text that fits the screen whole
            bad = [{**request, "source": "hermes"}, {**request, "source": None}, {k: v for k, v in request.items() if k != "source"},
                   {**request, "always": 1}, {**request, "always": None}, {**request, "always": ["x"]},
                   {**request, "always": "x" * (buddy_hermes.MAX_ALWAYS_CHARS + 1)}, {**request, "always": " Bash "},
                   {**request, "always": "Bash(a)\nBash(b)"}]
            for n, body in enumerate(bad):
                status, _, _ = await submit(server, {**body, "id": f"cli-bad-{n}"})
                assert status == 400, body
            assert rig.view()["approvals"] == []
            # without it, "always" is not an answer the screen can give
            assert (await submit(server, {**request, "id": "cli-2", "always": ""}))[0] == 202
            assert rig.view()["approvals"][0]["always"] == ""
            assert (await tap(page, "cli:cli-2", "always"))[0] == 400
            assert json.loads((await poll(server, "cli-2"))[2])["state"] == "pending"

    run(scenario)


def test_claude_and_hermes_share_one_queue_in_order_of_arrival(rig):
    async def scenario():
        async with hermes(rig) as server, screen(rig) as page:
            rig.rx(heartbeat(waiting=1, prompt=PROMPT))
            await submit(server)
            keys = lambda: [a["key"] for a in rig.view()["approvals"]]  # noqa: E731
            assert keys() == ["claude:req_1", "hermes:h_1"]
            assert rig.core.broker.current().key == "claude:req_1"  # the keyboard answers the oldest first too
            assert (await tap(page, "claude:req_1", "once"))[0] == 200
            assert keys() == ["hermes:h_1"]
            assert answered_to_desktop(rig) == [{"cmd": "permission", "id": "req_1", "decision": "once"}]
            rig.rx(heartbeat(waiting=1, prompt={"id": "req_2", "tool": "Edit", "hint": "a.py"}))
            assert keys() == ["hermes:h_1", "claude:req_2"]
            assert (await tap(page, "hermes:h_1", "deny"))[0] == 200
            assert keys() == ["claude:req_2"]
            assert json.loads((await poll(server, "h_1"))[2])["decision"] == "deny"
            assert len(answered_to_desktop(rig)) == 1  # Hermes' answer never went to the Mac
            rig.advance(3)  # past the "heart" one-shot that a quick approval earns
            assert rig.core.persona() == "attention"  # a pending request of any source keeps the buddy alert

    run(scenario)


# --- who may talk to it ------------------------------------------------------------------


def test_without_the_right_token_nothing_is_queued(rig, monkeypatch):
    monkeypatch.setattr(buddy_hermes, "AUTH_FAIL_MAX", 100)  # the lockout has its own test below

    async def scenario():
        async with hermes(rig) as server:
            for token in ("", "wrong", TOKEN[:-1], TOKEN + "x", TOKEN.upper(), "ñandú", None):
                status, _, body = await submit(server, token=token)
                assert status == 403 and json.loads(body)["error"] == "bad token", token
            for headers in ({"Authorization": TOKEN}, {"Authorization": f"Basic {TOKEN}"}, {"X-Buddy-Token": TOKEN},
                            {"Authorization": f"Bearer  {TOKEN} extra"}):
                assert (await submit(server, token=None, headers=headers))[0] == 403, headers
            assert rig.view()["approvals"] == [] and server.pending() == 0
            await submit(server)
            assert (await poll(server, "h_1", token="wrong"))[0] == 403
            assert (await withdraw(server, "h_1", token="wrong"))[0] == 403
            assert rig.view()["approvals"]  # still there

    run(scenario)


def test_repeated_bad_tokens_lock_that_client_out_for_a_while(rig):
    async def scenario():
        async with hermes(rig) as server:
            for _ in range(buddy_hermes.AUTH_FAIL_MAX):
                assert (await submit(server, token="wrong"))[0] == 403
            status, _, body = await submit(server)  # the right token, too late
            assert status == 429 and "bad tokens" in json.loads(body)["error"]
            assert (await http(server, path="/healthz"))[0] == 429  # everything from that client
            assert rig.view()["approvals"] == []

    run(scenario)


def test_too_many_requests_in_a_minute_are_throttled(rig, monkeypatch):
    monkeypatch.setattr(buddy_hermes, "RATE_MAX", 5)

    async def scenario():
        async with hermes(rig) as server:
            for _ in range(5):
                assert (await http(server, path="/healthz"))[0] == 200
            assert (await http(server, path="/healthz"))[0] == 429
            assert (await submit(server))[0] == 429

    run(scenario)


def test_malformed_requests_are_refused(rig):
    async def scenario():
        async with hermes(rig) as server:
            bad = [
                b"", b"not json", b"\xff\xfe", b"[]", b"null", b"42", b'"h_1"',
                b'{"tool": "terminal"}',  # no id
                b'{"id": ""}', b'{"id": 7}', b'{"id": ["h_1"]}', b'{"id": "has space"}', b'{"id": "a/b"}',
                b'{"id": "a:b"}', b'{"id": "' + b"x" * 65 + b'"}', b'{"id": "../x"}',
                b'{"id": "h_1", "tool": 7}', b'{"id": "h_1", "hint": ["x"]}',
                b'{"id": "h_1", "ttl": "900"}', b'{"id": "h_1", "ttl": true}', b'{"id": "h_1", "ttl": 0}',
                b'{"id": "h_1", "ttl": 99999}',
            ]
            for body in bad:
                status, _, _ = await submit(server, body=body)
                assert status == 400, body
            status, _, body = await submit(server, {**REQUEST, "callback": "http://10.0.0.5:9/hook"})
            assert status == 400 and "poll GET /approval/<id>" in json.loads(body)["error"]
            for ctype in ("application/x-www-form-urlencoded", "text/plain", "multipart/form-data; boundary=x", None):
                headers = {**auth(), **({"Content-Type": ctype} if ctype else {})}
                status, _, _ = await http(server, "POST", "/approval", headers=headers, body=json.dumps(REQUEST).encode())
                assert status == 415, ctype
            assert rig.view()["approvals"] == [] and server.pending() == 0
            # what is accepted is cut to what the screen can show
            status, _, _ = await submit(server, {"id": "h_1", "tool": "T" * 200, "hint": "H" * 2000, "ttl": 60.5})
            assert status == 202
            a = rig.view()["approvals"][0]
            assert len(a["tool"]) == 80 and len(a["hint"]) == 500
            assert (await submit(server, {"id": "h.2-ok_3"}))[0] == 202  # tool and hint are optional

    run(scenario)


def test_the_host_must_be_the_address_hermes_connected_to(rig):
    async def scenario():
        async with hermes(rig, hosts={"buddy", "Buddy.local"}) as server:
            port = server.port
            for host in ("evil.example", f"evil.example:{port}", f"127.0.0.1:{port + 1}", "127.0.0.1", "",
                         f"10.0.0.5:{port}", "buddy.local", f"buddy.local:{port + 1}"):
                status, _, body = await submit(server, host=host or None, headers={"Host": host})
                assert status == 403 and json.loads(body)["error"] == "unexpected host", host
                assert (await http(server, path="/healthz", headers={"Host": host}))[0] == 403, host
            for host in (f"127.0.0.1:{port}", f"buddy:{port}", f"buddy.local:{port}", f"BUDDY.LOCAL:{port}"):
                assert (await http(server, path="/healthz", headers={"Host": host}))[0] == 200, host
            assert rig.view()["approvals"] == []

    run(scenario)


def test_a_browser_is_refused_even_with_the_token(rig):
    async def scenario():
        async with hermes(rig) as server:
            for origin in ("http://evil.example", f"http://127.0.0.1:{server.port}", "null", ""):
                status, _, body = await submit(server, headers={"Origin": origin})
                assert status == 403 and "browsers" in json.loads(body)["error"], origin
            assert rig.view()["approvals"] == []

    run(scenario)


def test_the_screen_routes_are_not_here_and_the_hermes_routes_are_not_on_the_screen(rig):
    async def scenario():
        async with hermes(rig) as server, screen(rig) as page:
            await submit(server)
            for method, path in (("GET", "/"), ("GET", "/index.html"), ("GET", "/events"), ("GET", "/state"),
                                 ("POST", "/decide"), ("GET", "/character/bufo/sleep.gif"), ("GET", "/favicon.ico")):
                headers = {**auth(), "Content-Type": "application/json"}
                body = json.dumps({"key": "hermes:h_1", "decision": "once"}).encode() if method == "POST" else b""
                status, _, _ = await http(server, method, path, headers=headers, body=body)
                assert status == 404, (method, path)
            assert rig.view()["approvals"]  # nothing on the Hermes port approved it
            for method, path in (("POST", "/approval"), ("GET", "/approval/h_1"), ("DELETE", "/approval/h_1")):
                headers = {**auth(), "Content-Type": "application/json", "X-Buddy-Token": page.token}
                status, _, _ = await http(page, method, path, headers=headers, body=json.dumps(REQUEST).encode())
                assert status == 404, (method, path)
            assert (await http(server, "POST", "/approval/h_1", headers={**auth(), "Content-Type": "application/json"},
                               body=b"{}"))[0] == 405
            assert (await http(server, "GET", "/approval", headers=auth()))[0] == 405
            assert (await http(server, "POST", "/healthz", headers=auth()))[0] == 405
            assert (await http(server, path="/approval/..%2Fx", headers=auth()))[0] == 404
            assert (await http(server, path="/healthz"))[0] == 200  # the only thing with no token

    run(scenario)


def test_it_listens_where_it_is_told_and_only_with_a_real_token(rig):
    for token in ("", "short", "x" * (buddy_hermes.MIN_TOKEN_CHARS - 1), None, 12345):
        with pytest.raises(ValueError):
            HermesServer(rig.core.broker, token, host="127.0.0.1")
    with pytest.raises(ValueError):
        HermesServer(rig.core.broker, TOKEN, host="")

    async def scenario():
        async with hermes(rig, host="0.0.0.0") as server:  # the screen refuses this; Hermes' listener is for the network
            assert server._server.sockets[0].getsockname()[0] == "0.0.0.0"
            assert server.url == f"http://0.0.0.0:{server.port}"
            # the Host check follows the address the client used, whatever the socket is bound to
            assert (await http(server, path="/healthz"))[0] == 200
            assert (await http(server, path="/healthz", headers={"Host": f"0.0.0.0:{server.port}"}))[0] == 403
            assert (await submit(server))[0] == 202

    run(scenario)


def test_the_token_is_never_logged(rig, caplog):
    caplog.set_level(logging.DEBUG)

    async def scenario():
        async with hermes(rig) as server:
            await submit(server)
            await submit(server, token="wrong-" + TOKEN)
            rig.hub.decide("hermes:h_1", "once")
            await poll(server, "h_1")

    run(scenario)
    assert caplog.records  # it does log...
    assert TOKEN not in caplog.text  # ...but never that
    assert "hermes" in caplog.text


def test_answered_requests_are_forgotten_after_a_while(rig, monkeypatch):
    async def scenario():
        monkeypatch.setattr(buddy_hermes, "MAX_DONE", 1)
        async with hermes(rig) as server:
            for i in range(3):
                await submit(server, {"id": f"h_{i}"})
                rig.hub.decide(f"hermes:h_{i}", "once")
            assert (await poll(server, "h_0"))[0] == 404  # the oldest went first
            assert (await poll(server, "h_1"))[0] == 404
            assert json.loads((await poll(server, "h_2"))[2])["state"] == "decided"
            monkeypatch.setattr(buddy_hermes, "DONE_TTL_S", 0.0)
            await submit(server, {"id": "h_9"})
            await asyncio.sleep(0.01)
            rig.hub.decide("hermes:h_9", "deny")
            assert (await poll(server, "h_2"))[0] == 404  # too old now

    run(scenario)


def test_there_is_room_for_only_so_many_pending_requests(rig, monkeypatch):
    monkeypatch.setattr(buddy_hermes, "MAX_PENDING", 2)

    async def scenario():
        async with hermes(rig) as server:
            assert (await submit(server, {"id": "a"}))[0] == 202
            assert (await submit(server, {"id": "b"}))[0] == 202
            status, _, body = await submit(server, {"id": "c"})
            assert status == 429 and json.loads(body)["error"] == "too many pending requests"
            rig.hub.decide("hermes:a", "once")
            assert (await submit(server, {"id": "c"}))[0] == 202

    run(scenario)


# --- the page ----------------------------------------------------------------------------


def test_the_page_names_the_source_when_it_is_not_claude():
    page = PAGE.read_text(encoding="utf-8")
    assert "a.source !== 'claude'" in page
    assert "'approval-hermes'" in page and "'approval-cli'" in page


@needs_chromium
def test_the_page_shows_hermes_as_the_source_of_the_request(rig):
    dom = _dump(f"{PAGE.as_uri()}?demo=approval-hermes&tag=0")
    assert _attr(dom, "data-screen") == "ask"
    assert "hermes · terminal" in dom and "Hermes pide permiso" in dom

    async def scenario():
        async with hermes(rig) as server, screen(rig) as page:
            await submit(server, {"id": "h_1", "tool": "terminal", "hint": "sudo reboot <b>now</b>"})
            return await asyncio.get_running_loop().run_in_executor(None, _dump, page.url + "/?poll=300")

    dom = run(scenario)
    assert _attr(dom, "data-screen") == "ask" and _attr(dom, "data-mood") == "attention"
    assert "hermes · terminal" in dom
    assert "sudo reboot &lt;b&gt;now&lt;/b&gt;" in dom and "<b>now</b>" not in dom  # text, never markup


@needs_chromium
def test_the_page_shows_a_terminal_session_as_claude_code(rig):
    dom = _dump(f"{PAGE.as_uri()}?demo=approval-cli&tag=0")
    assert _attr(dom, "data-screen") == "ask"
    assert "cli · Bash" in dom and "Claude Code pide permiso" in dom

    async def scenario():
        async with hermes(rig) as server, screen(rig) as page:
            await submit(server, {"id": "cli-1", "tool": "Bash", "hint": "sudo reboot <b>now</b>", "source": "cli"})
            return await asyncio.get_running_loop().run_in_executor(None, _dump, page.url + "/?poll=300")

    dom = run(scenario)
    assert _attr(dom, "data-screen") == "ask"
    assert "cli · Bash" in dom and "Claude Code pide permiso" in dom
    assert "sudo reboot &lt;b&gt;now&lt;/b&gt;" in dom and "<b>now</b>" not in dom  # text, never markup


@needs_chromium
def test_the_page_offers_always_only_with_the_rule_in_sight(rig):
    def shown(dom):
        btns = re.search(r'<div id="btns" class="([^"]*)"', dom).group(1)
        note = re.search(r'<div id="always-note"( style="[^"]*")?>', dom).group(1) or ""
        return "three" in btns.split(), not re.search(r"display:\s*none", note)

    dom = _dump(f"{PAGE.as_uri()}?demo=approval-cli-always&tag=0")
    assert shown(dom) == (True, True) and "Bash(npm run *) en este proyecto" in dom and "Siempre" in dom
    assert shown(_dump(f"{PAGE.as_uri()}?demo=approval-cli&tag=0")) == (False, False)
    assert shown(_dump(f"{PAGE.as_uri()}?demo=approval&tag=0")) == (False, False)

    async def scenario():
        async with hermes(rig) as server, screen(rig) as page:
            await submit(server, {"id": "cli-1", "tool": "Bash", "hint": "npm run lint", "source": "cli",
                                  "always": "Bash(<b>x</b>) en este proyecto"})
            return await asyncio.get_running_loop().run_in_executor(None, _dump, page.url + "/?poll=300")

    dom = run(scenario)
    assert shown(dom) == (True, True)
    assert "Bash(&lt;b&gt;x&lt;/b&gt;) en este proyecto" in dom and "<b>x</b>" not in dom  # text, never markup


# --- end to end: the real daemon ----------------------------------------------------------

needs_dbus = pytest.mark.skipif(shutil.which("dbus-daemon") is None, reason="dbus-daemon not installed")


@needs_dbus
def test_the_daemon_takes_a_request_from_hermes_and_the_screen_answers_it(tmp_path):
    from fake_bluez import DBusDaemon, FakeBlueZ
    from test_cli import Proc

    dbus = DBusDaemon()
    token_file = tmp_path / "hermes.token"
    token_file.write_text(TOKEN + "\n", encoding="utf-8")
    os.chmod(token_file, 0o600)

    async def scenario():
        fake = FakeBlueZ(dbus.address)
        await fake.start()
        buddy = await Proc.start(dbus, tmp_path, "--hermes-bind", "127.0.0.1", "--hermes-port", "0",
                                 "--hermes-token-file", str(token_file), keyboard=False)
        try:
            await buddy.expect("Bluetooth listo")
            await buddy.expect("Hermes en http://127.0.0.1:")
            hport = int(re.search(r"Hermes en http://127\.0\.0\.1:(\d+)/approval", buddy.text).group(1))
            port = await buddy.screen_port()
            assert hport != port
            _, _, page = await http(port, path="/")
            page_token = re.search(rb'name="buddy-token" content="([^"]+)"', page).group(1).decode()

            status, _, body = await submit(hport)
            assert status == 202 and json.loads(body)["key"] == "hermes:h_1"
            await buddy.expect("¿APROBAR?  [hermes] terminal: sudo systemctl restart pihole-FTL")
            stream = await Stream.open(port)
            try:
                v = await stream.state(lambda v: v["approvals"])
                assert v["approvals"] == [{"key": "hermes:h_1", "tool": "terminal", "source": "hermes",
                                           "hint": "sudo systemctl restart pihole-FTL", "always": ""}]
                waiting = asyncio.ensure_future(poll(hport, "h_1", wait=10))
                await asyncio.sleep(0.2)
                headers = {"Content-Type": "application/json", "X-Buddy-Token": page_token}
                status, _, _ = await http(port, "POST", "/decide", headers=headers,
                                          body=json.dumps({"key": "hermes:h_1", "decision": "once"}).encode())
                assert status == 200
                status, _, body = await asyncio.wait_for(waiting, 3)
                assert status == 200 and json.loads(body)["decision"] == "once"
                await stream.state(lambda v: not v["approvals"])
                await buddy.expect("APROBADO [hermes] terminal")
            finally:
                stream.close()
            # the screen's routes are not on Hermes' port, and the page's token means nothing there
            assert (await http(hport, path="/", headers=auth()))[0] == 404
            assert (await http(hport, "POST", "/approval", headers={"Content-Type": "application/json", "X-Buddy-Token": page_token},
                               body=json.dumps(REQUEST).encode()))[0] == 403
            assert TOKEN not in buddy.text
            buddy.proc.send_signal(signal.SIGTERM)
            assert await buddy.finish() == 0
            with pytest.raises(OSError):
                await asyncio.open_connection("127.0.0.1", hport)
        finally:
            await buddy.kill()
            fake.stop()

    try:
        run(scenario)
    finally:
        dbus.close()


@needs_dbus
def test_without_a_token_hermes_stays_off_and_the_buddy_runs(tmp_path):
    from fake_bluez import DBusDaemon, FakeBlueZ
    from test_cli import Proc

    dbus = DBusDaemon()
    short = tmp_path / "short.token"
    short.write_text("abc", encoding="utf-8")

    async def scenario():
        fake = FakeBlueZ(dbus.address)
        await fake.start()
        cases = [
            ((), "Hermes apagado: falta el token"),
            (("--hermes-token-file", str(tmp_path / "missing.token")), "Hermes apagado: no pude leer el token"),
            (("--hermes-token-file", str(short)), "Hermes apagado: el token de Hermes necesita al menos"),
        ]
        for extra, expected in cases:
            buddy = await Proc.start(dbus, tmp_path, "--hermes-bind", "127.0.0.1", "--hermes-port", "0", *extra, keyboard=False)
            try:
                await buddy.expect(expected)
                await buddy.screen_port()  # the screen is up all the same
                await buddy.expect("Bluetooth listo")
                assert "Hermes en http" not in buddy.text
                buddy.proc.send_signal(signal.SIGTERM)
                assert await buddy.finish() == 0
            finally:
                await buddy.kill()
        fake.stop()

    try:
        run(scenario)
    finally:
        dbus.close()


# --- what each session is doing (POST /session) ----------------------------------------------


def report(server, body, token=TOKEN):
    headers = {"Content-Type": "application/json", **auth(token)}
    return http(server, "POST", "/session", headers=headers, body=json.dumps(body).encode())


def test_sessions_report_what_they_do_and_the_view_lists_them(rig):
    async def scenario():
        async with hermes(rig, tasks=rig.core.tasks) as server:
            status, _, body = await report(server, {"id": "sess-1", "project": "relay", "tool": "Bash", "what": "npm test"})
            assert status == 200 and json.loads(body) == {"ok": True, "id": "sess-1", "state": "working"}
            rig.t += 3
            assert (await report(server, {"id": "sess-2", "project": "claude-buddy", "tool": "Edit", "what": "~/buddy_ui.py"}))[0] == 200
            tasks = rig.view()["tasks"]
            assert [(t["project"], t["tool"], t["what"], t["state"], t["age"]) for t in tasks] == [
                ("claude-buddy", "Edit", "~/buddy_ui.py", "working", 0), ("relay", "Bash", "npm test", "working", 3)]
            assert (await report(server, {"id": "sess-1", "state": "idle"}))[0] == 200
            assert [t["state"] for t in rig.view()["tasks"]] == ["working", "idle"]
            status, _, body = await report(server, {"id": "sess-1", "state": "gone"})
            assert status == 200 and json.loads(body)["state"] == "gone"
            assert [t["id"] for t in rig.view()["tasks"]] == ["sess-2"]

    run(scenario)


def test_session_reports_are_validated_and_need_the_token(rig):
    async def scenario():
        async with hermes(rig, tasks=rig.core.tasks) as server:
            assert (await report(server, {"id": "s", "project": "p"}, token="wrong-token-0123456789"))[0] == 403
            assert (await report(server, {"id": "bad id!", "project": "p"}))[0] == 400
            assert (await report(server, {"id": "s", "state": "dancing"}))[0] == 400
            assert (await report(server, {"id": "s", "project": ["no"]}))[0] == 400
            assert (await http(server, "GET", "/session", headers=auth()))[0] == 405
            assert rig.view()["tasks"] == []
        async with hermes(rig) as server:  # no board wired: the route does not exist
            assert (await report(server, {"id": "s", "project": "p"}))[0] == 404

    run(scenario)


def error_of(body):
    return json.loads(body)["error"]


def test_session_reports_carry_title_group_and_parent_into_the_view(rig):
    async def scenario():
        async with hermes(rig, tasks=rig.core.tasks) as server:
            mother = {"id": "mother-1", "project": "api-pagos", "tool": "Bash", "what": "pytest -q",
                      "title": "Council retirado", "group": "Acme Studio"}
            kid = {"id": "kid-1", "project": "api-pagos", "tool": "Edit", "what": "~/x.py",
                   "title": "Subtarea del council", "group": "Acme Studio", "parent": "mother-1"}
            assert (await report(server, mother))[0] == 200
            rig.t += 2
            assert (await report(server, kid))[0] == 200
            by_id = {t["id"]: t for t in rig.view()["tasks"]}
            assert by_id["mother-1"] == {"id": "mother-1", "project": "api-pagos", "tool": "Bash", "what": "pytest -q",
                                         "state": "working", "age": 2, "title": "Council retirado",
                                         "group": "Acme Studio", "source": "hook"}  # no parent, no parent key
            assert by_id["kid-1"]["parent"] == "mother-1" and by_id["kid-1"]["title"] == "Subtarea del council"
            # the next report of the same thread does not repeat them: the board keeps what it has
            assert (await report(server, {"id": "kid-1", "tool": "Bash", "what": "ls"}))[0] == 200
            assert (await report(server, {"id": "kid-1", "state": "idle"}))[0] == 200
            kept = {t["id"]: t for t in rig.view()["tasks"]}["kid-1"]
            assert (kept["title"], kept["group"], kept["parent"], kept["state"]) == (
                "Subtarea del council", "Acme Studio", "mother-1", "idle")
            # "" clears one field and leaves the others
            assert (await report(server, {"id": "kid-1", "group": "", "parent": ""}))[0] == 200
            cleared = {t["id"]: t for t in rig.view()["tasks"]}["kid-1"]
            assert "group" not in cleared and "parent" not in cleared and cleared["title"] == "Subtarea del council"
            # a new title replaces; text is cut like the board cuts it
            assert (await report(server, {"id": "kid-1", "title": "t" * 200, "group": "g" * 200}))[0] == 200
            cut = {t["id"]: t for t in rig.view()["tasks"]}["kid-1"]
            assert cut["title"] == "t" * 60 and cut["group"] == "g" * 40
            # the same report goes out of the daemon as the page reads it
            stream_state = json.loads(rig.hub.current())
            assert {t["id"] for t in stream_state["tasks"]} == {"mother-1", "kid-1"}

    run(scenario)


def test_session_report_carries_said_and_the_view_shows_it(rig):
    async def scenario():
        async with hermes(rig, tasks=rig.core.tasks) as server:
            assert (await report(server, {"id": "s", "project": "p", "tool": "Bash", "what": "ls"}))[0] == 200
            assert "said" not in rig.view()["tasks"][0]
            said = "Terminé el login.\n\nFalta el test de sesión: ¿lo escribo ahora?"
            status, _, body = await report(server, {"id": "s", "state": "idle", "said": said})
            assert status == 200 and json.loads(body) == {"ok": True, "id": "s", "state": "idle"}
            assert rig.view()["tasks"][0]["said"] == said  # paragraph breaks survive
            assert (await report(server, {"id": "s", "tool": "Edit", "what": "x.py"}))[0] == 200  # next turn, no said
            assert rig.view()["tasks"][0]["said"] == said
            assert (await report(server, {"id": "s", "state": "idle", "said": ""}))[0] == 200
            assert "said" not in rig.view()["tasks"][0]
            long = "palabra " * 100
            assert (await report(server, {"id": "s", "said": long}))[0] == 200
            assert len(rig.view()["tasks"][0]["said"]) == 300

    run(scenario)


def test_session_thread_fields_are_validated(rig):
    async def scenario():
        async with hermes(rig, tasks=rig.core.tasks) as server:
            for key in ("said", "title", "group", "parent", "project", "tool", "what"):
                for bad in (["x"], 5, {"a": 1}, None, True):
                    status, _, body = await report(server, {"id": "s", key: bad})
                    assert status == 400 and error_of(body) == f"{key} must be a string", (key, bad)
            for bad in ("bad id!", "with space", "../up", "a/b", "x" * 65, "ñandú", "<img src=x>"):
                status, _, body = await report(server, {"id": "s", "parent": bad})
                assert status == 400 and "parent must be 1 to 64 characters" in error_of(body), bad
            assert rig.view()["tasks"] == []  # none of the refused reports left a row behind
            token_only = await report(server, {"id": "s", "title": "x"}, token="wrong-token-0123456789")
            assert token_only[0] == 403 and rig.view()["tasks"] == []
            # a good report, then a refused one: the board keeps what the good one said
            assert (await report(server, {"id": "s", "title": "uno", "group": "Buddy", "parent": "p-1"}))[0] == 200
            assert (await report(server, {"id": "s", "title": "dos", "group": ["no"]}))[0] == 400
            task = rig.view()["tasks"][0]
            assert (task["title"], task["group"], task["parent"]) == ("uno", "Buddy", "p-1")
            # an empty parent is allowed (it clears); 64 characters is the longest id
            assert (await report(server, {"id": "s", "parent": ""}))[0] == 200
            assert "parent" not in rig.view()["tasks"][0]
            assert (await report(server, {"id": "s", "parent": "p" * 64}))[0] == 200
            assert rig.view()["tasks"][0]["parent"] == "p" * 64
            # a thread is never its own parent
            assert (await report(server, {"id": "s", "parent": "s"}))[0] == 200
            assert "parent" not in rig.view()["tasks"][0]

    run(scenario)


def test_session_ids_groups_and_parents_that_are_names_of_object_properties_are_harmless(rig):
    async def scenario():
        async with hermes(rig, tasks=rig.core.tasks) as server:
            for name in ("constructor", "__proto__", "toString", "hasOwnProperty", "valueOf"):
                status, _, body = await report(server, {"id": name, "project": name, "title": name, "group": name,
                                                         "parent": "__proto__", "said": name})
                assert status == 200 and json.loads(body) == {"ok": True, "id": name, "state": "working"}
            tasks = rig.view()["tasks"]
            assert sorted(t["id"] for t in tasks) == ["__proto__", "constructor", "hasOwnProperty", "toString", "valueOf"]
            assert all(t["group"] == t["id"] == t["title"] == t["said"] for t in tasks)
            assert {t["id"]: t.get("parent") for t in tasks}["__proto__"] is None  # its own name: not its own parent
            state = json.loads(rig.hub.current())  # and the view still serialises as one JSON document
            assert len(state["tasks"]) == 5
            assert (await report(server, {"id": "__proto__", "state": "gone"}))[0] == 200
            assert "__proto__" not in {t["id"] for t in rig.view()["tasks"]}

    run(scenario)


def test_a_300_char_said_in_the_worst_encoding_fits_the_body_limit(rig):
    async def scenario():
        async with hermes(rig, tasks=rig.core.tasks) as server:
            emoji = "\U0001F600"  # json.dumps writes each as a 12 byte surrogate pair escape
            body = {"id": "7" * 36, "project": "claude-buddy", "state": "idle", "said": emoji * 300}
            assert 3000 < len(json.dumps(body)) < 4096
            assert (await report(server, body))[0] == 200
            assert rig.view()["tasks"][0]["said"] == emoji * 300
            body["said"] = emoji * 400  # why the cap is 300: the body would not fit
            assert len(json.dumps(body)) > 4096
            assert (await report(server, body))[0] == 413
            assert rig.view()["tasks"][0]["said"] == emoji * 300  # the refused report changed nothing

    run(scenario)


def test_session_with_all_thread_fields_fits_the_body_cap_and_logs_none_of_them(rig, caplog):
    marker = "MARCA-SECRETA-4f9a"
    caplog.set_level(logging.DEBUG)

    async def scenario():
        async with hermes(rig, tasks=rig.core.tasks) as server:
            # what the hook can post at most: the hook trims its own body to about 3.9 KB, the listener takes 4 KB
            body = {"id": "9" * 36, "project": marker + "p" * 20, "tool": "Bash", "what": marker + " pytest", "state": "idle",
                    "said": marker + " dijo " + "s" * 150, "title": marker + " " + "t" * 40, "group": marker + " " + "g" * 20,
                    "parent": "8" * 36}
            assert len(json.dumps(body)) < 4096
            status, _, answer = await report(server, body)
            assert status == 200 and json.loads(answer) == {"ok": True, "id": "9" * 36, "state": "idle"}
            task = rig.view()["tasks"][0]
            assert all(marker in task[key] for key in ("project", "what", "said", "title", "group"))
            # refused reports are not logged either, nor are the 400 messages made of what was sent
            for refused in ({"id": "9" * 36, "title": [marker]}, {"id": "9" * 36, "parent": marker + " no es un id"},
                            {"id": marker + " bad id", "title": marker}):
                status, _, answer = await report(server, refused)
                assert status == 400 and marker not in answer.decode()

    run(scenario)
    assert "hermes listener on" in caplog.text  # the capture works...
    assert marker not in caplog.text and "dijo" not in caplog.text  # ...and holds nothing of what the sessions said



# --- POST /census: what runs on the Mac without a hook to speak for it ------------------------------


def census(server, body, token=TOKEN, **kw):
    headers = {"Content-Type": "application/json", **auth(token), **kw.pop("headers", {})}
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    return http(server, "POST", "/census", headers=headers, body=data, **kw)


def crow(row_id, **kw):
    return {"id": row_id, "name": str(row_id).split("-", 1)[-1], "group": "Corridas", "state": "working", "elapsed": 100, **kw}


def snap(*rows, **kw):
    return {"v": 1, "source": "mac", "ttl": 60, "more": 0, "rows": list(rows), **kw}


def view_ids(rig):
    return [t["id"] for t in rig.view()["tasks"]]


SESSION = "3f2a9c1e-7b44-4d0a-9e51-0c8d6a2b1f77"


def test_a_census_snapshot_lists_the_runs_and_a_new_one_replaces_it(rig):
    async def scenario():
        async with hermes(rig, tasks=rig.core.tasks) as server:
            body = snap(crow("run-quietud", name="quietud", elapsed=3643, idle=538, session=SESSION, project="relay-x"),
                        crow("job-noche13", elapsed=3048), seq=1760793600123)
            status, headers, answer = await census(server, body)
            assert status == 200 and headers["content-type"] == "application/json"
            assert json.loads(answer) == {"ok": True, "rows": 2, "ttl": 60}
            view = rig.view()
            assert [(t["id"], t["source"], t["group"], t["title"], t["state"], t["elapsed"]) for t in view["tasks"]] == [
                ("run-quietud", "census", "Corridas", "quietud", "working", 3643),
                ("job-noche13", "census", "Corridas", "noche13", "working", 3048)]
            assert view["tasks"][0]["idle"] == 538 and "idle" not in view["tasks"][1]
            assert (view["working_total"], view["tasks_total"], view["census_total"], view["tasks_more"]) == (2, 2, 2, 0)
            assert (await census(server, snap(crow("job-noche13", elapsed=3063), seq=1760793615123)))[0] == 200
            assert view_ids(rig) == ["job-noche13"]  # the run that is no longer in the snapshot left at once
            assert (await census(server, snap(seq=1760793630123)))[0] == 200
            assert rig.view()["tasks"] == [] and rig.view()["census_total"] == 0  # an empty snapshot is a valid answer

    run(scenario)


def test_a_run_that_is_also_a_session_is_one_row_and_a_killed_one_leaves_with_the_next_snapshot(rig):
    async def scenario():
        async with hermes(rig, tasks=rig.core.tasks) as server:
            assert (await report(server, {"id": SESSION, "project": "relay-x", "tool": "Bash", "what": "pnpm e2e"}))[0] == 200
            assert (await report(server, {"id": "desk-1", "project": "relay", "tool": "Edit", "what": "a.ts"}))[0] == 200
            assert (await census(server, snap(crow("run-quietud", name="quietud", session=SESSION, elapsed=40))))[0] == 200
            rows = rig.view()["tasks"]
            assert [(t["id"], t["tool"], t["what"]) for t in rows] == [("run-quietud", "Bash", "pnpm e2e"), ("desk-1", "Edit", "a.ts")]
            rig.t += 15  # the run is killed: no SessionEnd, only a snapshot that no longer has it
            assert (await census(server, snap()))[0] == 200
            assert view_ids(rig) == ["desk-1"]  # not a "working" row with its last tool for ten more minutes
            assert (await report(server, {"id": SESSION, "project": "relay-x", "tool": "Bash"}))[0] == 200
            assert (await census(server, snap(crow("run-quietud", session=SESSION))))[0] == 200
            assert (await report(server, {"id": SESSION, "state": "gone"}))[0] == 200  # a clean end: SessionEnd hides the run now
            assert view_ids(rig) == ["desk-1"]

    run(scenario)


def test_the_farewell_of_a_census_that_stops_empties_its_group_and_leaves_the_sessions_alone(rig):
    """What census.py posts on SIGTERM: no rows and the smallest ttl. The runs it listed may well go on."""
    async def scenario():
        async with hermes(rig, tasks=rig.core.tasks) as server:
            assert (await report(server, {"id": SESSION, "project": "relay-x", "tool": "Bash", "what": "pnpm e2e"}))[0] == 200
            assert (await census(server, snap(crow("run-quietud", name="quietud", session=SESSION, elapsed=40))))[0] == 200
            assert view_ids(rig) == ["run-quietud"]
            assert (await census(server, {"v": 1, "source": "mac", "ttl": 5, "more": 0, "rows": []}))[0] == 200
            rows = rig.view()["tasks"]
            assert [(t["id"], t["source"], t["tool"]) for t in rows] == [(SESSION, "hook", "Bash")]  # still a thread, not deleted
            assert rig.view()["census_total"] == 0
            assert (await census(server, snap(crow("run-quietud", name="quietud", session=SESSION, elapsed=55))))[0] == 200
            assert view_ids(rig) == ["run-quietud"]  # the census came back: one row again
            assert (await census(server, snap()))[0] == 200  # but an empty snapshot of one that goes on does mean the run ended
            assert rig.view()["tasks"] == []

    run(scenario)


BAD_SNAPSHOTS = [
    b"", b"not json", b"\xff\xfe", b"[]", b"null", b"42", b'"run-a"', b"{}",
    {"rows": []},  # no v
    {"v": 2, "rows": []}, {"v": "1", "rows": []}, {"v": True, "rows": []}, {"v": 1.0, "rows": []}, {"v": None, "rows": []},
    {"v": 1},  # no rows
    {"v": 1, "rows": {}}, {"v": 1, "rows": "run-a"}, {"v": 1, "rows": None},
    {"v": 1, "rows": [crow(f"run-{n}") for n in range(9)]},  # at most 8 on the wire
    {"v": 1, "source": "Mac", "rows": []}, {"v": 1, "source": "a b", "rows": []}, {"v": 1, "source": "", "rows": []},
    {"v": 1, "source": "x" * 25, "rows": []}, {"v": 1, "source": 7, "rows": []}, {"v": 1, "source": ["mac"], "rows": []},
    {"v": 1, "source": None, "rows": []}, {"v": 1, "source": "mac/../x", "rows": []},
    {"v": 1, "seq": -1, "rows": []}, {"v": 1, "seq": True, "rows": []}, {"v": 1, "seq": "5", "rows": []},
    {"v": 1, "seq": 1.5, "rows": []}, {"v": 1, "seq": None, "rows": []}, {"v": 1, "seq": 2 ** 63, "rows": []},
    {"v": 1, "ttl": "60", "rows": []}, {"v": 1, "ttl": True, "rows": []}, {"v": 1, "ttl": None, "rows": []},
    {"v": 1, "ttl": [60], "rows": []},
    {"v": 1, "more": "3", "rows": []}, {"v": 1, "more": 1.5, "rows": []}, {"v": 1, "more": True, "rows": []},
    {"v": 1, "more": None, "rows": []},
    {"v": 1, "rows": ["run-a"]}, {"v": 1, "rows": [None]}, {"v": 1, "rows": [[]]}, {"v": 1, "rows": [7]},
    {"v": 1, "rows": [crow("run-a"), crow("run-a")]},  # the same id twice
    {"v": 1, "rows": [crow("run-a"), "x"]},  # one bad row refuses the whole snapshot
    {"v": 1, "rows": [{"name": "a", "elapsed": 1}]},  # no id
    {"v": 1, "rows": [crow("")]}, {"v": 1, "rows": [crow(7)]}, {"v": 1, "rows": [crow(None)]}, {"v": 1, "rows": [crow(["run-a"])]},
    {"v": 1, "rows": [crow("run-has space")]}, {"v": 1, "rows": [crow("run-a/b")]}, {"v": 1, "rows": [crow("run-a:b")]},
    {"v": 1, "rows": [crow("run-" + "x" * 61)]},  # 65 characters
    {"v": 1, "rows": [crow("run-ñandú")]},
    {"v": 1, "rows": [crow(SESSION)]},  # a census row can never take the id of a session
    {"v": 1, "rows": [crow("task-a")]}, {"v": 1, "rows": [crow("Run-a")]}, {"v": 1, "rows": [crow("xrun-a")]},
    {"v": 1, "rows": [{"id": "run-a", "elapsed": 1}]},  # no name
    {"v": 1, "rows": [crow("run-a", name=7)]}, {"v": 1, "rows": [crow("run-a", name=None)]},
    {"v": 1, "rows": [crow("run-a", name=["a"])]},
    {"v": 1, "rows": [crow("run-a", group=7)]}, {"v": 1, "rows": [crow("run-a", project=["x"])]},
    {"v": 1, "rows": [crow("run-a", what=7)]},
    {"v": 1, "rows": [crow("run-a", state="idle")]}, {"v": 1, "rows": [crow("run-a", state="gone")]},
    {"v": 1, "rows": [crow("run-a", state="")]}, {"v": 1, "rows": [crow("run-a", state=None)]},
    {"v": 1, "rows": [crow("run-a", state=["working"])]},
    {"v": 1, "rows": [{"id": "run-a", "name": "a"}]},  # no elapsed
    {"v": 1, "rows": [crow("run-a", elapsed="12")]}, {"v": 1, "rows": [crow("run-a", elapsed=True)]},
    {"v": 1, "rows": [crow("run-a", elapsed=1.5)]}, {"v": 1, "rows": [crow("run-a", elapsed=None)]},
    {"v": 1, "rows": [crow("run-a", elapsed=[1])]},
    {"v": 1, "rows": [crow("run-a", idle="3")]}, {"v": 1, "rows": [crow("run-a", idle=False)]},
    {"v": 1, "rows": [crow("run-a", idle=2.5)]}, {"v": 1, "rows": [crow("run-a", idle=None)]},
    {"v": 1, "rows": [crow("run-a", session=7)]}, {"v": 1, "rows": [crow("run-a", session="")]},
    {"v": 1, "rows": [crow("run-a", session="has space")]}, {"v": 1, "rows": [crow("run-a", session="s" * 65)]},
    {"v": 1, "rows": [crow("run-a", session=None)]},
]


@pytest.mark.parametrize("body", BAD_SNAPSHOTS, ids=lambda b: repr(b)[:60])
def test_a_census_snapshot_of_the_wrong_type_or_shape_is_a_400_and_changes_nothing(rig, body):
    async def scenario():
        async with hermes(rig, tasks=rig.core.tasks) as server:
            assert (await census(server, snap(crow("run-keep"))))[0] == 200
            status, _, answer = await census(server, body)
            assert status == 400, body
            assert "error" in json.loads(answer)
            assert view_ids(rig) == ["run-keep"]  # the whole snapshot is refused, nothing half-applied

    run(scenario)


def test_numbers_out_of_range_are_clamped_never_a_reason_to_refuse_the_snapshot(rig):
    async def scenario():
        async with hermes(rig, tasks=rig.core.tasks) as server:
            body = snap(crow("run-a", elapsed=10 ** 15, idle=10 ** 15), crow("run-b", elapsed=-5, idle=-1),
                        crow("job-c", elapsed=2678400, idle=2678401), ttl=9999, more=5000)
            status, _, answer = await census(server, body)
            assert status == 200 and json.loads(answer) == {"ok": True, "rows": 3, "ttl": 120}
            by_id = {t["id"]: t for t in rig.view()["tasks"]}
            assert (by_id["run-a"]["elapsed"], by_id["run-a"]["idle"]) == (2678400, 2678400)
            assert (by_id["run-b"]["elapsed"], by_id["run-b"]["idle"]) == (0, 0)
            assert (by_id["job-c"]["elapsed"], by_id["job-c"]["idle"]) == (2678400, 2678400)
            assert rig.view()["census_total"] == 3 + 999  # more is clamped to 999
            for ttl, told in ((1, 5), (0, 5), (-30, 5), (4.9, 5), (5, 5), (60.5, 60.5), (120, 120), (121, 120), (10 ** 30, 120)):
                status, _, answer = await census(server, snap(crow("run-a"), ttl=ttl))
                assert status == 200 and json.loads(answer)["ttl"] == told, ttl
            for more in (-3, -10 ** 9, 0, 7, 999, 1000, 10 ** 20):
                assert (await census(server, snap(crow("run-a"), more=more)))[0] == 200
                assert rig.view()["census_total"] == 1 + min(max(more, 0), 999), more
            assert (await census(server, snap(crow("run-a"), ttl=1)))[0] == 200
            rig.t += 4.9
            assert view_ids(rig) == ["run-a"]
            rig.t += 0.2
            assert view_ids(rig) == []  # a ttl of 1 became 5 and no more than that

    run(scenario)


def test_a_ttl_that_is_not_a_finite_number_is_refused(rig):
    async def scenario():
        async with hermes(rig, tasks=rig.core.tasks) as server:
            for literal in (b"NaN", b"Infinity", b"-Infinity"):
                body = b'{"v":1,"ttl":' + literal + b',"rows":[]}'
                assert (await census(server, body))[0] == 400, literal  # it would never expire

    run(scenario)


def test_the_census_needs_the_token_a_json_body_and_the_right_method(rig, monkeypatch):
    monkeypatch.setattr(buddy_hermes, "AUTH_FAIL_MAX", 100)

    async def scenario():
        async with hermes(rig, tasks=rig.core.tasks) as server:
            for token in ("", "wrong", TOKEN[:-1], TOKEN + "x", None):
                status, _, answer = await census(server, snap(crow("run-a")), token=token)
                assert status == 403 and error_of(answer) == "bad token", token
            for headers in ({"Authorization": TOKEN}, {"Authorization": f"Basic {TOKEN}"}, {"X-Buddy-Token": TOKEN}):
                assert (await census(server, snap(crow("run-a")), token=None, headers=headers))[0] == 403
            assert view_ids(rig) == []
            for ctype in ("application/x-www-form-urlencoded", "text/plain", "multipart/form-data; boundary=x", None):
                headers = {**auth(), **({"Content-Type": ctype} if ctype else {})}
                status, _, _ = await http(server, "POST", "/census", headers=headers, body=json.dumps(snap()).encode())
                assert status == 415, ctype
            assert (await census(server, snap(), headers={"Content-Type": "application/json; charset=utf-8"}))[0] == 200
            for method in ("GET", "PUT", "DELETE", "PATCH"):
                assert (await http(server, method, "/census", headers={**auth(), "Content-Type": "application/json"},
                                   body=b"{}"))[0] == 405, method
            assert (await census(server, snap(), headers={"Origin": "http://evil.example"}))[0] == 403  # a browser, never
            assert (await census(server, snap(), host="evil.example:80"))[0] == 403  # nor a wrong Host
            assert (await http(server, "POST", "/census/", headers={**auth(), "Content-Type": "application/json"}, body=b"{}"))[0] == 404

    run(scenario)


def test_without_a_board_there_is_no_census_route(rig):
    async def scenario():
        async with hermes(rig) as server:  # like /session: no board wired, the route does not exist
            assert (await census(server, snap(crow("run-a"))))[0] == 404
            assert (await census(server, snap(), token="wrong-token-0123456789"))[0] == 403  # but the token comes first

    run(scenario)


def test_a_body_over_the_cap_is_a_413_and_the_largest_honest_snapshot_fits(rig):
    async def scenario():
        async with hermes(rig, tasks=rig.core.tasks) as server:
            huge = snap(crow("run-a"), junk="x" * 5000)
            assert len(json.dumps(huge)) > 4096
            assert (await census(server, huge))[0] == 413
            assert view_ids(rig) == []
            worst = [crow("run-" + chr(ord("a") + n) * 60, name="n" * 40, group="g" * 24, elapsed=2678400, idle=2678400,
                          session=chr(ord("0") + n) * 64, project="p" * 40, what="w" * 40) for n in range(8)]
            body = snap(*worst, seq=2 ** 62, more=999, ttl=120.5)
            assert len(json.dumps(body)) < 4096  # eight rows with every field at its largest still fit the cap
            status, _, answer = await census(server, body)
            assert status == 200 and json.loads(answer)["rows"] == 8
            assert len(rig.view()["tasks"]) == 5 and rig.view()["tasks_total"] == 8 + 999  # the screen draws 5, counts all (and the unlisted)

    run(scenario)


def test_late_snapshots_are_answered_but_ignored(rig):
    async def scenario():
        async with hermes(rig, tasks=rig.core.tasks) as server:
            assert json.loads((await census(server, snap(crow("run-a"), seq=100)))[2]) == {"ok": True, "rows": 1, "ttl": 60}
            for late in (100, 99, 0):
                status, _, answer = await census(server, snap(crow("run-late"), crow("job-late"), seq=late))
                assert status == 200 and json.loads(answer) == {"ok": True, "stale": True, "rows": 2, "ttl": 60}
            assert view_ids(rig) == ["run-a"]
            assert (await census(server, snap(crow("run-b"), seq=101)))[0] == 200 and view_ids(rig) == ["run-b"]
            assert "stale" not in json.loads((await census(server, snap(crow("run-c"))))[2])  # no seq: always taken
            assert (await census(server, snap(crow("run-d"), source="linux", seq=1)))[0] == 200  # each source counts alone
            assert sorted(view_ids(rig)) == ["run-c", "run-d"]
            rig.t += 61  # both expired: an old seq is welcome again
            status, _, answer = await census(server, snap(crow("run-e"), seq=5))
            assert status == 200 and "stale" not in json.loads(answer) and view_ids(rig) == ["run-e"]

    run(scenario)


def test_sources_do_not_wipe_each_other_and_the_default_source_is_mac(rig):
    async def scenario():
        async with hermes(rig, tasks=rig.core.tasks) as server:
            first = {"v": 1, "ttl": 60, "rows": [crow("run-a")]}  # no source: "mac"
            assert (await census(server, first))[0] == 200
            assert (await census(server, snap(crow("run-b"), source="linux")))[0] == 200
            assert sorted(view_ids(rig)) == ["run-a", "run-b"]
            assert (await census(server, {"v": 1, "ttl": 60, "rows": []}))[0] == 200  # replaces "mac" only
            assert view_ids(rig) == ["run-b"]
            assert (await census(server, snap(crow("run-x"), source="mac")))[0] == 200
            assert (await census(server, snap(crow("run-x", elapsed=7), source="linux")))[0] == 200
            ids = view_ids(rig)
            assert len(ids) == len(set(ids)) == 2 and "run-x" in ids  # the same id from two machines: one gets the source as suffix

    run(scenario)


def test_unknown_keys_are_ignored_and_the_row_keeps_only_what_it_knows(rig):
    async def scenario():
        async with hermes(rig, tasks=rig.core.tasks) as server:
            body = snap(crow("run-a", tool="rm -rf /", said="hola", kind="sdk-cli", cwd="/Users/x"), future="yes", seq=1)
            assert (await census(server, body))[0] == 200
            (row,) = rig.view()["tasks"]
            assert (row["tool"], row["what"]) == ("", "") and "said" not in row and "kind" not in row and "cwd" not in row

    run(scenario)


def test_row_text_is_cut_cleaned_and_stays_text(rig):
    hostile = '<img src=x onerror="alert(1)">'

    async def scenario():
        async with hermes(rig, tasks=rig.core.tasks) as server:
            body = snap(crow("run-a", name=hostile + "x" * 100, group="</script><script>1</script>" + "g" * 40,
                             project="p\nq\x07r" + "p" * 80, what="w\nx" * 50),
                        crow("job-b", name="ñandú ✓", group="", what=""))
            assert (await census(server, body))[0] == 200
            a, b = rig.view()["tasks"]
            assert a["title"] == (hostile + "x" * 100)[:40] and a["project"] == a["title"]
            assert a["group"] == ("</script><script>1</script>" + "g" * 40)[:24]
            assert "\n" not in a["what"] and len(a["what"]) == 40
            assert (b["title"], b["group"]) == ("ñandú ✓", "Corridas")  # no group: the default one
            assert rig.hub.current().isascii()  # the stream carries ASCII only, whatever the names were
            assert json.loads(rig.hub.current())["tasks"][1]["title"] == "ñandú ✓"
            props = [crow("run-" + name, name=name, group=name, session=name) for name in ("constructor", "__proto__")]
            assert (await census(server, snap(*props)))[0] == 200  # names of object properties are ordinary names
            assert sorted(t["title"] for t in rig.view()["tasks"]) == ["__proto__", "constructor"]
            assert len(json.loads(rig.hub.current())["tasks"]) == 2

    run(scenario)


def test_census_names_never_reach_a_log(rig, caplog):
    marker = "MARCA-SECRETA-4f9a"
    caplog.set_level(logging.DEBUG)

    async def scenario():
        async with hermes(rig, tasks=rig.core.tasks) as server:
            body = snap(crow("run-" + marker, name=marker, project=marker, what=marker, group=marker, session="s-" + marker),
                        source="m" + marker.lower()[:8], seq=1)
            assert (await census(server, body))[0] == 200
            assert (await census(server, body))[0] == 200  # a late one is not logged with its names either
            for refused in (snap(crow("run-" + marker, name=[marker])), snap(crow("x-" + marker, name=marker)),
                            snap(crow("run-a", name=marker, state=marker)), snap(crow("run-a", name=marker, group=[marker]))):
                status, _, answer = await census(server, refused)
                assert status == 400 and marker not in answer.decode()
            assert (await census(server, snap(), token="wrong-token-" + marker))[0] == 403

    run(scenario)
    assert "hermes listener on" in caplog.text  # the capture works...
    assert marker not in caplog.text and marker.lower() not in caplog.text  # ...and holds no name, folder or source


def test_the_census_has_its_own_request_budget_and_cannot_starve_the_hooks(rig):
    async def scenario():
        async with hermes(rig, tasks=rig.core.tasks) as server:
            for _ in range(buddy_hermes.CENSUS_RATE_MAX):
                assert (await census(server, snap(crow("run-a"))))[0] == 200
            status, _, answer = await census(server, snap(crow("run-a")))
            assert status == 429 and error_of(answer) == "too many requests"
            # the hooks post on every tool call: all of their 300 a minute are still theirs
            for n in range(buddy_hermes.RATE_MAX):
                assert (await report(server, {"id": "s", "project": "p", "tool": f"T{n}"}))[0] == 200, n
            assert (await report(server, {"id": "s", "project": "p"}))[0] == 429
            assert (await census(server, snap(crow("run-a"))))[0] == 429
        async with hermes(rig, tasks=rig.core.tasks) as server:  # and the other way round
            for n in range(buddy_hermes.RATE_MAX):
                assert (await report(server, {"id": "s", "project": "p", "tool": f"T{n}"}))[0] == 200, n
            assert (await report(server, {"id": "s", "project": "p"}))[0] == 429
            assert (await submit(server))[0] == 429  # the shared budget is gone for everything else
            for _ in range(buddy_hermes.CENSUS_RATE_MAX):
                assert (await census(server, snap(crow("run-a"))))[0] == 200
            assert (await census(server, snap(crow("run-a"))))[0] == 429

    assert (buddy_hermes.CENSUS_RATE_MAX, buddy_hermes.RATE_MAX) == (60, 300)
    run(scenario)


def test_a_census_refused_for_shape_still_costs_budget_so_a_flood_of_bad_ones_is_throttled(rig, monkeypatch):
    monkeypatch.setattr(buddy_hermes, "CENSUS_RATE_MAX", 4)

    async def scenario():
        async with hermes(rig, tasks=rig.core.tasks) as server:
            for _ in range(4):
                assert (await census(server, b"not json"))[0] == 400
            assert (await census(server, snap()))[0] == 429
            assert (await report(server, {"id": "s", "project": "p"}))[0] == 200  # the hooks do not notice

    run(scenario)


def test_bad_tokens_on_the_census_feed_the_shared_lockout(rig):
    async def scenario():
        async with hermes(rig, tasks=rig.core.tasks) as server:
            for _ in range(buddy_hermes.AUTH_FAIL_MAX):
                assert (await census(server, snap(), token="wrong"))[0] == 403
            status, _, answer = await census(server, snap())  # the right token, too late
            assert status == 429 and "bad tokens" in error_of(answer)
            status, _, answer = await report(server, {"id": "s", "project": "p"})  # the hooks share the lockout
            assert status == 429 and "bad tokens" in error_of(answer)
            assert (await http(server, path="/healthz"))[0] == 429 and rig.view()["tasks"] == []
        async with hermes(rig, tasks=rig.core.tasks) as server:  # and the other way: bad tokens on /session lock the census
            for _ in range(buddy_hermes.AUTH_FAIL_MAX):
                assert (await report(server, {"id": "s"}, token="wrong"))[0] == 403
            assert (await census(server, snap()))[0] == 429

    run(scenario)


def test_the_screens_port_cannot_be_fed_a_census(rig):
    async def scenario():
        async with screen(rig) as page:
            headers = {"Content-Type": "application/json", "X-Buddy-Token": page.token, **auth()}
            assert (await http(page, "POST", "/census", headers=headers, body=json.dumps(snap(crow("run-a"))).encode()))[0] == 404
            assert rig.view()["tasks"] == []

    run(scenario)
