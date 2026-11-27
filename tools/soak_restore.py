"""Foreground restore/replay endurance probe for the public native fixture, without scheduling."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("manifest_sha256")
    parser.add_argument("--seconds", type=float, default=7 * 86400)
    parser.add_argument("--interval", type=float, default=60)
    args = parser.parse_args()
    if (not all(math.isfinite(n) for n in (args.seconds, args.interval))
            or not 0 < args.interval <= args.seconds <= 7 * 86400
            or hashlib.sha256((args.source/"manifest.json").read_bytes()).hexdigest() != args.manifest_sha256):
        parser.error("Expected the pinned source manifest and a positive bounded observation interval.")
    script = Path(__file__).with_name("restore_native_fixture.py")
    start = time.monotonic()
    previous = start
    checks = 0
    while True:
        now = time.monotonic()
        if now - previous > args.interval + 60:
            raise RuntimeError("Observation gap exceeds tolerance.")
        previous = now
        # New process, database, CAS and worktree root for every attempt.
        out = subprocess.run([sys.executable, str(script), str(args.source), args.manifest_sha256],
                             capture_output=True, check=True, timeout=45)
        if args.manifest_sha256.encode() not in out.stdout:
            raise RuntimeError("Missing independent receipt replay evidence.")
        checks += 1
        elapsed = time.monotonic() - start
        print(json.dumps({"utc":datetime.now(timezone.utc).isoformat(),"check":checks,
                          "elapsed_seconds":round(elapsed,3),"source_sha256":args.manifest_sha256}),flush=True)
        if elapsed >= args.seconds:
            print(json.dumps({"successful_restores":checks,"observed_seconds":elapsed,
                              "seven_days_observed":elapsed >= 7*86400}),flush=True)
            return
        time.sleep(min(args.interval,args.seconds-elapsed))


if __name__ == "__main__":
    main()
