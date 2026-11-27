"""Collect blinded numeric answers from a preloaded loopback llama.cpp server."""
import argparse
from collections import Counter
from hashlib import sha256
import http.client
import json
from pathlib import Path
import re
import time

from opencognitiveos.verification.benchmark import compare


def main():
    p = argparse.ArgumentParser()
    p.add_argument("plan", type=Path)
    p.add_argument("sha256")
    p.add_argument("output", type=Path)
    p.add_argument("--pid", required=True, type=int)
    p.add_argument("--port", type=int, required=True)
    args = p.parse_args()
    raw = args.plan.read_bytes()
    if sha256(raw).hexdigest() != args.sha256:
        raise ValueError("Plan changed")
    plan = json.loads(raw)
    observations = []
    def post(path, data):
        c = http.client.HTTPConnection("127.0.0.1",args.port,timeout=10)
        try:
            c.request("POST",path,json.dumps(data),{"Content-Type":"application/json"})
            r = c.getresponse()
            if r.status != 200:
                raise RuntimeError(f"Server status {r.status} at {path}")
            return json.loads(r.read(1048576))
        finally:
            c.close()
    for idx, task in enumerate(plan["tasks"]):
        prompt = plan["prompt"].format(task=task)
        a,op,b = task.split()
        x,y=int(a),int(b)
        expected = {"+":x+y,"-":x-y,"*":x*y}[op]
        for n,seed in enumerate(plan["seeds"]):
            arms = [plan["baseline"],plan["candidate"]]
            if (idx+n)%2:
                arms.reverse()
            for arm in arms:
                replies = []
                answers = []
                start = time.monotonic()
                for k in range(1 if arm==plan["baseline"] else 3):
                    out = post("/completion",{"prompt":prompt,"temperature":plan["temperature"],
                        "n_predict":plan["max_new_tokens"],"seed":seed+k,"cache_prompt":False,"stop":["\n"]})
                    match = re.fullmatch(r"\s*(-?\d+)\s*[.]?\s*",out["content"])
                    answers.append(int(match[1]) if match else None)
                    replies.append({"text":out["content"],"prompt_tokens":out["tokens_evaluated"],
                                    "generated_tokens":out["tokens_predicted"]})
                wall = round((time.monotonic()-start)*1000)
                tokens = sum(r["prompt_tokens"]+r["generated_tokens"] for r in replies)
                rss = next(line for line in Path(f"/proc/{args.pid}/status").read_text().splitlines() if line.startswith("VmRSS:"))
                chosen = Counter(answers).most_common(1)[0][0]
                observations.append({"task":task,"seed":seed,"arm":arm,"model":plan["model"],"pass":chosen==expected,
                    "safe":True,"tokens":tokens,"wall_ms":wall,"ram_bytes":int(rss.split()[1])*1024,"message_bytes":0,
                    "samples":replies,"expected":expected,"selected":chosen})
    args.output.mkdir(exist_ok=True)
    rows = args.output / (plan["mode"]+"-observations.json")
    rows.write_text(json.dumps(observations,ensure_ascii=False,separators=(",",":"))+"\n")
    report=compare(args.plan,args.sha256,observations)
    (args.output/(plan["mode"]+"-result.json")).write_text(json.dumps(report,indent=2)+"\n")
    print(json.dumps(report))

if __name__ == "__main__":
    main()
