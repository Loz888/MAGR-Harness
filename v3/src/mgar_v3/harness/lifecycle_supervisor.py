"""Fixed container-side supervisor. Invoked as source by the original Broker."""

import base64
import ctypes
import json
import os
import selectors
import signal
import subprocess
import sys
from types import FrameType
from typing import Any


def main() -> None:
    config = json.loads(sys.argv[1])
    original = json.loads(sys.argv[2])
    # Keep detached grandchildren observable after the project root exits.
    if ctypes.CDLL(None).prctl(36, 1, 0, 0, 0):
        raise RuntimeError("subreaper unavailable")
    read_fd, write_fd = os.pipe()
    os.set_blocking(read_fd, False)
    environment = dict(os.environ)
    environment["PYTHONUNBUFFERED"] = "1"
    environment["HARNESS_R04_AUDIT"] = f"/proc/{os.getpid()}/fd/{write_fd}"
    command = original
    if config["mode"] == "entry":
        spec = config["entry"]
        if spec["runtime"] == "python":
            command = ["python3", "-B", "-u", "-c", config["python_loader"], json.dumps(spec)]
        else:
            command = ["node", "-e", config["node_loader"], json.dumps(spec)]
    elif config["mode"] == "oneshot":
        # npm descendants can open the parent's pipe even when npm doesn't
        # inherit additional descriptors. No host environment is passed in.
        hook = base64.b64encode(config["node_hook"].encode()).decode()
        environment["NODE_OPTIONS"] = (
            environment.get("NODE_OPTIONS", "") + " --import=data:text/javascript;base64," + hook
        ).strip()
        if os.path.basename(original[0]) in {"python", "python3"}:
            bootstrap = (
                config["python_hook"]
                + "\n"
                + (
                    "import runpy,sys,os,json\n"
                    "args=json.loads(sys.argv[1])[1:]\n"
                    "while args and args[0] in ('-B','-u'): args.pop(0)\n"
                    "if args[0]=='-m':\n"
                    " module=args[1];sys.argv=[module]+args[2:];"
                    "runpy.run_module(module,run_name='__main__',alter_sys=True)\n"
                    "else:\n"
                    " target=args[0];sys.argv=args;sys.path.insert(0,os.path.dirname("
                    "os.path.abspath(target)));runpy.run_path(target,run_name='__main__')\n"
                )
            )
            command = [original[0], "-B", "-u", "-c", bootstrap, json.dumps(original)]
    child = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        env=environment,
        start_new_session=True,
        pass_fds=(write_fd,),
    )

    def terminate(signum: int, frame: FrameType | None) -> None:
        del frame
        try:
            os.killpg(child.pid, signum)
        except ProcessLookupError:
            pass

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    assert child.stdout is not None and child.stderr is not None
    os.set_blocking(child.stdout.fileno(), False)
    os.set_blocking(child.stderr.fileno(), False)
    selector = selectors.DefaultSelector()
    selector.register(child.stdout, selectors.EVENT_READ, "stdout")
    selector.register(child.stderr, selectors.EVENT_READ, "stderr")
    selector.register(read_fd, selectors.EVENT_READ, "audit")
    audit = bytearray()

    def emit(value: dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")
        sys.stdout.flush()

    emit({"kind": "launch", "pid": child.pid, "supervisor": os.getpid()})
    exited = False
    while True:
        for key, _ in selector.select(0.02):
            raw = os.read(key.fd, 8192)
            if not raw:
                selector.unregister(key.fileobj)
                continue
            if key.data in {"stdout", "stderr"}:
                emit(
                    {"kind": "output", "stream": key.data, "bytes": base64.b64encode(raw).decode()}
                )
            else:
                audit.extend(raw)
                if len(audit) > 65536:
                    raise RuntimeError("audit frame exceeds limit")
                while b"\n" in audit:
                    line, _, tail = audit.partition(b"\n")
                    audit = bytearray(tail)
                    try:
                        value = json.loads(line)
                    except (ValueError, UnicodeError):
                        continue
                    if isinstance(value, dict):
                        emit({"kind": "observation", "value": value})
        if not exited:
            outcome = os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            if outcome is not None:
                code = outcome.si_status if outcome.si_code == os.CLD_EXITED else -outcome.si_status
                emit({"kind": "exit", "pid": child.pid, "exit_code": code})
                exited = True
        # Do not reap the project root until final cleanup: its zombie keeps
        # the original PID/start-time/group independently inspectable.
        for entry in os.listdir("/proc"):
            if entry.isdigit() and int(entry) not in {os.getpid(), child.pid}:
                try:
                    os.waitpid(int(entry), os.WNOHANG)
                except (ChildProcessError, ProcessLookupError):
                    pass


if __name__ == "__main__":
    main()
