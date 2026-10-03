"""Mark a held-out frame as out-of-envelope, or put it back.

Excluding a frame from the gate is a decision, so it is recorded as one: the frame
stays on disk, its metadata gains an ``envelope``/``envelope_reason`` pair, and
``holdout_test.py`` prints every exclusion and its reason before it scores
anything. There is deliberately no delete option.

The distinction this tool exists to enforce:

    excluding  a frame the deployment will never see (subject looking 60 degrees
               up at a camera mounted at or below eye level)
    deleting   a frame that failed - indistinguishable in the result from the
               first case, and the reason every reported accuracy is suspect

Usage
-----
    python tools/holdout_envelope.py --frame 0040 --reason "..."
    python tools/holdout_envelope.py --frame 0040 --in
    python tools/holdout_envelope.py --list
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dataset_meta import (  # noqa: E402
    ENVELOPE_KEY,
    ENVELOPE_OUT,
    ENVELOPE_REASON_KEY,
    GATE_SPLITS,
    HOLDOUT_HARD,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="record an out-of-envelope holdout frame (never delete one)",
    )
    parser.add_argument("--dataset", default="rpi/datasets/faces", help="dataset root")
    parser.add_argument(
        "--frame",
        help="frame name or unique fragment, e.g. 0040 or holdout_front-light_0040",
    )
    parser.add_argument("--reason", help="why the deployment would never produce this frame")
    parser.add_argument(
        "--in",
        dest="back_in",
        action="store_true",
        help="clear the exclusion and score the frame again",
    )
    parser.add_argument("--list", action="store_true", help="show every exclusion and exit")
    return parser.parse_args()


def resolve_frame(root: Path, fragment: str) -> tuple[str, str]:
    """Find the one gate frame matching *fragment*. Returns (name, split)."""
    matches: list[tuple[str, str]] = []
    for split in GATE_SPLITS:
        for path in sorted((root / "images" / split).glob("*.jpg")):
            if fragment in path.name:
                matches.append((path.name, split))
    if not matches:
        raise SystemExit(f"no gate frame matches {fragment!r}")
    if len(matches) > 1:
        listed = "\n  ".join(f"{n}  ({s})" for n, s in matches)
        raise SystemExit(f"{fragment!r} is ambiguous:\n  {listed}")
    return matches[0]


def main() -> int:
    args = parse_args()
    root = Path(args.dataset)
    meta_path = root / "metadata.jsonl"
    if not meta_path.is_file():
        raise SystemExit(f"no metadata at {meta_path}")

    rows = [json.loads(line) for line in meta_path.read_text(encoding="utf-8").splitlines() if line.strip()]

    if args.list:
        found = [r for r in rows if r.get(ENVELOPE_KEY) == ENVELOPE_OUT]
        if not found:
            print("no frames are excluded; every gate frame is scored")
            return 0
        print(f"{len(found)} excluded frame(s), all still on disk:\n")
        for r in found:
            print(f"  {r['file']}  [{r.get('split')}]")
            print(f"      reason: {r.get(ENVELOPE_REASON_KEY, '(none recorded)')}")
        return 0

    if not args.frame:
        raise SystemExit("need --frame, --list, or both")
    if not args.back_in and not (args.reason or "").strip():
        raise SystemExit("--reason is required: an unexplained exclusion is hiding a failure")

    name, split = resolve_frame(root, args.frame)
    if split == HOLDOUT_HARD:
        raise SystemExit(
            f"{name} is in {HOLDOUT_HARD}, which is the fixed regression witness. "
            "It exists to be scored, so it cannot be excluded. If the frame is "
            "genuinely broken, say so in DECISIONS.md instead of shrinking it."
        )

    # metadata stores 'file' as a path relative to the dataset root
    # (images/holdout2/foo.jpg) while the gate looks frames up by bare name, so
    # both forms have to match here.
    target = next(
        (r for r in rows if Path(str(r.get("file", ""))).name == name or r.get("file") == name),
        None,
    )
    if target is None:
        raise SystemExit(f"{name} has no metadata row; refusing to guess")

    shutil.copy2(meta_path, meta_path.with_suffix(".jsonl.bak-envelope"))
    if args.back_in:
        target.pop(ENVELOPE_KEY, None)
        target.pop(ENVELOPE_REASON_KEY, None)
        verb = "restored to the gate"
    else:
        target[ENVELOPE_KEY] = ENVELOPE_OUT
        target[ENVELOPE_REASON_KEY] = args.reason.strip()
        verb = "excluded from the gate"

    meta_path.write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows), encoding="utf-8"
    )
    print(f"{name} [{split}] {verb}")
    print(f"  reason: {target.get(ENVELOPE_REASON_KEY, '(cleared)')}")
    print(f"  frame is still on disk at {root / 'images' / split / name}")
    print("  run tools/holdout_test.py to rescore")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
