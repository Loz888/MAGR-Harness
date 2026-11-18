"""Build a pinned, disconnected DNS/TLS fixture from trusted programs only.

This preparation never copies project sources or runs a project hook. The
private CA signing key and server key stay in the helper image; only the public
certificate is copied to the derived application dependency image.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

from mgar_v3.harness.broker import _docker_environment

ROOT = Path(__file__).resolve().parents[1]
PROGRAMS = ("offline_fixture.py", "offline_http2.cjs")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-lock", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--reuse-fixture-manifest", type=Path)
    args = parser.parse_args()
    base = json.loads(args.base_lock.read_text())
    image = base["image_id"]
    if not isinstance(image, str) or not image.startswith("sha256:") or len(image) != 71:
        raise ValueError("Preparation requires a pinned image ID")
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False, mode=0o700)
    context = out / "helper-context"
    context.mkdir(mode=0o700)
    base_tag = "mgar-h12-fixture-pinned-base:" + uuid4().hex
    hashes = {}
    for name in PROGRAMS:
        source = ROOT / "src/mgar_v3/harness" / name
        hashes[name] = hashlib.sha256(source.read_bytes()).hexdigest()
        shutil.copyfile(source, context / name)
    (context / "certificate.conf").write_text(
        "basicConstraints=critical,CA:FALSE\n"
        "keyUsage=critical,digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\n"
        "subjectAltName=DNS:example.com,DNS:www.google.com\n"
    )
    reuse = (
        json.loads(args.reuse_fixture_manifest.read_text()) if args.reuse_fixture_manifest else None
    )
    reuse_tag = "mgar-h12-fixture-certificate-base:" + uuid4().hex
    certificate_setup = (
        "RUN openssl req -x509 -newkey rsa:2048 -nodes -days 3650 "
        "-subj '/CN=Harness isolated protocol fixture CA' "
        "-addext 'basicConstraints=critical,CA:TRUE' "
        "-addext 'keyUsage=critical,keyCertSign,cRLSign' "
        "-keyout ca.key -out ca.crt && "
        "openssl req -new -newkey rsa:2048 -nodes -subj '/CN=example.com' "
        "-keyout server.key -out server.csr && "
        "openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial "
        "-days 3650 -extfile certificate.conf -out server.crt\n"
    )
    if reuse:
        certificate_setup = (
            "COPY --from=fixturecertificates /opt/harness/server.key "
            "/opt/harness/server.crt /opt/harness/ca.crt ./\n"
        )
    (context / "Dockerfile").write_text(
        (f"FROM {reuse_tag} AS fixturecertificates\n" if reuse else "")
        + f"FROM {base_tag}\nUSER 0:0\nWORKDIR /opt/harness\n"
        + "COPY offline_fixture.py offline_http2.cjs certificate.conf ./\n"
        + certificate_setup
        + "RUN chmod 0400 server.key && chmod 0444 ca.crt server.crt "
        "offline_fixture.py offline_http2.cjs\n"
        "ENTRYPOINT [\"python3\",\"-B\",\"/opt/harness/offline_fixture.py\"]\n"
    )
    tag = "mgar-h12-offline-network:got-v1-" + uuid4().hex[:8]

    def docker(arguments: list[str], *, log: str) -> bytes:
        result = subprocess.run(
            ["docker", *arguments], env=_docker_environment(), capture_output=True, check=False,
        )
        (out / log).write_bytes(result.stdout + result.stderr)
        if result.returncode:
            raise RuntimeError("Trusted offline fixture preparation failed: " + log)
        return result.stdout

    helper_iid = out / "helper-image.id"
    observed = docker(["image", "inspect", "--format", "{{.Id}}", image], log="base-inspect.log")
    if observed.decode().strip() != image:
        raise ValueError("Base image does not match its lock")
    docker(["image", "tag", image, base_tag], log="base-tag.log")
    if reuse:
        old_image = reuse["helper_image_id"]
        observed = docker(["image", "inspect", "--format", "{{.Id}}", old_image],
                          log="certificate-base-inspect.log")
        if observed.decode().strip() != old_image:
            raise ValueError("Certificate source image identity changed")
        docker(["image", "tag", old_image, reuse_tag], log="certificate-base-tag.log")
    docker(["build", "--network=none", "--pull=false", "--iidfile", str(helper_iid),
            "--tag", tag, str(context)], log="helper-build.log")
    helper_image = helper_iid.read_text().strip()
    public_ca = docker([
        "run", "--rm", "--network=none", "--user=65532:65532", "--cap-drop=ALL",
        "--security-opt=no-new-privileges", "--read-only", "--pids-limit=8", "--memory=64m",
        "--entrypoint", "cat", helper_image, "/opt/harness/ca.crt",
    ], log="public-ca-extract.log")
    if reuse and hashlib.sha256(public_ca).hexdigest() != reuse["public_ca_sha256"]:
        raise ValueError("Reused public certificate identity changed")
    (out / "public-ca.pem").write_bytes(public_ca)
    derived = out / "project-context"
    derived.mkdir(mode=0o700)
    (derived / "public-ca.pem").write_bytes(public_ca)
    (derived / "Dockerfile").write_text(
        f"FROM {base_tag}\nCOPY public-ca.pem /opt/harness-fixture/ca.pem\n"
        "ENV NODE_EXTRA_CA_CERTS=/opt/harness-fixture/ca.pem\n"
    )
    project_iid = out / "project-image.id"
    project_tag = "mgar-h12-got-fixture-ca:" + uuid4().hex[:8]
    docker(["build", "--network=none", "--pull=false", "--iidfile", str(project_iid),
            "--tag", project_tag, str(derived)], log="project-build.log")
    manifest = {
        "schema": "h12-offline-protocol-fixture-v1", "mode": "got-network-v1",
        "helper_image_id": helper_image, "base_image_id": image,
        "programs": hashes, "public_ca_sha256": hashlib.sha256(public_ca).hexdigest(),
        "dns": {"localhost": "127.0.0.1", "example.com": "127.0.0.2",
                "www.google.com": "127.0.0.3"},
        "dns_unknown": "NXDOMAIN", "dns_aaaa": "NODATA",
        "tls": {"port": 443, "alpn": ["h2", "http/1.1"], "verify": True},
        "http": {"method": "GET", "path": "/", "response_delay_ms": 15,
                 "cache_control": "public, max-age=60"},
        "timeout_routes": ["10.255.255.1/32", "123.123.123.123/32"],
        "addrconfig_interface": {"kind": "dummy", "name": "harness0", "ipv4": "192.0.2.2/32",
                                 "peer": None, "default_route": False},
        "project_authority": "no external interface/peer/default route, zero capabilities",
        "private_keys": "helper image only, root mode 0400; never in project image",
    }
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()
    (out / "fixture-manifest.json").write_bytes(manifest_bytes)
    project_lock = {
        **base, "image_id": project_iid.read_text().strip(), "image": project_tag,
        "parent_image_id": image,
        "offline_fixture_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "offline_fixture_public_ca_sha256": manifest["public_ca_sha256"],
    }
    (out / "sandbox-lock.json").write_text(json.dumps(project_lock, indent=2) + "\n")
    print(json.dumps({"out": str(out), "helper_image_id": helper_image,
                      "project_image_id": project_lock["image_id"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
