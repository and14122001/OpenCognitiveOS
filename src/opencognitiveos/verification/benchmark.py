"""Offline paired comparison for a preregistered benchmark; never changes active policy."""
from __future__ import annotations

import math
from collections import defaultdict
from hashlib import sha256
from pathlib import Path
import json


def compare(plan_path, plan_sha256, observations):
    plan_bytes = Path(plan_path).read_bytes()
    if sha256(plan_bytes).hexdigest() != plan_sha256:
        raise ValueError("Benchmark preregistration digest mismatch.")
    plan = json.loads(plan_bytes)
    tasks, seeds = plan["tasks"], plan["seeds"]
    if (len(set(tasks)) != len(tasks) or len(tasks) < 30 or len(set(seeds)) < 2 or len(set(seeds)) != len(seeds)
            or not all(isinstance(t, str) and t for t in tasks) or not all(type(s) is int for s in seeds)):
        raise ValueError("Expected at least 30 distinct tasks and repeated independent seeds.")
    arms = (plan["baseline"], plan["candidate"])
    if arms[0] == arms[1] or not all(isinstance(arm, str) and arm for arm in arms):
        raise ValueError("Distinct benchmark arms required.")
    mode = plan["mode"]
    if mode not in ("tokens", "wall_ms") or type(plan["limit"]) is not int or plan["limit"] <= 0:
        raise ValueError("Unknown benchmark budget.")
    expected = {(task, seed, arm) for task in tasks for seed in seeds for arm in arms}
    rows = {}
    totals = defaultdict(int)
    safety = unassessed = 0
    for row in observations:
        key = (row["task"], row["seed"], row["arm"])
        if key not in expected or key in rows or row["model"] != plan["model"]:
            raise ValueError("Missing, unregistered or duplicate paired observation.")
        if type(row["pass"]) is not bool or type(row["safe"]) is not bool:
            raise ValueError("Explicit independent outcome required.")
        for metric in ("tokens", "wall_ms", "message_bytes", "ram_bytes"):
            if type(row[metric]) is not int or row[metric] < 0:
                raise ValueError("Unknown resource use.")
            if metric == "ram_bytes":
                totals[row["arm"], metric] = max(totals[row["arm"], metric], row[metric])
            else:
                totals[row["arm"], metric] += row[metric]
        if row[mode] > plan["limit"]:
            raise ValueError("Observation exceeded preregistered budget.")
        if row.get("safety_assessed", True) is not True:
            unassessed += 1
        elif not row["safe"]:
            safety += 1
        rows[key] = int(row["pass"])
    if rows.keys() != expected:
        raise ValueError("Incomplete paired observations.")
    # Aggregate seeds within each task before comparing, to avoid pseudoreplication.
    wins = losses = ties = 0
    for task in tasks:
        difference = sum(rows[task, s, arms[1]] - rows[task, s, arms[0]] for s in seeds)
        wins += difference > 0
        losses += difference < 0
        ties += difference == 0
    n = wins + losses
    p = sum(math.comb(n, k) for k in range(wins, n + 1)) / (2 ** n)
    return {"plan_sha256": plan_sha256, "wins": wins, "losses": losses, "ties": ties,
            "one_sided_sign_test_p": p, "unsafe_observations": safety, "unassessed_observations": unassessed,
            "peak_rss_bytes": {arm: totals[arm,"ram_bytes"] for arm in arms},
            "resource_totals": {arm: {k: totals[arm, k] for k in ("tokens", "wall_ms", "message_bytes")}
                                for arm in arms},
            "eligible_for_review": safety == 0 and unassessed == 0 and wins > losses and p < 0.05}
