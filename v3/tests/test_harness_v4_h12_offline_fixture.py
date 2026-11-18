"""Real DNS/TLS/ALPN fixture with no network interface or forwarding path."""

from __future__ import annotations

import json
import socket
import struct
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest
from test_harness_v4_h06 import _grant, _request, _workspace

from mgar_v3.domain import DomainError, digest
from mgar_v3.harness.broker import CommandIdentity, IsolatedExecutionBroker, issue_lease
from mgar_v3.harness.commands import CommandSession
from mgar_v3.harness.contracts import CancellationToken
from mgar_v3.harness.offline_fixture import dns_answer
from mgar_v3.harness.offline_network import (
    GOT_NETWORK,
    create_network,
    network_labels,
    network_policy_hash,
    remove_network,
)

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / ".local/h12/environment-v4/got-offline-protocol-v4/sandbox-lock.json"


def _question(host: str, kind: int = 1) -> bytes:
    name = b"".join(bytes([len(part)]) + part.encode() for part in host.split(".")) + b"\0"
    return struct.pack("!6H", 42, 0x0100, 1, 0, 0, 0) + name + struct.pack("!HH", kind, 1)


def test_dns_allowlist_has_real_a_ttl_and_no_forwarding_or_wildcards() -> None:
    response = dns_answer(_question("example.com"))
    assert socket.inet_ntoa(response[-4:]) == "127.0.0.2"
    assert struct.unpack("!6H", response[:12])[3] == 1
    for name in ("example.com.attacker", "attacker.example.com", "unlisted.invalid"):
        assert dns_answer(_question(name))[3] & 15 == 3
    assert struct.unpack("!6H", dns_answer(_question("example.com", 28))[:12])[3] == 0
    assert dns_answer(b"malformed") == b""


NODE_PROBE = r"""
const dns=require('node:dns').promises,https=require('node:https'),http2=require('node:http2');
async function get(url,options={}) { return await new Promise((resolve,reject)=>{
 https.get(url,options,res=>{let data='';res.on('data',x=>data+=x);
 res.on('end',()=>resolve({status:res.statusCode,version:res.httpVersion,data}));}).on('error',reject);
}); }
(async()=>{
 const a=await dns.resolve4('example.com',{ttl:true});
 if(a[0].address!=='127.0.0.2'||a[0].ttl!==60)throw Error('DNS A/TTL');
 const {default:Cache}=await import('/node_modules/cacheable-lookup/source/index.js');
 const cache=new Cache();
 const hints=require('node:dns').ADDRCONFIG;
 for(const host of ['example.com','localhost']) {
  const first=await cache.lookupAsync(host,{hints});
  const second=await cache.lookupAsync(host,{hints});
  if(first.family!==4||second.source!=='cache')throw Error('ADDRCONFIG/cache metadata');
 }
 let unknown=false;try{await dns.resolve4('unlisted.invalid')}catch(e){unknown=e.code==='ENOTFOUND'}
 if(!unknown)throw Error('DNS forwarded/wildcarded');
 const one=await get('https://example.com/');
 if(one.status!==200||one.version!=='1.1')throw Error('HTTP1/TLS');
 const absent=await get('https://example.com/absent');
 if(absent.status!==404)throw Error('permissive fake endpoint');
 let mismatch=false;
 try{await get('https://example.com/',{servername:'unlisted.invalid'})}
 catch(e){mismatch=e.code==='ERR_TLS_CERT_ALTNAME_INVALID'}
 if(!mismatch)throw Error('TLS identity validation disabled');
 await new Promise((resolve,reject)=>{
  const session=http2.connect('https://www.google.com');session.on('error',reject);
  session.on('connect',()=>{if(session.alpnProtocol!=='h2')reject(Error('ALPN'));});
  const req=session.request({':path':'/'});let data='';
  req.on('data',x=>data+=x);req.on('end',()=>{session.close();resolve(data)});req.end();
 });
 console.log(JSON.stringify({dns:a,http1:one.version,http2:'h2',tls_verified:true}));
})().catch(e=>{console.error(e);process.exitCode=1});
"""


def test_real_disconnected_dns_tls_http2_and_cleanup(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    lease = issue_lease(
        mode="investigate",
        actor="owner",
        workspace_id="ws-h06",
        repository="example/project",
        execution_epoch=3,
        read_paths=("",),
        write_paths=(),
        allowed_commands=("node", "python3"),
        timeout_seconds=30,
        memory_mb=256,
        pids=64,
        offline_network=GOT_NETWORK,
    )
    broker = IsolatedExecutionBroker(root, lease, lock_path=LOCK)
    session = CommandSession(broker)
    try:
        handle = session.run(
            _request(("node", "-e", NODE_PROBE)), _grant(write=()), CancellationToken()
        )
        finished = session.wait(handle.handle_id, timeout=30)
        page = session.read_log(handle.handle_id)
        (tmp_path / "protocol-evidence.json").write_text(
            json.dumps(
                {
                    "receipt": finished.model_dump(mode="json"),
                    "output": page.data,
                    "network_policy_sha256": network_policy_hash(GOT_NETWORK),
                },
                indent=2,
            )
        )
        assert finished.state == "exited", page.data
        assert finished.exit_code == 0
        assert '"tls_verified":true' in page.data
    finally:
        session.close()


@pytest.mark.parametrize("host", ["10.255.255.1", "123.123.123.123"])
def test_fixture_timeout_target_really_times_out(tmp_path: Path, host: str) -> None:
    root = _workspace(tmp_path)
    lease = issue_lease(
        mode="investigate",
        actor="owner",
        workspace_id="ws-h06",
        repository="example/project",
        execution_epoch=3,
        read_paths=("",),
        write_paths=(),
        allowed_commands=("python3",),
        timeout_seconds=10,
        offline_network=GOT_NETWORK,
    )
    broker = IsolatedExecutionBroker(root, lease, lock_path=LOCK)
    session = CommandSession(broker)
    program = (
        "import socket,time\ns=socket.socket();s.settimeout(.1);start=time.monotonic()\n"
        f"try:s.connect(('{host}',443))\n"
        "except TimeoutError:print('REAL_CONNECT_TIMEOUT',time.monotonic()-start)\n"
    )
    try:
        handle = session.run(
            _request(("python3", "-c", program)), _grant(write=()), CancellationToken()
        )
        finished = session.wait(handle.handle_id, timeout=15)
        output = session.read_log(handle.handle_id).data
        assert finished.exit_code == 0, output
        assert "REAL_CONNECT_TIMEOUT" in output
    finally:
        session.close()


def _network_broker(path: Path) -> IsolatedExecutionBroker:
    root = _workspace(path)
    lease = issue_lease(
        mode="investigate",
        actor="owner",
        workspace_id="ws-h06",
        repository="example/project",
        execution_epoch=3,
        read_paths=("",),
        write_paths=(),
        allowed_commands=("python3",),
        timeout_seconds=30,
        offline_network=GOT_NETWORK,
    )
    return IsolatedExecutionBroker(root, lease, lock_path=LOCK)


def test_project_cannot_reach_host_or_other_command_and_has_no_setup_capabilities(
    tmp_path: Path,
) -> None:
    listener = socket.socket()
    listener.bind(("0.0.0.0", 0))
    listener.listen(1)
    listener.settimeout(0.1)
    accepted = []
    stopped = threading.Event()

    def count_accepts() -> None:
        while not stopped.is_set():
            try:
                peer, _ = listener.accept()
                accepted.append(True)
                peer.close()
            except TimeoutError:
                pass

    thread = threading.Thread(target=count_accepts, daemon=True)
    thread.start()
    first, second = _network_broker(tmp_path / "first"), _network_broker(tmp_path / "second")
    one, two = CommandSession(first), CommandSession(second)
    service = (
        "import socket,time\ns=socket.socket();s.bind(('127.0.0.1',34567));s.listen();"
        "print('READY',flush=True);time.sleep(20)"
    )
    try:
        handle = one.run(
            _request(("python3", "-c", service)), _grant(write=()), CancellationToken()
        )
        deadline = time.monotonic() + 5
        while "READY" not in one.read_log(handle.handle_id).data:
            assert time.monotonic() < deadline
            time.sleep(0.05)
        # Host loopback and cross-command loopback are distinct namespaces.
        probe = (
            "import socket,os,pathlib\n"
            "assert os.geteuid()==65532\n"
            "status=pathlib.Path('/proc/self/status').read_text()\n"
            "assert any(x.startswith('CapEff:') and int(x.split()[1],16)==0 "
            "for x in status.splitlines())\n"
            "assert sorted(os.listdir('/sys/class/net'))==['harness0','lo']\n"
            "iface=pathlib.Path('/sys/class/net/harness0')\n"
            "assert iface.joinpath('ifindex').read_text()==iface.joinpath('iflink').read_text()\n"
            "assert not pathlib.Path('/opt/harness/server.key').exists()\n"
            f"targets=[(socket.AF_INET,'127.0.0.1',{listener.getsockname()[1]}),"
            "(socket.AF_INET,'127.0.0.1',34567),"
            "(socket.AF_INET6,'::1',34567),(socket.AF_INET6,'fd00::1',443)]\n"
            "for family,address,port in targets:\n"
            " s=socket.socket(family);s.settimeout(.1)\n"
            " try:s.connect((address,port))\n"
            " except OSError:pass\n"
            " else:raise AssertionError('namespace escaped')\n"
            " finally:s.close()\n"
            "try:socket.socket(socket.AF_PACKET,socket.SOCK_RAW)\n"
            "except PermissionError:pass\n"
            "else:raise AssertionError('raw capability leaked')\n"
            "print('HOST_CROSS_NAMESPACE_IPV6_CAPS_DENIED')\n"
        )
        other = two.run(_request(("python3", "-c", probe)), _grant(write=()), CancellationToken())
        final = two.wait(other.handle_id, timeout=15)
        output = two.read_log(other.handle_id).data
        assert final.exit_code == 0, output
        assert "HOST_CROSS_NAMESPACE_IPV6_CAPS_DENIED" in output
        assert accepted == []
        # Revoking the first launch must kill both its project and namespace owner.
        name = one._records[handle.handle_id].launched.name
        first.authority.revoke()
        assert one.wait(handle.handle_id, timeout=5).state == "revoked"
        assert first._docker(["inspect", name + "-offline"]).returncode != 0
    finally:
        one.close()
        two.close()
        stopped.set()
        thread.join(timeout=1)
        listener.close()


def test_recovery_removes_orphan_namespace_even_when_project_container_is_missing(
    tmp_path: Path,
) -> None:
    broker = _network_broker(tmp_path)
    identity = CommandIdentity("env-task", "env-run", "orphan", 3, digest(str(broker.root)))
    create_network(broker, identity.name, GOT_NETWORK)
    try:
        assert broker.inspect_command(identity.name)[0] == "missing"
        broker.remove_owned_command(identity)
        assert broker._docker(["inspect", identity.name + "-offline"]).returncode != 0
    finally:
        remove_network(broker, identity.name)


def test_restarted_broker_removes_existing_owned_project_and_its_namespace(tmp_path: Path) -> None:
    broker = _network_broker(tmp_path)
    request = _request(("python3", "-c", "import time;time.sleep(15)"))
    identity = CommandIdentity(
        "env-task", "env-run", request.operation_id, 3, digest(str(broker.root))
    )
    broker.launch(request, _grant(write=()), CancellationToken(), identity=identity)
    restarted = IsolatedExecutionBroker(broker.root, broker.authority.peek(), lock_path=LOCK)
    try:
        assert restarted._offline_names == set()
        assert restarted.owned_command_state(identity)[0] == "running"
        restarted.remove_owned_command(identity)
        assert restarted.inspect_command(identity.name)[0] == "missing"
        assert restarted._docker(["inspect", identity.name + "-offline"]).returncode != 0
    finally:
        broker.remove_owned_command(identity)
        broker._remove_placeholders(identity.name)


def test_cleanup_keeps_foreign_namespace_and_accepts_owned_previous_program(
    tmp_path: Path,
) -> None:
    broker = _network_broker(tmp_path)
    identity = CommandIdentity("env-task", "env-run", "owner-proof", 3, digest(str(broker.root)))
    labels = network_labels(identity.name, str(broker.root), GOT_NETWORK)
    labels["mgar.v4.offline-program"] = "0" * 64  # Previous trusted implementation.
    foreign = replace(identity, operation_id="foreign")
    for project, workspace in [
        (identity.name, str(broker.root)),
        (foreign.name, "another-workspace"),
    ]:
        args = ["create", "--name", project + "-offline", "--network=none"]
        for key, value in {
            **labels,
            "mgar.v4.offline-project": project,
            "mgar.v4.offline-workspace": digest(workspace),
        }.items():
            args += ["--label", f"{key}={value}"]
        args += ["--entrypoint", "python3", broker.image_id, "-c", "pass"]
        assert broker._docker(args).returncode == 0
    try:
        remove_network(broker, identity.name)
        assert broker._docker(["inspect", identity.name + "-offline"]).returncode != 0
        with pytest.raises(DomainError, match="owner differs"):
            remove_network(broker, foreign.name)
        assert broker._docker(["inspect", foreign.name + "-offline"]).returncode == 0
    finally:
        broker._docker(["rm", "-f", identity.name + "-offline", foreign.name + "-offline"])
