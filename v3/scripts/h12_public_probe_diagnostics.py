"""Create a reviewable public diagnostic asset; never overwrite a frozen probe."""

import argparse
import hashlib
import json
from pathlib import Path

from mgar_v3.quality_eval.public_probe_diagnostics import instrument_public_probe


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raw = args.input.read_bytes()
    if hashlib.sha256(raw).hexdigest() != args.sha256:
        raise SystemExit("Original public probe hash differs")
    revised = instrument_public_probe(raw.decode("utf-8")).encode()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("xb") as stream:
        stream.write(revised)
    print(json.dumps({"source_sha256": args.sha256,
                      "new_sha256": hashlib.sha256(revised).hexdigest(),
                      "output": str(args.output), "requires_new_freeze": True,
                      "old_asset_overwritten": False, "new_model_calls": 0}))


if __name__ == "__main__":
    main()
