"""Independent replay of archived integer decisions without submitting any model requests."""
import argparse
import base64
from collections import Counter
import gzip
import hashlib
import json
import operator
from pathlib import Path
import re


def integer_score(problem, answer):
    match = re.fullmatch(r"(\d+) ([+*-]) (\d+)", problem)
    if match is None:
        raise ValueError("Unknown problem")
    left, symbol, right = match.groups()
    expected = {"+":operator.add, "-":operator.sub, "*":operator.mul}[symbol](int(left), int(right))
    return re.fullmatch(r"-?\d+", answer) is not None and int(answer) == expected


def replay(directory):
    plan = json.loads((directory / "plan.json").read_bytes())
    inventory = json.loads((directory / "summary.json").read_bytes())
    expected_rows = {(task, seed) for task in plan["tasks"] for seed in plan["seeds"]}
    results = []
    for mode in plan["modes"]:
        paired = {}
        for arm in plan["arms"]:
            name = f"{mode}-{arm}.jsonl"
            raw = gzip.decompress(base64.decodebytes((directory / (name + ".gz.b64")).read_bytes()))
            if hashlib.sha256(raw).hexdigest() != inventory[name]["sha256"]:
                raise ValueError("Archive digest mismatch")
            scores = {}
            for line in raw.splitlines():
                row = json.loads(line)
                identity = row["task"], row["seed"]
                if identity in scores or row["mode"] != mode or row["arm"] != arm:
                    raise ValueError("Duplicate or misfiled observation")
                replies = [c["response"].strip() for c in row["calls"]]
                votes = replies if arm == "independent" else replies[-2:] if arm == "flat" else replies[-1:]
                answer = Counter(votes).most_common(1)[0][0]
                # Only question and response reach the scoring function. No arm, timing or original score.
                passed = integer_score(row["task"], answer)
                total = sum(c["prompt_tokens"] + c["new_tokens"] for c in row["calls"])
                bounds = ((plan["token_limit"], plan["secondary_wall_limit_ms"]) if mode == "tokens"
                          else (plan["secondary_token_limit"], plan["wall_limit_ms"]))
                if (answer != row["answer"] or passed != row["pass"] or total != row["tokens"]
                        or total > bounds[0] or row["wall_ms"] > bounds[1]):
                    raise ValueError("Decision, score or measured resource budget differs")
                scores[identity] = passed
            if set(scores) != expected_rows:
                raise ValueError("Missing or unregistered task/seed")
            paired[arm] = scores
        for arm in plan["arms"]:
            diffs = [sum(paired[arm][t,s] - paired["single"][t,s] for s in plan["seeds"]) for t in plan["tasks"]]
            results.append(dict(mode=mode, arm=arm, correct=sum(paired[arm].values()),
                                wins=sum(x>0 for x in diffs), losses=sum(x<0 for x in diffs), ties=diffs.count(0)))
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    print(json.dumps(replay(parser.parse_args().directory), indent=2))
