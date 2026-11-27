"""Run the six preregistered logical strategies against one local llama.cpp slot."""
import argparse
from collections import Counter
from hashlib import sha256
import http.client
import json
from pathlib import Path
import re
import time


def rpc(port, endpoint, body, timeout):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("POST", endpoint, json.dumps(body), {"Content-Type": "application/json"})
        reply = conn.getresponse()
        if reply.status != 200:
            raise RuntimeError(f"llama-server {endpoint} status {reply.status}")
        return json.loads(reply.read(1048576))
    finally:
        conn.close()


def trial(plan, task, seed, arm, mode, port, pid):
    start = time.monotonic()
    calls = []
    limit = plan["token_limit" if mode == "tokens" else "secondary_token_limit"]
    seconds = plan["wall_limit_ms" if mode == "wall_ms" else "secondary_wall_limit_ms"] / 1000
    remaining = limit
    message_bytes = 0

    def ask(instruction, note=False, messages=()):
        nonlocal remaining, message_bytes
        context = "\n".join(messages)
        message_bytes += sum(len(message.encode()) for message in messages)
        prompt = (f"<|im_start|>user\nProblem: {task}. {instruction}\n{context}<|im_end|>\n"
                  "<|im_start|>assistant\n")
        def time_left():
            left = start + seconds - time.monotonic()
            if left <= 0:
                raise TimeoutError("Trial time budget exhausted")
            return left
        tokenized = rpc(port, "/tokenize", {"content": prompt, "add_special": True, "parse_special": True}, time_left())
        count = len(tokenized["tokens"])
        cap = min(16 if note else 6, remaining - count - 1)
        if cap < 1:
            raise ValueError("Trial token budget exhausted")
        body = dict(prompt=prompt, n_predict=cap, seed=seed+len(calls), temperature=plan["temperature"],
                    cache_prompt=False, stop=["<|im_end|>", "\n"])
        if not note:
            body["grammar"] = 'root ::= "-"? [0-9] [0-9]? [0-9]? [0-9]?'
        out = rpc(port, "/completion", body, time_left())
        used = out["tokens_evaluated"] + out["tokens_predicted"]
        remaining -= used
        if remaining < 0:
            raise ValueError("Measured use exceeded budget")
        calls.append({"response": out["content"], "prompt_tokens": out["tokens_evaluated"],
                      "new_tokens": out["tokens_predicted"], "timings": out["timings"]})
        return out["content"].strip()

    direct = "Answer with an integer."
    if arm == "single":
        answer = ask(direct)
    elif arm == "long-thinking":
        thought = ask("Work through the arithmetic.", note=True)
        answer = ask("Finish your own calculation. Answer with an integer.", messages=[thought])
        message_bytes = 0  # same logical agent
    elif arm == "independent":
        answer = Counter(ask(direct) for _ in range(3)).most_common(1)[0][0]
    elif arm == "hierarchical":
        plan_note = ask("As the planner, give your solver an arithmetic method.", note=True)
        answer = ask("As the solver, use the planner's method; return the integer.", messages=[plan_note])
    elif arm == "flat":
        left, right = ask(direct), ask(direct)
        a = ask("Recheck your answer after reading your peer's candidate.", messages=[left, right])
        b = ask("Recheck your answer after reading your peer's candidate.", messages=[right, left])
        answer = Counter((a, b)).most_common(1)[0][0]
    elif arm == "dynamic":
        a, b = ask(direct), ask(direct)
        answer = a if a == b else ask("Resolve these conflicting answers.", messages=[a, b])
    else:
        raise ValueError(arm)
    wall_ms = round((time.monotonic() - start) * 1000)
    if wall_ms > seconds * 1000:
        raise TimeoutError("Measured duration exceeded trial budget")
    rss = re.search(r"^VmRSS:\s+(\d+) kB", Path(f"/proc/{pid}/status").read_text(), re.M)
    a, op, b = task.split()
    x, y = int(a), int(b)
    expected = {"+":x+y, "-":x-y, "*":x*y}[op]
    return {"task":task, "seed":seed, "arm":arm, "mode":mode, "expected":expected, "answer":answer,
            "pass":bool(re.fullmatch(r"-?\d+", answer)) and int(answer) == expected, "safe":False,
            "safety_assessed":False, "tokens":limit-remaining, "wall_ms":wall_ms,
            "message_bytes":message_bytes,"ram_bytes":int(rss[1])*1024, "calls":calls}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("plan", type=Path)
    parser.add_argument("digest")
    parser.add_argument("port", type=int)
    parser.add_argument("pid", type=int)
    args = parser.parse_args()
    raw = args.plan.read_bytes()
    if sha256(raw).hexdigest() != args.digest:
        raise ValueError("Unregistered plan")
    plan = json.loads(raw)
    for mode in plan["modes"]:
        rows = []
        for idx, task in enumerate(plan["tasks"]):
            for n, seed in enumerate(plan["seeds"]):
                arms = plan["arms"]
                shift = (idx + n) % len(arms)
                for arm in arms[shift:] + arms[:shift]:
                    row = trial(plan, task, seed, arm, mode, args.port, args.pid)
                    rows.append(row)
        for arm in plan["arms"]:
            subset = [r for r in rows if r["arm"] == arm]
            out = args.plan.parent / f"{mode}-{arm}.jsonl"
            out.write_text("".join(json.dumps(r,separators=(",",":")) + "\n" for r in subset))
            print(mode, arm, sum(r["pass"] for r in subset), "/", len(subset), flush=True)


if __name__ == "__main__":
    main()
