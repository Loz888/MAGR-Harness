"""HV4-H05 versioned incremental edit protocol tests.

The editor must let a repair touch a few snippets of a large file without
resending or truncating it, pin both the file and snippet hashes, locate the
snippet by unique context, apply multi-file groups atomically, and keep the
historical full-file protocol working.  Deletion and rename stay behind their
own capability switches.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

import pytest

from mgar_v3.domain import DomainError
from mgar_v3.repair.providers.structured import StructuredPatchProvider
from mgar_v3.repair.repair_agent import PatchProposalRejected, RepairPrompt
from mgar_v3.repair.worktree import (
    FileEdit,
    PatchLimits,
    PatchWorktree,
    TextReplacement,
)

LARGE_LIMITS = PatchLimits(
    max_file_bytes=300_000,
    max_total_bytes=4096,
    max_changed_lines=50,
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@contextmanager
def _worktree(
    files: Mapping[str, str],
    *,
    allowed: list[str] | None = None,
    limits: PatchLimits | None = None,
) -> Iterator[PatchWorktree]:
    tree = PatchWorktree(
        dict(files),
        allowed_paths=allowed if allowed is not None else list(files),
        limits=limits,
    )
    try:
        yield tree
    finally:
        tree.cleanup()


def _replacement(old: str, new: str, *, occurrence: int | None = None) -> TextReplacement:
    return TextReplacement(
        old_sha256=_sha(old), old_text=old, new_text=new, occurrence=occurrence
    )


def _large_content() -> str:
    lines = [
        f"value_{index:05d} = {index * 7:08d}  # large fixture padding"
        for index in range(3100)
    ]
    return "\n".join(lines) + "\n"


def _large_plan(content: str) -> dict[str, Any]:
    lines = content.splitlines()
    replacements = []
    for index in (10, 1550, 3080):
        old = lines[index]
        replacements.append(
            {
                "old_sha256": _sha(old),
                "old_text": old,
                "new_text": old + "  # patched",
            }
        )
    return {
        "summary": "Patch three distant lines",
        "edits": [
            {
                "path": "src/large.py",
                "operation": "update",
                "expected_sha256": _sha(content),
                "replacements": replacements,
            }
        ],
    }


class MockModel:
    def __init__(self, *replies: Any) -> None:
        self.replies = list(replies)
        self.calls: list[Mapping[str, Any]] = []

    def complete(self, *, system: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        self.calls.append(payload)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def _reply(content: str, **overrides: Any) -> dict[str, Any]:
    reply: dict[str, Any] = {
        "content": content,
        "usage": {"prompt_tokens": 500, "completion_tokens": 40},
        "model": "mock-repair",
        "latency_ms": 9.0,
        "finish_reason": "stop",
    }
    reply.update(overrides)
    return reply


def _prompt(
    files: Mapping[str, str],
    *,
    allowed: list[str] | None = None,
    limits: PatchLimits | None = None,
) -> RepairPrompt:
    return RepairPrompt(
        repository="example/h05-fixture",
        objective="Apply a small verified repair",
        language="python",
        attempt=1,
        max_attempts=3,
        files=dict(files),
        allowed_paths=allowed if allowed is not None else list(files),
        limits=limits or PatchLimits(),
    )


def test_large_file_incremental_edit_stays_small() -> None:
    content = _large_content()
    assert len(content.encode()) >= 142 * 1024
    old = content.splitlines()[10]
    with _worktree(
        {"src/large.py": content}, allowed=["src/large.py"], limits=LARGE_LIMITS
    ) as tree:
        result = tree.apply_patch(
            [
                FileEdit(
                    path="src/large.py",
                    operation="update",
                    expected_sha256=_sha(content),
                    replacements=[
                        _replacement(old, old + "  # patched"),
                        _replacement(
                            content.splitlines()[1550],
                            content.splitlines()[1550] + "  # patched",
                        ),
                        _replacement(
                            content.splitlines()[3080],
                            content.splitlines()[3080] + "  # patched",
                        ),
                    ],
                )
            ]
        )
        assert result.protocol == "incremental"
        assert result.replacements == 3
        assert result.output_bytes == result.bytes_written
        assert result.bytes_written < 1024
        assert result.bytes_written * 100 < len(content.encode())
        on_disk = (tree.root / "src/large.py").read_text(encoding="utf-8")
        assert on_disk.count("# patched") == 3
        assert len(tree.get_diff().encode()) < 3000
        assert tree.receipt().patch.patch_id == result.patch_id

    # The historical full-file protocol cannot ship a 142KB+ replacement under
    # the same byte budget; this is the regression the incremental path avoids.
    with _worktree(
        {"src/large.py": content}, allowed=["src/large.py"], limits=LARGE_LIMITS
    ) as tree:
        full = content.replace(old, old + "  # patched")
        with pytest.raises(DomainError) as raised:
            tree.apply_patch(
                [
                    FileEdit(
                        path="src/large.py",
                        operation="update",
                        expected_sha256=_sha(content),
                        content=full,
                    )
                ]
            )
        assert raised.value.code == "PATCH_LIMIT"
        assert tree.get_diff() == ""


def test_repeated_snippet_requires_occurrence() -> None:
    content = "value = 1\nvalue = 1\n"
    with _worktree({"src/dup.py": content}) as tree:
        with pytest.raises(DomainError) as raised:
            tree.apply_patch(
                [
                    FileEdit(
                        path="src/dup.py",
                        operation="update",
                        expected_sha256=_sha(content),
                        replacements=[_replacement("value = 1\n", "value = 2\n")],
                    )
                ]
            )
        assert raised.value.code == "PATCH_AMBIGUOUS"
        assert tree.get_diff() == ""

        tree.apply_patch(
            [
                FileEdit(
                    path="src/dup.py",
                    operation="update",
                    expected_sha256=_sha(content),
                    replacements=[
                        _replacement("value = 1\n", "value = 2\n", occurrence=2)
                    ],
                )
            ]
        )
        assert (tree.root / "src/dup.py").read_text(encoding="utf-8") == (
            "value = 1\nvalue = 2\n"
        )


def test_crlf_and_utf8_replacements_preserve_file_text() -> None:
    content = "alpha\r\n名字 = '旧值'\r\nomega\r\n"
    with _worktree({"src/text.py": content}) as tree:
        tree.apply_patch(
            [
                FileEdit(
                    path="src/text.py",
                    operation="update",
                    expected_sha256=_sha(content),
                    replacements=[
                        _replacement(
                            "名字 = '旧值'\n", "名字 = '新值'\n"
                        ),
                        _replacement("omega\n", "omega  # tail\n"),
                    ],
                )
            ]
        )
        raw = (tree.root / "src/text.py").read_bytes()
        decoded = raw.decode("utf-8")
        assert "名字 = '新值'" in decoded
        assert "omega  # tail" in decoded
        assert "\r\n" in decoded
        assert decoded.replace("\r\n", "").count("\n") == 0


def test_stale_hash_missing_context_and_empty_edit_are_deterministic() -> None:
    content = "def add(a, b):\n    return a - b\n"
    with _worktree({"src/calc.py": content}) as tree:
        with pytest.raises(DomainError) as stale:
            tree.apply_patch(
                [
                    FileEdit(
                        path="src/calc.py",
                        operation="update",
                        expected_sha256=_sha("other"),
                        replacements=[_replacement("a - b", "a + b")],
                    )
                ]
            )
        assert stale.value.code == "PATCH_STALE"

        with pytest.raises(DomainError) as mismatch:
            tree.apply_patch(
                [
                    FileEdit(
                        path="src/calc.py",
                        operation="update",
                        expected_sha256=_sha(content),
                        replacements=[
                            TextReplacement(
                                old_sha256=_sha("not the snippet"),
                                old_text="a - b",
                                new_text="a + b",
                            )
                        ],
                    )
                ]
            )
        assert mismatch.value.code == "PATCH_HASH_MISMATCH"

        with pytest.raises(DomainError) as missing:
            tree.apply_patch(
                [
                    FileEdit(
                        path="src/calc.py",
                        operation="update",
                        expected_sha256=_sha(content),
                        replacements=[_replacement("a * b", "a + b")],
                    )
                ]
            )
        assert missing.value.code == "PATCH_CONTEXT_NOT_FOUND"

        with pytest.raises(DomainError) as empty:
            tree.apply_patch(
                [
                    FileEdit(
                        path="src/calc.py",
                        operation="update",
                        expected_sha256=_sha(content),
                        replacements=[_replacement("a - b", "a - b")],
                    )
                ]
            )
        assert empty.value.code == "PATCH_EMPTY"
        assert (tree.root / "src/calc.py").read_text(encoding="utf-8") == content


def test_multi_file_group_is_atomic_when_one_edit_fails() -> None:
    files = {"src/a.py": "a = 1\n", "src/b.py": "b = 1\n"}
    with _worktree(files) as tree:
        before_tree = tree.tree_hash()
        with pytest.raises(DomainError) as raised:
            tree.apply_patch(
                [
                    FileEdit(
                        path="src/a.py",
                        operation="update",
                        expected_sha256=_sha(files["src/a.py"]),
                        replacements=[_replacement("a = 1\n", "a = 2\n")],
                    ),
                    FileEdit(
                        path="src/b.py",
                        operation="update",
                        expected_sha256=_sha("stale"),
                        replacements=[_replacement("b = 1\n", "b = 2\n")],
                    ),
                ]
            )
        assert raised.value.code == "PATCH_STALE"
        assert tree.tree_hash() == before_tree
        assert tree.get_diff() == ""
        assert (tree.root / "src/a.py").read_text(encoding="utf-8") == "a = 1\n"
        assert (tree.root / "src/b.py").read_text(encoding="utf-8") == "b = 1\n"


def test_resubmitting_the_same_edit_does_not_duplicate_insertion() -> None:
    content = "x = 1\n"
    edit = FileEdit(
        path="src/once.py",
        operation="update",
        expected_sha256=_sha(content),
        replacements=[_replacement("x = 1\n", "x = 1\ny = 2\n")],
    )
    with _worktree({"src/once.py": content}) as tree:
        tree.apply_patch([edit])
        on_disk = (tree.root / "src/once.py").read_text(encoding="utf-8")
        assert on_disk == "x = 1\ny = 2\n"
        with pytest.raises(DomainError) as raised:
            tree.apply_patch([edit])
        assert raised.value.code == "PATCH_STALE"
        assert (tree.root / "src/once.py").read_text(encoding="utf-8") == on_disk


def test_delete_and_rename_require_their_own_capabilities() -> None:
    files = {"src/a.py": "a = 1\n", "src/b.py": "b = 1\n"}
    with _worktree(files) as tree:
        with pytest.raises(DomainError) as delete_denied:
            tree.apply_patch(
                [
                    FileEdit(
                        path="src/b.py",
                        operation="delete",
                        expected_sha256=_sha(files["src/b.py"]),
                    )
                ]
            )
        assert delete_denied.value.code == "PATCH_DELETE_DENIED"

        with pytest.raises(DomainError) as rename_denied:
            tree.apply_patch(
                [
                    FileEdit(
                        path="src/a.py",
                        operation="rename",
                        expected_sha256=_sha(files["src/a.py"]),
                        target="src/moved.py",
                    )
                ]
            )
        assert rename_denied.value.code == "PATCH_RENAME_DENIED"

    limits = PatchLimits(max_deleted_files=1, allow_rename=True)
    with _worktree(files, allowed=["src/a.py", "src/b.py", "src/moved.py"], limits=limits) as tree:
        result = tree.apply_patch(
            [
                FileEdit(
                    path="src/a.py",
                    operation="rename",
                    expected_sha256=_sha(files["src/a.py"]),
                    target="src/moved.py",
                )
            ]
        )
        assert result.files_changed == 2
        assert not (tree.root / "src/a.py").exists()
        assert (tree.root / "src/moved.py").read_text(encoding="utf-8") == "a = 1\n"

        with pytest.raises(DomainError) as conflict:
            tree.apply_patch(
                [
                    FileEdit(
                        path="src/b.py",
                        operation="rename",
                        expected_sha256=_sha(files["src/b.py"]),
                        target="src/moved.py",
                    )
                ]
            )
        assert conflict.value.code == "PATCH_CONFLICT"


def test_failed_verification_keeps_base_and_reviewable_candidate() -> None:
    content = "alpha\nbeta\n"
    with _worktree({"src/keep.py": content}) as tree:
        base_tree = tree.tree_hash()
        tree.apply_patch(
            [
                FileEdit(
                    path="src/keep.py",
                    operation="update",
                    expected_sha256=_sha(content),
                    replacements=[_replacement("beta\n", "gamma\n")],
                )
            ]
        )
        assert tree.candidate() == {"src/keep.py": "alpha\ngamma\n"}
        tree.revert_patch()
        assert (tree.root / "src/keep.py").read_text(encoding="utf-8") == content
        assert tree.tree_hash() == base_tree
        assert tree.get_diff() == ""
        # The candidate remains reviewable after the failed attempt is reverted.
        assert tree.candidate() == {"src/keep.py": "alpha\ngamma\n"}


def test_two_edits_for_one_path_are_rejected_before_writing() -> None:
    content = "value = 1\n"
    with _worktree({"src/conflict.py": content}) as tree:
        with pytest.raises(DomainError) as raised:
            tree.apply_patch(
                [
                    FileEdit(
                        path="src/conflict.py",
                        operation="update",
                        expected_sha256=_sha(content),
                        replacements=[_replacement("value = 1\n", "value = 2\n")],
                    ),
                    FileEdit(
                        path="src/conflict.py",
                        operation="update",
                        expected_sha256=_sha(content),
                        replacements=[_replacement("value = 1\n", "value = 3\n")],
                    ),
                ]
            )
        assert raised.value.code == "PATCH_FORMAT"
        assert (tree.root / "src/conflict.py").read_text(encoding="utf-8") == content


def test_patch_receipt_records_hashes_and_real_worktree_diff() -> None:
    content = "alpha\nbeta\n"
    with _worktree({"src/r.py": content}) as tree:
        result = tree.apply_patch(
            [
                FileEdit(
                    path="src/r.py",
                    operation="update",
                    expected_sha256=_sha(content),
                    replacements=[_replacement("beta\n", "gamma\n")],
                )
            ]
        )
        receipt = tree.receipt()
        assert receipt.patch.patch_id == result.patch_id
        assert receipt.before_sha256 == {"src/r.py": _sha(content)}
        assert receipt.after_sha256 == {"src/r.py": _sha("alpha\ngamma\n")}
        assert receipt.diff_sha256 == _sha(tree.get_diff())
        assert "gamma" in tree.get_diff()


def test_provider_accepts_incremental_and_keeps_full_file_compatibility() -> None:
    content = "def add(a, b):\n    return a - b\n"
    fixed = "def add(a, b):\n    return a + b\n"
    plan = {
        "summary": "Fix add",
        "edits": [
            {
                "path": "src/calc.py",
                "operation": "update",
                "expected_sha256": _sha(content),
                "replacements": [
                    {
                        "old_sha256": _sha("a - b"),
                        "old_text": "a - b",
                        "new_text": "a + b",
                    }
                ],
            }
        ],
    }
    provider = StructuredPatchProvider(MockModel(_reply(json.dumps(plan))))
    accepted = provider.propose(_prompt({"src/calc.py": content}))
    assert accepted.edits[0].replacements is not None
    assert accepted.edits[0].content is None
    with _worktree({"src/calc.py": content}) as tree:
        tree.apply_patch(accepted.edits)
        assert (tree.root / "src/calc.py").read_text(encoding="utf-8") == fixed

    legacy = {
        "summary": "Fix add",
        "edits": [
            {
                "path": "src/calc.py",
                "operation": "update",
                "expected_sha256": _sha(content),
                "content": fixed,
            }
        ],
    }
    provider = StructuredPatchProvider(MockModel(_reply(json.dumps(legacy))))
    accepted = provider.propose(_prompt({"src/calc.py": content}))
    assert accepted.edits[0].content == fixed


def test_provider_rejects_unsafe_shapes() -> None:
    content = "def add(a, b):\n    return a - b\n"
    plans = [
        (
            {
                "summary": "raw hunk",
                "edits": [
                    {
                        "path": "src/calc.py",
                        "operation": "update",
                        "expected_sha256": _sha(content),
                        "hunks": [{"old": "a", "new": "b"}],
                    }
                ],
            },
            "PATCH_DIFF_UNSUPPORTED",
        ),
        (
            {
                "summary": "raw hunk in replacement",
                "edits": [
                    {
                        "path": "src/calc.py",
                        "operation": "update",
                        "expected_sha256": _sha(content),
                        "replacements": [
                            {
                                "old_sha256": _sha("a - b"),
                                "old_text": "a - b",
                                "new_text": "a + b",
                                "hunks": [],
                            }
                        ],
                    }
                ],
            },
            "PATCH_DIFF_UNSUPPORTED",
        ),
        (
            {
                "summary": "both shapes",
                "edits": [
                    {
                        "path": "src/calc.py",
                        "operation": "update",
                        "expected_sha256": _sha(content),
                        "content": content,
                        "replacements": [
                            {
                                "old_sha256": _sha("a - b"),
                                "old_text": "a - b",
                                "new_text": "a + b",
                            }
                        ],
                    }
                ],
            },
            "PATCH_FORMAT",
        ),
        (
            {
                "summary": "rename disabled",
                "edits": [
                    {
                        "path": "src/calc.py",
                        "operation": "rename",
                        "expected_sha256": _sha(content),
                        "target": "src/moved.py",
                    }
                ],
            },
            "PATCH_RENAME_DENIED",
        ),
    ]
    for plan, code in plans:
        provider = StructuredPatchProvider(MockModel(_reply(json.dumps(plan))))
        with pytest.raises(PatchProposalRejected) as raised:
            provider.propose(_prompt({"src/calc.py": content}))
        assert raised.value.code == code


def test_provider_rejects_stale_fragment_hash_and_disabled_delete() -> None:
    content = "def add(a, b):\n    return a - b\n"
    bad_fragment = {
        "summary": "bad fragment",
        "edits": [
            {
                "path": "src/calc.py",
                "operation": "update",
                "expected_sha256": _sha(content),
                "replacements": [
                    {
                        "old_sha256": _sha("other"),
                        "old_text": "a - b",
                        "new_text": "a + b",
                    }
                ],
            }
        ],
    }
    provider = StructuredPatchProvider(MockModel(_reply(json.dumps(bad_fragment))))
    with pytest.raises(PatchProposalRejected) as raised:
        provider.propose(_prompt({"src/calc.py": content}))
    assert raised.value.code == "PATCH_HASH_MISMATCH"

    delete = {
        "summary": "delete",
        "edits": [
            {
                "path": "src/calc.py",
                "operation": "delete",
                "expected_sha256": _sha(content),
            }
        ],
    }
    provider = StructuredPatchProvider(MockModel(_reply(json.dumps(delete))))
    with pytest.raises(PatchProposalRejected) as raised:
        provider.propose(_prompt({"src/calc.py": content}))
    assert raised.value.code == "PATCH_DELETE_DENIED"


def test_large_file_provider_plan_is_small_and_truncation_is_rejected() -> None:
    content = _large_content()
    plan = _large_plan(content)
    provider = StructuredPatchProvider(MockModel(_reply(json.dumps(plan))))
    accepted = provider.propose(
        _prompt({"src/large.py": content}, allowed=["src/large.py"], limits=LARGE_LIMITS)
    )
    with _worktree(
        {"src/large.py": content}, allowed=["src/large.py"], limits=LARGE_LIMITS
    ) as tree:
        result = tree.apply_patch(accepted.edits)
        assert result.protocol == "incremental"
        assert result.output_bytes is not None and result.output_bytes < 2048
        assert (tree.root / "src/large.py").read_text(encoding="utf-8").count(
            "# patched"
        ) == 3

    full_plan = {
        "summary": "resend everything",
        "edits": [
            {
                "path": "src/large.py",
                "operation": "update",
                "expected_sha256": _sha(content),
                "content": content + "tail\n",
            }
        ],
    }
    provider = StructuredPatchProvider(MockModel(_reply(json.dumps(full_plan))))
    with pytest.raises(PatchProposalRejected) as raised:
        provider.propose(
            _prompt(
                {"src/large.py": content},
                allowed=["src/large.py"],
                limits=LARGE_LIMITS,
            )
        )
    assert raised.value.code == "PATCH_LIMIT"

    provider = StructuredPatchProvider(
        MockModel(_reply(json.dumps(plan), finish_reason="length"))
    )
    with pytest.raises(PatchProposalRejected) as truncated:
        provider.propose(
            _prompt(
                {"src/large.py": content},
                allowed=["src/large.py"],
                limits=LARGE_LIMITS,
            )
        )
    assert truncated.value.code == "PATCH_TRUNCATED"
