#!/usr/bin/env python3
"""Stand-in for agy in tests: records argv/stdin, writes a file, emits stream-json events."""
import json
import os
import sys

if "--version" in sys.argv:
    print("0.0.0-fake")
    sys.exit(0)

log = os.environ.get("FAKE_AGY_LOG")
stdin = sys.stdin.read()
if log:
    with open(log, "w", encoding="utf-8") as f:
        json.dump({"argv": sys.argv[1:], "stdin": stdin}, f)

msg = json.loads(stdin.splitlines()[0])
prompt = msg["message"]["content"]
conv = "conv-" + os.environ.get("FAKE_AGY_CONV", "1")
out = os.environ.get("FAKE_AGY_WRITE", "out.md")
target = os.path.join(os.getcwd(), out)
os.makedirs(os.path.dirname(target), exist_ok=True)
with open(target, "w", encoding="utf-8") as f:
    f.write("draft\n")

status = os.environ.get("FAKE_AGY_STATUS", "SUCCESS")
events = [
    {"event": "init", "conversation_id": conv, "init": {"model": "x"}},
    {"event": "step_update", "step_update": {"conversation_id": conv, "step_index": 1, "state": "DONE",
                                             "step_type": "tool", "tool_name": "write_file",
                                             "tool_info": {"name": "write_file", "parameters": {"Path": target}}}},
    {"event": "step_update", "step_update": {"conversation_id": conv, "step_index": 2, "state": "ERROR",
                                             "step_type": "tool", "tool_name": "run_command",
                                             "tool_info": {"name": "run_command", "error": {"message": "denied"}}}},
    {"event": "result", "result": {"conversation_id": conv, "status": status,
                                   "response": "WROTE " + out + (" PREAMBLE" if "Writer preamble" in prompt else ""),
                                   "usage": {"input_tokens": 10, "output_tokens": 5},
                                   "denied_actions": [{"action": "command"}]}},
]
for e in events:
    print(json.dumps(e))
sys.exit(int(os.environ.get("FAKE_AGY_RC", "0")))
