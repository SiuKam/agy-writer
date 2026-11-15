#!/usr/bin/env python3
"""Delegate one bounded writing task from a Claude Code orchestrator to the local Antigravity CLI (agy).

Cross-platform (macOS, Linux, Windows), standard library only, Python 3.9+.
Modelled on codex-worker/codex_task.py. Every run gets an audit folder with the prompt,
command, event stream, final message and meta.json (model, effort, mode, token usage,
conversation id, exit code, files agy wrote, files changed during the run).

Usage:
  python agy_write.py --name <slug> --effort <low|medium|high> [--model <id>]
                      [--resume <conversation_id> | --continue] [--add-dir <dir>]...
                      [--with <extra preamble>]... [--timeout <seconds>] [--wait <seconds>]
                      [--root <dir>] [--prompt-file <path>]   (prompt may also come on stdin)

Settings come from built-in defaults, then ~/.agy-writer/config.json, then
<root>/.agy-writer.json (later wins). See README.md.
"""
import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
IS_WIN = os.name == "nt"
EFFORTS = ["low", "medium", "high"]

DEFAULTS = {
    # models the wrapper will accept; the first one is the default
    "allowed_models": ["gemini-3.8-flash"],
    # models whose agy ids carry the effort as a suffix (gemini-3.8-flash-high); --effort is passed as well
    "effort_suffix_models": ["gemini-3.8-flash"],
    "max_concurrent": 4,
    # agy execution mode: accept-edits lets agy write files; shell commands stay denied in headless mode
    # unless allowed in agy's own settings.json (permissions.allow)
    "mode": "accept-edits",
    "sandbox": True,                       # agy --sandbox (terminal restrictions)
    "timeout": 1800,                       # agy --print-timeout, seconds; 0 = no limit
    "agy_bin": "",                         # empty: auto-detect
    "runs_dir": ".agy_runs",               # relative to the root
    "preamble": "",                        # empty: preamble.md next to this script
    "extra_preambles": {},                 # name -> file, e.g. {"style": "STYLE.md"}
    "lock_dir": "",                        # empty: ~/.agy-writer/locks (machine-wide slots)
    "track_changes": True,                 # before/after stat scan of the root
    "max_scan_files": 20000,               # give up the scan (changed_files = null) above this
    "exclude_from_changes": [".git/", "node_modules/", ".venv/", "__pycache__/"],
}


def die(msg, code=2):
    print(msg, file=sys.stderr)
    sys.exit(code)


def utcnow(fmt="%Y-%m-%dT%H:%M:%SZ"):
    return datetime.datetime.now(datetime.timezone.utc).strftime(fmt)


def load_config(root):
    cfg = dict(DEFAULTS)
    for p in (os.path.join(os.path.expanduser("~"), ".agy-writer", "config.json"),
              os.path.join(root, ".agy-writer.json")):
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8") as f:
                    cfg.update(json.load(f))
            except Exception as e:
                die(f"Cannot read config {p}: {e}")
    return cfg


def find_root(start):
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=start, capture_output=True, text=True)
        if out.returncode == 0 and out.stdout.strip():
            return os.path.realpath(out.stdout.strip())
    except FileNotFoundError:
        pass
    return os.path.realpath(start)


def find_agy(cfg):
    if cfg.get("agy_bin"):
        p = os.path.expanduser(cfg["agy_bin"])
        if os.path.isfile(p):
            return p
        die(f"agy not found at configured agy_bin: {p}")
    env = os.environ.get("AGY_BIN")
    if env and os.path.isfile(env):
        return env
    for name in (["agy.exe", "agy.cmd", "agy"] if IS_WIN else ["agy"]):
        p = shutil.which(name)
        if p:
            return p
    home = os.path.expanduser("~")
    cands = ([os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "agy", "agy.exe"),
              os.path.join(home, ".local", "bin", "agy.exe")] if IS_WIN
             else [os.path.join(home, ".local", "bin", "agy"), "/opt/homebrew/bin/agy", "/usr/local/bin/agy"])
    for p in cands:
        if p and os.path.isfile(p):
            return p
    die("Antigravity CLI (agy) not found. Install it and sign in, or set agy_bin in the config / AGY_BIN.")


# ---- concurrency slots: a slot is a directory (mkdir is atomic everywhere) holding owner.json ----

def pid_alive(pid):
    if pid <= 0:
        return False
    if IS_WIN:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k32.GetExitCodeProcess.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ctypes.get_last_error() != 87  # ERROR_INVALID_PARAMETER
        try:
            code = wintypes.DWORD()
            ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
            return bool(ok) and code.value == 259  # STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def acquire_slot(lock_dir, n, wait):
    os.makedirs(lock_dir, exist_ok=True)
    deadline = time.time() + wait
    while True:
        for i in range(1, n + 1):
            d = os.path.join(lock_dir, f"slot{i}.lock")
            try:
                os.mkdir(d)
            except FileExistsError:
                try:
                    with open(os.path.join(d, "owner.json"), encoding="utf-8") as f:
                        pid = int(json.load(f).get("pid", 0))
                except Exception:
                    if time.time() - os.path.getmtime(d) < 30:
                        continue
                    pid = 0
                if pid_alive(pid):
                    continue
                stale = f"{d}.stale.{uuid.uuid4().hex}"
                try:
                    os.rename(d, stale)          # only one reclaimer wins the rename
                    shutil.rmtree(stale, ignore_errors=True)
                    os.mkdir(d)
                except OSError:
                    continue
            with open(os.path.join(d, "owner.json"), "w", encoding="utf-8") as f:
                json.dump({"pid": os.getpid(), "since": utcnow()}, f)
            return d
        if time.time() >= deadline:
            return None
        time.sleep(3)


# ---- change tracking: stat() every file under the root (no content reads) ----

def snapshot(root, excludes, cap):
    snap = {}
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root).replace("\\", "/")
        rel_dir = "" if rel_dir == "." else rel_dir + "/"
        dirnames[:] = [d for d in dirnames if not any((rel_dir + d + "/").startswith(x) for x in excludes)]
        for fn in filenames:
            rel = rel_dir + fn
            if any(rel.startswith(x) for x in excludes):
                continue
            try:
                s = os.stat(os.path.join(dirpath, fn))
                snap[rel] = f"{s.st_size}:{s.st_mtime_ns}"
            except OSError:
                snap[rel] = "-"
            if len(snap) > cap:
                return None
    return snap


WRITE_TOOLS = ("write_file", "write_to_file", "replace_file_content", "multi_replace_file_content",
               "sed_file", "notebook_edit")
PATH_KEYS = ("Path", "TargetFile", "AbsolutePath", "path", "file_path")


def scan_events(path):
    """Return conversation id, result object, files written by agy, tool counts, denied tools."""
    conv, result, written, tools, errors = None, None, [], {}, []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                kind = ev.get("event")
                if kind == "init" and not conv:
                    conv = ev.get("conversation_id")
                elif kind == "result":
                    result = ev.get("result") or {}
                    conv = conv or result.get("conversation_id")
                elif kind == "step_update":
                    su = ev.get("step_update") or {}
                    if su.get("step_type") != "tool" or su.get("state") == "ACTIVE":
                        continue
                    name = su.get("tool_name") or "?"
                    tools[name] = tools.get(name, 0) + 1
                    info = su.get("tool_info") or {}
                    if su.get("state") == "ERROR":
                        msg = ((info.get("error") or {}).get("message") or "")[:300]
                        errors.append({"tool": name, "message": msg})
                    elif name in WRITE_TOOLS:
                        params = info.get("parameters") or {}
                        for k in PATH_KEYS:
                            if isinstance(params.get(k), str) and params[k] not in written:
                                written.append(params[k])
                                break
    except OSError:
        pass
    return conv, result, written, tools, errors


def load_sessions(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_session(path, name, conv):
    sessions = load_sessions(path)
    sessions[name] = {"conversation_id": conv, "updated_utc": utcnow()}
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(sessions, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def resolve(p, root):
    p = os.path.expanduser(p)
    return p if os.path.isabs(p) else os.path.join(root, p)


def main():
    ap = argparse.ArgumentParser(description="Run one agy writing task with an audit trail.")
    ap.add_argument("--name", required=True)
    ap.add_argument("--effort", required=True, choices=EFFORTS, help="reasoning effort, chosen per task")
    ap.add_argument("--model")
    ap.add_argument("--resume", default="", help="continue a given agy conversation id")
    ap.add_argument("--continue", dest="cont", action="store_true",
                    help="continue the last conversation recorded for this --name under this root")
    ap.add_argument("--add-dir", dest="add_dirs", action="append", default=[],
                    help="extra directory agy may read/write (repeatable)")
    ap.add_argument("--with", dest="extras", action="append", default=[],
                    help="prepend a named extra preamble from the config (e.g. --with style)")
    ap.add_argument("--timeout", type=int, help="seconds before agy gives up (default: config timeout)")
    ap.add_argument("--wait", type=int, default=0, help="seconds to wait for a free concurrency slot")
    ap.add_argument("--root", default="")
    ap.add_argument("--prompt-file", default="")
    a = ap.parse_args()

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", a.name):
        die("--name must be a slug [A-Za-z0-9._-], max 64 chars")
    if a.wait < 0:
        die("--wait must be >= 0")
    if a.cont and a.resume:
        die("use either --resume <conversation_id> or --continue, not both")

    root = find_root(a.root or os.getcwd())
    cfg = load_config(root)

    allowed = cfg["allowed_models"]
    model = a.model or allowed[0]
    if model not in allowed:
        die(f"Model '{model}' is not permitted (allowed: {', '.join(allowed)})")
    effort = a.effort
    model_id = f"{model}-{effort}" if model in cfg["effort_suffix_models"] else model
    timeout = cfg["timeout"] if a.timeout is None else a.timeout
    if timeout < 0:
        die("--timeout must be >= 0")
    if cfg["mode"] not in ("accept-edits", "plan", "auto"):
        die(f"mode must be accept-edits, plan or auto, not '{cfg['mode']}'")

    add_dirs = []
    for d in a.add_dirs:
        d = os.path.realpath(resolve(d, root))
        if not os.path.isdir(d):
            die(f"--add-dir: not a directory: {d}")
        add_dirs.append(d)

    agy = find_agy(cfg)
    preamble = resolve(cfg["preamble"], root) if cfg["preamble"] else os.path.join(HERE, "preamble.md")
    if not os.path.isfile(preamble):
        die(f"Missing preamble {preamble}")
    extra_files = []
    for x in a.extras:
        p = cfg["extra_preambles"].get(x)
        if not p:
            die(f"--with {x}: no such entry in extra_preambles")
        p = resolve(p, root)
        if not os.path.isfile(p):
            die(f"Missing extra preamble {p}")
        extra_files.append(p)

    if a.prompt_file:
        if not os.path.isfile(a.prompt_file):
            die(f"Prompt file not found: {a.prompt_file}")
        with open(a.prompt_file, encoding="utf-8") as f:
            user_prompt = f.read()
    elif not sys.stdin.isatty():
        user_prompt = sys.stdin.read()
    else:
        user_prompt = ""
    if not user_prompt.strip():
        die("Empty prompt (use --prompt-file or stdin)")

    runs_dir = resolve(cfg["runs_dir"], root)
    sessions_file = os.path.join(runs_dir, "sessions.json")
    resume = a.resume
    if a.cont:
        resume = (load_sessions(sessions_file).get(a.name) or {}).get("conversation_id") or ""
        if not resume:
            die(f"--continue: no earlier conversation recorded for --name {a.name} in {sessions_file}")

    lock_dir = resolve(cfg["lock_dir"], root) if cfg["lock_dir"] else os.path.join(os.path.expanduser("~"), ".agy-writer", "locks")
    slot = acquire_slot(lock_dir, int(cfg["max_concurrent"]), a.wait)
    if not slot:
        die(f"Concurrency limit reached ({cfg['max_concurrent']} agy runs active). Retry later or use --wait <seconds>.", 75)

    try:
        os.makedirs(runs_dir, exist_ok=True)
        ts = utcnow("%Y%m%dT%H%M%SZ")
        run_dir = os.path.join(runs_dir, f"{ts}_{a.name}")
        n = 1
        while True:
            try:
                os.mkdir(run_dir)
                break
            except FileExistsError:
                n += 1
                run_dir = os.path.join(runs_dir, f"{ts}_{a.name}-{n}")

        parts = []
        for p in [preamble] + extra_files:
            with open(p, encoding="utf-8") as g:
                parts.append(g.read().rstrip("\n"))
        parts.append(user_prompt.rstrip("\n"))
        full_prompt = "\n\n".join(parts) + "\n"
        with open(os.path.join(run_dir, "prompt.md"), "w", encoding="utf-8", newline="\n") as f:
            f.write(full_prompt)
        # agy reads the prompt from stdin as one stream-json user message (no command-line length limit)
        stdin_path = os.path.join(run_dir, "stdin.jsonl")
        with open(stdin_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps({"event": "user", "message": {"content": full_prompt}}, ensure_ascii=False) + "\n")

        rel_runs = os.path.relpath(runs_dir, root).replace("\\", "/")
        excludes = list(cfg["exclude_from_changes"])
        if not rel_runs.startswith(".."):
            excludes.append(rel_runs.rstrip("/") + "/")
        track = cfg["track_changes"]
        before = snapshot(root, excludes, int(cfg["max_scan_files"])) if track else None
        start = utcnow()
        try:
            version = subprocess.run([agy, "--version"], capture_output=True, text=True).stdout.strip().splitlines()[0]
        except Exception:
            version = "unknown"

        cmd = [agy, "--model", model_id, "--effort", effort, "--mode", cfg["mode"]]
        if cfg["sandbox"]:
            cmd += ["--sandbox"]
        for d in add_dirs:
            cmd += ["--add-dir", d]
        if resume:
            cmd += ["--conversation", resume]
        cmd += ["--input-format", "stream-json", "--output-format", "stream-json",
                "--print-timeout", f"{timeout}s", "-p", ""]
        cmd_txt = subprocess.list2cmdline(cmd) if IS_WIN else " ".join(_q(c) for c in cmd)
        with open(os.path.join(run_dir, "command.txt"), "w", encoding="utf-8") as f:
            f.write(cmd_txt + " < stdin.jsonl\n")

        t0 = time.time()
        with open(stdin_path, "rb") as fin, \
                open(os.path.join(run_dir, "events.jsonl"), "wb") as fout, \
                open(os.path.join(run_dir, "stderr.log"), "wb") as ferr:
            try:
                rc = subprocess.run(cmd, cwd=root, stdin=fin, stdout=fout, stderr=ferr).returncode
            except KeyboardInterrupt:
                rc = 130
        elapsed = round(time.time() - t0, 1)

        end = utcnow()
        after = snapshot(root, excludes, int(cfg["max_scan_files"])) if track else None
        if before is None or after is None:
            changed = None
        else:
            changed = sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))
        conv, result, written, tools, tool_errors = scan_events(os.path.join(run_dir, "events.jsonl"))
        result = result or {}
        response = result.get("response") or ""
        with open(os.path.join(run_dir, "last_message.md"), "w", encoding="utf-8", newline="\n") as f:
            f.write(response)
        status = result.get("status")
        if rc == 0 and status and status != "SUCCESS":
            rc = 1                          # agy reported a failed turn with exit 0
        meta = {
            "name": a.name, "run_dir": run_dir, "cwd": root,
            "start_utc": start, "end_utc": end, "elapsed_seconds": elapsed,
            "agy_bin": agy, "agy_version": version,
            "model": model, "model_id": model_id, "reasoning_effort": effort,
            "mode": cfg["mode"], "sandbox": bool(cfg["sandbox"]), "add_dirs": add_dirs,
            "extra_preambles": a.extras,
            "status": status, "finished_reason": result.get("finished_reason"), "error": result.get("error"),
            "usage": result.get("usage"),
            "denied_actions": result.get("denied_actions"),
            "tool_calls": tools, "tool_errors": tool_errors or None,
            "files_written_by_agy": written,
            "resumed_conversation_id": resume or None, "continued": a.cont,
            "conversation_id": conv or (resume or None),
            "exit_code": rc,
            "changed_files": changed,
            "command": cmd_txt,
            "wrapper": "agy-writer/agy_write.py",
        }
        with open(os.path.join(run_dir, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)
            f.write("\n")
        if meta["conversation_id"]:
            save_session(sessions_file, a.name, meta["conversation_id"])
    finally:
        shutil.rmtree(slot, ignore_errors=True)

    print(f"RUN_DIR: {run_dir}")
    print(f"EXIT_CODE: {rc}")
    print(f"STATUS: {status}")
    print(f"CONVERSATION_ID: {meta['conversation_id']}")
    print(f"FILES_WRITTEN: {json.dumps(written, ensure_ascii=False)}")
    if meta["denied_actions"]:
        print(f"DENIED: {json.dumps(meta['denied_actions'], ensure_ascii=False)}")
    print("----- LAST MESSAGE -----")
    if response:
        sys.stdout.write(response if response.endswith("\n") else response + "\n")
    else:
        print(f"(no final message; see {os.path.join(run_dir, 'stderr.log')})")
    sys.exit(rc)


def _q(s):
    return s if re.fullmatch(r"[A-Za-z0-9_./:=@%+-]+", s) else "'" + s.replace("'", "'\\''") + "'"


if __name__ == "__main__":
    main()
