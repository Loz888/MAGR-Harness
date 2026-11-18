"""Bind or seal an explicit public H12 v2 development candidate, without a DB.

This controller selects only development assets from the previously frozen
benchmark metadata. It never parses held-out catalogs or Judge implementations.
Readiness execution is a separate h12_eval.py --prepare-* invocation. Sealing
pins both reports; h12_eval revalidates their full evidence before any dispatch.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any

from mgar_v3.quality_eval.h12_v2_freeze import write_public_development_freeze

ROOT = Path(__file__).resolve().parents[1]


def source_fingerprint() -> str:
    spec = spec_from_file_location("h12_public_dev_fingerprint", ROOT / "scripts/verify.py")
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return str(module.source_fingerprint(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark-freeze", type=Path, required=True)
    parser.add_argument("--benchmark-freeze-sha256", required=True)
    parser.add_argument("--public-plan-map", type=Path, required=True,
                        help='JSON object: task_id -> {"path": root-relative path, "sha256": hash}')
    parser.add_argument("--public-probe-root", type=Path, required=True)
    parser.add_argument("--dev-image-map", type=Path,
                        help="Root-local relocated map; every original lock must be byte-identical")
    parser.add_argument("--development-amendment", type=Path)
    parser.add_argument("--development-amendment-sha256")
    parser.add_argument("--development-scope-amendment", type=Path)
    parser.add_argument("--development-scope-amendment-sha256")
    parser.add_argument("--public-subset-readiness", type=Path)
    parser.add_argument("--public-subset-readiness-sha256")
    parser.add_argument("--full-dev-readiness", type=Path)
    parser.add_argument("--full-dev-readiness-sha256")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    for name in ('public_subset_readiness', 'full_dev_readiness', 'development_amendment',
                 'development_scope_amendment'):
        if bool(getattr(args, name)) != bool(getattr(args, name + '_sha256')):
            parser.error('each readiness path requires its explicit SHA256')
    if bool(args.public_subset_readiness) != bool(args.full_dev_readiness):
        parser.error('sealing requires both executed readiness reports')
    try:
        values: Any = json.loads(args.public_plan_map.read_bytes())
        if not isinstance(values, dict) or not values:
            raise ValueError('explicit public plan map is required')
        references = {}
        for task_id, row in values.items():
            if (
                not isinstance(task_id, str) or not isinstance(row, dict)
                or set(row) != {'path', 'sha256'} or not isinstance(row['path'], str)
                or not isinstance(row['sha256'], str)
                or Path(row['path']).is_absolute() or '..' in Path(row['path']).parts
            ):
                raise ValueError('invalid public plan reference')
            references[task_id] = (ROOT / row['path'], row['sha256'])
        frozen = write_public_development_freeze(
            ROOT, args.out.absolute(),
            benchmark=(args.benchmark_freeze.absolute(), args.benchmark_freeze_sha256),
            candidate_source_sha256=source_fingerprint(), public_plan_refs=references,
            public_probe_root=args.public_probe_root.absolute(),
            public_readiness_ref=(args.public_subset_readiness.absolute(),
                                  args.public_subset_readiness_sha256)
            if args.public_subset_readiness else None,
            full_dev_readiness_ref=(args.full_dev_readiness.absolute(),
                                    args.full_dev_readiness_sha256)
            if args.full_dev_readiness else None,
            dev_image_map=args.dev_image_map.absolute() if args.dev_image_map else None,
            development_amendment=(args.development_amendment.absolute(),
                                   args.development_amendment_sha256)
            if args.development_amendment else None,
            development_scope_amendment=(args.development_scope_amendment.absolute(),
                                         args.development_scope_amendment_sha256)
            if args.development_scope_amendment else None,
        )
        print(json.dumps({
            'status': frozen['document']['phase'], 'paid_calls': 0, 'quality_status': 'NOT_RUN',
            'freeze_sha256': hashlib.sha256(args.out.read_bytes()).hexdigest(),
            'dev_tasks': len(frozen['tasks']['dev']),
            'public_plans': len(frozen['public_plan_refs']),
            'source_sha256': frozen['candidate_source_sha256'],
        }))
        return 0
    except Exception as exc:
        print(json.dumps({'status': 'BLOCKED', 'paid_calls': 0,
                          'error_type': type(exc).__name__,
                          'error_code': getattr(exc, 'code', 'H12_PUBLIC_DEV_FREEZE_ERROR')}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
