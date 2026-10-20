"""tools/census_agent.sh: the installer of the census LaunchAgent, run against a stub launchctl, a temp HOME and a fake repo.

Nothing here touches the real launchd, the real ~/Library, the real token or the network: every run gets a PATH that starts
with stub launchctl / curl / sleep / xcode-select, `CLAUDE_BUDDY_LAUNCHCTL` pointing at the stub, and its own HOME.
"""
import os
import plistlib
import re
import shutil
import socket
import stat
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tools" / "census_agent.sh"
BASH = "/bin/bash" if os.path.exists("/bin/bash") else shutil.which("bash")  # the Mac's 3.2 when there is one
PLUTIL = "/usr/bin/plutil"
LABEL = "com.claude-buddy.census"
DOMAIN = "gui/%d" % os.getuid()
SECRET_TOKEN = "FAKE-TOKEN-CONTENT-0123456789abcdef"
SECRET_KEY = "sk-ant-FAKE-KEY-0123456789abcdef"

pytestmark = pytest.mark.skipif(BASH is None, reason="needs bash")

STUBS = {
    "launchctl": r"""#!/bin/bash
echo "launchctl $*" >> "$STUB_LOG"
case "$1" in
  print)
    [ -f "$STUB_STATE" ] || exit 113
    if [ -f "$STUB_STATE.linger" ]; then          # a bootout in flight: still there for a few more questions
      n=$(cat "$STUB_STATE.linger")
      if [ "$n" -le 0 ]; then rm -f "$STUB_STATE" "$STUB_STATE.linger"; exit 113; fi
      echo $((n - 1)) > "$STUB_STATE.linger"
    fi
    printf '\tactive count = 1\n\tstate = running\n\tpid = 4242\n\truns = 1\n\tlast exit code = (never exited)\n'
    printf '\tendpoints = {\n\t\tstate = active\n\t}\n'
    ;;
  bootstrap)
    fails=$(cat "$STUB_STATE.fails" 2>/dev/null || echo "${STUB_BOOTSTRAP_FAILS:-0}")
    if [ "$fails" -gt 0 ]; then
      echo $((fails - 1)) > "$STUB_STATE.fails"
      echo "Bootstrap failed: 5: Input/output error" >&2
      exit 5
    fi
    echo 0 > "$STUB_STATE.fails"
    touch "$STUB_STATE"
    ;;
  bootout)
    if [ "${STUB_LINGER:-0}" -gt 0 ]; then echo "$STUB_LINGER" > "$STUB_STATE.linger"; else rm -f "$STUB_STATE"; fi
    ;;
esac
exit 0
""",
    "curl": r"""#!/bin/bash
echo "curl $*" >> "$STUB_LOG"
if [ "${STUB_CURL_FAIL:-0}" = 1 ]; then printf '\n000'; exit 7; fi
printf '%s\n%s' "${STUB_CURL_BODY-ok}" "${STUB_CURL_CODE:-200}"
""",
    "sleep": '#!/bin/bash\necho "sleep $*" >> "$STUB_LOG"\n',
    "xcode-select": '#!/bin/bash\necho "xcode-select $*" >> "$STUB_LOG"\nexit "${STUB_XCODE_RC:-0}"\n',
}


class Rig:
    """A fake repo (the script next to a stub census.py), a HOME of its own and the stubs."""

    def __init__(self, tmp_path, repo_name="repo", home_name="home"):
        self.root = tmp_path.resolve()
        self.home = self.root / home_name
        self.home.mkdir()
        self.repo = self.root / repo_name
        self.stubs = self.root / "stubbin"
        self.stubs.mkdir()
        for name, text in STUBS.items():
            (self.stubs / name).write_text(text)
            (self.stubs / name).chmod(0o755)
        self.log = self.root / "stub.log"
        self.state = self.root / "loaded"
        self.script = self.place(self.repo)

    def place(self, repo):
        """The script and a census.py in `repo`/tools, as a checkout would have them."""
        tools = repo / "tools"
        tools.mkdir(parents=True)
        shutil.copy2(SCRIPT, tools / "census_agent.sh")
        (tools / "census.py").write_text("# stand-in: the installer only needs the file to exist\n")
        return tools / "census_agent.sh"

    # -- running it --------------------------------------------------------------------------------------------

    def run(self, *args, script=None, python=sys.executable, home=None, env=None):
        full = {
            "PATH": "%s:/usr/bin:/bin" % self.stubs,
            "HOME": str(home or self.home),
            "CLAUDE_BUDDY_LAUNCHCTL": str(self.stubs / "launchctl"),
            "STUB_LOG": str(self.log),
            "STUB_STATE": str(self.state),
            "ANTHROPIC_API_KEY": SECRET_KEY,  # an installer has no business with the environment's secrets
        }
        if python:
            full["CLAUDE_BUDDY_PYTHON"] = python
        full.update(env or {})
        done = subprocess.run([BASH, str(script or self.script)] + list(args), env=full, capture_output=True,
                              encoding="utf-8", timeout=60)
        for text in (done.stdout, done.stderr):
            assert SECRET_KEY not in text and SECRET_TOKEN not in text
        return done

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def launchctl(self):
        """The launchctl calls that change something, in order (asking `print` is not a change)."""
        return [c.split(" ", 1)[1] for c in self.calls() if c.startswith("launchctl ") and not c.startswith("launchctl print")]

    # -- the state of the fake machine ---------------------------------------------------------------------------

    @property
    def plist_path(self):
        return self.home / "Library" / "LaunchAgents" / (LABEL + ".plist")

    @property
    def log_path(self):
        return self.home / "Library" / "Logs" / "claude-buddy-census.log"

    @property
    def token_path(self):
        return self.home / ".config" / "claude-buddy" / "hermes.token"

    def token(self, text=SECRET_TOKEN + "\n", mode=0o600):
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(text)
        self.token_path.chmod(mode)

    def plist(self, *args):
        done = self.run("print", *args, python=None)
        assert done.returncode == 0, done.stderr
        return plistlib.loads(done.stdout.encode())


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def installed(rig, *args):
    done = rig.run("install", *args)
    assert done.returncode == 0, done.stdout + done.stderr
    return done


# -- the plist -------------------------------------------------------------------------------------------------------

def test_the_plist_is_what_the_contract_says(rig):
    plist = rig.plist()
    census = str((rig.repo / "tools" / "census.py").resolve())
    assert plist == {
        "Label": LABEL,
        "ProgramArguments": ["/usr/bin/python3", "-I", "-u", census, "run"],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 30,
        "ExitTimeOut": 5,
        "LimitLoadToSessionType": "Aqua",
        "StandardOutPath": str(rig.log_path),
        "StandardErrorPath": str(rig.log_path),
        "EnvironmentVariables": {"PATH": "/usr/bin:/bin"},
    }
    assert "ProcessType" not in plist  # a Background census flickers under load: the default class stays


def test_the_plist_holds_no_secret_and_nothing_from_the_environment(rig):
    rig.token()
    done = rig.run("print", python=None, env={"CLAUDE_BUDDY_TOKEN_FILE": "/x/y.token", "CLAUDE_BUDDY_URL": "http://buddy:8766"})
    text = done.stdout.lower()
    for secret in ("hermes.token", "/x/y.token", "bearer", "sk-ant", SECRET_TOKEN.lower(), "claude_buddy_token"):
        assert secret not in text
    assert plistlib.loads(done.stdout.encode())["EnvironmentVariables"] == {"PATH": "/usr/bin:/bin"}


@pytest.mark.skipif(not os.path.exists(PLUTIL), reason="plutil is macOS only")
def test_plutil_accepts_the_plist_with_and_without_a_url(rig, tmp_path):
    for args in ((), ("--url", "http://buddy:8766")):
        out = tmp_path / "out.plist"
        out.write_text(rig.run("print", *args, python=None).stdout)
        lint = subprocess.run([PLUTIL, "-lint", str(out)], capture_output=True, encoding="utf-8")
        assert lint.returncode == 0, lint.stdout + lint.stderr


def test_paths_with_xml_and_shell_characters_come_out_escaped_and_parse(tmp_path):
    odd = Rig(tmp_path, repo_name="a&b <c> \"d\" 'e' \u00f1 $x `y`", home_name="h&m <e>")
    plist = odd.plist()
    assert plist["ProgramArguments"][3] == str(odd.repo.resolve() / "tools" / "census.py")
    assert plist["StandardOutPath"] == str(odd.log_path)
    raw = odd.run("print", python=None).stdout
    assert "&amp;" in raw and "&lt;c&gt;" in raw and "<c>" not in raw
    if os.path.exists(PLUTIL):
        out = tmp_path / "odd.plist"
        out.write_text(raw)
        assert subprocess.run([PLUTIL, "-lint", str(out)], capture_output=True).returncode == 0


def test_print_is_exactly_what_install_writes(rig):
    installed(rig)
    assert rig.plist_path.read_text() == rig.run("print").stdout


# -- --url -----------------------------------------------------------------------------------------------------------

def test_the_url_goes_into_the_plist_only_when_given_with_the_flag(rig):
    assert "CLAUDE_BUDDY_URL" not in rig.plist()["EnvironmentVariables"]
    given = rig.plist("--url", "http://buddy:8766")
    assert given["EnvironmentVariables"] == {"PATH": "/usr/bin:/bin", "CLAUDE_BUDDY_URL": "http://buddy:8766"}
    assert rig.plist("--url=http://127.0.0.1:8766/")["EnvironmentVariables"]["CLAUDE_BUDDY_URL"] == "http://127.0.0.1:8766"
    from_shell = rig.run("print", python=None, env={"CLAUDE_BUDDY_URL": "http://buddy:8766"})  # launchd would not see it either
    assert "CLAUDE_BUDDY_URL" not in plistlib.loads(from_shell.stdout.encode())["EnvironmentVariables"]


@pytest.mark.parametrize("url", [
    "http://buddy:8766", "http://buddy", "http://BUDDY:8766", "http://127.0.0.1:8766", "http://127.9.9.9", "http://localhost:8766",
    "http://[::1]:8766", "http://100.64.0.0", "http://100.101.102.103:8766", "http://100.127.255.255:1",
    "http://mac.tail1234.ts.net:8766", "http://a-b.c.ts.net", "http://buddy:8766/",
])
def test_urls_the_client_would_accept(rig, url):
    done = rig.run("print", "--url", url, python=None)
    assert done.returncode == 0, done.stderr
    assert plistlib.loads(done.stdout.encode())["EnvironmentVariables"]["CLAUDE_BUDDY_URL"] == url.rstrip("/")


@pytest.mark.parametrize("url", [
    "", "buddy:8766", "https://buddy:8766", "HTTP://buddy:8766", "ftp://buddy", "http://", "http://:8766",
    "http://192.168.1.50:8766", "http://10.0.0.5", "http://buddy.local:8766", "http://example.com", "http://1.1.1.1",
    "http://100.63.255.255", "http://100.128.0.0", "http://100.64.0", "http://100.64.0.0.1", "http://100.064.0.1", "http://256.1.1.1",
    "http://127.1", "http://2130706433:8766", "http://0x7f000001", "http://ts.net", "http://x.ts.net.evil.com",
    "http://localhost.evil.com", "http://127.0.0.1.evil.com", "http://[fd7a:115c:a1e0::1]:8766", "http://[::2]",
    "http://user:pw@buddy:8766", "http://buddy@evil.com", "http://buddy:8766/path", "http://buddy:8766?x=1", "http://buddy:0",
    "http://buddy:70000", "http://buddy:80 ", "http://bud dy", "http://buddy&x", "http://a..ts.net",
])
def test_urls_the_installer_refuses_without_changing_anything(rig, url):
    for command in (["install"], ["print"]):  # the check runs while the options are read, before any command
        done = rig.run(*command, "--url", url)
        assert done.returncode == 2, (command, done.stdout, done.stderr)
        assert "--url" in done.stderr and done.stdout == ""
    assert rig.calls() == [] and not rig.plist_path.exists()
    if url.strip() not in ("", "http://"):
        assert url.strip() not in done.stderr  # never echo what may be a pasted credential


def test_a_refused_url_says_why_and_where_to_go_for_another_destination(rig):
    why = rig.run("print", "--url", "http://192.168.1.50:8766").stderr
    assert "Tailscale" in why and "insecure_url_ok" in why
    assert "http://" in rig.run("print", "--url", "buddy:8766").stderr


def test_url_without_a_value_is_a_usage_error(rig):
    done = rig.run("install", "--url")
    assert done.returncode == 2 and "--url" in done.stderr and rig.calls() == []


# -- usage -----------------------------------------------------------------------------------------------------------

def test_help_and_usage(rig):
    helped = rig.run("--help")
    assert helped.returncode == 0 and "install [-n] [--url URL]" in helped.stdout and "status [--ping]" in helped.stdout
    for args in ((), ("bogus",), ("--bogus",)):
        done = rig.run(*args)
        assert done.returncode == 2 and "install [-n] [--url URL]" in done.stderr


@pytest.mark.parametrize("args", [
    ("install", "--purge"), ("install", "--ping"), ("install", "--bogus"), ("uninstall", "--url", "http://buddy"),
    ("uninstall", "--ping"), ("status", "-n"), ("status", "--purge"), ("status", "--url", "http://buddy"),
    ("restart", "--ping"), ("restart", "-n"), ("print", "-n"), ("print", "--ping"), ("print", "--purge"),
])
def test_options_that_do_not_belong_to_the_command_are_refused(rig, args):
    done = rig.run(*args)
    assert done.returncode == 2 and done.stdout == ""
    assert rig.calls() == []


# -- install / uninstall against the stub launchctl ----------------------------------------------------------------

def test_dry_run_prints_the_plan_and_changes_and_launches_nothing(rig):
    rig.token()
    done = rig.run("install", "-n")
    assert done.returncode == 0
    assert "(nuevo)" in done.stdout and "-n: no cambié nada" in done.stdout
    assert str(rig.plist_path) in done.stdout and "census.py run" in done.stdout and "Para deshacerlo" in done.stdout
    assert not [c for c in rig.calls() if c.startswith("launchctl")]
    assert not rig.plist_path.exists() and not rig.log_path.exists()
    assert rig.run("install", "--dry-run").returncode == 0


def test_install_prints_the_plan_first_then_loads_in_order(rig):
    done = installed(rig)
    assert done.stdout.index("Va a hacer") < done.stdout.index("Instalado y cargado")
    assert rig.launchctl() == ["enable %s/%s" % (DOMAIN, LABEL), "bootstrap %s %s" % (DOMAIN, rig.plist_path)]
    assert rig.plist_path.read_text() == rig.run("print").stdout
    assert stat.S_IMODE(rig.plist_path.stat().st_mode) == 0o644
    assert stat.S_IMODE(rig.log_path.stat().st_mode) == 0o600  # it carries folder names: only yours
    assert [p.name for p in rig.plist_path.parent.iterdir()] == [LABEL + ".plist"]  # no temp file left behind
    assert "state = running" in done.stdout and "pid = 4242" in done.stdout


def test_a_second_install_changes_nothing(rig):
    installed(rig)
    before = rig.plist_path.stat().st_mtime_ns
    rig.log.unlink()
    done = rig.run("install")
    assert done.returncode == 0 and "nada que cambiar" in done.stdout and "(igual)" in done.stdout
    assert rig.launchctl() == []
    assert rig.plist_path.stat().st_mtime_ns == before


def test_install_loads_an_equal_plist_that_is_not_loaded(rig):
    installed(rig)
    before = rig.plist_path.stat().st_mtime_ns
    rig.state.unlink()  # after a logout the service is gone, the plist stays
    rig.log.unlink()
    done = rig.run("install")
    assert done.returncode == 0 and "Instalado y cargado" in done.stdout
    assert rig.launchctl() == ["enable %s/%s" % (DOMAIN, LABEL), "bootstrap %s %s" % (DOMAIN, rig.plist_path)]
    assert rig.plist_path.stat().st_mtime_ns == before


def test_a_changed_plist_is_rewritten_after_bootout_and_the_wait_for_it_to_be_gone(rig):
    installed(rig)
    rig.log.unlink()
    done = rig.run("install", "--url", "http://buddy:8766", env={"STUB_LINGER": "3"})
    assert done.returncode == 0 and "(cambia)" in done.stdout and "fijado en el plist" in done.stdout
    assert rig.launchctl() == ["bootout %s/%s" % (DOMAIN, LABEL), "enable %s/%s" % (DOMAIN, LABEL),
                               "bootstrap %s %s" % (DOMAIN, rig.plist_path)]
    assert rig.calls().count("sleep 0.5") == 3  # it waited while launchd still showed the service
    assert plistlib.loads(rig.plist_path.read_bytes())["EnvironmentVariables"]["CLAUDE_BUDDY_URL"] == "http://buddy:8766"


def test_installing_again_without_url_says_it_drops_the_installed_one(rig):
    installed(rig, "--url", "http://buddy:8766")
    done = rig.run("install", "-n")
    assert "(cambia)" in done.stdout and "http://buddy:8766" in done.stdout and "sin --url se quita" in done.stdout
    installed(rig)
    assert "CLAUDE_BUDDY_URL" not in plistlib.loads(rig.plist_path.read_bytes())["EnvironmentVariables"]


def test_a_url_in_your_terminal_is_called_out_as_not_reaching_the_agent(rig):
    done = rig.run("install", "-n", env={"CLAUDE_BUDDY_URL": "http://buddy:8766"})
    assert "no llega al agente" in done.stdout and "--url" in done.stdout
    assert "no llega al agente" not in rig.run("install", "-n", "--url", "http://buddy:8766").stdout


def test_bootstrap_is_retried_when_launchd_is_slow(rig):
    done = rig.run("install", env={"STUB_BOOTSTRAP_FAILS": "2"})
    assert done.returncode == 0 and "Instalado y cargado" in done.stdout
    assert len([c for c in rig.launchctl() if c.startswith("bootstrap")]) == 3
    assert rig.calls().count("sleep 1") == 2


def test_bootstrap_gives_up_after_four_tries_with_launchctls_own_words(rig):
    done = rig.run("install", env={"STUB_BOOTSTRAP_FAILS": "9"})
    assert done.returncode == 1 and "Instalado" not in done.stdout
    assert "4 veces" in done.stderr and "Input/output error" in done.stderr and "Ajustes del Sistema" in done.stderr
    assert len([c for c in rig.launchctl() if c.startswith("bootstrap")]) == 4
    assert [p.name for p in rig.plist_path.parent.iterdir()] == [LABEL + ".plist"]
    (rig.state.parent / (rig.state.name + ".fails")).unlink()  # the next try finds launchd in a better mood
    again = rig.run("install", env={"STUB_BOOTSTRAP_FAILS": "0"})
    assert again.returncode == 0 and "Instalado y cargado" in again.stdout


def test_uninstall_unloads_deletes_the_plist_and_is_idempotent(rig):
    installed(rig)
    rig.token()
    rig.log_path.write_text("una linea\n")
    config = rig.home / ".config" / "claude-buddy" / "census.json"
    config.write_text("{}")
    rig.log.unlink()
    done = rig.run("uninstall")
    assert done.returncode == 0 and "descargado" in done.stdout and "plist borrado" in done.stdout
    assert rig.launchctl() == ["bootout %s/%s" % (DOMAIN, LABEL)]
    assert not rig.plist_path.exists()
    assert rig.log_path.exists() and config.exists() and rig.token_path.exists()  # yours stay
    rig.log.unlink()
    again = rig.run("uninstall")
    assert again.returncode == 0 and "no estaba cargado" in again.stdout and "no había plist" in again.stdout
    assert rig.launchctl() == []


def test_uninstall_purge_also_removes_the_log(rig):
    installed(rig)
    done = rig.run("uninstall", "--purge")
    assert done.returncode == 0 and "log borrado" in done.stdout and not rig.log_path.exists()


def test_uninstall_dry_run_changes_nothing(rig):
    installed(rig)
    rig.log.unlink()
    done = rig.run("uninstall", "-n", "--purge")
    assert done.returncode == 0 and "-n: no cambié nada" in done.stdout
    assert rig.launchctl() == [] and rig.plist_path.exists() and rig.log_path.exists()


def test_restart_kickstarts_a_loaded_agent_and_refuses_an_unloaded_one(rig):
    refused = rig.run("restart")
    assert refused.returncode == 1 and "install" in refused.stderr and rig.launchctl() == []
    installed(rig)
    rig.log.unlink()
    done = rig.run("restart")
    assert done.returncode == 0 and "reiniciado" in done.stdout
    assert rig.launchctl() == ["kickstart -k %s/%s" % (DOMAIN, LABEL)]


# -- what it refuses -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("folder", [
    "Documents", "Desktop", "Downloads", "Library/Mobile Documents/com~apple~CloudDocs", "Library/CloudStorage/Dropbox",
    "Documents/deep/er",
])
def test_a_checkout_in_a_folder_macos_protects_is_refused(tmp_path, folder):
    rig = Rig(tmp_path)
    script = rig.place(rig.home / folder / "claude-buddy")
    for command in (["install"], ["install", "-n"]):
        done = rig.run(*command, script=script)
        assert done.returncode == 1 and "macOS protege" in done.stderr and "fuera de Documents" in done.stderr
    assert not [c for c in rig.calls() if c.startswith("launchctl")]
    assert not rig.plist_path.exists()
    assert rig.run("print", script=script, python=None).returncode == 0  # reading it is fine
    assert rig.run("uninstall", "-n", script=script).returncode == 0   # so is leaving it


def test_the_protected_folder_check_sees_through_a_symlinked_or_slashed_home(tmp_path):
    rig = Rig(tmp_path)
    script = rig.place(rig.home / "Documents" / "claude-buddy")
    alias = rig.root / "home-alias"
    alias.symlink_to(rig.home)
    for home in (alias, str(rig.home) + "/"):
        done = rig.run("install", "-n", script=script, home=home)
        assert done.returncode == 1 and "macOS protege" in done.stderr


def test_folders_that_merely_look_protected_are_fine(tmp_path):
    rig = Rig(tmp_path)
    script = rig.place(rig.home / "Projects" / "Documents" / "claude-buddy")
    assert rig.run("install", "-n", script=script).returncode == 0
    other = rig.place(rig.root / "Documents" / "claude-buddy")  # someone else's Documents, not this HOME's
    assert rig.run("install", "-n", script=other).returncode == 0


def test_without_census_py_next_to_it_install_refuses(rig):
    (rig.repo / "tools" / "census.py").unlink()
    done = rig.run("install")
    assert done.returncode == 1 and "census.py" in done.stderr and not rig.calls()


def test_the_usr_bin_python_shim_needs_the_command_line_tools(rig):
    done = rig.run("install", python="/usr/bin/python3", env={"STUB_XCODE_RC": "2"})
    assert done.returncode == 1 and "xcode-select --install" in done.stderr
    assert "xcode-select -p" in rig.calls() and not rig.launchctl() and not rig.plist_path.exists()


def test_another_python_does_not_ask_xcode_select(rig):
    installed(rig)
    assert not [c for c in rig.calls() if c.startswith("xcode-select")]
    assert rig.plist()["ProgramArguments"][0] == "/usr/bin/python3"  # the default is the shim; this run used another


def test_an_unusable_python_is_refused(rig, tmp_path):
    old = tmp_path / "old-python"
    old.write_text("#!/bin/sh\nexit 1\n")
    old.chmod(0o755)
    done = rig.run("install", python=str(old))
    assert done.returncode == 1 and "3.9" in done.stderr and not rig.launchctl()
    assert rig.run("install", python="python3").returncode == 1
    assert rig.run("install", python=str(tmp_path / "nope")).returncode == 1
    assert not rig.plist_path.exists()


def test_a_missing_launchctl_is_refused_before_anything_is_written(rig):
    done = rig.run("install", env={"CLAUDE_BUDDY_LAUNCHCTL": str(rig.root / "no-launchctl")})
    assert done.returncode == 1 and "launchctl" in done.stderr and not rig.plist_path.exists()


# -- status never goes to the network unless asked -------------------------------------------------------------------

def test_nothing_but_status_ping_ever_calls_curl(rig):
    rig.token()
    for command in (["install", "-n"], ["install"], ["install"], ["print"], ["status"], ["restart"], ["uninstall", "-n"],
                    ["uninstall"], ["status"], ["uninstall"]):
        rig.run(*command)
    assert not [c for c in rig.calls() if c.startswith("curl")]


def test_status_says_it_did_not_contact_the_pi(rig):
    installed(rig)
    done = rig.run("status")
    assert done.returncode == 0 and "sin contactar al Pi" in done.stdout and "--ping" in done.stdout
    assert "igual a lo que escribiría" in done.stdout
    assert [l for l in done.stdout.splitlines() if l.startswith("cargado")] == [
        "cargado   state = running", "cargado   pid = 4242", "cargado   runs = 1", "cargado   last exit code = (never exited)"]


def test_status_when_nothing_is_installed(rig):
    done = rig.run("status")
    assert done.returncode == 0 and "no está instalado" in done.stdout and "cargado   no" in done.stdout
    assert "token     falta" in done.stdout and "ninguna" in done.stdout


def test_status_notices_a_plist_that_no_longer_matches_this_checkout(rig):
    installed(rig)
    moved = rig.run("status", script=rig.place(rig.root / "moved"))
    assert "distinto de lo que escribiría" in moved.stdout and "install" in moved.stdout


def test_ping_asks_healthz_once_and_never_sends_the_token(rig):
    rig.token()
    done = rig.run("status", "--ping")
    curls = [c for c in rig.calls() if c.startswith("curl")]
    assert len(curls) == 1 and curls[0].endswith(" http://buddy:8766/healthz")
    assert "-H" not in curls[0].split() and "Authorization" not in curls[0] and SECRET_TOKEN not in curls[0]
    assert "buddy     responde (http://buddy:8766, de fábrica)" in done.stdout


def test_ping_reports_a_dead_or_foreign_answer(rig):
    assert "no responde" in rig.run("status", "--ping", env={"STUB_CURL_FAIL": "1"}).stdout
    wrong = rig.run("status", "--ping", env={"STUB_CURL_CODE": "403", "STUB_CURL_BODY": ""}).stdout
    assert "contestó HTTP 403" in wrong
    assert "contestó HTTP 200" in rig.run("status", "--ping", env={"STUB_CURL_BODY": "<html>"}).stdout


def test_ping_follows_the_url_the_installed_plist_fixes(rig):
    installed(rig, "--url", "http://127.0.0.1:9999")
    rig.log.unlink()
    done = rig.run("status", "--ping", env={"CLAUDE_BUDDY_URL": "http://100.64.1.2:8766"})
    assert [c for c in rig.calls() if c.startswith("curl")][0].endswith(" http://127.0.0.1:9999/healthz")
    assert "responde (http://127.0.0.1:9999, del plist)" in done.stdout
    assert "igual a lo que escribiría" in done.stdout


def test_ping_takes_the_terminal_url_when_the_plist_has_none_and_refuses_a_bad_one(rig):
    ok = rig.run("status", "--ping", env={"CLAUDE_BUDDY_URL": "http://100.64.1.2:8766/"})
    assert [c for c in rig.calls() if c.startswith("curl")][0].endswith(" http://100.64.1.2:8766/healthz")
    assert "de CLAUDE_BUDDY_URL" in ok.stdout
    rig.log.unlink()
    refused = rig.run("status", "--ping", env={"CLAUDE_BUDDY_URL": "http://192.168.1.50:8766"})
    assert "no le hablo a ese destino" in refused.stdout and "192.168" not in refused.stdout
    assert not [c for c in rig.calls() if c.startswith("curl")]


class Healthz(BaseHTTPRequestHandler):
    reply = (200, b"ok")
    seen = []

    def do_GET(self):
        Healthz.seen.append((self.path, self.headers.get("Authorization")))
        code, body = Healthz.reply
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def test_ping_with_the_real_curl_against_a_loopback_listener(rig):
    if shutil.which("curl", path="/usr/bin:/bin") is None:
        pytest.skip("no curl here")
    (rig.stubs / "curl").unlink()  # PATH falls through to the system's own
    Healthz.seen = []
    server = HTTPServer(("127.0.0.1", 0), Healthz)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % server.server_port
    try:
        Healthz.reply = (200, b"ok")
        assert "responde (%s, de CLAUDE_BUDDY_URL" % base in rig.run("status", "--ping", env={"CLAUDE_BUDDY_URL": base}).stdout
        Healthz.reply = (200, b"<html>not the buddy</html>")
        assert "contestó HTTP 200" in rig.run("status", "--ping", env={"CLAUDE_BUDDY_URL": base}).stdout
        Healthz.reply = (403, b"unexpected host")
        assert "contestó HTTP 403" in rig.run("status", "--ping", env={"CLAUDE_BUDDY_URL": base}).stdout
    finally:
        server.shutdown()
        server.server_close()
    assert Healthz.seen == [("/healthz", None)] * 3  # one GET per ping and never a token
    with socket.socket() as spare:  # a port nobody listens on
        spare.bind(("127.0.0.1", 0))
        gone = "http://127.0.0.1:%d" % spare.getsockname()[1]
    assert "no responde" in rig.run("status", "--ping", env={"CLAUDE_BUDDY_URL": gone}).stdout


def test_status_shows_the_last_five_log_lines_without_control_characters(rig):
    rig.log_path.parent.mkdir(parents=True)
    rig.log_path.write_text("".join("linea %d\n" % n for n in range(1, 7)) + "rojo \x1b[31mfuego\x07\n")
    done = rig.run("status")
    assert "linea 1" not in done.stdout and "linea 2" not in done.stdout and "linea 3" in done.stdout
    assert "\x1b" not in done.stdout and "\x07" not in done.stdout and "rojo [31mfuego" in done.stdout


# -- the token is looked at, never opened ----------------------------------------------------------------------------

@pytest.mark.parametrize("text,mode,expected", [
    (SECRET_TOKEN, 0o600, "token     ok "),
    (SECRET_TOKEN, 0o400, "token     ok "),
    (SECRET_TOKEN, 0o644, "token     permisos 644 (tiene que ser 600: chmod 600) "),
    (SECRET_TOKEN, 0o660, "token     permisos 660 "),
    (SECRET_TOKEN, 0o606, "token     permisos 606 "),
    ("corto", 0o600, "token     demasiado corto "),
    (SECRET_TOKEN, 0o000, "token     tuyo pero sin permiso de lectura "),
])
def test_token_states_by_owner_mode_and_size_only(rig, text, mode, expected):
    if mode == 0o000 and os.getuid() == 0:
        pytest.skip("root reads anything")
    rig.token(text, mode)
    try:
        done = rig.run("status")
        assert expected in done.stdout
        plan = rig.run("install", "-n").stdout
        assert (plan.count("(ok)") == 1) == expected.startswith("token     ok")
    finally:
        rig.token_path.chmod(0o600)


def test_a_token_that_is_a_folder_is_not_a_token(rig):
    rig.token_path.mkdir(parents=True)
    assert "no es un archivo común" in rig.run("status").stdout


def test_the_plan_says_what_a_missing_token_means(rig):
    plan = rig.run("install", "-n").stdout
    assert "falta: sin un token usable no manda nada" in plan


def test_the_script_never_opens_the_token():
    opens = re.compile(r"\b(cat|head|tail|grep|sed|awk|read|source|curl|wc|xxd|od|base64|cp|mv)\b|<\s*\"?\$\{?TOKEN|\.\s+\"?\$\{?TOKEN")
    for number, line in enumerate(SCRIPT.read_text().splitlines(), 1):
        if "$TOKEN" in line and not line.lstrip().startswith("#"):
            assert not opens.search(line), (number, line)


# -- the script itself -----------------------------------------------------------------------------------------------

def test_the_script_is_executable_and_uses_the_system_bash():
    assert SCRIPT.read_text().startswith("#!/bin/bash\n")
    assert os.access(SCRIPT, os.X_OK)


def test_the_script_parses():
    for shell in {BASH, shutil.which("bash")} - {None}:
        done = subprocess.run([shell, "-n", str(SCRIPT)], capture_output=True, encoding="utf-8")
        assert done.returncode == 0, done.stderr


def test_the_script_passes_shellcheck_when_there_is_one():
    shellcheck = shutil.which("shellcheck")
    if shellcheck is None:
        pytest.skip("no shellcheck here")
    done = subprocess.run([shellcheck, "-s", "bash", "-S", "warning", str(SCRIPT)], capture_output=True, encoding="utf-8")
    assert done.returncode == 0, done.stdout


def test_the_script_sticks_to_what_the_macs_bash_3_2_has():
    code = "\n".join(line for line in SCRIPT.read_text().splitlines() if not line.lstrip().startswith("#"))
    for newer in (r"\b(declare|local|typeset)\s+-[A-Za-z]*[An]", r"\bmapfile\b", r"\breadarray\b", r"\$\{[A-Za-z_]+(,,|\^\^|@[QPAa])\}", r"&>>",
                  r"\|&", r";&", r"\bcoproc\b", r"\bwait\s+-n\b", r"\bread\s+-[a-z]*t\s+[0-9]*\.", r"\$EPOCHSECONDS"):
        assert not re.search(newer, code), newer
    assert "\u2014" not in SCRIPT.read_text() and "\u2013" not in SCRIPT.read_text()  # no dashes in the texts, as in the rest of the repo
