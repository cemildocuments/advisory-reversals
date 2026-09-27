"""Reproduce all experiment results from the frozen local observations."""

from pathlib import Path
import argparse
import gzip
import hashlib
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

ROOT = Path(__file__).resolve().parent


def digest(path, decompressed=False):
    opener = gzip.open if decompressed and path.suffix == ".gz" else open
    h = hashlib.sha256()
    with opener(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def verify(manifest, decompressed=False):
    entries = json.loads((ROOT / manifest).read_text())["sha256"]

    def check(item):
        name, expected = item
        p = ROOT / name
        if not p.is_file():
            return name + ": absent"
        return (
            None if digest(p, decompressed) == expected else name + ": digest differs"
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        errors = [e for e in pool.map(check, entries.items()) if e]
    if errors:
        raise RuntimeError("Integrity check failed: " + "; ".join(errors[:12]))
    print(f"Verified {len(entries):,} files using {manifest}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workers",
        type=int,
        default=min(8, os.cpu_count() or 1),
        help="parallel advisory-history workers (default: up to 8)",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="verify frozen inputs and the existing numeric results",
    )
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("--workers must be positive")
    started = time.perf_counter()
    verify("input_checksums.json")
    if not args.verify_only:
        (ROOT / "logs").mkdir(exist_ok=True)
        env = os.environ.copy()
        env.update(
            OMP_NUM_THREADS="1",
            OPENBLAS_NUM_THREADS="1",
            MKL_NUM_THREADS="1",
            VECLIB_MAXIMUM_THREADS="1",
            NUMEXPR_NUM_THREADS="1",
            PYTHONHASHSEED="0",
        )
        stages = [
            (
                "01-tests",
                ["-m", "unittest", "discover", "-s", "src", "-p", "test_*.py", "-v"],
            ),
            ("02-prepare", ["src/prepare.py", "--workers", str(args.workers)]),
            ("03-analyze", ["src/analyze.py"]),
            ("04-audit", ["src/audit_results.py"]),
        ]
        for name, argv in stages:
            print(f"Running {name} ...", flush=True)
            with (ROOT / "logs" / (name + ".log")).open("w") as log:
                result = subprocess.run(
                    [sys.executable, *argv],
                    cwd=ROOT,
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
            if result.returncode:
                raise RuntimeError(f"{name} failed; details: logs/{name}.log")
    verify("expected_results.json", decompressed=True)
    print(
        f"All numerical results match. Elapsed: {time.perf_counter()-started:.1f} seconds.",
        flush=True,
    )


if __name__ == "__main__":
    main()
