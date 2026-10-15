#!/home/jgo/usr/venv-3.11/bin/python
"""restart_engine.py — restart the longshort-server on one port after a cache publish and verify /api/config (2H, DAG task restart_engine).
Production caller (outside this repo): /home/jgo/universe_builder/dag/run_step.sh -> restart_engine.sh -> this file. Keep the CLI stable.

Generalises ~/longshort_workspace/research/universe_v2_20261002/V2DEPLOY/rename/restart.py (left untouched):
SIGTERM -> restart -> /api/config check -> roll back to the previous launch on failure.

  restart_engine.py [--config PATH] [--port N] [--timeout S] [--force] [--stop] [--binary PATH] [--log-dir DIR]
                    [--asof D] [--build DIR] [--prev DIR]
  defaults: ~/longshort_workspace/config.yaml, 50007, 600 s,
            ~/longshort_workspace/.longshort/build/longshort-server and .longshort/logs.
            --build/--prev (STEP_CONTRACT common args) are accepted and ignored.

1. Pre-flight (nothing is stopped when it fails; a RESULT line is always printed): parse the yaml (must be a mapping).
   Expected asOf = --asof if given (a different manifest asOfDate is logged as WARN), else <base.cache>/manifest.json asOfDate,
   then asOf, else the max of base_data.parquet row-group `date` statistics (the readiness_probe / update_daily rule, logged as
   WARN). Expected simEnd = asOf when backtest.end_date is latest/absent or a fixed date >= asOf (a fixed earlier end_date only
   requires simEnd <= end_date). Expected universes = the yaml universe.names (the engine reports exactly that list, [] without
   names); every name must exist in the published universe.parquet (published names the yaml does not select are only logged).
2. Find every server process for --port: the pid file <log-dir>/engine_<port>.pid, the owners of LISTEN sockets on the port, and a
   /proc scan of this user's processes running --binary with LONGSHORT_PORT=<port> in their environment (catches a server that is
   still loading and has no pid file). A process counts only if its executable (argv[0]/argv[1] for interpreter-run binaries) is
   --binary and it serves --port (LONGSHORT_PORT env or socket owner). A LISTEN socket on the port held by anything else ->
   exit 1, nothing stopped. Servers on other ports are never signalled.
3. Idempotent: without --force, when exactly one server is running --binary with this --config, its /api/config already reports
   the expected simEnd and universes, and it did not start more than 60 s before manifest.json was last written (process start
   from /proc is ~1 s coarse) -> RESULT action=already_serving,
   exit 0, nothing restarted. (A republish with the same asOf has a newer manifest.json and therefore still restarts.)
4. SIGTERM every old server process and wait up to 60 s (no SIGKILL: if one does not exit, exit 1 with it still running).
   Nothing is started while the port is still in LISTEN (the engine binds with SO_REUSEPORT, so a second server would silently
   share the port).
5. Start `<binary> <config>` (cwd = old server's cwd, else the config directory; new session; stdout/stderr appended to
   <log-dir>/engine_<port>.log, which also receives this script's lines), write the pid file, poll /api/config (0.5 s, after
   10 s every 2 s) up to --timeout; every LISTEN socket on the port must belong to the new pid. Verify simEnd and universes -> 0.
   Environment: after a restart the new server and any rollback get the old process's /proc/<pid>/environ with LONGSHORT_PORT
   set to --port (env fidelity: that file is the environment the old process was exec'd with — later setenv calls inside the
   process are not visible — and it is readable only for this user's processes; the engine itself reads only LONGSHORT_PORT).
   A cold start uses this script's environment plus LONGSHORT_PORT.
6. On failure: stop the new process, relaunch the old server's exact argv/cwd/environ (the previous configuration), wait for its
   /api/config -> RESULT action=rolled_back, or action=rollback_failed (with pid) when the relaunch dies or never serves; exit 1.
   Cold start failure: stop the new process, action=failed, exit 1.
   Note: when the old server ran the same yaml, the relaunch reads whatever that yaml points to now (the new publish);
   restoring the previous data is a publish rollback (cache_v2_cash_1d.prev), not part of this script.
7. SIGTERM to this script (e.g. an Airflow timeout) raises inside it: before any server was stopped -> action=terminated; after the
   old server was stopped -> the new process is stopped, the previous launch is relaunched without waiting for it
   (action=rollback_started). Later SIGTERMs are ignored by the handler (SIGKILL still works; children get the default action).
--stop: SIGTERM every identified server process and wait (exit 0); exit 4 when nothing serves the port.
Last stdout line: `RESULT key=value ...` (STEP_CONTRACT). Exit 0 ok or already serving / 1 failed / 4 nothing to stop.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]                 # .../cpp-engine
DS = ENGINE / "examples" / "datasets"
DEFAULT_WORKSPACE = Path.home() / "longshort_workspace"
DEFAULT_CONFIG = DEFAULT_WORKSPACE / "config.yaml"
DEFAULT_BINARY = DEFAULT_WORKSPACE / ".longshort" / "build" / "longshort-server"
DEFAULT_LOG_DIR = DEFAULT_WORKSPACE / ".longshort" / "logs"
STOP_GRACE_S = 60
PORT_FREE_S = 15
REPUBLISH_SLACK_S = 60     # process start from /proc (btime + ticks) is ~1 s coarse; republishes are hours apart


class Preflight(RuntimeError):
    """A check before anything was stopped failed."""


class StepFail(RuntimeError):
    """The new server did not come up or failed verification."""


class OldAlive(RuntimeError):
    """An old server did not exit after SIGTERM (it is still running)."""


class PortBusy(RuntimeError):
    """The port is still in LISTEN after the old servers exited."""


class Terminated(BaseException):
    """SIGTERM received by this script (BaseException: not swallowed by `except Exception`)."""


# ---------------------------------------------------------------- SIGTERM handling
class _Sig:
    count = 0          # SIGTERMs received
    shielded = 0       # >0 while spawning/signalling: the raise is deferred to the end of the block
    pending = False


def _on_sigterm(signum, frame):
    _Sig.count += 1
    if _Sig.count > 1:
        return                                  # finish the rollback / RESULT; a Python handler is reset to default in children
    if _Sig.shielded:
        _Sig.pending = True
        return
    raise Terminated()


@contextlib.contextmanager
def shield():
    """Defer a SIGTERM raise until a spawn/kill and its bookkeeping are complete (no untracked server on the port)."""
    _Sig.shielded += 1
    try:
        yield
    finally:
        _Sig.shielded -= 1
    if _Sig.pending and not _Sig.shielded:
        _Sig.pending = False
        raise Terminated()


@dataclass
class Expected:
    asof: str
    asof_src: str
    sim_end: str | None                 # None: fixed end_date before asOf -> only simEnd <= end_date
    end_date: str
    universes: list[str]
    published: list[str] = field(default_factory=list)
    manifest: Path | None = None


@dataclass
class Proc:
    pid: int
    argv: list[str]
    cwd: str
    exe: str
    env: dict
    start: str                          # /proc/<pid>/stat starttime (clock ticks since boot): guards against pid reuse

    @property
    def env_port(self) -> str | None:
        return self.env.get("LONGSHORT_PORT")


class Log:
    def __init__(self, path: Path):
        self.path = path

    def __call__(self, msg: str):
        line = f"[restart_engine {dt.datetime.now().astimezone():%Y-%m-%d %H:%M:%S %Z}] {msg}"
        print(line, flush=True)
        with self.path.open("a") as f:
            f.write(line + "\n")


# ---------------------------------------------------------------- config / published cache
def _path(cfg: Path, p) -> Path | None:
    """Engine rule: config_dir / path, lexically normalised (absolute paths stay as they are)."""
    return Path(os.path.normpath(cfg.parent / str(p))) if p else None


def _map(doc: dict, key: str) -> dict:
    v = doc.get(key)
    return v if isinstance(v, dict) else {}


def base_data_max_date(cache: Path) -> str:
    """Max of the `date` row-group statistics of base_data.parquet (same rule as readiness_probe / update_daily as_of)."""
    import pyarrow.parquet as pq
    pf = pq.ParquetFile(cache / "base_data.parquet")
    i = pf.schema_arrow.get_field_index("date")
    if i < 0:
        raise ValueError("base_data.parquet has no date column")
    mx = None
    for rg in range(pf.num_row_groups):
        s = pf.metadata.row_group(rg).column(i).statistics
        if s is not None and s.has_min_max:
            mx = s.max if mx is None else max(mx, s.max)
    if mx is None:
        raise ValueError("base_data.parquet has no date statistics")
    return str(mx)[:10]


def expected_state(cfg: Path, asof_arg: str | None = None, log=print) -> Expected:
    import yaml
    try:
        doc = yaml.safe_load(cfg.read_text())
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise Preflight(f"cannot parse {cfg}: {' '.join(str(exc).split())}") from exc
    if not isinstance(doc, dict):
        raise Preflight(f"{cfg}: top level is {type(doc).__name__}, expected a mapping")
    cache = _path(cfg, _map(doc, "base").get("cache"))
    if cache is None:
        raise Preflight(f"{cfg}: no base.cache (thin-v1 cache directory) -> cannot read the published manifest")
    man_p = cache / "manifest.json"
    try:
        man = json.loads(man_p.read_text())
        if not isinstance(man, dict):
            raise ValueError("not a JSON object")
    except (OSError, ValueError) as exc:
        if asof_arg is None:
            raise Preflight(f"cannot read {man_p}: {exc}") from exc
        log(f"WARN cannot read {man_p}: {exc}")
        man, man_p = {}, None
    man_asof = next((str(man[k])[:10] for k in ("asOfDate", "asOf") if man.get(k)), None)
    if asof_arg is not None:
        try:
            asof, src = str(dt.date.fromisoformat(asof_arg)), "--asof"
        except ValueError as exc:
            raise Preflight(f"bad --asof {asof_arg!r}: {exc}") from exc
        if man_asof is not None and man_asof != asof:
            log(f"WARN --asof {asof} differs from {man_p} asOfDate {man_asof}")
    elif man_asof is not None:
        asof, src = man_asof, "manifest.json:" + ("asOfDate" if man.get("asOfDate") else "asOf")
    else:
        try:
            asof, src = base_data_max_date(cache), "base_data.parquet:max(date)"
        except Exception as exc:  # noqa: BLE001
            raise Preflight(f"{man_p} has no asOfDate and base_data.parquet date statistics are unreadable: {exc}") from exc
        log(f"WARN {man_p} has no asOfDate/asOf; expected asOf taken from {cache / 'base_data.parquet'} date statistics = {asof}")
    uni = _map(doc, "universe")
    names = [str(n) for n in (uni.get("names") or [])]
    published: list[str] = []
    if names:
        upath = _path(cfg, uni.get("path"))
        if upath is None:
            raise Preflight(f"{cfg}: universe.names without universe.path")
        try:
            import pyarrow.compute as pc
            import pyarrow.parquet as pq
            published = sorted(pc.unique(pq.read_table(upath, columns=["universe"])["universe"]).to_pylist())
        except Exception as exc:  # noqa: BLE001
            raise Preflight(f"cannot read universe names from {upath}: {exc}") from exc
        missing = [n for n in names if n not in published]
        if missing:
            raise Preflight(f"yaml universe.names not in the published {upath.name}: {missing} (published: {published})")
    end = _map(doc, "backtest").get("end_date")
    end = "latest" if end in (None, "", "latest") else str(end)[:10]
    sim_end = asof if end == "latest" or end >= asof else None
    return Expected(asof=asof, asof_src=src, sim_end=sim_end, end_date=end, universes=names, published=published, manifest=man_p)


# ---------------------------------------------------------------- process identification (/proc, own processes only)
def proc_info(pid: int) -> Proc | None:
    """Live, readable, non-zombie process -> Proc; gone/zombie/unreadable -> None."""
    base = Path(f"/proc/{pid}")
    try:
        fields = (base / "stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] in ("Z", "X"):
            return None
        argv = [a.decode(errors="replace") for a in (base / "cmdline").read_bytes().split(b"\0")[:-1]]
        cwd = os.readlink(base / "cwd")
        exe = os.readlink(base / "exe").removesuffix(" (deleted)")
        env = {}
        for kv in (base / "environ").read_bytes().split(b"\0"):
            if b"=" in kv:
                k, v = kv.split(b"=", 1)
                env[k.decode(errors="replace")] = v.decode(errors="replace")
        return Proc(pid, argv, cwd, exe, env, fields[19])
    except (FileNotFoundError, ProcessLookupError, PermissionError, IndexError):
        return None


def alive(p: Proc) -> bool:
    q = proc_info(p.pid)
    return q is not None and q.start == p.start


def start_epoch(p: Proc) -> float:
    btime = next(int(ln.split()[1]) for ln in Path("/proc/stat").read_text().splitlines() if ln.startswith("btime "))
    return btime + int(p.start) / os.sysconf("SC_CLK_TCK")


def listening(port: int) -> tuple[set[str], set[int]]:
    """(socket inodes in LISTEN on the port, pids of this user's processes owning them)."""
    inodes = set()
    for f in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = Path(f).read_text().splitlines()[1:]
        except OSError:
            continue
        for ln in lines:
            parts = ln.split()
            if len(parts) > 9 and parts[3] == "0A" and int(parts[1].rsplit(":", 1)[1], 16) == port:
                inodes.add(parts[9])
    owners = set()
    if inodes:
        targets = {f"socket:[{i}]" for i in inodes}
        for d in Path("/proc").iterdir():
            if d.name.isdigit() and sockets_of(int(d.name)) & targets:
                owners.add(int(d.name))
    return inodes, owners


def sockets_of(pid: int) -> set[str]:
    out = set()
    try:
        fds = list(Path(f"/proc/{pid}/fd").iterdir())
    except OSError:
        return out
    for fd in fds:
        try:
            out.add(os.readlink(fd))
        except OSError:
            pass
    return out


def is_binary(p: Proc, binary: Path) -> bool:
    want = os.path.realpath(binary)
    cands = [p.exe] + [os.path.join(p.cwd, a) for a in p.argv[:2]]
    return want in {os.path.realpath(c) for c in cands}


def config_of(p: Proc, binary: Path) -> str | None:
    """The config path the server process was started with (argument after the binary)."""
    want = os.path.realpath(binary)
    for i, a in enumerate(p.argv[:2]):
        if os.path.realpath(os.path.join(p.cwd, a)) == want and i + 1 < len(p.argv):
            return os.path.realpath(os.path.join(p.cwd, p.argv[i + 1]))
    if os.path.realpath(p.exe) == want and len(p.argv) > 1:
        return os.path.realpath(os.path.join(p.cwd, p.argv[1]))
    return None


def find_servers(port: int, binary: Path, pid_file: Path) -> list[Proc]:
    """All server processes for `port`; socket owners first, then the pid file, then still-loading ones from the /proc scan."""
    inodes, owners = listening(port)
    cands = sorted(owners)
    if pid_file.exists():
        try:
            cands.append(int(pid_file.read_text().split()[0]))
        except (ValueError, IndexError):
            pass
    tag = os.path.basename(binary).encode()
    for d in Path("/proc").iterdir():                       # still loading (no socket yet) and no pid file
        if d.name.isdigit():
            try:
                if tag in (d / "cmdline").read_bytes():
                    cands.append(int(d.name))
            except OSError:
                pass
    found, seen = [], set()
    for pid in cands:
        if pid in seen or pid == os.getpid():
            continue
        seen.add(pid)
        p = proc_info(pid)
        if p and is_binary(p, binary) and (p.env_port == str(port) or pid in owners):
            found.append(p)
    held = set().union(*(sockets_of(p.pid) for p in found)) if found else set()
    foreign = [i for i in inodes if f"socket:[{i}]" not in held]
    if foreign:
        others = sorted(owners - {p.pid for p in found})
        who = f"pid {others}" if others else "a process this user cannot see"
        raise Preflight(f"port {port} has LISTEN sockets held by {who}, not by {binary}; nothing stopped")
    return found


def get_config(port: int, timeout: float = 10.0) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/config", timeout=timeout) as r:
        return json.load(r)


def summary(c: dict | None) -> str:
    if not c:
        return "none"
    return f"simEnd={c.get('simEnd')} T={c.get('T')} N={c.get('N')} universes={c.get('universes')}"


# ---------------------------------------------------------------- stop / start / wait
def stop_old(p: Proc, binary: Path, log, signalled: list, grace: float = STOP_GRACE_S) -> bool:
    """SIGTERM a verified server (identity re-checked just before the signal) and wait for it to disappear."""
    with shield():
        now = proc_info(p.pid)
        if now is None or now.start != p.start:
            return True
        if not is_binary(now, binary):
            raise Preflight(f"pid {p.pid} changed identity before SIGTERM; nothing stopped")
        log(f"SIGTERM pid={p.pid}")
        os.kill(p.pid, signal.SIGTERM)
        signalled.append(p.pid)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not alive(p):
            return True
        time.sleep(0.2)
    return False


def stop_child(proc: subprocess.Popen, log):
    if proc.poll() is None:
        log(f"stopping new process pid={proc.pid}")
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def wait_port_free(port: int, timeout: float = PORT_FREE_S) -> bool:
    deadline = time.monotonic() + timeout
    while listening(port)[0]:
        if time.monotonic() > deadline:
            return False
        time.sleep(0.2)
    return True


def spawn(argv: list[str], cwd: str, env: dict, port: int, log_path: Path, pid_file: Path) -> subprocess.Popen:
    """Call inside shield(): the caller must hold the returned Popen before a SIGTERM may raise."""
    with log_path.open("ab") as lf:
        proc = subprocess.Popen(argv, cwd=cwd, env=dict(env, LONGSHORT_PORT=str(port)), stdin=subprocess.DEVNULL,
                                stdout=lf, stderr=subprocess.STDOUT, start_new_session=True)
    pid_file.write_text(f"{proc.pid}\n")
    return proc


def wait_config(proc: subprocess.Popen, port: int, timeout: float) -> dict:
    t0 = time.monotonic()
    while True:
        rc = proc.poll()
        if rc is not None:
            raise StepFail(f"server pid={proc.pid} exited rc={rc} before serving /api/config (see log)")
        try:
            c = get_config(port)
            inodes, mine = listening(port)[0], sockets_of(proc.pid)
            if not inodes or any(f"socket:[{i}]" not in mine for i in inodes):
                raise StepFail(f"port {port} has LISTEN sockets not owned by pid={proc.pid} (SO_REUSEPORT sharing?)")
            return c
        except urllib.error.HTTPError as exc:
            raise StepFail(f"/api/config HTTP {exc.code}: {exc.read()[:300]!r}") from exc
        except (urllib.error.URLError, OSError, ValueError):
            pass
        el = time.monotonic() - t0
        if el > timeout:
            raise StepFail(f"/api/config not ready after {timeout:.0f}s")
        time.sleep(0.5 if el < 10 else 2)


def verify(c: dict, exp: Expected) -> list[str]:
    bad = []
    got_end = c.get("simEnd")
    if exp.sim_end is not None and got_end != exp.sim_end:
        bad.append(f"simEnd {got_end} != expected {exp.sim_end} (asOf from {exp.asof_src})")
    if exp.sim_end is None and not (isinstance(got_end, str) and got_end <= exp.end_date):
        bad.append(f"simEnd {got_end} not <= backtest.end_date {exp.end_date}")
    got_u = c.get("universes") or []
    if set(got_u) != set(exp.universes) or len(got_u) != len(set(got_u)):
        bad.append(f"universes {sorted(got_u)} != expected {sorted(exp.universes)}")
    return bad


def restart_reason(olds: list[Proc], before: dict | None, exp: Expected, binary: Path, cfg: Path) -> str | None:
    """None when the running server already serves this config and this publish; else why a restart is needed."""
    if len(olds) != 1:
        return f"{len(olds)} server processes on the port"
    if before is None:
        return "old server does not answer /api/config (still loading?)"
    running = config_of(olds[0], binary)
    if running != os.path.realpath(cfg):
        return f"old server runs config {running}, requested {cfg}"
    bad = verify(before, exp)
    if bad:
        return "; ".join(bad)
    if exp.manifest is not None and exp.manifest.exists() and start_epoch(olds[0]) < exp.manifest.stat().st_mtime - REPUBLISH_SLACK_S:
        return f"old server started before {exp.manifest} was last written (republished)"
    return None


def result(**kv) -> None:
    print("RESULT " + " ".join(f"{k}={'' if v is None else v}" for k, v in kv.items()), flush=True)


# ---------------------------------------------------------------- actions
def do_stop(a, binary: Path, pid_file: Path, log) -> int:
    olds = find_servers(a.port, binary, pid_file)
    if not olds:
        log(f"nothing serves port {a.port}; nothing to stop")
        pid_file.unlink(missing_ok=True)
        result(port=a.port, action="none", pid=None, rc=4)
        return 4
    for p in olds:
        log(f"stopping pid={p.pid} argv={p.argv} cwd={p.cwd}")
        if not stop_old(p, binary, log, []):
            log(f"FAIL pid={p.pid} did not exit within {STOP_GRACE_S}s (still running)")
            result(port=a.port, action="stop_failed", pid=p.pid, rc=1)
            return 1
        log(f"stopped pid={p.pid}")
    pid_file.unlink(missing_ok=True)
    result(port=a.port, action="stopped", pid=",".join(str(p.pid) for p in olds), rc=0)
    return 0


def do_restart(a, binary: Path, cfg: Path, log_path: Path, pid_file: Path, log) -> int:
    if not (binary.is_file() and os.access(binary, os.X_OK)):
        raise Preflight(f"binary not executable: {binary}")
    if not cfg.is_file():
        raise Preflight(f"config not found: {cfg}")
    exp = expected_state(cfg, a.asof, log)
    extra = sorted(set(exp.published) - set(exp.universes))
    log(f"expected: asOf={exp.asof} ({exp.asof_src}) simEnd={exp.sim_end or '<= ' + exp.end_date} "
        f"universes={exp.universes}" + (f" (published but not selected by the yaml: {extra})" if extra else ""))
    olds = find_servers(a.port, binary, pid_file)
    old = olds[0] if olds else None
    before = None
    if old is not None:
        try:
            before = get_config(a.port)
        except (urllib.error.URLError, OSError, ValueError):
            before = None
        for p in olds:
            log(f"old server pid={p.pid} argv={p.argv} cwd={p.cwd}")
        log(f"old /api/config: {summary(before)}" + (f" (WARN {len(olds)} server processes for one port)" if len(olds) > 1 else ""))
        why = restart_reason(olds, before, exp, binary, cfg)
        if why is None and not a.force:
            log(f"already serving: pid={old.pid} {summary(before)}; nothing restarted (use --force to restart anyway)")
            result(port=a.port, action="already_serving", pid=old.pid, sim_end=before.get("simEnd"), expected_sim_end=exp.sim_end,
                   asof_src=exp.asof_src, universes=len(before.get("universes") or []), rc=0)
            return 0
        log("restart: " + ("--force" if why is None else why))
    else:
        log(f"no server on port {a.port}: cold start")
    signalled: list[int] = []
    proc = None
    try:
        for p in olds:
            if not stop_old(p, binary, log, signalled):
                raise OldAlive(f"old server pid={p.pid} did not exit within {STOP_GRACE_S}s; it is still running, nothing started")
            log(f"old server pid={p.pid} exited")
        if not wait_port_free(a.port):
            raise PortBusy(f"port {a.port} still in LISTEN {PORT_FREE_S}s after the old server exited; nothing started "
                           f"(the engine uses SO_REUSEPORT, a second server would share the port)")
        argv, cwd = [str(binary), str(cfg)], (old.cwd if old else str(cfg.parent))
        env = old.env if old else dict(os.environ)
        with shield():
            proc = spawn(argv, cwd, env, a.port, log_path, pid_file)
        log(f"started pid={proc.pid} argv={argv} cwd={cwd} env={'old process environ' if old else 'this script'} "
            f"LONGSHORT_PORT={a.port}; waiting up to {a.timeout:.0f}s for /api/config")
        c = wait_config(proc, a.port, a.timeout)
        bad = verify(c, exp)
        if bad:
            raise StepFail("; ".join(bad))
    except OldAlive as exc:
        log(f"FAIL {exc}")
        result(port=a.port, action="stop_failed", pid=None, rc=1)
        return 1
    except PortBusy as exc:
        log(f"FAIL {exc}")
        pid_file.unlink(missing_ok=True)
        result(port=a.port, action="port_busy", pid=None, old_pid=old.pid if old else None, rc=1)
        return 1
    except BaseException as exc:  # noqa: BLE001  (StepFail, SIGTERM, Ctrl-C, unexpected: never leave the port without the old launch)
        return recover(exc, a, olds, signalled, proc, before, exp, log_path, pid_file, log)
    log(f"OK pid={proc.pid} {summary(c)}")
    result(port=a.port, action="restarted" if old else "started", pid=proc.pid, old_pid=old.pid if old else None,
           sim_end=c.get("simEnd"), expected_sim_end=exp.sim_end, asof_src=exp.asof_src, universes=len(c.get("universes") or []), rc=0)
    return 0


def recover(exc, a, olds, signalled, proc, before, exp, log_path, pid_file, log) -> int:
    terminated = isinstance(exc, Terminated)
    log("FAIL " + ("SIGTERM received" if terminated else f"{type(exc).__name__}: {exc}"))
    if proc is not None:
        stop_child(proc, log)
    old = olds[0] if olds else None
    if old is None:
        pid_file.unlink(missing_ok=True)
        log("cold start failed; nothing running on the port")
        result(port=a.port, action="terminated" if terminated else "failed", pid=None, expected_sim_end=exp.sim_end, rc=1)
        return 1
    running = [p.pid for p in olds if alive(p)]
    if running:            # interrupted before every old server was stopped: never start a second server next to one
        log(f"old server(s) {running} still running (signalled: {signalled}); no relaunch")
        result(port=a.port, action="terminated" if terminated else "aborted", pid=running[0], rc=1)
        return 1
    if not wait_port_free(a.port):
        log(f"FAIL port {a.port} still in LISTEN after stopping the new process; rollback not started")
        pid_file.unlink(missing_ok=True)
        result(port=a.port, action="rollback_port_busy", pid=None, old_pid=old.pid, expected_sim_end=exp.sim_end, rc=1)
        return 1
    with shield():
        r = spawn(old.argv, old.cwd, old.env, a.port, log_path, pid_file)
    log(f"ROLLBACK: relaunched previous argv={old.argv} cwd={old.cwd} (old process environ) as pid={r.pid}")
    if terminated:
        result(port=a.port, action="rollback_started", pid=r.pid, old_pid=old.pid, expected_sim_end=exp.sim_end, rc=1)
        return 1
    try:
        c = wait_config(r, a.port, a.timeout)
    except StepFail as exc2:
        log(f"FAIL rollback server pid={r.pid} not serving: {exc2}")
        result(port=a.port, action="rollback_failed", pid=r.pid, old_pid=old.pid, expected_sim_end=exp.sim_end, rc=1)
        return 1
    except Terminated:
        log(f"SIGTERM while waiting for the rollback server pid={r.pid}; left running")
        result(port=a.port, action="rollback_started", pid=r.pid, old_pid=old.pid, expected_sim_end=exp.sim_end, rc=1)
        return 1
    same = before is None or all(c.get(k) == before.get(k) for k in ("simEnd", "universes", "T", "N"))
    log(f"rollback serving: {summary(c)}" + ("" if same else f" (differs from pre-restart {summary(before)})"))
    result(port=a.port, action="rolled_back", pid=r.pid, old_pid=old.pid, expected_sim_end=exp.sim_end, rc=1)
    return 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help=f"engine yaml (default {DEFAULT_CONFIG})")
    ap.add_argument("--port", type=int, default=50007, help="LONGSHORT_PORT of the server to restart (default 50007)")
    ap.add_argument("--timeout", type=float, default=600, help="seconds to wait for /api/config after a start (default 600)")
    ap.add_argument("--force", action="store_true", help="restart even if the server already serves the expected state")
    ap.add_argument("--stop", action="store_true", help="only stop the server on --port (exit 4 if none)")
    ap.add_argument("--binary", type=Path, default=DEFAULT_BINARY, help=f"server binary (default {DEFAULT_BINARY})")
    ap.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR, help="engine_<port>.log/.pid/.lock directory")
    ap.add_argument("--asof", default=None, help="expected simEnd (STEP_CONTRACT --asof); default: published manifest asOfDate")
    ap.add_argument("--build", default=None, help="STEP_CONTRACT common argument (ignored)")
    ap.add_argument("--prev", default=None, help="STEP_CONTRACT common argument (ignored)")
    a = ap.parse_args(argv)
    cfg = Path(os.path.abspath(a.config))
    binary = Path(os.path.abspath(a.binary))
    a.log_dir.mkdir(parents=True, exist_ok=True)
    log_path, pid_file = a.log_dir / f"engine_{a.port}.log", a.log_dir / f"engine_{a.port}.pid"
    log = Log(log_path)
    signal.signal(signal.SIGTERM, _on_sigterm)
    with (a.log_dir / f"engine_{a.port}.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log(f"FAIL another restart_engine holds {lock.name}")
            result(port=a.port, action="locked", rc=1)
            return 1
        try:
            log(f"{'stop' if a.stop else 'restart'} port={a.port} config={cfg} binary={binary} timeout={a.timeout:.0f}s"
                + (f" asof={a.asof}" if a.asof else "") + (" force" if a.force else ""))
            if a.stop:
                return do_stop(a, binary, pid_file, log)
            return do_restart(a, binary, cfg, log_path, pid_file, log)
        except Preflight as exc:
            log(f"FAIL pre-flight: {exc}")
            result(port=a.port, action="preflight_failed", rc=1)
            return 1
        except (Terminated, KeyboardInterrupt) as exc:
            log(f"FAIL interrupted ({type(exc).__name__}); no server was started by this run")
            result(port=a.port, action="terminated", rc=1)
            return 1
        except Exception as exc:  # noqa: BLE001
            log(f"FAIL unexpected {type(exc).__name__}: {' '.join(str(exc).split())}")
            result(port=a.port, action="error", rc=1)
            return 1


if __name__ == "__main__":
    sys.exit(main())
