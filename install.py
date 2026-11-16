#!/usr/bin/env python3
"""Install the agy-writer Claude Code agent on this computer (macOS, Linux or Windows).

  python install.py                    # user-level agent: ~/.claude/agents/agy-writer.md (all projects)
  python install.py --project <dir>    # project-level agent: <dir>/.claude/agents/agy-writer.md
  python install.py --check            # only report what is found

The agent file is filled in with this computer's Python and the absolute path of agy_write.py.
A default ~/.agy-writer/config.json is written if none exists. Nothing else is changed.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import agy_write  # noqa: E402


def python_cmd():
    exe = sys.executable or "python3"
    return f'"{exe}"' if " " in exe else exe


def check():
    ok = True
    print(f"Python : {sys.executable} ({sys.version.split()[0]})")
    try:
        agy = agy_write.find_agy(dict(agy_write.DEFAULTS))
        v = subprocess.run([agy, "--version"], capture_output=True, text=True).stdout.strip()
        print(f"agy    : {agy} ({v})")
    except SystemExit:
        print("agy    : NOT FOUND (install the Antigravity CLI and sign in)")
        ok = False
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default="", help="install into <dir>/.claude/agents instead of ~/.claude/agents")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()

    ok = check()
    if a.check:
        sys.exit(0 if ok else 1)

    wrapper = os.path.join(HERE, "agy_write.py")
    with open(os.path.join(HERE, "agent", "agy-writer.md"), encoding="utf-8") as f:
        text = f.read().replace("__WRAPPER__", wrapper.replace("\\", "/")).replace("__PYTHON__", python_cmd())
    base = os.path.join(os.path.abspath(a.project), ".claude") if a.project else os.path.join(os.path.expanduser("~"), ".claude")
    dest = os.path.join(base, "agents", "agy-writer.md")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if os.path.exists(dest):
        shutil.copy2(dest, dest + ".bak")
        print(f"Existing agent backed up to {dest}.bak")
    with open(dest, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    print(f"Agent written: {dest}")

    cfg = os.path.join(os.path.expanduser("~"), ".agy-writer", "config.json")
    if not os.path.exists(cfg):
        os.makedirs(os.path.dirname(cfg), exist_ok=True)
        with open(cfg, "w", encoding="utf-8") as f:
            json.dump({k: agy_write.DEFAULTS[k] for k in ("allowed_models", "model_efforts", "max_concurrent", "timeout")},
                      f, indent=2)
            f.write("\n")
        print(f"Default config written: {cfg}")
    else:
        print(f"Config kept: {cfg}")
    if not ok:
        print("\nWarning: agy is missing (see above); the agent will fail until it is installed.")
    print("\nRestart Claude Code (or open a new session) so it picks up the agent.")


if __name__ == "__main__":
    main()
