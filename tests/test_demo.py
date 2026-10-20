"""Demo mode: the real hub and core driven through an invented story (buddy_demo.py)."""
import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import buddy  # noqa: E402
import buddy_demo  # noqa: E402
from buddy_core import ApprovalBroker, BuddyCore  # noqa: E402
from buddy_ui import UIHub  # noqa: E402
from test_ui import Rig  # noqa: E402


class Story:
    def __init__(self, tmp_path, lang="en"):
        self.sent = []
        self.t = 1000.0
        self.core = BuddyCore(tmp_path, self.sent.append, broker=ApprovalBroker(), clock=lambda: self.t)
        self.hub = UIHub(lang=lang)
        self.hub.bind(self.core, buddy_demo.Radio())
        self.demo = buddy_demo.Demo(self.core, self.hub, lang, started=0.0)

    def at(self, seconds):
        self.demo.step(float(seconds))
        return self.hub.view()


@pytest.fixture
def story(tmp_path):
    return Story(tmp_path / "state")


def test_the_story_is_a_pure_function_of_time():
    a = buddy_demo.story_state(31)
    assert a["permission"] and a["migration"] and a["notice_ok"]
    assert buddy_demo.story_state(31 + buddy_demo.PERIOD_S) == {**a, "t": 31}
    assert not buddy_demo.story_state(5)["permission"] and not buddy_demo.story_state(95)["permission"]


def test_a_full_loop_shows_threads_runs_notices_and_one_permission(story):
    v = story.at(1)
    assert v["lang"] == "en" and v["pet"]["owner"] == "Ana"
    assert {t["title"] for t in v["tasks"] if t.get("title")} >= {"Product catalog for the web store", "Fix refund rounding"}
    assert any(t["source"] == "census" for t in v["tasks"])
    v = story.at(20)
    assert [n["title"] for n in v["notices"]] == ["Deploy finished"]
    v = story.at(31)
    assert len(v["approvals"]) == 1 and v["approvals"][0]["tool"] == "Bash" and v["mood"] == "attention"
    v = story.at(65)
    assert "Meeting in 10 minutes" in [n["title"] for n in v["notices"]]


def test_a_tap_answers_the_permission_and_it_does_not_come_back_in_that_loop(story):
    v = story.at(31)
    key = v["approvals"][0]["key"]
    assert story.hub.decide(key, "once") is True
    assert story.at(33)["approvals"] == [] and story.at(60)["approvals"] == []
    assert story.sent  # the decision went to the (fake) radio
    assert len(story.at(buddy_demo.PERIOD_S + 31)["approvals"]) == 1  # and the next loop asks again


def test_an_unanswered_permission_withdraws_itself(story):
    assert len(story.at(40)["approvals"]) == 1
    assert story.at(buddy_demo.PERMISSION_UNTIL_S + 1)["approvals"] == []


def test_the_spanish_story_is_spanish_and_invented(tmp_path):
    v = Story(tmp_path / "s", "es").at(20)
    assert v["lang"] == "es" and [n["title"] for n in v["notices"]] == ["Terminó el despliegue"]


def test_the_demo_command_is_in_the_cli_and_uses_a_temporary_state_dir(tmp_path, capsys):
    args = buddy.parse_args(["demo", "--lang", "en", "--ui-port", "0", "--state-dir", str(tmp_path / "real")])
    assert args.command == "demo" and args.lang == "en"

    async def short():
        task = asyncio.ensure_future(buddy_demo.run_demo(args))
        await asyncio.sleep(1.5)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(short())
    assert not (tmp_path / "real").exists()  # nothing is written to the real state dir
    assert "Demo mode" in capsys.readouterr().out
