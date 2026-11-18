"""R04 real isolated processes: dynamic ports, HTTP and independent cleanup."""

import json
from pathlib import Path
from uuid import uuid4

import pytest
from test_harness_v4_h06 import _broker, _grant, _lease, _workspace

from mgar_v3.domain import DomainError
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.harness.broker import IsolatedExecutionBroker
from mgar_v3.harness.commands import CommandSession
from mgar_v3.harness.contracts import CancellationToken, TaskBinding
from mgar_v3.harness.lifecycle import LifecycleRunner
from mgar_v3.harness.lifecycle_types import EntryPointSpec, HTTPExpectation, LifecyclePolicy


def runtime(tmp_path, files):
    root = _workspace(tmp_path, files)
    node = any(path.endswith((".js", ".cjs", ".mjs")) for path in files)
    broker = (
        IsolatedExecutionBroker(
            root,
            _lease(timeout=60),
            lock_path=Path(__file__).parents[1]
            / ".local/h12/environment-v4/got-offline-protocol-v4/sandbox-lock.json",
        )
        if node
        else _broker(root, _lease(timeout=60))
    )
    session = CommandSession(broker)
    task = TaskBinding(
        task_id="r04-" + uuid4().hex[:10],
        thread_id=uuid4(),
        run_id=uuid4(),
        execution_epoch=3,
        actor="owner",
        workspace_id="ws-h06",
        repository="example/project",
        base_commit="a" * 40,
        acceptance_revision="r04-fixture",
        objective="Verify app lifecycle",
        task_kind="run_app",
        requirements=(),
    )
    return (
        LifecycleRunner(
            session,
            task,
            LifecyclePolicy(
                entry_timeout_seconds=3,
                shutdown_seconds=1,
                cleanup_seconds=2,
            ),
            ArtifactStore(tmp_path / "cas"),
        ),
        session,
        broker,
    )


PYTHON_HTTP = """
import http.server,signal,threading
class Handler(http.server.BaseHTTPRequestHandler):
 def do_GET(self):
  body=b'ok' if self.path=='/' else b'{"status":"ok"}'
  self.send_response(200)
  self.send_header('Content-Type','text/plain' if self.path=='/' else 'application/json')
  self.end_headers();self.wfile.write(body)
server=http.server.HTTPServer(('127.0.0.1',0),Handler)
print('READY port='+str(server.server_port),flush=True)
def stop(*args):
 threading.Thread(target=server.shutdown,daemon=True).start()
signal.signal(signal.SIGTERM,stop)
server.serve_forever()
server.server_close()
"""


def test_python_http_dynamic_port_bound_routes_and_graceful_release(tmp_path):
    runner, session, broker = runtime(tmp_path, {"app.py": PYTHON_HTTP})
    tree = broker.workspace_tree_hash()
    try:
        handle, evidence = runner.application(
            ("python3", "-B", "app.py"),
            _grant(),
            "r04-http",
            tree,
            CancellationToken(),
            mode="http",
            port=0,
            routes=(
                HTTPExpectation(body="ok", content_type="text/plain"),
                HTTPExpectation(
                    path="/healthz", json_body={"status": "ok"}, content_type="application/json"
                ),
            ),
            cwd=".",
            timeout=4,
        )
        assert evidence.ready.ready, evidence.ready.detail
        assert evidence.ready.port and evidence.ready.port != 8000
        assert evidence.ready.log_port == evidence.ready.port
        assert len(evidence.ready.observations) == 2
        cleanup = runner.cleanup(
            handle.handle_id, handle.operation_id, tree, port=evidence.ready.port
        )
        assert cleanup.graceful and not cleanup.forced, cleanup.model_dump()
        assert cleanup.processes_exited and cleanup.port_released and cleanup.container_removed
        assert broker.workspace_tree_hash() == tree
    finally:
        session.close()


@pytest.mark.parametrize(
    "variant",
    [
        "missing_ready",
        "missing_port",
        "body",
        "sigterm",
        "status",
        "type",
        "route",
        "port",
        "stderr_ready",
        "late_probe",
    ],
)
def test_http_negative_readiness_or_sigterm_never_passes(tmp_path, variant):
    source = PYTHON_HTTP
    routes = (HTTPExpectation(body="ok"),)
    if variant == "missing_ready":
        source = source.replace("print('READY port='+str(server.server_port),flush=True)", "pass")
    if variant == "missing_port":
        source = source.replace(
            "print('READY port='+str(server.server_port),flush=True)", "print('READY',flush=True)"
        )
    if variant == "stderr_ready":
        source = "import sys\n" + source.replace("flush=True)", "file=sys.stderr,flush=True)")
    if variant == "body":
        routes = (HTTPExpectation(body="wrong"),)
    if variant == "status":
        routes = (HTTPExpectation(status_code=204),)
    if variant == "type":
        routes = (HTTPExpectation(content_type="application/json"),)
    if variant == "route":
        routes = (HTTPExpectation(path="/not-the-root", body="ok"),)
    if variant == "port":
        source = source.replace("str(server.server_port)", "str(1)")
    if variant == "sigterm":
        source = source.replace(
            "signal.signal(signal.SIGTERM,stop)", "signal.signal(signal.SIGTERM,signal.SIG_IGN)"
        )
    if variant == "late_probe":
        source = source.replace(
            "def do_GET(self):", "def do_GET(self):\n  import time;time.sleep(2)"
        )
    runner, session, broker = runtime(tmp_path, {"app.py": source})
    try:
        handle, evidence = runner.application(
            ("python3", "-B", "app.py"),
            _grant(),
            "r04-negative",
            broker.workspace_tree_hash(),
            CancellationToken(),
            mode="http",
            port=0,
            routes=routes,
            cwd=".",
            timeout=1.5,
        )
        if variant == "sigterm":
            assert evidence.ready.ready, evidence.ready.detail
            cleanup = runner.cleanup(
                handle.handle_id,
                handle.operation_id,
                broker.workspace_tree_hash(),
                port=evidence.ready.port,
            )
            assert not cleanup.graceful and cleanup.forced
        else:
            assert not evidence.ready.ready
            cleanup = evidence.cleanup
        assert cleanup.processes_exited and cleanup.port_released and cleanup.container_removed
    finally:
        session.close()


def test_detached_orphan_holding_port_requires_forced_cleanup_and_never_graceful(tmp_path):
    parent = (
        "import subprocess,sys,time,signal\n"
        f"subprocess.Popen([sys.executable,'-B','-u','-c',{PYTHON_HTTP!r}],"
        "start_new_session=True)\n"
        "signal.signal(signal.SIGTERM,lambda *args:sys.exit(0))\n"
        "while True:time.sleep(0.1)\n"
    )
    runner, session, broker = runtime(tmp_path, {"app.py": parent})
    tree = broker.workspace_tree_hash()
    try:
        handle, evidence = runner.application(
            ("python3", "-B", "app.py"),
            _grant(),
            "r04-orphan",
            tree,
            CancellationToken(),
            mode="http",
            port=0,
            routes=(HTTPExpectation(body="ok"),),
            cwd=".",
            timeout=4,
        )
        assert evidence.ready.ready, evidence.ready.detail
        cleanup = runner.cleanup(
            handle.handle_id,
            handle.operation_id,
            tree,
            port=evidence.ready.port,
            expect_running=True,
        )
        assert cleanup.orphan_pids and cleanup.forced and not cleanup.graceful
        assert cleanup.before.bind_available is False
        assert cleanup.processes_exited and cleanup.port_released and cleanup.container_removed
        assert cleanup.after.bind_available is True
    finally:
        session.close()


def test_cancellation_waiting_for_ready_still_observes_exit_and_port_reuse(tmp_path, monkeypatch):
    runner, session, broker = runtime(tmp_path, {"app.py": PYTHON_HTTP})
    token = CancellationToken()
    original = session.runtime_snapshot

    def cancel_after_listener(*args, **kwargs):
        snapshot = original(*args, **kwargs)
        if any(value.get("pids") for value in snapshot.listeners):
            token.cancel()
        return snapshot

    monkeypatch.setattr(session, "runtime_snapshot", cancel_after_listener)
    try:
        _, evidence = runner.application(
            ("python3", "-B", "app.py"),
            _grant(),
            "r04-cancel",
            broker.workspace_tree_hash(),
            token,
            mode="http",
            port=0,
            routes=(HTTPExpectation(body="ok"),),
            cwd=".",
            timeout=4,
        )
        assert not evidence.ready.ready and "cancelled" in evidence.ready.detail
        assert evidence.cleanup.processes_exited and evidence.cleanup.port_released
        assert evidence.cleanup.container_removed
    finally:
        session.close()


@pytest.mark.parametrize(
    "path,runtime_name,files,cwd,roots,expected",
    [
        (
            "src/pkg/entry.py",
            "python",
            {
                "src/pkg/__init__.py": "",
                "src/pkg/other.py": "VALUE=7\n",
                "src/pkg/entry.py": "from .other import VALUE\n",
            },
            "src",
            ("src",),
            "PASS",
        ),
        ("missing.py", "python", {"marker.py": "VALUE=7\n"}, ".", ("",), "FAIL"),
        (
            "app.py",
            "python",
            {"app.py": "import socket\ns=socket.socket();s.bind(('127.0.0.1',0));s.listen(1)\n"},
            ".",
            ("",),
            "FAIL",
        ),
        (
            "entry.mjs",
            "node",
            {"package.json": '{"type":"module"}', "entry.mjs": "export const value=7;\n"},
            ".",
            ("",),
            "PASS",
        ),
        (
            "entry.cjs",
            "node",
            {"entry.cjs": "require('http').createServer().listen(0)\n"},
            ".",
            ("",),
            "FAIL",
        ),
    ],
)
def test_entry_src_cwd_missing_module_esm_and_import_binding(
    tmp_path,
    path,
    runtime_name,
    files,
    cwd,
    roots,
    expected,
):
    runner, session, broker = runtime(tmp_path, files)
    try:
        receipt = runner.entry(
            EntryPointSpec(
                path=path,
                runtime=runtime_name,
                cwd=cwd,
                import_roots=roots,
                no_listen_on_import=True,
            ),
            ("python3", "-B", "-m", "unittest"),
            _grant(),
            "r04-entry-layout",
            broker.workspace_tree_hash(),
            CancellationToken(),
        )
        assert receipt.status == expected, receipt.model_dump()
        assert receipt.cleanup.processes_exited and receipt.cleanup.port_released
        assert receipt.cleanup.container_removed
    finally:
        session.close()


def test_controlled_entry_does_not_bypass_original_lease_grant_or_frozen_assets(tmp_path):
    runner, session, broker = runtime(tmp_path, {"app.py": "VALUE=7\n"})
    try:
        for grant in (_grant(execute=False), _grant(commands=("node",))):
            with pytest.raises(DomainError):
                runner.launch(
                    ("python3", "-B", "app.py"),
                    grant,
                    "r04-denied",
                    "entry",
                    cwd=".",
                    timeout=1,
                    entry=EntryPointSpec(path="app.py", runtime="python"),
                )
        assert not session._records
        runner.policy = runner.policy.model_copy(update={"control_assets_hash": "0" * 64})
        with pytest.raises(DomainError, match="assets"):
            runner.launch(
                ("python3", "-B", "app.py"),
                _grant(),
                "r04-assets",
                "entry",
                cwd=".",
                timeout=1,
                entry=EntryPointSpec(path="app.py", runtime="python"),
            )
        assert not session._records
        assert not broker._mount_fds
    finally:
        session.close()


def test_revoked_lease_removes_owned_container_without_manufacturing_cleanup_proof(tmp_path):
    runner, session, broker = runtime(tmp_path, {"app.py": PYTHON_HTTP})
    tree = broker.workspace_tree_hash()
    try:
        handle, evidence = runner.application(
            ("python3", "-B", "app.py"),
            _grant(),
            "r04-revoke",
            tree,
            CancellationToken(),
            mode="http",
            port=0,
            routes=(HTTPExpectation(body="ok"),),
            cwd=".",
            timeout=4,
        )
        assert evidence.ready.ready
        broker.authority.revoke()
        cleanup = runner.cleanup(
            handle.handle_id, handle.operation_id, tree, port=evidence.ready.port
        )
        assert not cleanup.graceful and cleanup.processes_exited is None
        assert cleanup.port_released is None and cleanup.container_removed
    finally:
        session.close()


PYTHON_SELFCHECK = """
import http.server,http.client,threading
class Handler(http.server.BaseHTTPRequestHandler):
 def do_GET(self):
  self.send_response(200);self.send_header('Content-Type','text/plain')
  self.end_headers();self.wfile.write(b'ok')
server=http.server.HTTPServer(('127.0.0.1',0),Handler)
thread=threading.Thread(target=server.serve_forever);thread.start()
try:
 connection=http.client.HTTPConnection('127.0.0.1',server.server_port)
 connection.request('GET','/');response=connection.getresponse()
 assert response.status==200 and response.read()==b'ok'
 print('observed status',response.status,flush=True)
 connection.close()
finally:
 server.shutdown();thread.join();server.server_close()
"""


@pytest.mark.parametrize("variant", ["ok", "no_status", "wrong_status", "error", "stderr_status"])
def test_python_selfcheck_observed_response_stdout_and_failure_cleanup(tmp_path, variant):
    source = PYTHON_SELFCHECK
    if variant == "no_status":
        source = source.replace("print('observed status',response.status,flush=True)", "pass")
    if variant == "wrong_status":
        source = source.replace(
            "print('observed status',response.status,flush=True)",
            "print('observed status',204,flush=True)",
        )
    if variant == "error":
        source = source.replace("connection.close()", "connection.close();raise ValueError('bad')")
    if variant == "stderr_status":
        source = "import sys\n" + source.replace("flush=True)", "file=sys.stderr,flush=True)")
    runner, session, broker = runtime(tmp_path, {"scripts/selfcheck.py": source})
    try:
        _, evidence = runner.application(
            ("python3", "-B", "scripts/selfcheck.py"),
            _grant(),
            "r04-python-selfcheck",
            broker.workspace_tree_hash(),
            CancellationToken(),
            mode="oneshot",
            port=0,
            routes=(HTTPExpectation(body="ok", content_type="text/plain"),),
            cwd=".",
            timeout=6,
        )
        assert evidence.ready.ready is (variant == "ok"), evidence.ready.model_dump()
        assert evidence.ready.observations[0].status_code == 200
        assert evidence.root_exit_code == (1 if variant == "error" else 0)
        assert evidence.cleanup.processes_exited and evidence.cleanup.port_released
        assert evidence.cleanup.container_removed
    finally:
        session.close()


@pytest.mark.parametrize(
    "path,source,expected",
    [
        ("pkg/entry.py", "from .missing import nope\n", "FAIL"),
        ("examples/entry.cjs", "module.exports=require('../../missing')\n", "FAIL"),
        ("pkg/entry.py", "from .other import VALUE\n", "PASS"),
        ("examples/entry.cjs", "module.exports={value:7}\n", "PASS"),
    ],
)
def test_independent_entry_import_and_load(tmp_path, path, source, expected):
    files = {path: source, "pkg/__init__.py": "", "pkg/other.py": "VALUE=7\n"}
    runner, session, broker = runtime(tmp_path, files)
    spec = EntryPointSpec(path=path, runtime="python" if path.endswith(".py") else "node")
    try:
        receipt = runner.entry(
            spec,
            ("python3", "-B", "-m", "unittest"),
            _grant(),
            "r04-entry",
            broker.workspace_tree_hash(),
            CancellationToken(),
        )
        assert receipt.status == expected, receipt.model_dump()
        assert receipt.cleanup.processes_exited and receipt.cleanup.port_released
        assert receipt.cleanup.container_removed
    finally:
        session.close()


NODE_SELFCHECK = """
import http from 'node:http';
const server=http.createServer((req,res)=>{
 res.setHeader('Content-Type','text/plain');res.end('ok');
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
try {
 const response=await new Promise((resolve,reject)=>http.get(
  'http://127.0.0.1:'+server.address().port+'/',resolve).on('error',reject));
 let text='';for await (const part of response)text+=part;
 if(response.statusCode!==200||text!=='ok')throw new Error('bad response');
 console.log('observed status',response.statusCode);
} finally { await new Promise(resolve=>server.close(resolve)); }
"""


@pytest.mark.parametrize("prints", [True, False])
def test_npm_selfcheck_observes_actual_http_and_ignores_npm_banner(tmp_path, prints):
    source = (
        NODE_SELFCHECK
        if prints
        else NODE_SELFCHECK.replace("console.log('observed status',response.statusCode);", "")
    )
    runner, session, broker = runtime(
        tmp_path,
        {
            "scripts/selfcheck.mjs": source,
            "package.json": json.dumps(
                {
                    "name": "r04-fixture",
                    "version": "0.200.0",
                    "scripts": {"selfcheck": "node scripts/selfcheck.mjs"},
                }
            ),
        },
    )
    try:
        handle, evidence = runner.application(
            ("npm", "run", "selfcheck"),
            _grant(),
            "r04-selfcheck",
            broker.workspace_tree_hash(),
            CancellationToken(),
            mode="oneshot",
            port=8000,
            routes=(),
            cwd=".",
            timeout=6,
        )
        assert evidence.ready.ready is prints, evidence.ready.model_dump()
        assert evidence.ready.port and evidence.ready.port != 8000
        assert evidence.root_exit_code == 0
        assert evidence.cleanup.graceful and evidence.cleanup.container_removed
        assert evidence.cleanup.port_released and evidence.cleanup.processes_exited
    finally:
        session.close()
