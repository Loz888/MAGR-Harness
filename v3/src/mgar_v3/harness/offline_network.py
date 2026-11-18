"""Trusted, per-command network namespaces with no interface to the host.

The namespace owner has no mounts or project code. Its only route is a /32
through loopback: real TCP connect attempts time out without leaving the
namespace. Project processes retain zero capabilities and no network egress.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from mgar_v3.domain import DomainError, digest

TIMEOUT_NETWORK = "connect-timeout-v1"
GOT_NETWORK = "got-network-v1"
FIXTURE_MANIFEST = Path(__file__).resolve().parents[3] / "config/h12-network-fixture-v1.json"
NETWORK_PROGRAM = r'''
import os, socket, struct, time
s = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, 0)
s.bind((0, 0))
def attr(kind, data):
    size = 4 + len(data)
    return struct.pack('HH', size, kind) + data + b'\0' * ((-size) % 4)
payload = struct.pack('BBBBBBBBI', socket.AF_INET, 32, 0, 0, 254, 3, 253, 1, 0)
payload += attr(1, socket.inet_aton('10.255.255.1')) + attr(4, struct.pack('I', 1))
s.send(struct.pack('IHHII', 16 + len(payload), 24, 1|4|0x400|0x200, 1, 0) + payload)
reply = s.recv(4096)
if len(reply) < 20 or struct.unpack_from('i', reply, 16)[0] != 0:
    raise RuntimeError('Offline timeout route setup failed')
s.close()
os.setgroups([])
os.setgid(65532)
os.setuid(65532)
print('HARNESS_OFFLINE_NETWORK_READY', flush=True)
while True:
    time.sleep(3600)
'''


def network_policy_hash(mode: str = TIMEOUT_NETWORK) -> str:
    if mode == TIMEOUT_NETWORK:
        return digest(NETWORK_PROGRAM)
    if mode != GOT_NETWORK:
        raise DomainError("HARNESS_OFFLINE_NETWORK_UNAVAILABLE", "Unknown offline mode", 409)
    manifest = json.loads(FIXTURE_MANIFEST.read_text())
    if manifest.get("schema") != "h12-offline-protocol-fixture-v1" or manifest.get("mode") != mode:
        raise DomainError("HARNESS_OFFLINE_NETWORK_UNAVAILABLE", "Fixture manifest changed", 409)
    for name in ("offline_fixture.py", "offline_http2.cjs"):
        raw = Path(__file__).with_name(name).read_bytes()
        if hashlib.sha256(raw).hexdigest() != manifest.get("programs", {}).get(name):
            raise DomainError("HARNESS_OFFLINE_NETWORK_UNAVAILABLE", "Fixture program changed", 409)
    return digest(manifest)


def network_labels(project: str, workspace: str, mode: str = TIMEOUT_NETWORK) -> dict[str, str]:
    return {
        "mgar.v4.offline-network": mode,
        "mgar.v4.offline-project": project,
        "mgar.v4.offline-workspace": digest(workspace),
        "mgar.v4.offline-program": network_policy_hash(mode),
    }


def remove_network(broker: Any, project: str) -> None:
    """Only remove the deterministic namespace owner with matching scope labels."""
    name = project + "-offline"
    probe = broker._docker(["inspect", "--format", "{{json .Config.Labels}}", name])
    if probe.returncode:
        return
    try:
        labels = json.loads(probe.stdout)
        if not isinstance(labels, dict):
            raise ValueError("invalid labels")
    except (ValueError, TypeError) as error:
        raise DomainError("HARNESS_NETWORK_OWNER", "Invalid namespace owner labels", 409) from error
    mode = labels.get("mgar.v4.offline-network")
    # Deletion authority is the original owned scope, not today's program hash:
    # a trusted code/manifest upgrade must not strand yesterday's namespace.
    program = labels.get("mgar.v4.offline-program")
    if mode not in {TIMEOUT_NETWORK, GOT_NETWORK} or any(
        labels.get(k) != v for k, v in {
            "mgar.v4.offline-project": project,
            "mgar.v4.offline-workspace": digest(str(broker.root)),
        }.items()
    ) or not isinstance(program, str) or len(program) != 64 or any(
        character not in "0123456789abcdef" for character in program
    ):
        raise DomainError("HARNESS_NETWORK_OWNER", "Offline namespace owner differs", 409)
    broker._docker(["rm", "-f", name])


def create_network(broker: Any, project: str, mode: str = TIMEOUT_NETWORK) -> str:
    name = project + "-offline"
    # A deterministic recovery intent may have left only the namespace owner.
    remove_network(broker, project)
    labels = network_labels(project, str(broker.root), mode)
    args = [
        "run", "-d", "--name", name, "--network=none", "--user=0:0",
        "--read-only", "--cap-drop=ALL", "--cap-add=NET_ADMIN",
        "--cap-add=SETUID", "--cap-add=SETGID",
        "--security-opt=no-new-privileges", "--pids-limit=32", "--memory=128m",
        "--memory-swap=128m", "--cpus=0.25", "--log-opt=max-size=16k",
    ]
    for key, value in labels.items():
        args += ["--label", f"{key}={value}"]
    if mode == GOT_NETWORK:
        manifest = json.loads(FIXTURE_MANIFEST.read_text())
        image = manifest["helper_image_id"]
        if not isinstance(image, str) or not image.startswith("sha256:") or len(image) != 71:
            raise DomainError("HARNESS_OFFLINE_NETWORK_UNAVAILABLE", "Unpinned fixture image", 409)
        args += ["--cap-add=NET_BIND_SERVICE", "--dns=127.0.0.1", "--entrypoint", "python3",
                 image, "-B", "/opt/harness/offline_fixture.py"]
    else:
        args += ["--entrypoint", "python3", broker.image_id, "-B", "-c", NETWORK_PROGRAM]
    result = broker._docker(args)
    if result.returncode:
        remove_network(broker, project)
        raise DomainError("HARNESS_OFFLINE_NETWORK_UNAVAILABLE", "Namespace setup failed", 409)
    try:
        for _ in range(40):
            state = broker._docker(["inspect", "--format", "{{.State.Running}}", name])
            logs = broker._docker(["logs", "--tail", "1", name])
            if state.stdout.strip() != b"true":
                break
            if logs.stdout.strip() == b"HARNESS_OFFLINE_NETWORK_READY":
                return str(result.stdout.decode().strip())
            time.sleep(0.05)
    except BaseException:
        remove_network(broker, project)
        raise
    remove_network(broker, project)
    raise DomainError("HARNESS_OFFLINE_NETWORK_UNAVAILABLE", "Timeout route is not ready", 409)
