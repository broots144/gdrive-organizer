"""Record examples/demo/run.sh into an asciicast v2 file (for docs/demo.svg).

  python3 examples/demo/record.py demo.cast
  npx svg-term-cli --in demo.cast --out docs/demo.svg --window --no-cursor

Runs the demo in a pseudo-terminal of fixed size (or a pipe where none can be opened, as in some
sandboxes) and caps idle gaps at 2 seconds.
"""
import json
import os
import pty
import select
import subprocess
import sys
import time

COLS, ROWS, MAX_GAP = 100, 32, 2.0
out = sys.argv[1]
here = os.path.dirname(os.path.abspath(__file__))
env = dict(os.environ, COLUMNS=str(COLS), LINES=str(ROWS), TERM="xterm-256color",
           PYTHONUNBUFFERED="1")
cmd = ["bash", os.path.join(here, "run.sh")]
try:
    pid, fd = pty.fork()
    if pid == 0:
        os.execvpe("bash", cmd, env)
    done = lambda: os.waitpid(pid, os.WNOHANG)[0]  # noqa: E731
except OSError:  # no pty available: a pipe works, the demo only prints
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    fd = proc.stdout.fileno()
    done = lambda: proc.poll() is not None  # noqa: E731
events, start, last, clock = [], time.time(), time.time(), 0.0
while True:
    r, _, _ = select.select([fd], [], [], 0.1)
    if not r:
        if done():
            break
        continue
    try:
        data = os.read(fd, 4096)
    except OSError:
        break
    if not data:
        break
    now = time.time()
    clock += min(now - last, MAX_GAP)
    last = now
    events.append([round(clock, 3), "o", data.decode("utf-8", "replace").replace("\n", "\r\n")])
with open(out, "w", encoding="utf-8") as fh:
    fh.write(json.dumps({"version": 2, "width": COLS, "height": ROWS,
                         "timestamp": int(start), "env": {"TERM": "xterm-256color"}}) + "\n")
    for e in events:
        fh.write(json.dumps(e) + "\n")
print(f"{len(events)} events, {clock:.1f}s -> {out}")
