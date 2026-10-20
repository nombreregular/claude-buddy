"""A pretend headless Claude run, and a way to point tools/census.py at it (shared by the process-level census tests).

A plain helper module like page_runner.py, and also a script: `python -I tests/census_machine.py <ps> <pid> once|run`
is tools/census.py with one difference in what its `ps` shows it. The ps is the real one, the table is this machine's
real one, and the decoy is in it; the only change is that the OTHER headless runs of whoever is using the machine are
taken out of the text (<ps> = "mine", <pid> = the decoy; a table of fewer than the 20 rows the census calls sane is padded
with idle filler), or that the call prints its table and then fails ("fail") or comes back short ("short"). Without that, a Mac that is busy with
its own `claude -p` runs fills the eight rows of a snapshot with older runs and the decoy, the youngest, is the one left
out. Everything after `ps` is the census itself: its parser, its rules, its loop, its signal handlers and its HTTP post
run unchanged."""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional

TOOLS = Path(__file__).resolve().parent.parent / "tools"
SELF = Path(__file__).resolve()
PS = "/bin/ps"
DECOY_LIFE_S = 60  # it is killed long before; this only bounds a leftover after a test run that died


def _ps(*args: str) -> str:
    done = subprocess.run([PS, *args], capture_output=True, text=True, timeout=30)
    return done.stdout if done.returncode == 0 else ""


def command_of(pid: int) -> str:
    """What `ps` shows for this pid: "" when it is not there."""
    return _ps("-p", str(pid), "-ww", "-o", "command=").strip()


def start_decoy(folder: Path, prompt: str) -> int:
    """A process that `ps` shows as `claude -c ... -p <prompt>`, started in `folder` and handed to init (ppid 1, or the
    session's subreaper): a decoy that stayed the child of the test would hang below whatever runs the tests, and when
    that is Claude Code or Claude desktop the census rightly takes it for a part of that run and lists nothing.

    It is bash (`exec -a claude bash -c 'sleep N; :'`) and not python: on macOS the python of a venv or of Homebrew starts
    the real interpreter again under its full path, so `exec -a` is lost; bash keeps its argv[0]. The `; :` is there so
    bash does not exec `sleep` away (a run that is one simple command becomes that command)."""
    bash = shutil.which("bash")
    if bash is None:
        raise RuntimeError("no bash to start the decoy with")
    folder.mkdir(parents=True, exist_ok=True)
    script = 'cd "$1" && ( exec -a claude "$4" -c "sleep $3; :" -p "$2" </dev/null >/dev/null 2>&1 & echo $! )'
    done = subprocess.run([bash, "-c", script, "bash", str(folder), prompt, str(DECOY_LIFE_S), bash],
                          capture_output=True, text=True, timeout=30)
    pid = int(done.stdout.strip().splitlines()[-1])
    end = time.monotonic() + 15
    while not command_of(pid).startswith("claude -c "):  # between the fork and the exec `ps` still shows bash
        assert time.monotonic() < end, "the decoy never showed up in ps as `claude`: %r" % command_of(pid)
        time.sleep(0.05)
    return pid


def children_of(pid: int) -> List[int]:
    rows = [line.split() for line in _ps("-U", str(os.getuid()), "-o", "pid=,ppid=").splitlines()]
    return [int(r[0]) for r in rows if len(r) == 2 and r[1] == str(pid)]


def stop_decoy(pid: int) -> None:
    """Kill it without a word, the way a run dies without saying SessionEnd, and wait until `ps` no longer shows it."""
    for child in children_of(pid):  # the `sleep`: killing the parent first would hand it to init for a minute
        try:
            os.kill(child, 9)
        except OSError:
            pass
    try:
        os.kill(pid, 9)
    except OSError:
        pass
    end = time.monotonic() + 10
    while command_of(pid).startswith("claude"):  # a zombie has no command, a gone process has no row
        assert time.monotonic() < end, "the decoy did not die"
        time.sleep(0.05)


def drive(argv: List[str]) -> int:
    """The script: tools/census.py with the `ps` of ps_mode."""
    ps_mode, keep, rest = argv[0], int(argv[1]), argv[2:]
    sys.path.insert(0, str(TOOLS))
    import census

    real_read_ps = census.read_ps

    def foreign_run(line: str) -> bool:
        return any(p.headless and p.pid != keep for p in census.parse_ps(line))

    def mine(stop=None, timeout=census.PS_TIMEOUT_S) -> Optional[str]:
        text = real_read_ps(stop, timeout)
        if text is None:
            return None
        lines = [line for line in text.splitlines(True) if not foreign_run(line)]
        # a user alone on a machine (a container) has fewer rows than the census believes: `ps` failed, it would say
        lines += ["%7d 1 01:00 /bin/filler%d\n" % (4_000_000 + n, n) for n in range(max(0, census.MIN_PROCS_SANE + 5 - len(lines)))]
        return "".join(lines)

    def short(stop=None, timeout=census.PS_TIMEOUT_S) -> Optional[str]:
        text = real_read_ps(stop, timeout)
        return None if text is None else "".join(text.splitlines(True)[:10])

    if ps_mode == "mine":
        census.read_ps = mine
    elif ps_mode == "short":
        census.read_ps = short
    elif ps_mode == "fail":  # the real read_ps, whose ps prints the whole table and then exits 1: a table it must not trust
        real_popen = subprocess.Popen
        census.subprocess.Popen = lambda cmd, **kw: real_popen(["/bin/sh", "-c", '"$@"; exit 1', "sh"] + list(cmd), **kw)
    else:
        raise SystemExit("ps_mode: mine, short or fail")
    return census.main(["census.py"] + rest)


if __name__ == "__main__":
    sys.exit(drive(sys.argv[1:]))
