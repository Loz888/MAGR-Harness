"""Bind current independent readiness to the same 12+12 quality candidate."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from mgar_v3.quality_eval.h12_quality_runner import QualityRunner, write_quality_freeze

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public-dev-freeze", type=Path, required=True)
    parser.add_argument("--public-dev-freeze-sha256", required=True)
    parser.add_argument("--holdout-image-map", type=Path, required=True)
    parser.add_argument("--holdout-readiness", type=Path)
    parser.add_argument("--holdout-readiness-sha256")
    parser.add_argument("--complete-matrix-authorization", type=Path)
    parser.add_argument("--complete-matrix-authorization-sha256")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if bool(args.holdout_readiness) != bool(args.holdout_readiness_sha256):
        parser.error("readiness path and SHA256 are required together")
    if bool(args.complete_matrix_authorization) != bool(args.complete_matrix_authorization_sha256):
        parser.error('complete-matrix authorization path and SHA256 are required together')
    try:
        frozen = write_quality_freeze(
            ROOT, args.out.absolute(),
            development=(args.public_dev_freeze.absolute(), args.public_dev_freeze_sha256),
            holdout_image_map=args.holdout_image_map.absolute(),
            readiness=(args.holdout_readiness.absolute(), args.holdout_readiness_sha256)
            if args.holdout_readiness else None,
            complete_matrix_authorization=(args.complete_matrix_authorization.absolute(),
                                           args.complete_matrix_authorization_sha256)
            if args.complete_matrix_authorization else None,
        )
        digest = hashlib.sha256(args.out.read_bytes()).hexdigest()
        controller = QualityRunner(
            SimpleNamespace(harness=None), experiment_id="quality-readiness-validation",
            evidence=args.out.with_name(args.out.name + "-validation"),
            freeze_path=args.out.absolute(), freeze_sha256=digest, api_key=None, root=ROOT,
        )
        # A frozen label alone grants nothing: validate all physical evidence
        # before reporting FROZEN, and again at every paid dispatch.
        if args.holdout_readiness:
            controller._require_public_development_frozen()
        print(json.dumps({"status": frozen["document"]["phase"], "paid_calls": 0,
                          "quality_status": "NOT_RUN", "freeze_sha256": digest}))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "BLOCKED", "paid_calls": 0,
                          "error_type": type(exc).__name__,
                          "error_code": getattr(exc, "code", "H12_QUALITY_FREEZE_ERROR")}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
