"""Exact-source preparation checks; no network, task/oracle data or paid calls."""

from __future__ import annotations

import importlib.util
import io
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/h12_prepare_sources.py"
SPEC = importlib.util.spec_from_file_location("h12_sources", SCRIPT)
PREP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PREP)


def archive(entries):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as tar:
        for name, kind, content in entries:
            member = tarfile.TarInfo(name)
            if kind == "link":
                member.type = tarfile.SYMTYPE
                member.linkname = content
                tar.addfile(member)
            else:
                payload = content.encode()
                member.size = len(payload)
                tar.addfile(member, io.BytesIO(payload))
    return stream.getvalue()


@pytest.mark.parametrize(
    "entries",
    [
        [("root/../escape", "file", "bad")],
        [("root/link", "link", "../../escape")],
        [("root/link/file", "file", "bad"), ("root/link", "link", "other")],
        [("root/.git/config", "file", "bad")],
    ],
)
def test_archive_rejects_traversal_before_writing(tmp_path, entries):
    with pytest.raises(ValueError):
        PREP.safe_extract(archive(entries), tmp_path)
    assert not list(tmp_path.iterdir())


def test_raw_merkle_matches_real_git_tree_with_mode_and_symlink(tmp_path):
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested/code.py").write_text("value = 1\n")
    executable = tmp_path / "execute"
    executable.write_text("#!/bin/false\n")
    executable.chmod(0o755)
    (tmp_path / "link").symlink_to("nested/code.py")
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "--all"], check=True, capture_output=True)
    expected = subprocess.check_output(["git", "-C", str(tmp_path), "write-tree"]).decode().strip()
    assert PREP.tree_hash(tmp_path) == expected


def test_original_signed_commit_requires_exact_object_hash():
    headers = (
        "tree "
        + "a" * 40
        + "\nauthor Test <test@example.com> 1 +0000\ncommitter Test <test@example.com> 1 +0000"
    )
    signature = "-----BEGIN PGP SIGNATURE-----\nsynthetic\n-----END PGP SIGNATURE-----\n"
    raw = (headers + "\ngpgsig " + signature.replace("\n", "\n ") + "\n\nmessage").encode()
    identity = PREP.object_hash("commit", raw)
    metadata = {
        "sha": identity,
        "verification": {"payload": headers + "\n\nmessage", "signature": signature},
    }
    assert PREP.original_commit(metadata, identity) == raw
    metadata["verification"]["signature"] += "changed"
    with pytest.raises(ValueError):
        PREP.original_commit(metadata, identity)


def incomplete(tmp_path, *, origin="https://github.com/psf/requests.git"):
    cache = tmp_path / "cache"
    target = cache / "psf/requests" / ("a" * 40)
    target.mkdir(parents=True)
    subprocess.run(["git", "init", str(target)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(target), "remote", "add", "origin", origin],
        check=True,
        capture_output=True,
    )
    output = tmp_path / "attempt"
    output.mkdir()
    return (
        PREP.Preparation(cache, output),
        target,
        {
            "repository": "psf/requests",
            "commit": "a" * 40,
            "steps": [],
        },
    )


def test_incomplete_owned_cache_is_preserved_without_deleting_old_lock(tmp_path):
    preparation, target, record = incomplete(tmp_path)
    (target / ".git/shallow.lock").write_text("old attempt lock")
    assert not preparation.inspect_existing(record, target)
    preserved = Path(record["preserved_incomplete_cache"])
    assert not target.exists()
    assert (preserved / ".git/shallow.lock").read_text() == "old attempt lock"


@pytest.mark.parametrize("foreign", ["origin", "working", "filter"])
def test_incomplete_foreign_or_populated_cache_is_not_moved(tmp_path, foreign):
    preparation, target, record = incomplete(
        tmp_path,
        origin="https://github.com/other/user.git"
        if foreign == "origin"
        else "https://github.com/psf/requests.git",
    )
    if foreign == "working":
        (target / "user-file").write_text("preserve me")
    if foreign == "filter":
        with (target / ".git/config").open("a") as stream:
            stream.write('\n[filter "external"]\nclean = unwanted-project-command\n')
    with pytest.raises(ValueError):
        preparation.inspect_existing(record, target)
    assert target.exists() and "preserved_incomplete_cache" not in record


def test_timeout_kills_only_its_own_spawned_process_group(tmp_path):
    unrelated = subprocess.Popen(
        [sys.executable, "-c", "import time;time.sleep(30)"], start_new_session=True
    )
    try:
        child = (
            "import subprocess,sys,time; "
            "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']); "
            "print(p.pid,flush=True); print('kept stderr',file=sys.stderr,flush=True); "
            "time.sleep(30)"
        )
        result = PREP.run_owned([sys.executable, "-c", child], dict(os.environ), 0.2)
        assert result["timed_out"] and b"kept stderr" in result["stderr"]
        child_pid = int(result["stdout"].strip())
        state = Path(f"/proc/{child_pid}/stat")
        assert not state.exists() or state.read_text().split()[2] == "Z"
        assert unrelated.poll() is None
    finally:
        unrelated.kill()
        unrelated.wait(timeout=3)
