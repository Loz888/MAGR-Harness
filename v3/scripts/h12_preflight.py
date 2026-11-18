"""Developer H12 source/judge preflight; never a model-exposed project tool."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from mgar_v3.quality_eval.h12 import preflight
from mgar_v3.quality_eval.harness_v4 import CatalogError, repo_root


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=("dev", "holdout"), default="dev")
    parser.add_argument("--root", type=Path, default=repo_root())
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--execute-readiness",
        action="store_true",
        help="Run all twelve public dev prerequisite commands through the offline Broker",
    )
    parser.add_argument("--image-map", type=Path)
    parser.add_argument(
        "--environment-revision",
        default="h12-dev-environment-v3",
        choices=("h12-dev-environment-v3", "h12-dev-environment-v4"),
    )
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--draft-proposal", type=Path,
                        help="Zero-model draft validity only; never grants paid dispatch")
    parser.add_argument(
        "--sources", type=Path, help="Private JSON map from task ID to local source path"
    )
    args = parser.parse_args()
    if args.execute_readiness:
        if args.split != "dev" or args.image_map is None or args.evidence is None:
            parser.error(
                "execution readiness requires --split dev, --image-map "
                "and a new --evidence directory"
            )
        from mgar_v3.quality_eval.h12_readiness import execution_readiness

        image_locks = json.loads(args.image_map.read_text())
        if not isinstance(image_locks, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in image_locks.items()
        ):
            parser.error("image map must map source pins to private environment locks")
        result = execution_readiness(
            image_locks,
            args.evidence,
            root=args.root,
            environment_revision=args.environment_revision,
            **({"draft_proposal": args.draft_proposal} if args.draft_proposal else {}),
        )
        print(
            json.dumps(
                {
                    "status": result["status"],
                    "split": "dev",
                    "denominator": 12,
                    "ready_count": result["ready_count"],
                    "paid_model_calls": 0,
                    "record": str(
                        args.evidence
                        / f"readiness-{args.environment_revision.rsplit('-', 1)[1]}.json"
                    ),
                }
            )
        )
        return 0 if result["status"] in {"READY", "DRAFT_READY"} else 2
    if args.split == "holdout" and args.output is None:
        parser.error("holdout requires a private --output; its task metadata is never printed")
    try:
        sources = json.loads(args.sources.read_text()) if args.sources else None
        if sources is not None and (
            not isinstance(sources, dict)
            or not all(isinstance(k, str) and isinstance(v, str) for k, v in sources.items())
        ):
            raise CatalogError("Source overrides must map task IDs to local paths")
        result = preflight(args.split, args.root, sources)
    except (CatalogError, OSError, ValueError):
        print(json.dumps({"status": "NOT_RUN", "error": "PREFLIGHT_INVALID_OR_UNAVAILABLE"}))
        return 2
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary = tempfile.mkstemp(prefix=".h12-preflight-", dir=args.output.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(result, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, args.output)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        print(
            json.dumps(
                {
                    "status": result["status"],
                    "split": args.split,
                    "denominator": 12,
                    "record": str(args.output),
                }
            )
        )
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "SOURCE_READY" else 2


if __name__ == "__main__":
    sys.exit(main())
