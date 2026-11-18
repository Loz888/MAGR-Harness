"""Incremental replacement edits that reuse the H05 patch protocol.

The V4 write tool keeps accepting the historical full-file ``content`` form and
adds a hash-pinned ``replacements`` form.  Matching and ambiguity rules are
delegated to the H05 implementation so ``PATCH_HASH_MISMATCH``,
``PATCH_CONTEXT_NOT_FOUND`` and ``PATCH_AMBIGUOUS`` have exactly one definition.
Only the replacement fragments travel through the model output; the file itself
never has to be echoed back for a small change.
"""

from __future__ import annotations

from collections.abc import Sequence

from mgar_v3.domain import DomainError
from mgar_v3.harness.contracts import TextReplacement, WorkspaceEdit, sha256_text
from mgar_v3.repair.worktree import TextReplacement as H05TextReplacement
from mgar_v3.repair.worktree import apply_text_replacements

DEFAULT_MAX_OUTPUT_BYTES = 65_536
DEFAULT_MAX_REPLACEMENTS = 64
MAX_FILE_BYTES = 2_000_000


def _h05(replacements: Sequence[TextReplacement]) -> list[H05TextReplacement]:
    pinned = []
    for item in replacements:
        payload = item.model_dump()
        payload["old_sha256"] = item.old_sha256 or sha256_text(item.old_text)
        pinned.append(H05TextReplacement.model_validate(payload))
    return pinned


def apply_replacements(content: str, replacements: Sequence[TextReplacement]) -> str:
    """Apply hash-pinned replacements and reject an oversized result."""
    updated = apply_text_replacements(content, _h05(replacements))
    if len(updated.encode()) > MAX_FILE_BYTES:
        raise DomainError("PATCH_LIMIT", "Edited file exceeds the per-file byte bound", 413)
    return updated


def output_bytes(edits: Sequence[WorkspaceEdit]) -> int:
    """Bytes the model had to emit for these edits (not the resulting file size)."""
    total = 0
    for edit in edits:
        if edit.replacements is not None:
            total += sum(len(item.new_text.encode()) for item in edit.replacements)
        elif edit.content is not None:
            total += len(edit.content.encode())
    return total


def check_output_budget(
    edits: Sequence[WorkspaceEdit],
    *,
    max_output_bytes: int,
    max_replacements: int,
) -> None:
    """Enforce per-edit replacement count and the emitted-byte budget."""
    for edit in edits:
        if edit.replacements is not None and len(edit.replacements) > max_replacements:
            raise DomainError("PATCH_LIMIT", "Patch carries too many replacements", 413)
    if output_bytes(edits) > max_output_bytes:
        raise DomainError("PATCH_LIMIT", "Patch exceeds the total byte budget", 413)


def protocol(edits: Sequence[WorkspaceEdit]) -> str:
    """Report which wire protocol one patch used."""
    full = any(edit.content is not None for edit in edits)
    incremental = any(edit.replacements is not None for edit in edits)
    if full and incremental:
        return "mixed"
    return "incremental" if incremental else "full"


def staged_content(edit: WorkspaceEdit, before: str) -> str:
    """Resolve one edit against its pre-image into the full updated content."""
    if edit.replacements is not None:
        return apply_replacements(before, edit.replacements)
    if edit.content is None:  # pragma: no cover - the contract enforces one form
        raise DomainError("HARNESS_TOOL_ARGUMENT", "Edit carries no content", 422)
    return edit.content
