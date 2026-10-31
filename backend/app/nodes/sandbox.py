"""Process sandbox for user code (Code node, Code Tool, every Code node language).

User code never runs inside the server process. Each run happens in a child process that

- gets a minimal, allowlisted environment (no ENCRYPTION_KEY, INTERNAL_TOKEN, DATABASE_URL, SMTP_*, ...);
- is killed (with every process it started) when it exceeds ``code_timeout_seconds``;
- is capped at ``code_memory_mb`` (a Windows Job Object, RLIMIT_AS elsewhere);
- for Python: has no access to the app's modules (``-I -S``, only the stdlib and installed packages on
  sys.path), cannot write files, start processes, load native code (ctypes) or read files outside the Python
  installation, and cannot connect to private / loopback / metadata addresses (same policy as netguard),
  enforced by an audit hook installed before the user's code is compiled.

Python runs in a pool of warm worker processes (``PythonPool``): a worker is bound to the first user (workflow
owner) it serves and is only ever reused for that same user, so no state leaks between users. Workers are
recycled after ``MAX_USES`` runs, after a timeout, a crash or a cancelled run, and when idle.

Audit hooks are defence in depth, not a perfect jail: for hostile multi-tenant installs run the backend in a
container / as a low-privilege OS user as well.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..config import get_settings

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

_ENV_ALLOW = {
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "HOME", "USERPROFILE", "HOMEDRIVE",
    "HOMEPATH", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432",
    "COMMONPROGRAMFILES", "COMMONPROGRAMFILES(X86)", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS",
    "LANG", "LANGUAGE", "TZ", "TERM", "USER", "USERNAME", "LOGNAME", "SHELL",
    # runtimes' own needs
    "JAVA_HOME", "GOROOT", "GOPATH", "GOCACHE", "GOMODCACHE", "GOFLAGS", "GOPROXY", "NODE_PATH", "PERL5LIB",
    "RUBYLIB", "GEM_HOME", "GEM_PATH", "PHPRC", "DOTNET_ROOT", "SSL_CERT_FILE", "SSL_CERT_DIR",
}


def minimal_env(tmp: str | os.PathLike | None = None, extra: dict | None = None) -> dict:
    """Allowlisted environment for user code: enough to find and run a runtime, no app secrets."""
    env = {k: v for k, v in os.environ.items() if k.upper() in _ENV_ALLOW or k.upper().startswith("LC_")}
    if tmp is not None:
        env.update(TEMP=str(tmp), TMP=str(tmp), TMPDIR=str(tmp))
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(extra or {})
    return env


# ---------------------------------------------------------------------------
# Resource limits + killing whole process trees
# ---------------------------------------------------------------------------

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in ("r", "w", "o", "rb", "wb", "ob")]

    class _BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

    class _EXTENDED(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _BASIC), ("IoInfo", _IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]


class Limits:
    """Memory cap + process-tree kill for one child process (Job Object on Windows, rlimit + session elsewhere)."""

    def __init__(self, memory_mb: int | None = None):
        self.memory_mb = memory_mb if memory_mb is not None else get_settings().code_memory_mb
        self._job = None

    def popen_kwargs(self) -> dict:
        if sys.platform == "win32":
            return {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
        mb = self.memory_mb

        def preexec():  # pragma: no cover - POSIX only
            import resource

            if mb and mb > 0:
                resource.setrlimit(resource.RLIMIT_AS, (mb * 1024 * 1024, mb * 1024 * 1024))
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

        return {"start_new_session": True, "preexec_fn": preexec}

    def attach(self, proc: subprocess.Popen) -> None:
        if sys.platform != "win32":
            return
        job = _k32.CreateJobObjectW(None, None)
        if not job:
            return
        info = _EXTENDED()
        flags = 0x2000 | 0x400  # KILL_ON_JOB_CLOSE | DIE_ON_UNHANDLED_EXCEPTION
        if self.memory_mb and self.memory_mb > 0:
            flags |= 0x100  # LIMIT_PROCESS_MEMORY
            info.ProcessMemoryLimit = self.memory_mb * 1024 * 1024
        info.BasicLimitInformation.LimitFlags = flags
        _k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
        _k32.AssignProcessToJobObject(job, int(proc._handle))  # type: ignore[attr-defined]
        self._job = job

    def kill(self, proc: subprocess.Popen) -> None:
        try:
            if sys.platform == "win32":
                if self._job:
                    _k32.TerminateJobObject(self._job, 1)
                proc.kill()
            else:  # pragma: no cover
                import signal

                os.killpg(proc.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass

    def close(self) -> None:
        if sys.platform == "win32" and self._job:
            _k32.CloseHandle(self._job)  # KILL_ON_JOB_CLOSE: anything left in the job dies
            self._job = None


class Timeout(Exception):
    pass


def run_limited(argv, *, cwd, env, timeout: float, shell: bool = False, memory_mb: int | None = None):
    """subprocess.run with a memory cap; on timeout the whole process tree is killed. Returns CompletedProcess."""
    lim = Limits(memory_mb)
    proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", shell=shell,
                            **lim.popen_kwargs())
    lim.attach(proc)
    try:
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            lim.kill(proc)
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            raise Timeout(f"Timed out after {timeout:.0f}s") from None
        return subprocess.CompletedProcess(argv, proc.returncode, out, err)
    finally:
        lim.close()


def code_timeout(requested=None) -> float:
    """The node's timeout, never above the server's CODE_TIMEOUT_SECONDS."""
    cap = float(get_settings().code_timeout_seconds or 60)
    try:
        t = float(requested) if requested not in (None, "") else cap
    except (TypeError, ValueError):
        t = cap
    return max(1.0, min(t, cap))


# ---------------------------------------------------------------------------
# Python worker
# ---------------------------------------------------------------------------

_RUNNER = r'''
import sys, os, json, io, builtins, traceback, contextlib
_cfg = json.loads(sys.stdin.readline())
sys.path[:] = _cfg["sys_path"]
import math, re, datetime as _dt, asyncio, copy, ipaddress, socket
import httpx
_proto = os.fdopen(os.dup(1), "w", encoding="utf-8")
os.dup2(2, 1)                       # stray writes to fd 1 can't corrupt the protocol
_in = sys.stdin
sys.stdin = io.StringIO()
_loop = asyncio.SelectorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()
asyncio.set_event_loop(_loop)

# ---- policy (audit hook) -------------------------------------------------------------------------
_norm = lambda p: os.path.normcase(os.path.abspath(p))
_READ = tuple(_norm(p) + os.sep for p in [*_cfg["read_roots"], os.getcwd()])
_META = [ipaddress.ip_network(n) for n in ("169.254.169.254/32", "169.254.170.2/32", "fd00:ec2::254/128", "100.100.100.200/32")]
_NETS = [ipaddress.ip_network(n, strict=False) for n in _cfg["allowed_nets"]]
_ALLOW_PRIVATE = _cfg["allow_private"]
_DENY_PREFIX = ("subprocess.", "os.system", "os.exec", "os.spawn", "os.posix_spawn", "os.fork", "os.forkpty",
                "os.startfile", "os.kill", "os.killpg", "_winapi.CreateProcess", "_winapi.OpenProcess",
                "_winapi.TerminateProcess", "ctypes.", "winreg.", "os.remove", "os.rename", "os.rmdir", "os.mkdir",
                "os.chmod", "os.chown", "os.link", "os.symlink", "os.truncate", "os.utime", "os.putenv",
                "os.unsetenv", "shutil.", "sys.remote_exec", "_posixsubprocess.", "pty.", "msvcrt.locking",
                "signal.pthread_kill", "socket.bind", "mmap.")
_DENY_IMPORT = {"ctypes", "_ctypes", "cffi", "_cffi_backend", "_posixsubprocess", "winreg", "_winreg", "pty", "mmap",
                "multiprocessing", "_multiprocessing", "_testcapi", "_xxsubinterpreters", "_interpreters"}
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
_armed = False

def _readable(path):
    if isinstance(path, int):
        return True
    if isinstance(path, bytes):
        path = os.fsdecode(path)
    p = _norm(str(path)) + os.sep
    return p.startswith(_READ)

def _ip_ok(ip):
    ip = ipaddress.ip_address(str(ip).split("%")[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if any(ip in n for n in _META):
        return False
    priv = (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast
            or ip.is_unspecified or ip in ipaddress.ip_network("100.64.0.0/10"))
    return not priv or _ALLOW_PRIVATE or any(ip in n for n in _NETS)

def _hook(event, args):
    if not _armed:
        return
    if event == "open":
        path, mode, flags = (list(args) + [None, None, None])[:3]
        if (mode and any(c in str(mode) for c in "wax+")) or (isinstance(flags, int) and flags & _WRITE_FLAGS):
            raise PermissionError("Code runs in a sandbox: writing files is not allowed")
        if not _readable(path):
            raise PermissionError(f"Code runs in a sandbox: reading '{path}' is not allowed")
    elif event in ("os.listdir", "os.scandir", "glob.glob", "os.chdir"):
        if args and args[0] not in (None, ".") and not _readable(args[0]):
            raise PermissionError("Code runs in a sandbox: that folder is not accessible")
    elif event == "import":
        if args and str(args[0]).split(".")[0] in _DENY_IMPORT:
            raise ImportError(f"Module '{args[0]}' is not available in the Code sandbox")
    elif event in ("socket.connect", "socket.sendto", "socket.sendmsg"):
        addr = args[1] if len(args) > 1 else None
        if isinstance(addr, tuple) and addr:
            host = addr[0]
            try:
                ips = [host] if _isip(host) else [i[4][0] for i in socket.getaddrinfo(host, None)]
            except OSError:
                return
            for ip in ips:
                if not _ip_ok(ip):
                    raise PermissionError(f"Code runs in a sandbox: connecting to {host} ({ip}) is blocked "
                                          "(private / loopback / metadata address)")
        elif addr is not None and not isinstance(addr, tuple):
            raise PermissionError("Code runs in a sandbox: only internet (TCP/UDP) sockets are allowed")
    elif event.startswith(_DENY_PREFIX):
        raise PermissionError(f"Code runs in a sandbox: '{event}' is not allowed")

def _isip(h):
    try:
        ipaddress.ip_address(str(h).split("%")[0]); return True
    except ValueError:
        return False

sys.addaudithook(_hook)
for _m in list(sys.modules):
    if _m.split(".")[0] in ("ctypes", "_ctypes"):
        del sys.modules[_m]
_armed = True

# ---- running requests ----------------------------------------------------------------------------
_BASE = {"json": json, "re": re, "math": math, "datetime": _dt.datetime, "timedelta": _dt.timedelta,
         "timezone": _dt.timezone, "httpx": httpx, "asyncio": asyncio}

def _line(tb):
    n = None
    for fr in traceback.extract_tb(tb):
        if fr.filename == "<code>":
            n = fr.lineno - 1
    return n

def _handle(req):
    logs, state = [], []
    buf = io.StringIO()
    g = dict(_BASE)
    nodes = req.get("nodes") or {}
    g.update(vars=req.get("vars") or {}, execution_id=req.get("execution_id"), state=req.get("state") or {},
             set_state=lambda k, v, reducer=None: state.append([k, copy.deepcopy(v), reducer]),
             node=lambda name: nodes.get(name, []), __builtins__=builtins)
    params = req["params"]
    src = "async def __user_fn(" + ", ".join(params) + "):\n" + "".join(
        ("    " + l if l.strip() else l) + "\n" for l in (req["code"] or "pass").splitlines())
    try:
        code = compile(src, "<code>", "exec")
    except SyntaxError as e:
        return {"ok": False, "error": f"Syntax error line {(e.lineno or 1) - 1}: {e.msg}"}
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            exec(code, g)
            fn = g["__user_fn"]
            if req["kind"] == "tool":
                out = _loop.run_until_complete(fn(**req["args"]))
            else:
                items = req["items"]
                if req.get("mode") == "each":
                    out = []
                    for idx, item in enumerate(items):
                        r = _loop.run_until_complete(fn(items, item, idx))
                        if r is not None:
                            out.extend(r if isinstance(r, list) else [r])
                else:
                    r = _loop.run_until_complete(fn(items, items[0] if items else {}, 0))
                    out = [] if r is None else (r if isinstance(r, list) else [r])
    except BaseException as e:  # noqa: BLE001 - report every failure to the caller
        if isinstance(e, (SystemExit, KeyboardInterrupt)):
            e = RuntimeError(f"{type(e).__name__} is not allowed in the Code node")
        ln = _line(e.__traceback__)
        return {"ok": False, "error": f"{type(e).__name__}: {e}" + (f" (line {ln})" if ln else ""),
                "logs": buf.getvalue().splitlines()[-500:]}
    return {"ok": True, "out": out, "state": state, "logs": buf.getvalue().splitlines()[-500:]}

_proto.write("ready\n"); _proto.flush()
while True:
    _l = _in.readline()
    if not _l:
        break
    try:
        _resp = _handle(json.loads(_l))
        _txt = json.dumps(_resp, default=str)
    except BaseException as _e:  # noqa: BLE001
        _txt = json.dumps({"ok": False, "error": f"{type(_e).__name__}: {_e}"})
    _proto.write(_txt + "\n"); _proto.flush()
'''


class SandboxError(Exception):
    pass


def _runner_path() -> Path:
    d = Path(tempfile.gettempdir()) / "debbuilder_sandbox"
    d.mkdir(exist_ok=True)
    import hashlib

    p = d / f"runner_{hashlib.sha256(_RUNNER.encode()).hexdigest()[:16]}.py"
    if not p.exists() or p.read_text(encoding="utf-8") != _RUNNER:
        p.write_text(_RUNNER, encoding="utf-8")
    return p


def _worker_config() -> dict:
    import site

    s = get_settings()
    prefixes = {os.path.normcase(os.path.abspath(p)) for p in (sys.prefix, sys.base_prefix, sys.exec_prefix)}
    under = lambda p: any(os.path.normcase(os.path.abspath(p)).startswith(x) for x in prefixes)  # noqa: E731
    # stdlib + installed packages only: never the app's own folder (an editable install puts it on sys.path)
    paths = [p for p in sys.path if p and os.path.isdir(p) and under(p)]
    for p in site.getsitepackages():
        if under(p) and p not in paths:
            paths.append(p)
    nets: list[str] = []
    for part in (s.outbound_allowed_hosts or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            nets.append(str(ipaddress.ip_network(part, strict=False)))
        except ValueError:
            try:
                nets += [f"{i[4][0].split('%')[0]}/{128 if ':' in i[4][0] else 32}" for i in socket.getaddrinfo(part, None)]
            except OSError:
                pass
    roots = sorted(prefixes) + [p for p in ("/usr/share/zoneinfo", "/etc/ssl", "/etc/localtime") if os.path.exists(p)]
    return {"sys_path": paths, "read_roots": roots, "allowed_nets": nets, "allow_private": s.outbound_allow_private}


class _Worker:
    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="debbuilder_py_")
        self.lim = Limits()
        self.proc = subprocess.Popen(
            [sys.executable, "-I", "-S", "-B", str(_runner_path())], cwd=self.dir, env=minimal_env(self.dir),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
            errors="replace", bufsize=1, **self.lim.popen_kwargs())
        self.lim.attach(self.proc)
        self.owner: str | None = None
        self.uses = 0
        self.last = time.monotonic()
        self.timed_out = False
        self.proc.stdin.write(json.dumps(_worker_config()) + "\n")
        self.proc.stdin.flush()
        timer = threading.Timer(30, self.kill)
        timer.start()
        try:
            ready = self.proc.stdout.readline()
        finally:
            timer.cancel()
        if ready.strip() != "ready":
            self.close()
            raise SandboxError("The Python sandbox failed to start")

    @property
    def alive(self) -> bool:
        return self.proc.poll() is None

    def kill(self) -> None:
        self.timed_out = True
        self.lim.kill(self.proc)

    def close(self) -> None:
        self.lim.kill(self.proc)
        for f in (self.proc.stdin, self.proc.stdout):
            try:
                f.close()
            except OSError:
                pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        self.lim.close()
        import shutil

        shutil.rmtree(self.dir, ignore_errors=True)

    def call(self, req: dict, timeout: float) -> dict:
        self.uses += 1
        self.timed_out = False
        timer = threading.Timer(timeout, self.kill)
        timer.start()
        try:
            self.proc.stdin.write(json.dumps(req, default=str) + "\n")
            self.proc.stdin.flush()
            line = self.proc.stdout.readline()
        except (OSError, ValueError):
            line = ""
        finally:
            timer.cancel()
            self.last = time.monotonic()
        if not line:
            if self.timed_out:
                raise Timeout(f"Timed out after {timeout:.0f}s (CODE_TIMEOUT_SECONDS / the node's Timeout)")
            try:
                rc = self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                rc = None
            hint = " (probably over the CODE_MEMORY_MB memory limit)" if rc not in (0, None) else ""
            raise SandboxError(f"The code's process crashed (exit code {rc}){hint}")
        return json.loads(line)


class PythonPool:
    MAX_USES = 200
    IDLE_SECONDS = 120
    WARM = 2

    def __init__(self):
        self.size = max(2, min(8, os.cpu_count() or 2))
        self._exec = ThreadPoolExecutor(self.size, thread_name_prefix="py-sandbox")
        self._lock = threading.Lock()
        self._idle: dict[str | None, list[_Worker]] = {}  # owner -> idle workers (None = fresh, unbound)
        self._live = 0
        self._refilling = False

    # -- bookkeeping (any thread) -------------------------------------------------------------------
    def _take(self, owner: str) -> _Worker | None:
        with self._lock:
            now = time.monotonic()
            stale = [w for o, ws in self._idle.items() if o is not None for w in ws if now - w.last > self.IDLE_SECONDS]
            for o in list(self._idle):
                self._idle[o] = [w for w in self._idle[o] if w not in stale and w.alive]
            self._live -= len(stale)
            for key in (owner, None):
                if self._idle.get(key):
                    return self._idle[key].pop()
            # make room: retire another owner's idle worker
            if self._live >= self.size + self.WARM:
                for o, ws in self._idle.items():
                    if o is not None and ws:
                        stale.append(ws.pop())
                        self._live -= 1
                        break
            self._live += 1
        for w in stale:
            w.close()
        return None

    def _put(self, owner: str, w: _Worker | None, broken: bool) -> None:
        if w is None:
            with self._lock:
                self._live -= 1
            return
        if broken or not w.alive or w.uses >= self.MAX_USES:
            w.close()
            with self._lock:
                self._live -= 1
        else:
            with self._lock:
                self._idle.setdefault(owner, []).append(w)
        self._refill()

    def _refill(self) -> None:
        with self._lock:
            if self._refilling or len(self._idle.get(None, [])) >= self.WARM or self._live >= self.size + self.WARM:
                return
            self._refilling = True

        def work():
            try:
                while True:
                    with self._lock:
                        if len(self._idle.get(None, [])) >= self.WARM or self._live >= self.size + self.WARM:
                            return
                        self._live += 1
                    try:
                        w = _Worker()
                    except Exception:  # noqa: BLE001
                        with self._lock:
                            self._live -= 1
                        return
                    with self._lock:
                        self._idle.setdefault(None, []).append(w)
            finally:
                with self._lock:
                    self._refilling = False

        threading.Thread(target=work, daemon=True, name="py-sandbox-warm").start()

    def _run_sync(self, owner: str, req: dict, timeout: float, holder: dict) -> dict:
        w = self._take(owner)
        broken = True
        try:
            if w is None:
                w = _Worker()
            w.owner = owner
            holder["w"] = w
            if holder.get("cancelled"):
                raise SandboxError("cancelled")
            resp = w.call(req, timeout)
            broken = False
            return resp
        finally:
            self._put(owner, w, broken)

    async def run(self, owner: str | None, req: dict, timeout: float) -> dict:
        holder: dict = {}
        fut = asyncio.get_running_loop().run_in_executor(self._exec, self._run_sync, owner or "-", req, timeout, holder)
        try:
            return await asyncio.shield(fut)
        except asyncio.CancelledError:
            holder["cancelled"] = True
            if holder.get("w"):
                holder["w"].kill()  # stop the user's code; the worker is recycled
            raise

    def shutdown(self) -> None:
        with self._lock:
            ws = [w for v in self._idle.values() for w in v]
            self._idle.clear()
        for w in ws:
            w.close()


_POOL: PythonPool | None = None
_POOL_LOCK = threading.Lock()


def pool() -> PythonPool:
    global _POOL
    with _POOL_LOCK:
        if _POOL is None:
            _POOL = PythonPool()
            _POOL._refill()
        return _POOL


async def run_python(owner: str | None, req: dict, timeout: float) -> dict:
    """Run one request in the sandbox. Raises SandboxError / Timeout; returns the worker's response."""
    return await pool().run(owner, req, timeout)
