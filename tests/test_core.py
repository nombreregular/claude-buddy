"""Unit tests for buddy_core (no Bluetooth, no D-Bus)."""
import asyncio
import base64
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from buddy_core import (  # noqa: E402
    Approval,
    ApprovalBroker,
    BuddyCore,
    LineBuffer,
    PackReceiver,
    StateStore,
    encode_line,
    validate_filename,
    PackError,
)


class Harness:
    """A BuddyCore with a fake clock and everything it sends captured."""

    def __init__(self, state_dir, **kw):
        self.t = 1000.0
        self.wall = 1_800_000_000.0
        self.sent = []
        self.events = []
        self.core = BuddyCore(
            state_dir,
            self.sent.append,
            clock=lambda: self.t,
            wall=lambda: self.wall,
            on_event=lambda ev, d: self.events.append((ev, d)),
            **kw,
        )

    def rx(self, obj):
        self.core.feed(encode_line(obj))

    def pop(self):
        out, self.sent[:] = list(self.sent), []
        return out

    def one(self):
        out = self.pop()
        assert len(out) == 1, out
        return out[0]

    def event_names(self):
        return [name for name, _ in self.events]

    def advance(self, seconds):
        self.t += seconds
        self.core.tick()


@pytest.fixture
def h(tmp_path):
    return Harness(tmp_path / "state")


def heartbeat(**kw):
    base = {"total": 1, "running": 0, "waiting": 0, "msg": "idle", "entries": [], "tokens": 0, "tokens_today": 0}
    base.update(kw)
    return base


PROMPT = {"id": "req_1", "tool": "Bash", "hint": "rm -rf /tmp/foo"}


# --- framing ----------------------------------------------------------------------


def test_line_buffer_partial_and_multiple():
    lb = LineBuffer()
    assert lb.feed(b'{"a":1}\n{"b"') == [b'{"a":1}']
    assert lb.feed(b":2}\n") == [b'{"b":2}']
    assert lb.feed(b'{"c":3}\n{"d":4}\n') == [b'{"c":3}', b'{"d":4}']


def test_line_buffer_crlf_and_blank_lines():
    assert LineBuffer().feed(b'{"a":1}\r\n\r\n{"b":2}\n') == [b'{"a":1}', b'{"b":2}']


def test_utf8_character_split_across_writes_survives(h):
    raw = encode_line({"cmd": "owner", "name": "Ñandú"})
    # encode_line escapes non-ASCII; also try raw UTF-8 split in the middle of the char
    line = b'{"cmd":"owner","name":"\xc3\x91and\xc3\xba"}\n'
    cut = line.index(b"\xc3") + 1
    h.core.feed(line[:cut])
    assert h.sent == []
    h.core.feed(line[cut:])
    assert h.one()["ok"] is True
    assert h.core.state.owner == "Ñandú"
    assert raw.endswith(b"\n")


def test_oversize_line_is_dropped_and_stream_recovers():
    lb = LineBuffer(max_bytes=10)
    assert lb.feed(b"A" * 15) == []  # overflow while still waiting for the terminator
    assert lb.feed(b"BBB\nok\n") == [b"ok"]  # tail of the long line skipped, next line fine
    assert lb.dropped == 1
    assert lb.feed(b"A" * 15 + b"\nfine\n") == [b"fine"]  # overflow inside a complete line
    assert lb.dropped == 2


def test_garbage_lines_are_ignored(h):
    h.core.feed(b"hello\n\xff\xfe\n{not json}\n[1,2]\n")
    assert h.sent == []
    assert not h.core.connected


# --- heartbeat and prompts --------------------------------------------------------


def test_heartbeat_updates_snapshot_and_connects(h):
    assert h.core.persona() == "sleep"
    h.rx(heartbeat(total=3, running=1, waiting=0, msg="working", entries=["a", "b"], tokens_today=31200))
    s = h.core.snapshot
    assert (s.total, s.running, s.waiting, s.msg, s.entries, s.tokens_today) == (3, 1, 0, "working", ["a", "b"], 31200)
    assert h.core.connected
    assert "link_up" in h.event_names()
    assert h.core.persona() == "busy"


def test_empty_object_is_not_a_heartbeat(h):
    h.rx({})
    h.rx({"hello": "world"})
    assert not h.core.connected


def test_prompt_goes_to_broker_and_once_replies_with_permission(h):
    h.rx(heartbeat(waiting=1, prompt=PROMPT))
    approval = h.core.broker.current()
    assert (approval.id, approval.tool, approval.hint, approval.source) == ("req_1", "Bash", "rm -rf /tmp/foo", "claude")
    assert h.core.persona() == "attention"
    h.t += 2.0
    assert h.core.broker.decide(approval.key, "once") is True
    assert h.one() == {"cmd": "permission", "id": "req_1", "decision": "once"}
    st = h.core.state
    assert (st.approvals, st.denials, st.velocity) == (1, 0, [2])
    assert h.core.persona() == "heart"  # approved in under 5s
    assert h.core.broker.current() is None


def test_slow_approval_is_not_a_heart_and_deny_counts(h):
    h.rx(heartbeat(prompt=PROMPT))
    h.t += 9
    h.core.broker.decide("claude:req_1", "once")
    assert h.core.persona() != "heart"
    h.rx(heartbeat(prompt={"id": "req_2", "tool": "Edit", "hint": "x"}))
    h.core.broker.decide("claude:req_2", "deny")
    assert h.pop()[-1] == {"cmd": "permission", "id": "req_2", "decision": "deny"}
    assert (h.core.state.approvals, h.core.state.denials) == (1, 1)


def test_keepalive_with_same_prompt_does_not_resurrect_an_answered_prompt(h):
    h.rx(heartbeat(prompt=PROMPT))
    h.core.broker.decide("claude:req_1", "once")
    h.rx(heartbeat(prompt=PROMPT))
    h.rx(heartbeat(prompt=PROMPT))
    assert h.core.broker.current() is None


def test_prompt_withdrawn_when_snapshot_has_none_and_replaced_by_new_id(h):
    seen = []
    h.core.broker.subscribe(lambda ev, a: seen.append((ev, a.id)))
    h.rx(heartbeat(prompt=PROMPT))
    h.rx(heartbeat(prompt={"id": "req_2", "tool": "Edit", "hint": ""}))
    assert [a.id for a in h.core.broker.pending()] == ["req_2"]
    h.rx(heartbeat())
    assert h.core.broker.current() is None
    assert seen == [("new", "req_1"), ("withdrawn", "req_1"), ("new", "req_2"), ("withdrawn", "req_2")]


def test_turn_event_does_not_clear_a_pending_prompt(h):
    # The official ESP32 firmware treats every JSON line as a snapshot, so a turn
    # event wipes the prompt. We must not.
    h.rx(heartbeat(prompt=PROMPT))
    h.rx({"evt": "turn", "role": "assistant", "content": [{"type": "text", "text": "hi"}]})
    assert h.core.broker.current() is not None
    assert "evt" in h.event_names()


def test_decision_for_a_stale_prompt_is_refused(h):
    h.rx(heartbeat(prompt=PROMPT))
    approval = h.core.broker.current()
    h.rx(heartbeat())  # prompt vanished (answered elsewhere)
    assert h.core.broker.decide(approval.key, "once") is False
    assert h.sent == []


# --- liveness ---------------------------------------------------------------------


def test_watchdog_kills_link_after_30s_and_drops_prompt(h):
    h.rx(heartbeat(total=2, running=1, prompt=PROMPT))
    h.advance(29)
    assert h.core.connected and h.core.broker.current() is not None
    h.advance(2)
    assert not h.core.connected
    assert h.core.broker.current() is None
    assert h.core.snapshot.total == 0
    assert h.core.persona() == "sleep"
    assert "link_down" in h.event_names()
    h.rx(heartbeat())
    assert h.core.connected


def test_status_polls_keep_the_link_up_but_a_stale_prompt_expires(h):
    h.rx(heartbeat(prompt=PROMPT))
    for _ in range(8):
        h.advance(5)
        h.rx({"cmd": "status"})
    assert h.core.connected
    assert h.core.broker.current() is None  # no heartbeat for 40s


def test_link_down_from_transport_resets_everything(h):
    h.rx(heartbeat(prompt=PROMPT))
    h.core.feed(b'{"cmd":"ow')  # half a line in flight
    h.core.link_down()
    assert not h.core.connected and h.core.broker.current() is None
    h.core.feed(b'ner","name":"X"}\n')  # the rest must not be glued to the old half
    assert h.sent == []


def test_time_sync(h):
    h.rx({"time": [1_800_000_100, -21600]})
    assert h.core.tz_offset == -21600
    assert h.core.local_time() == pytest.approx(1_800_000_100 - 21600)
    h.wall += 10
    assert h.core.local_time() == pytest.approx(1_800_000_110 - 21600)
    h.rx({"time": "nope"})
    assert h.core.tz_offset == -21600


# --- tokens, level, persona -------------------------------------------------------


def test_tokens_latch_then_delta_then_level_up(h):
    h.rx(heartbeat(tokens=900_000))  # first sight: latch only, no credit
    assert h.core.state.tokens == 0
    h.rx(heartbeat(tokens=930_000))
    assert (h.core.state.tokens, h.core.state.level) == (30_000, 0)
    h.rx(heartbeat(tokens=960_000))
    assert h.core.state.level == 1
    assert ("level_up", {"level": 1}) in h.events
    assert h.core.persona() == "celebrate"
    h.advance(4)
    h.rx(heartbeat(tokens=960_000))
    assert h.core.persona() == "idle"
    h.rx(heartbeat(tokens=100))  # desktop restarted: resync, no negative credit
    h.rx(heartbeat(tokens=600))
    assert h.core.state.tokens == 60_500


def test_persona_priority(h):
    assert h.core.persona() == "sleep"
    h.rx(heartbeat())
    assert h.core.persona() == "idle"
    h.rx(heartbeat(running=2))
    assert h.core.persona() == "busy"
    h.rx(heartbeat(running=2, completed=True))
    assert h.core.persona() == "celebrate"
    h.rx(heartbeat(running=2, completed=True, waiting=1))
    assert h.core.persona() == "attention"


def test_external_approval_also_means_attention(h):
    h.rx(heartbeat())
    h.core.broker.submit(Approval(id="x", source="hermes", tool="terminal"), lambda d: None)
    assert h.core.persona() == "attention"


# --- commands ---------------------------------------------------------------------


def test_status_ack_has_the_shape_the_desktop_expects(h):
    h.rx({"cmd": "status"})
    ack = h.one()
    assert (ack["ack"], ack["ok"], ack["n"]) == ("status", True, 0)
    data = ack["data"]
    assert set(data["stats"]) == {"appr", "deny", "vel", "nap", "lvl"}
    assert data["name"] == "Buddy" and data["sec"] is False
    assert isinstance(data["sys"]["up"], int)
    json.dumps(ack)


def test_name_owner_species_persist_across_restarts(tmp_path):
    h1 = Harness(tmp_path / "s")
    h1.rx({"cmd": "name", "name": "Clawd"})
    h1.rx({"cmd": "owner", "name": "Ana"})
    h1.rx({"cmd": "species", "idx": 7})
    assert [a["ack"] for a in h1.pop()] == ["name", "owner", "species"]
    h2 = Harness(tmp_path / "s")
    assert (h2.core.state.name, h2.core.state.owner, h2.core.state.species) == ("Clawd", "Ana", 7)
    h2.rx({"cmd": "status"})
    assert h2.one()["data"]["name"] == "Clawd"


def test_bad_name_payload_is_nacked(h):
    h.rx({"cmd": "name"})
    h.rx({"cmd": "owner", "name": 5})
    acks = h.pop()
    assert [(a["ack"], a["ok"]) for a in acks] == [("name", False), ("owner", False)]


def test_unknown_command_gets_a_negative_ack_and_permission_is_ignored(h):
    h.rx({"cmd": "dance"})
    ack = h.one()
    assert ack["ack"] == "dance" and ack["ok"] is False
    h.rx({"cmd": "permission", "id": "x", "decision": "once"})
    assert h.sent == []


def test_unpair_sync_hook(tmp_path):
    calls = []
    h = Harness(tmp_path / "s", unpair=lambda: calls.append(1))
    h.rx({"cmd": "unpair"})
    assert calls == [1] and h.one() == {"ack": "unpair", "ok": True, "n": 0}


def test_unpair_without_hook_still_acks(h):
    h.rx({"cmd": "unpair"})
    assert h.one()["ok"] is True


def test_unpair_async_hook_acks_after_it_finishes(tmp_path):
    async def scenario():
        gate = asyncio.Event()

        async def slow_unpair():
            await gate.wait()

        h = Harness(tmp_path / "s", unpair=slow_unpair)
        h.rx({"cmd": "unpair"})
        await asyncio.sleep(0.01)
        assert h.sent == []  # not acked yet
        gate.set()
        await asyncio.sleep(0.01)
        assert h.one() == {"ack": "unpair", "ok": True, "n": 0}

        async def broken():
            raise RuntimeError("bluez says no")

        h2 = Harness(tmp_path / "s2", unpair=broken)
        h2.rx({"cmd": "unpair"})
        await asyncio.sleep(0.01)
        ack = h2.one()
        assert ack["ok"] is False and "error" in ack

    asyncio.run(scenario())


def test_secure_flag_comes_from_the_hook(tmp_path):
    flag = {"v": True}
    h = Harness(tmp_path / "s", secure=lambda: flag["v"])
    h.rx({"cmd": "status"})
    assert h.one()["data"]["sec"] is True


# --- folder push ------------------------------------------------------------------


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


MANIFEST = json.dumps(
    {"name": "bufo", "colors": {"body": "#6B8E23"}, "states": {"sleep": "sleep.gif", "idle": ["idle_0.gif", "idle_1.gif"]}}
).encode()
GIF = b"GIF89a" + bytes(range(256)) * 3


def push_file(h, name, data, chunk=100):
    h.rx({"cmd": "file", "path": name, "size": len(data)})
    assert h.one()["ok"] is True
    written = 0
    for i in range(0, len(data), chunk):
        part = data[i : i + chunk]
        h.rx({"cmd": "chunk", "d": b64(part)})
        written += len(part)
        assert h.one() == {"ack": "chunk", "ok": True, "n": written}
    h.rx({"cmd": "file_end"})
    assert h.one() == {"ack": "file_end", "ok": True, "n": len(data)}


def push_pack(h, files, name="bufo"):
    total = sum(len(d) for d in files.values())
    h.rx({"cmd": "char_begin", "name": name, "total": total})
    assert h.one() == {"ack": "char_begin", "ok": True, "n": 0}
    for fname, data in files.items():
        push_file(h, fname, data)
    h.rx({"cmd": "char_end"})


GOOD_PACK = {"manifest.json": MANIFEST, "sleep.gif": GIF, "idle_0.gif": GIF[:300], "idle_1.gif": GIF[:200]}


def test_pack_roundtrip_installs_atomically_and_becomes_active(h):
    push_pack(h, GOOD_PACK)
    assert h.one() == {"ack": "char_end", "ok": True, "n": 0}
    root = h.core.state_dir / "characters" / "bufo"
    assert {p.name for p in root.iterdir()} == set(GOOD_PACK)
    assert (root / "sleep.gif").read_bytes() == GIF
    assert h.core.state.character == "bufo"
    assert not [p for p in (h.core.state_dir / "characters").iterdir() if p.name.startswith(".")]
    assert "character" in h.event_names()


def test_pack_replaces_an_existing_one_completely(h):
    push_pack(h, GOOD_PACK)
    h.pop()
    smaller = {"manifest.json": json.dumps({"name": "bufo", "states": {"sleep": "z.gif"}}).encode(), "z.gif": b"GIF89a"}
    push_pack(h, smaller)
    assert h.one()["ok"] is True
    root = h.core.state_dir / "characters" / "bufo"
    assert {p.name for p in root.iterdir()} == {"manifest.json", "z.gif"}


@pytest.mark.parametrize(
    "bad",
    ["../evil", "..", "/etc/passwd", "a/b.gif", ".hidden", "a\\b", "", "x" * 101, "bell\x07"],
)
def test_pack_rejects_hostile_file_paths(h, bad, tmp_path):
    h.rx({"cmd": "char_begin", "name": "x", "total": 10})
    h.pop()
    h.rx({"cmd": "file", "path": bad, "size": 10})
    ack = h.one()
    assert ack["ok"] is False and ack["ack"] == "file"
    assert not any(tmp_path.rglob("evil")) and not (tmp_path / "evil").exists()
    # the push was aborted: follow-up commands fail cleanly and nothing is left behind
    h.rx({"cmd": "chunk", "d": b64(b"hi")})
    assert h.one()["ok"] is False
    assert not [p for p in (h.core.state_dir / "characters").iterdir()]


def test_validate_filename_accepts_normal_names():
    for name in ["manifest.json", "idle_0.gif", "Some Pet.gif", "ñandú.gif"]:
        assert validate_filename(name) == name
    with pytest.raises(PackError):
        validate_filename(None)


def test_pack_rejects_bad_base64_and_aborts(h):
    h.rx({"cmd": "char_begin", "name": "x", "total": 10})
    h.rx({"cmd": "file", "path": "a.gif", "size": 10})
    h.pop()
    h.rx({"cmd": "chunk", "d": "***not base64***"})
    assert h.one()["ok"] is False
    assert not h.core.packs.active


def test_pack_file_size_mismatch_fails_file_end(h):
    h.rx({"cmd": "char_begin", "name": "x", "total": 100})
    h.rx({"cmd": "file", "path": "a.gif", "size": 100})
    h.rx({"cmd": "chunk", "d": b64(b"only ten..")})
    h.pop()
    h.rx({"cmd": "file_end"})
    ack = h.one()
    assert ack["ok"] is False and "mismatch" in ack["error"]


def test_pack_data_beyond_declared_size_is_refused(h):
    h.rx({"cmd": "char_begin", "name": "x", "total": 10})
    h.rx({"cmd": "file", "path": "a.gif", "size": 4})
    h.pop()
    h.rx({"cmd": "chunk", "d": b64(b"12345")})
    assert h.one()["ok"] is False


def test_pack_too_big_is_refused_up_front_and_leaves_current_pack_alone(h):
    push_pack(h, GOOD_PACK)
    h.pop()
    h.rx({"cmd": "char_begin", "name": "huge", "total": 5 * 1024 * 1024})
    ack = h.one()
    assert ack["ok"] is False and "big" in ack["error"]
    assert (h.core.state_dir / "characters" / "bufo" / "sleep.gif").exists()


def test_pack_total_cap_is_enforced_even_if_total_was_understated(tmp_path):
    h = Harness(tmp_path / "s")
    h.core.packs.max_total = 1000
    h.rx({"cmd": "char_begin", "name": "x", "total": 10})
    h.rx({"cmd": "file", "path": "a.bin", "size": 0})
    h.pop()
    for _ in range(3):
        h.rx({"cmd": "chunk", "d": b64(b"z" * 400)})
    acks = h.pop()
    assert [a["ok"] for a in acks] == [True, True, False]


def test_pack_with_missing_referenced_file_is_not_installed(h):
    push_pack(h, GOOD_PACK)
    h.pop()
    broken = {"manifest.json": MANIFEST, "sleep.gif": GIF}  # idle_*.gif missing
    push_pack(h, broken, name="bufo")
    ack = h.one()
    assert ack["ok"] is False and "missing file" in ack["error"]
    root = h.core.state_dir / "characters"
    assert (root / "bufo" / "idle_1.gif").exists()  # the old one survived
    assert not [p for p in root.iterdir() if p.name.startswith(".")]


def test_pack_with_invalid_manifest_is_rejected(h):
    push_pack(h, {"manifest.json": b"{nope"})
    assert h.one()["ok"] is False


def test_pack_without_manifest_is_accepted_but_not_made_active(h):
    push_pack(h, {"blob.bin": b"abc"}, name="stuff")
    assert h.one()["ok"] is True
    assert (h.core.state_dir / "characters" / "stuff" / "blob.bin").exists()
    assert h.core.state.character == ""


def test_pack_commands_without_char_begin_are_nacked(h):
    for cmd in ({"cmd": "file", "path": "a", "size": 1}, {"cmd": "chunk", "d": "AA=="}, {"cmd": "file_end"}, {"cmd": "char_end"}):
        h.rx(cmd)
    acks = h.pop()
    assert [a["ok"] for a in acks] == [False] * 4


def test_new_char_begin_restarts_a_stale_push(h):
    h.rx({"cmd": "char_begin", "name": "a", "total": 10})
    h.rx({"cmd": "file", "path": "x.bin", "size": 3})
    h.rx({"cmd": "chunk", "d": b64(b"abc")})
    h.pop()
    h.rx({"cmd": "char_begin", "name": "b", "total": 10})
    assert h.one()["ok"] is True
    assert len([p for p in (h.core.state_dir / "characters").iterdir()]) == 1


def test_idle_push_times_out_and_cleans_up(h):
    h.rx({"cmd": "char_begin", "name": "a", "total": 10})
    h.pop()
    h.advance(10)
    h.rx({"cmd": "status"})  # the link is alive, only the push stalled
    h.pop()
    h.advance(11)
    assert h.core.connected
    assert not h.core.packs.active
    assert "push_failed" in h.event_names()
    assert not [p for p in (h.core.state_dir / "characters").iterdir()]


def test_disconnect_mid_push_cleans_up(h):
    h.rx({"cmd": "char_begin", "name": "a", "total": 10})
    h.core.link_down()
    assert not h.core.packs.active
    assert not [p for p in (h.core.state_dir / "characters").iterdir()]


def test_stale_temp_dirs_are_removed_on_startup(tmp_path):
    root = tmp_path / "s" / "characters"
    (root / ".incoming-dead").mkdir(parents=True)
    (root / ".old-dead").mkdir()
    (root / "keep").mkdir()
    Harness(tmp_path / "s")
    assert sorted(p.name for p in root.iterdir()) == ["keep"]


def test_pack_receiver_directory_names_are_sanitised(tmp_path):
    pr = PackReceiver(tmp_path / "chars")
    pr.begin("../../etc/passwd", 0)
    dest, manifest = pr.finish()
    assert dest.parent == tmp_path / "chars" and ".." not in dest.name and manifest is None


# --- broker and storage ------------------------------------------------------------


def test_broker_validation_and_failure_paths():
    broker = ApprovalBroker()
    with pytest.raises(ValueError):
        broker.decide("x", "maybe")
    assert broker.decide("nope", "once") is False

    def broken(decision):
        raise OSError("link down")

    broker.submit(Approval(id="a", source="s"), broken)
    assert broker.decide("s:a", "once") is False
    assert broker.current().id == "a"  # still pending: the user can retry

    got = []
    broker.submit(Approval(id="b", source="s"), got.append)
    assert broker.decide("s:b", "deny") is True and got == ["deny"]
    assert broker.withdraw("s", "a") is True and broker.withdraw("s", "a") is False


def test_always_is_only_for_a_request_that_offers_it():
    broker = ApprovalBroker()
    got = []
    broker.submit(Approval(id="a", source="claude"), got.append)
    with pytest.raises(ValueError):
        broker.decide("claude:a", "always")  # Claude's own prompts have no such answer
    assert got == [] and broker.current().id == "a"
    broker.submit(Approval(id="b", source="cli", always="Bash(npm run *) en este proyecto"), got.append)
    assert broker.decide("cli:b", "always") is True and got == ["always"]
    assert broker.decide("cli:b", "always") is False  # answered once


def test_broker_listener_errors_do_not_break_delivery():
    broker = ApprovalBroker()
    broker.subscribe(lambda ev, a: 1 / 0)
    got = []
    broker.submit(Approval(id="a"), got.append)
    assert broker.decide("claude:a", "once") is True and got == ["once"]


def test_corrupt_state_file_is_set_aside(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{this is not json")
    store = StateStore(path)
    assert store.data.name == "Buddy"
    assert (tmp_path / "state.json.bad").exists()


def test_state_store_ignores_wrong_types_and_roundtrips(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"name": 5, "approvals": "many", "owner": "Ana", "velocity": [1, 2, "x", 3]}))
    store = StateStore(path)
    assert (store.data.name, store.data.approvals, store.data.owner, store.data.velocity) == ("Buddy", 0, "Ana", [1, 2, 3])
    store.data.denials = 4
    store.save()
    assert StateStore(path).data.denials == 4


def test_state_flush_is_rate_limited_for_lazy_updates(tmp_path):
    t = {"now": 1000.0}
    store = StateStore(tmp_path / "state.json", clock=lambda: t["now"])
    store.data.tokens = 5
    store.touch()
    store.flush_if_due()  # first write is allowed...
    assert (tmp_path / "state.json").exists()
    store.data.tokens = 6
    store.touch()
    t["now"] += 10
    store.flush_if_due()
    assert json.loads((tmp_path / "state.json").read_text())["tokens"] == 5  # ...but not again within 60s
    t["now"] += 60
    store.flush_if_due()
    assert json.loads((tmp_path / "state.json").read_text())["tokens"] == 6


def test_encode_line_is_ascii_single_line():
    line = encode_line({"ack": "name", "name": "Ñandú\n✓"})
    assert line.endswith(b"\n") and line.count(b"\n") == 1
    assert line.decode("ascii")
    assert json.loads(line)["name"] == "Ñandú\n✓"


# --- the task board: what each session on the Mac says it is doing -----------------------------


def test_task_board_lists_working_sessions_first_and_most_recent_first():
    from buddy_core import TaskBoard

    t = [1000.0]
    board = TaskBoard(clock=lambda: t[0])
    board.update("s1", project="relay", tool="Bash", what="npm test")
    t[0] += 5
    board.update("s2", project="claude-buddy", tool="Edit", what="~/x.py")
    t[0] += 5
    board.update("s1", state="idle")
    tasks = board.tasks()
    assert [(x.id, x.state) for x in tasks] == [("s2", "working"), ("s1", "idle")]
    assert tasks[1].tool == "Bash" and tasks[1].what == "npm test"  # "finished" keeps saying what it was doing
    assert board.age(tasks[1]) == 0 and board.age(tasks[0]) == 5


def test_task_board_forgets_quiet_sessions_and_cleans_text():
    from buddy_core import MAX_TASK_WHAT_CHARS, TASK_IDLE_TTL_S, TASK_TTL_S, TaskBoard

    t = [0.0]
    board = TaskBoard(clock=lambda: t[0])
    board.update("w", project="p", tool="Bash", what="a\nb\x07c" + "x" * 500)
    board.update("i", project="p", state="idle")
    (w, i) = board.tasks()
    assert w.what.startswith("a b c") and len(w.what) == MAX_TASK_WHAT_CHARS
    t[0] = TASK_TTL_S + 1
    assert [x.id for x in board.tasks()] == ["i"]  # a thread that waits for Ana outlives one that went silent mid-turn
    t[0] = TASK_IDLE_TTL_S + 1
    assert board.tasks() == []
    board.update("g", project="p")
    assert board.update("g", state="gone") is None and board.tasks() == []
    with pytest.raises(ValueError):
        board.update("x", state="dancing")


def test_task_board_thread_lifetimes():
    """A working thread that falls silent is gone after 10 minutes; one that finished and waits for Ana stays 30."""
    from buddy_core import TASK_IDLE_TTL_S, TASK_TTL_S, TaskBoard

    assert (TASK_TTL_S, TASK_IDLE_TTL_S) == (600.0, 1800.0)
    t = [0.0]
    board = TaskBoard(clock=lambda: t[0])
    board.update("work", project="a", tool="Bash", what="npm test")
    board.update("wait", project="b", state="idle")
    t[0] = 599
    assert {x.id for x in board.tasks()} == {"work", "wait"}
    t[0] = 601
    assert {x.id for x in board.tasks()} == {"wait"}
    t[0] = 1799
    assert {x.id for x in board.tasks()} == {"wait"}
    board.update("wait", state="working")  # he answered: it works again, the clock restarts
    t[0] = 1799 + 599
    assert {x.id for x in board.tasks()} == {"wait"}
    t[0] = 1799 + 601
    assert board.tasks() == []


def test_task_board_keeps_at_most_max_tasks():
    from buddy_core import MAX_TASKS, TaskBoard

    t = [0.0]
    board = TaskBoard(clock=lambda: t[0])
    for n in range(MAX_TASKS + 3):
        t[0] += 1
        board.update(f"s{n}", project="p")
    ids = [x.id for x in board.tasks()]
    assert len(ids) == MAX_TASKS and ids[0] == f"s{MAX_TASKS + 2}" and "s0" not in ids


def test_task_board_still_keeps_at_most_max_tasks_with_threads():
    from buddy_core import MAX_TASKS, TaskBoard

    t = [0.0]
    board = TaskBoard(clock=lambda: t[0])
    for n in range(MAX_TASKS + 3):
        t[0] += 1
        board.update(f"s{n}", project="p", title=f"hilo {n}", group="Buddy", parent="s0" if n else "")
    kept = board.tasks()
    assert len(kept) == MAX_TASKS and "s0" not in {x.id for x in kept}
    assert all(x.title and x.group == "Buddy" for x in kept)  # the oldest left, the rest kept their thread fields


# --- a thread: title, group and parent come from Claude desktop and follow the rule `said` follows ------------


def test_task_board_title_group_parent_follow_the_said_rule():
    from buddy_core import TaskBoard

    t = [0.0]
    board = TaskBoard(clock=lambda: t[0])
    first = board.update("child", project="claude-buddy", tool="Edit", what="~/x.py", title="Hilos en la pantalla",
                         group="Buddy", parent="mother")
    assert (first.title, first.group, first.parent) == ("Hilos en la pantalla", "Buddy", "mother")
    t[0] += 3
    kept = board.update("child", tool="Bash", what="pytest")  # a report that leaves them out keeps what the board has
    assert (kept.title, kept.group, kept.parent) == ("Hilos en la pantalla", "Buddy", "mother")
    idle = board.update("child", state="idle")  # and so does the report that the turn ended
    assert idle.state == "idle" and (idle.title, idle.group, idle.parent) == ("Hilos en la pantalla", "Buddy", "mother")
    assert (idle.tool, idle.what) == ("Bash", "pytest")
    renamed = board.update("child", title="Otro nombre", group="Acme Studio")
    assert (renamed.title, renamed.group, renamed.parent) == ("Otro nombre", "Acme Studio", "mother")
    cleared = board.update("child", title="", group="", parent="")  # "" clears, one by one
    assert (cleared.title, cleared.group, cleared.parent) == ("", "", "")
    board.update("child", title="Vuelve")
    assert board.update("child", group="", parent="mother").title == "Vuelve"
    assert board.update("child", state="gone") is None and board.tasks() == []  # the row leaves with its fields
    fresh = board.update("child", project="claude-buddy")
    assert (fresh.title, fresh.group, fresh.parent, fresh.said) == ("", "", "", "")


def test_task_board_thread_fields_are_one_line_and_cut():
    from buddy_core import MAX_TASK_GROUP_CHARS, MAX_TASK_PARENT_CHARS, MAX_TASK_TITLE_CHARS, TaskBoard

    assert (MAX_TASK_TITLE_CHARS, MAX_TASK_GROUP_CHARS, MAX_TASK_PARENT_CHARS) == (60, 40, 64)
    board = TaskBoard(clock=lambda: 0.0)
    task = board.update("s", project="p", title="una\nlinea\r\ncon\ttabs\x07\u202eY\u2028fin " + "x" * 200,
                        group="g" * 100, parent="p" * 100)
    assert "\n" not in task.title and "\r" not in task.title and "\u202e" not in task.title and "\x07" not in task.title
    assert task.title.startswith("una linea  con tabs") and len(task.title) == MAX_TASK_TITLE_CHARS
    assert task.group == "g" * MAX_TASK_GROUP_CHARS
    assert task.parent == "p" * MAX_TASK_PARENT_CHARS
    spaced = board.update("s", title="   ", group="  \n ")
    assert (spaced.title, spaced.group) == ("", "")  # only white space is nothing


def test_task_board_never_makes_a_thread_its_own_parent():
    from buddy_core import TaskBoard

    board = TaskBoard(clock=lambda: 0.0)
    assert board.update("loop", project="p", parent="loop").parent == ""
    board.update("kid", project="p", parent="mother")
    assert board.update("kid", parent="kid").parent == ""  # a hook that names the thread itself clears the old parent


def test_task_board_ids_that_are_names_of_object_properties_are_ordinary_threads():
    """Ids are only matched against a pattern: 'constructor' and '__proto__' are valid ones, and so are groups and parents."""
    from buddy_core import TaskBoard

    t = [0.0]
    board = TaskBoard(clock=lambda: t[0])
    for n, name in enumerate(("constructor", "__proto__", "toString", "hasOwnProperty")):
        t[0] += 1
        board.update(name, project=name, title=name, group=name, parent="constructor", said=name)
    got = board.tasks()
    assert sorted(x.id for x in got) == ["__proto__", "constructor", "hasOwnProperty", "toString"]
    assert all(x.group == x.id and x.title == x.id and x.said == x.id for x in got)
    assert {x.id: x.parent for x in got} == {"constructor": "", "__proto__": "constructor", "toString": "constructor",
                                               "hasOwnProperty": "constructor"}


# --- what a session said: plain text with its paragraph breaks ------------------------------------------------


def test_said_absent_keeps_empty_clears_and_text_replaces():
    from buddy_core import TaskBoard

    t = [0.0]
    board = TaskBoard(clock=lambda: t[0])
    assert board.update("s", project="p", tool="Bash", what="ls").said == ""
    assert board.update("s", state="idle", said="Terminé el login.").said == "Terminé el login."
    assert board.update("s", tool="Edit", what="x.py").said == "Terminé el login."  # the next turn starts: still there
    again = board.update("s", state="idle", said="Falta el test.")
    assert (again.said, again.tool, again.what) == ("Falta el test.", "Edit", "x.py")  # idle keeps tool and what
    assert board.update("s", state="idle", said="").said == ""  # a turn with no words clears it
    assert board.update("s", state="idle").said == ""
    board.update("s", state="idle", said="otra vez")
    assert board.update("s", state="gone") is None
    assert board.update("s", project="p").said == ""  # it left with the row


def test_said_keeps_paragraph_breaks_but_drops_control_and_bidi_characters_and_is_cut_at_the_limit():
    from buddy_core import MAX_TASK_SAID_CHARS, TaskBoard

    assert MAX_TASK_SAID_CHARS == 300
    board = TaskBoard(clock=lambda: 0.0)
    task = board.update("s", project="proyecto\nuno", tool="Bash\x07", what="a\nb",
                        said="uno\r\ndos\rtres\n\n\n\n\ncuatro\t\x07cinco\u202egnirts\u2028seis   \n   \n   \nsiete")
    assert task.said == "uno\ndos\ntres\n\ncuatro  cinco gnirts seis\n\nsiete"
    assert "\u202e" not in task.said and "\u2028" not in task.said and "\x07" not in task.said
    assert (task.project, task.tool, task.what) == ("proyecto uno", "Bash", "a b")  # the other fields stay one line
    long = board.update("s", said="x" * 500)
    assert long.said == "x" * MAX_TASK_SAID_CHARS
    cut_at_a_break = board.update("s", said="y" * 299 + "\n\nfin")
    assert cut_at_a_break.said == "y" * 299  # a cut never leaves a dangling break
    assert board.update("s", said="\n\n  \n").said == ""  # white space alone is nothing to say
    assert board.update("s", said=["no"]).said == ""  # the wire refuses it (400); the board does not trust it either


def test_clean_keeps_its_one_line_behaviour_and_keep_lines_is_opt_in():
    from buddy_core import _clean

    assert _clean("a\nb\r\nc\t\x07d  ", 50) == "a b  c  d"
    assert _clean("  x" * 30, 7) == "x  x  x"
    assert _clean(None, 5) == "" and _clean(7, 5) == ""
    assert _clean("a\nb", 50, keep_lines=True) == "a\nb"
    assert _clean("a\n\n\nb", 50, keep_lines=True) == "a\n\nb"
    assert _clean(None, 5, keep_lines=True) == ""


def test_said_is_ignored_when_the_daemon_has_it_off(monkeypatch):
    import buddy_core
    from buddy_core import TaskBoard

    board = TaskBoard(clock=lambda: 0.0)
    assert board.update("s", project="p", said="visible").said == "visible"
    monkeypatch.setattr(buddy_core, "SAID_ENABLED", False)
    off = board.update("s", state="idle", said="no se guarda", title="El titulo sigue", group="Buddy")
    assert off.said == "visible"  # nothing new is kept (what was there before the switch is not touched)
    fresh = board.update("other", project="p", said="tampoco")
    assert fresh.said == "" and board.update("other", said="ni así").said == ""
    assert off.title == "El titulo sigue" and off.group == "Buddy"  # the switch is about what sessions said only


@pytest.mark.parametrize("value,expected", [("", True), ("on", True), ("yes", True), ("off", False), ("OFF", False),
                                            (" 0 ", False), ("no", False)])
def test_the_daemon_said_switch_reads_the_environment_like_the_hook_does(monkeypatch, value, expected):
    import buddy_core

    monkeypatch.setenv("CLAUDE_BUDDY_SAID", value)
    assert buddy_core._switch_on("CLAUDE_BUDDY_SAID") is expected
    monkeypatch.delenv("CLAUDE_BUDDY_SAID")
    assert buddy_core._switch_on("CLAUDE_BUDDY_SAID") is True


# --- the census: runs and jobs that no hook speaks for --------------------------------------------


SESSION = "3f2a9c1e-7b44-4d0a-9e51-0c8d6a2b1f77"


def crow(row_id, **kw):
    """A census row as a source posts it (names are fixtures, not anyone's real folders)."""
    base = {"id": row_id, "name": row_id.split("-", 1)[1], "group": "Corridas", "state": "working", "elapsed": 100}
    base.update(kw)
    return base


def census_board():
    from buddy_core import TaskBoard

    t = [1000.0]
    return t, TaskBoard(clock=lambda: t[0])


def ids(board):
    return [x.id for x in board.tasks()]


def test_census_snapshot_replaces_the_previous_one_of_its_source_whole():
    t, board = census_board()
    assert board.replace_census("mac", [crow("run-a"), crow("run-b"), crow("job-c")], 60) is True
    assert sorted(ids(board)) == ["job-c", "run-a", "run-b"]
    t[0] += 15
    board.replace_census("mac", [crow("run-b"), crow("job-d")], 60)
    assert sorted(ids(board)) == ["job-d", "run-b"]  # what is not in the new snapshot is gone at once, not at the ttl
    board.replace_census("mac", [], 60)
    assert board.tasks() == []  # an empty snapshot is a valid answer: nothing runs


def test_census_rows_are_census_rows_with_name_group_and_a_working_state():
    t, board = census_board()
    board.replace_census("mac", [crow("run-quietud", name="quietud", elapsed=3643, idle=538, what="midiendo")], 60)
    (row,) = board.tasks()
    assert (row.id, row.source, row.project, row.title, row.group, row.state) == (
        "run-quietud", "census", "quietud", "quietud", "Corridas", "working")
    assert (row.tool, row.what, row.said, row.parent) == ("", "midiendo", "", "")
    assert (row.elapsed, row.idle, board.age(row)) == (3643, 538, 538)  # age: how long it has been quiet


def test_census_row_without_idle_has_age_zero_and_no_idle():
    t, board = census_board()
    board.replace_census("mac", [crow("job-noche", elapsed=50)], 60)
    (row,) = board.tasks()
    assert row.idle is None and board.age(row) == 0


def test_census_clocks_keep_running_between_snapshots():
    t, board = census_board()
    board.replace_census("mac", [crow("run-a", elapsed=3643, idle=21), crow("job-b", elapsed=7)], 60)
    t[0] += 10
    a, b = board.tasks()
    assert (a.elapsed, a.idle, board.age(a)) == (3653, 31, 31) and b.elapsed == 17 and b.idle is None
    t[0] += 5.9
    assert [x.elapsed for x in board.tasks()] == [3658, 22]  # whole seconds
    board.replace_census("mac", [crow("run-a", elapsed=3660, idle=2)], 60)  # a new snapshot restarts the count
    (a,) = board.tasks()
    assert (a.elapsed, a.idle) == (3660, 2)


def test_census_source_expires_and_takes_its_more_with_it():
    t, board = census_board()
    board.replace_census("mac", [crow("run-a")], 45, more=3)
    assert board.census_more() == 3
    t[0] += 45
    assert ids(board) == ["run-a"]  # the ttl is a deadline: at exactly 45 s it is still believed
    t[0] += 0.5
    assert board.tasks() == [] and board.census_more() == 0  # census dead: rows gone and the "+3" forgotten with them
    board.replace_census("mac", [crow("run-z")], 45)
    assert ids(board) == ["run-z"] and board.census_more() == 0


def test_census_ttl_is_clamped_and_a_bad_one_means_the_default():
    from buddy_core import CENSUS_TTL_DEFAULT_S, CENSUS_TTL_MAX_S, CENSUS_TTL_MIN_S

    assert (CENSUS_TTL_MIN_S, CENSUS_TTL_MAX_S, CENSUS_TTL_DEFAULT_S) == (5.0, 120.0, 60.0)
    for ttl, lives in ((1, 5), (0, 5), (-9, 5), (9999, 120), (60.5, 60.5), (float("nan"), 60), (float("inf"), 60), ("x", 60),
                       (None, 60), (True, 5)):
        t, board = census_board()
        board.replace_census("mac", [crow("run-a")], ttl)
        t[0] += lives - 0.1
        assert ids(board) == ["run-a"], ttl
        t[0] += 0.2
        assert board.tasks() == [], ttl


def test_census_sources_do_not_touch_each_other():
    t, board = census_board()
    board.replace_census("mac", [crow("run-a")], 5, more=2)
    board.replace_census("linux", [crow("run-b"), crow("run-c")], 60, more=4)
    assert sorted(ids(board)) == ["run-a", "run-b", "run-c"] and board.census_more() == 6
    board.replace_census("mac", [], 5)  # the Mac's empty snapshot does not wipe the other machine
    assert sorted(ids(board)) == ["run-b", "run-c"] and board.census_more() == 4
    board.replace_census("mac", [crow("run-a")], 5, more=1)
    t[0] += 6  # each source has its own expiry
    assert sorted(ids(board)) == ["run-b", "run-c"] and board.census_more() == 4
    t[0] += 60
    assert board.tasks() == [] and board.census_more() == 0


def test_census_ids_from_two_sources_get_the_source_as_a_suffix_in_the_view():
    t, board = census_board()
    board.update("run-x", project="p")  # even a session that happens to be called like a run
    board.replace_census("mac", [crow("run-x", elapsed=5), crow("run-y")], 60)
    board.replace_census("linux", [crow("run-y", elapsed=9), crow("run-x-mac")], 60)
    got = ids(board)
    assert len(got) == len(set(got))  # whatever collides, the view never repeats an id
    assert sorted(got) == ["run-x", "run-x-mac", "run-x-mac_", "run-y", "run-y-mac"]
    by_id = {x.id: x for x in board.tasks()}
    assert by_id["run-y"].elapsed == 9 and by_id["run-y-mac"].elapsed == 100  # the first source by name keeps the plain id
    assert by_id["run-x"].source == "hook"  # and a session that was there first keeps its id


def test_census_snapshot_with_an_old_seq_is_ignored_unless_the_last_one_expired():
    t, board = census_board()
    assert board.replace_census("mac", [crow("run-a")], 60, seq=100) is True
    t[0] += 10
    assert board.replace_census("mac", [crow("run-b")], 60, seq=100) is False  # same seq: a repeat
    assert board.replace_census("mac", [crow("run-b")], 60, seq=99) is False  # older: a late post of an abandoned thread
    assert ids(board) == ["run-a"] and board.tasks()[0].elapsed == 110  # nothing changed, not even its clock
    t[0] += 50.5  # the ttl runs from the last snapshot that counted
    assert board.tasks() == []
    assert board.replace_census("mac", [crow("run-c")], 60, seq=50) is True  # the last one expired: take it and start over
    assert ids(board) == ["run-c"]
    assert board.replace_census("mac", [crow("run-d")], 60, seq=51) is True


def test_census_seq_is_kept_per_source_and_missing_seq_is_always_taken():
    t, board = census_board()
    board.replace_census("mac", [crow("run-a")], 60, seq=10)
    assert board.replace_census("linux", [crow("run-b")], 60, seq=1) is True  # another machine counts on its own
    assert board.replace_census("mac", [crow("run-c")], 60) is True  # no seq, no ordering to break
    assert board.replace_census("mac", [crow("run-d")], 60, seq=10) is False  # but the mark it had is still there
    assert board.replace_census("mac", [crow("run-d")], 60, seq=11) is True


def test_census_rows_do_not_count_toward_max_tasks():
    from buddy_core import MAX_TASKS

    t, board = census_board()
    for n in range(MAX_TASKS):
        t[0] += 1
        board.update(f"s{n}", project="p")
    board.replace_census("mac", [crow(f"run-{n}") for n in range(8)], 60)
    assert len(board.tasks()) == MAX_TASKS + 8  # 12 sessions and 8 runs, nobody evicted
    t[0] += 1
    board.update("s-new", project="p")
    got = board.tasks()
    assert len(got) == MAX_TASKS + 8 and "s0" not in [x.id for x in got]  # a 13th session evicts a session, never a run
    assert len([x for x in got if x.source == "census"]) == 8


def test_census_snapshot_is_cut_to_eight_rows_and_the_rest_is_counted():
    t, board = census_board()
    board.replace_census("mac", [crow(f"run-{n}") for n in range(11)], 60, more=2)
    assert len(board.tasks()) == 8 and board.census_more() == 5
    board.replace_census("mac", [crow("run-a")], 60, more=5000)
    assert board.census_more() == 999
    for junk in (-4, True, "7", 2.5, None):
        board.replace_census("mac", [crow("run-a")], 60, more=junk)
        assert board.census_more() == 0, junk


def test_census_text_and_numbers_are_cleaned_and_clamped():
    from buddy_core import MAX_CENSUS_SECONDS

    t, board = census_board()
    bad = [
        {"id": "run-" + "x" * 80, "name": "a\nb\x07c" + "n" * 100, "group": "G" * 90, "elapsed": 10**12, "idle": -5,
         "session": "s" * 90, "project": "p\tq" + "p" * 90, "what": "w" * 90},
        {"id": "run-no-numbers", "name": 7, "group": "", "elapsed": "12", "idle": True},
        {"id": "run-no-numbers", "name": "duplicate"},  # the first of an id wins
        {"name": "no id"}, {"id": ""}, {"id": 7}, "not a row", None,
    ]
    board.replace_census("mac", bad, 60)
    long, plain = board.tasks()
    assert len(long.id) == 64 and long.id.startswith("run-x")
    assert long.project.startswith("a b c") and len(long.project) == 40 and len(long.group) == 24
    assert (long.elapsed, long.idle) == (MAX_CENSUS_SECONDS, 0) and len(long.what) == 40
    assert plain.id == "run-no-numbers" and plain.project == "run-no-numbers"  # no usable name: the id stands in
    assert (plain.group, plain.elapsed, plain.idle) == ("Corridas", 0, None)  # no group, no number: defaults, not errors


def test_census_row_merges_with_the_session_it_is_and_shows_one_row():
    t, board = census_board()
    board.update(SESSION, project="relay-x", tool="Bash", what="pnpm e2e", said="Voy por la mitad.", title="Hilo de escritorio",
                 group="Buddy")
    board.update("desk-1", project="relay", tool="Edit", what="a.ts")
    board.replace_census("mac", [crow("run-quietud", name="quietud", session=SESSION, project="relay-x", elapsed=3643, idle=21)], 60)
    got = board.tasks()
    assert [x.id for x in got] == ["run-quietud", "desk-1"]  # the session row is absorbed: no uuid row beside it
    row = got[0]
    assert (row.source, row.project, row.title, row.group, row.state) == ("census", "quietud", "quietud", "Corridas", "working")
    assert (row.tool, row.what, row.said) == ("Bash", "pnpm e2e", "Voy por la mitad.")  # the session's words
    assert (row.elapsed, row.idle) == (3643, 21)  # the process's clock


def test_the_sessions_words_are_shown_only_while_it_is_fresh():
    from buddy_core import CENSUS_HOOK_FRESH_S

    assert CENSUS_HOOK_FRESH_S == 120.0
    t, board = census_board()
    board.update(SESSION, project="relay-x", tool="Bash", what="pnpm e2e", said="Hola.")
    snap = [crow("run-q", session=SESSION, what="de la corrida")]
    board.replace_census("mac", snap, 60)
    t[0] += 60
    board.replace_census("mac", snap, 60)
    t[0] += 60  # 120 s since the session spoke
    board.replace_census("mac", snap, 60)
    (row,) = board.tasks()
    assert (row.tool, row.what, row.said) == ("Bash", "pnpm e2e", "Hola.")
    t[0] += 1  # 121 s: quiet
    board.replace_census("mac", snap, 60)
    (row,) = board.tasks()
    assert (row.tool, row.what, row.said) == ("", "de la corrida", "")  # the census text instead, never an old tool
    board.update(SESSION, tool="Read", what="x.py")
    (row,) = board.tasks()
    assert (row.tool, row.what) == ("Read", "x.py")  # and it speaks again as soon as the session does


def test_a_run_never_looks_idle_even_when_its_session_finished_a_turn():
    t, board = census_board()
    board.update(SESSION, project="relay-x", tool="Bash", what="ls", state="idle")
    board.replace_census("mac", [crow("run-q", session=SESSION)], 60)
    (row,) = board.tasks()
    assert row.state == "working"


def test_census_falls_back_to_the_folder_only_when_exactly_one_session_has_it():
    t, board = census_board()
    board.update("only", project="relay-x", tool="Bash", what="ls")
    board.update("elsewhere", project="relay-y")
    board.replace_census("mac", [crow("run-q", project="relay-x")], 60)
    got = board.tasks()
    assert [x.id for x in got] == ["run-q", "elsewhere"] and got[0].tool == "Bash"  # one session in that folder: it is the run
    board.update("twin", project="relay-x")
    got = board.tasks()
    assert sorted(x.id for x in got) == ["elsewhere", "only", "run-q", "twin"]  # two prove nothing: the run stands alone
    assert [x for x in got if x.id == "run-q"][0].tool == ""
    board.replace_census("mac", [crow("run-q", project="")], 60)
    assert "only" in ids(board)  # and no folder to go by means no guess


def test_a_session_claimed_by_its_id_is_not_taken_by_another_rows_folder_guess():
    t, board = census_board()
    board.update(SESSION, project="relay-x", tool="Bash")
    board.replace_census("mac", [crow("run-guess", project="relay-x", elapsed=900), crow("run-sure", session=SESSION, elapsed=10)], 60)
    by_id = {x.id: x for x in board.tasks()}
    assert by_id["run-sure"].tool == "Bash" and by_id["run-guess"].tool == ""


def test_a_killed_run_leaves_the_board_at_the_next_snapshot_not_at_the_hook_ttl():
    """No SessionEnd ever comes from a killed run; the census noticing it is gone must be enough."""
    t, board = census_board()
    board.update(SESSION, project="relay-x", tool="Bash", what="pnpm e2e")
    board.replace_census("mac", [crow("run-q", session=SESSION)], 60, seq=1)
    assert ids(board) == ["run-q"]
    t[0] += 15
    board.replace_census("mac", [], 60, seq=2)
    assert board.tasks() == []  # no "working" row with its last tool for the next ten minutes


def test_the_link_is_known_even_if_nobody_looked_at_the_board_in_between():
    t, board = census_board()
    board.update(SESSION, project="relay-x", tool="Bash")
    board.replace_census("mac", [crow("run-q", session=SESSION)], 60)
    board.replace_census("mac", [crow("job-other")], 60)  # no tasks() call between: the link is made when the rows change
    assert ids(board) == ["job-other"]


def test_a_run_found_by_folder_is_deleted_with_its_session_too():
    t, board = census_board()
    board.update("only", project="relay-x", tool="Bash")
    board.update("bystander", project="relay-y")
    board.replace_census("mac", [crow("run-q", project="relay-x")], 60)
    assert ids(board) == ["run-q", "bystander"]
    board.replace_census("mac", [], 60)
    assert ids(board) == ["bystander"]


def test_nothing_is_deleted_when_the_snapshot_was_cut_or_the_source_just_expired():
    t, board = census_board()
    board.update(SESSION, project="relay-x", tool="Bash", what="pnpm e2e")
    board.replace_census("mac", [crow("run-q", session=SESSION)], 60)
    assert ids(board) == ["run-q"]
    board.replace_census("mac", [crow("run-z")], 60, more=3)  # three more did not fit: the run may be one of them
    assert ids(board) == ["run-z", SESSION] and board.tasks()[1].tool == "Bash"  # it shows as the session it is
    board.replace_census("mac", [crow("run-q", session=SESSION)], 60)  # it was just beyond the cut, and is back
    assert ids(board) == ["run-q"]
    t[0] += 61  # the census went quiet: that says nothing about the run
    assert ids(board) == [SESSION]
    board.replace_census("mac", [crow("run-z")], 60)  # and a new snapshot after that does not know the old link
    assert ids(board) == ["run-z", SESSION]
    t[0] += 700  # the session's own ttl is what cleans it
    board.replace_census("mac", [crow("run-z")], 60)
    assert ids(board) == ["run-z"]


def test_a_vanished_run_deletes_only_the_session_it_was_linked_to():
    t, board = census_board()
    board.update(SESSION, project="relay-x", tool="Bash")
    board.update("other", project="relay-y", tool="Edit")
    board.update("unlinked", project="relay-x")
    board.replace_census("mac", [crow("run-q", session=SESSION), crow("run-w", session="somebody-else")], 60)
    board.replace_census("linux", [crow("run-l")], 60)
    board.replace_census("linux", [], 60)  # another source's snapshot touches no session of the Mac's runs
    assert SESSION not in ids(board) and "run-q" in ids(board)
    board.replace_census("mac", [crow("run-w", session="somebody-else")], 60)
    assert sorted(ids(board)) == ["other", "run-w", "unlinked"]  # the linked one went; "unlinked" was never the run's


def test_a_run_that_changed_its_id_but_keeps_its_session_keeps_its_row():
    t, board = census_board()
    board.update(SESSION, project="relay-x", tool="Bash")
    board.replace_census("mac", [crow("run-q", session=SESSION)], 60)
    assert ids(board) == ["run-q"]
    board.replace_census("mac", [crow("run-q-p12", session=SESSION)], 60)  # a second run of that name took the plain id
    assert ids(board) == ["run-q-p12"]
    board.replace_census("mac", [], 60)
    assert board.tasks() == []


def test_a_row_with_a_known_session_never_takes_a_thread_that_shares_its_folder():
    """The census knows whose run it is. A session that is not that one is somebody else, however alone it is in the folder."""
    t, board = census_board()
    board.update("thread", project="relay-x", state="idle", tool="Bash", what="ls", title="Pulir la landing", group="Buddy")
    board.replace_census("mac", [crow("run-q", session=SESSION, project="relay-x")], 60)  # the run's own hook has not spoken yet
    got = {x.id: x for x in board.tasks()}
    assert sorted(got) == ["run-q", "thread"]
    assert got["run-q"].tool == "" and (got["thread"].title, got["thread"].group, got["thread"].state) == ("Pulir la landing", "Buddy", "idle")
    board.update("thread", state="gone")  # the thread's SessionEnd is not the run's
    assert ids(board) == ["run-q"]


def test_the_end_of_a_run_does_not_take_the_thread_that_shared_its_folder():
    t, board = census_board()
    board.update("thread", project="relay-x", state="idle", title="Pulir la landing")
    board.replace_census("mac", [crow("run-q", session=SESSION, project="relay-x")], 60)
    board.tasks()  # the screen looks at the board about once a second, so whatever pairing there is gets recorded
    board.update(SESSION, project="relay-x", tool="Read")  # now the run's own hook speaks, and the exact join takes over
    assert ids(board) == ["run-q", "thread"]
    board.replace_census("mac", [], 60)  # the run ends
    assert ids(board) == ["thread"]  # the idle thread that waits for Ana is still there


def test_a_folder_guess_is_forgotten_as_soon_as_it_stops_holding():
    t, board = census_board()
    board.update("only", project="relay-x", tool="Bash")
    board.replace_census("mac", [crow("run-q", project="relay-x")], 60)  # no session: the folder is all there is to go by
    assert ids(board) == ["run-q"]
    board.update("twin", project="relay-x")  # a second thread in that folder: the guess no longer holds
    assert sorted(ids(board)) == ["only", "run-q", "twin"]
    board.replace_census("mac", [], 60)
    assert sorted(ids(board)) == ["only", "twin"]  # the run's end takes neither: nothing showed either was it


def test_a_killed_run_whose_row_id_a_new_run_took_does_not_look_alive():
    """Killed without a SessionEnd and relaunched in the same folder within one snapshot: same row id, another session."""
    t, board = census_board()
    board.update(SESSION, project="relay-x", tool="Bash", what="pnpm test")
    board.replace_census("mac", [crow("run-q", session=SESSION, project="relay-x")], 60, seq=1)
    t[0] += 15
    board.update("the-new-session", project="relay-x", tool="Read", what="a.ts")
    board.replace_census("mac", [crow("run-q", session="the-new-session", project="relay-x")], 60, seq=2)
    (row,) = board.tasks()
    assert (row.id, row.tool) == ("run-q", "Read")  # one run, saying what the live one says
    t[0] += 300
    board.replace_census("mac", [crow("run-q", session="the-new-session", project="relay-x")], 60, seq=3)
    assert ids(board) == ["run-q"]  # and the dead one is not a second "working" row for the ten minutes of its ttl


def test_a_session_the_census_could_not_tell_for_one_pass_is_not_taken_for_dead():
    """Two runs of one folder, the older leaves, and the survivor takes the plain id; if its session cannot be read in that
    pass the census cannot say which session is gone, so nothing is deleted."""
    t, board = census_board()
    board.update("s100", project="relay-foo", tool="Bash")
    board.update("s200", project="relay-foo", tool="Edit")
    both = [crow("run-foo", session="s100", project="relay-foo"), crow("run-foo-p200", session="s200", project="relay-foo")]
    board.replace_census("mac", both, 60)
    assert sorted(ids(board)) == ["run-foo", "run-foo-p200"]
    board.replace_census("mac", [crow("run-foo", project="relay-foo")], 60)  # s200 is this one, but nothing says so
    assert "s200" in ids(board)
    board.replace_census("mac", [crow("run-foo", session="s200", project="relay-foo")], 60)  # the next pass reads it
    assert [x.tool for x in board.tasks() if x.id == "run-foo"] == ["Edit"]
    board.replace_census("mac", [], 60)  # and when it really ends, it goes at once
    assert "s200" not in ids(board)


def test_only_a_run_of_the_same_folder_with_no_session_holds_back_the_end_of_a_run():
    t, board = census_board()
    board.update(SESSION, project="relay-x", tool="Bash")
    board.replace_census("mac", [crow("run-q", session=SESSION, project="relay-x"), crow("job-n")], 60)
    board.replace_census("mac", [crow("job-n")], 60)  # a job never has a session: it says nothing about the run's
    assert ids(board) == ["job-n"]
    board.update(SESSION, project="relay-x", tool="Bash")
    board.replace_census("mac", [crow("run-q", session=SESSION, project="relay-x"), crow("run-w", project="relay-y")], 60)
    board.replace_census("mac", [crow("run-w", project="relay-y")], 60)  # an unreadable run elsewhere cannot be this one
    assert ids(board) == ["run-w"]


def test_the_farewell_of_a_census_that_stops_takes_no_session_with_it():
    """SIGTERM posts an empty snapshot with the smallest ttl: "I am leaving", not "nothing runs". The runs may well be alive."""
    t, board = census_board()
    board.update(SESSION, project="relay-x", tool="Bash", what="pnpm e2e")
    board.replace_census("mac", [crow("run-q", session=SESSION, project="relay-x")], 60)
    board.replace_census("mac", [], 5)
    (row,) = board.tasks()
    assert (row.id, row.source, row.tool) == (SESSION, "hook", "Bash")  # the run's rows go, its session keeps its own life
    t[0] += 6
    assert ids(board) == [SESSION] and board.census_more() == 0
    board.replace_census("mac", [crow("run-q", session=SESSION, project="relay-x")], 60)  # the census is back
    assert ids(board) == ["run-q"]
    board.replace_census("mac", [], 20)  # an empty snapshot of a census that keeps going does mean nothing runs
    assert board.tasks() == []


def test_session_end_hides_the_run_it_belongs_to_for_thirty_seconds():
    from buddy_core import CENSUS_ENDED_HIDE_S

    assert CENSUS_ENDED_HIDE_S == 30.0
    t, board = census_board()
    board.update(SESSION, project="relay-x", tool="Bash")
    snap = [crow("run-q", session=SESSION), crow("job-n")]
    board.replace_census("mac", snap, 60)
    board.update(SESSION, state="gone")  # SessionEnd
    assert ids(board) == ["job-n"]  # the run does not linger the 15 s until the next snapshot says it is over
    t[0] += 15
    board.replace_census("mac", snap, 60)  # the process is still listed for a moment
    assert ids(board) == ["job-n"]
    t[0] += 14.5
    board.replace_census("mac", snap, 60)
    assert ids(board) == ["job-n"]  # 29.5 s
    t[0] += 1
    board.replace_census("mac", snap, 60)
    assert ids(board) == ["run-q", "job-n"]  # 30.5 s: if the census still sees it, so do we


def test_the_hiding_ends_when_a_snapshot_no_longer_has_the_run():
    t, board = census_board()
    board.update(SESSION, project="relay-x", tool="Bash")
    board.replace_census("mac", [crow("run-q", session=SESSION)], 60)
    board.update(SESSION, state="gone")
    board.replace_census("mac", [], 60)
    board.replace_census("mac", [crow("run-q", session="a-new-session")], 60)  # a new run with the same name, 2 s later
    assert ids(board) == ["run-q"]


def test_session_end_hides_a_run_found_by_folder_and_one_whose_session_never_spoke():
    t, board = census_board()
    board.update("only", project="relay-x", tool="Bash")
    board.replace_census("mac", [crow("run-q", project="relay-x"), crow("run-w", session=SESSION)], 60)
    assert ids(board) == ["run-q", "run-w"]
    board.update("only", state="gone")
    assert ids(board) == ["run-w"]
    board.update(SESSION, state="gone")  # the run's own session ends and the hook never had a row for it
    assert board.tasks() == []
    board.update("stranger", state="gone")  # a session no run belongs to changes nothing
    board.replace_census("mac", [crow("job-n")], 60)
    board.update("stranger", state="gone")
    assert ids(board) == ["job-n"]


def test_hook_ttls_do_not_apply_to_census_rows():
    from buddy_core import TASK_IDLE_TTL_S, TASK_TTL_S

    t, board = census_board()
    board.update(SESSION, project="relay-x", tool="Bash")
    snap = [crow("run-q", session=SESSION, elapsed=0, idle=0)]
    for _ in range(int(TASK_IDLE_TTL_S * 1.5 / 15)):  # a run that lasts longer than any thread would stay on the board
        t[0] += 15
        board.replace_census("mac", snap, 60)
    (row,) = board.tasks()  # 45 minutes later: both hook TTLs have passed, the run is still there
    assert row.id == "run-q" and row.tool == "" and t[0] - 1000 > TASK_IDLE_TTL_S > TASK_TTL_S
    t[0] += 60.5
    assert board.tasks() == []  # only the census going quiet takes it away


def test_tasks_come_working_first_then_census_before_sessions_runs_before_jobs_oldest_first():
    t, board = census_board()
    board.update("idle-old", project="p", state="idle")
    t[0] += 1
    board.update("work-old", project="p")
    t[0] += 1
    board.update("work-new", project="p")
    t[0] += 1
    board.update("idle-new", project="p", state="idle")
    board.replace_census("mac", [crow("job-long", elapsed=9000), crow("run-new", elapsed=10), crow("run-old", elapsed=500),
                                 crow("job-short", elapsed=20)], 60)
    assert ids(board) == ["run-old", "run-new", "job-long", "job-short", "work-new", "work-old", "idle-new", "idle-old"]


def test_too_many_sources_make_the_quietest_one_leave():
    from buddy_core import MAX_CENSUS_SOURCES

    t, board = census_board()
    for n in range(MAX_CENSUS_SOURCES):
        t[0] += 1
        board.replace_census(f"m{n}", [crow(f"run-{n}")], 120)
    t[0] += 1
    board.replace_census("m0", [crow("run-0")], 120)  # m0 speaks again: it is no longer the quietest
    t[0] += 1
    board.replace_census("newcomer", [crow("run-new")], 120)
    got = set(ids(board))
    assert len(got) == MAX_CENSUS_SOURCES and "run-new" in got and "run-0" in got and "run-1" not in got


def test_a_board_without_a_census_behaves_as_it_always_did():
    t, board = census_board()
    assert board.tasks() == [] and board.census_more() == 0
    board.update("s", project="p", tool="Bash")
    (row,) = board.tasks()
    assert (row.source, row.elapsed, row.idle, board.age(row)) == ("hook", 0, None, 0)
    board.update("s", state="gone")  # SessionEnd with no census at all
    assert board.tasks() == []


# --- the notice board: short messages Hermes leaves on the screen (the rest is in test_notice.py) ------------------------


def test_the_core_carries_a_notice_board_that_runs_on_its_own_clock(tmp_path):
    from buddy_core import NoticeBoard

    h = Harness(tmp_path / "state")
    assert isinstance(h.core.notices, NoticeBoard) and h.core.notices.active() == []
    h.core.notices.add(title="La tanda terminó", ttl=10, notice_id="tanda")
    h.t += 9
    assert [n.id for n in h.core.notices.active()] == ["tanda"]
    h.t += 1
    assert h.core.notices.active() == [] and h.core.notices.state("tanda") == "expired"
