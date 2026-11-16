"""Read only the migration jobs referenced by a recorded run."""

import argparse
import json
import subprocess
from pathlib import Path

from verify_runtime import PROJECT, REPO, dump

p = argparse.ArgumentParser()
p.add_argument("--run", required=True)
a = p.parse_args()
folder = Path(a.run)
cids = []
for path in (folder / "http").glob("*-stage.json"):
    response = json.loads(path.read_text(encoding="utf-8")).get("response", {})
    if isinstance(response, dict) and response.get("conversation_id"):
        cids.append(response["conversation_id"])
script = """import asyncio,json
from services.memory_service.core import MemoryStore,serialize
async def main():
    s=MemoryStore()
    await s.connect()
    rows=await s.jobs.find({'conversation_id':{'$in':CIDS}}).to_list()
    print('__EVIDENCE__'+json.dumps([serialize(r) for r in rows],ensure_ascii=False,default=str))
    await s.close()
asyncio.run(main())
""".replace("CIDS", repr(cids))
r = subprocess.run(
    ["docker", "compose", "-p", PROJECT, "exec", "-T", "memory-orchestrator", "python", "-"],
    cwd=REPO,
    input=script.encode("utf-8"),
    capture_output=True,
    timeout=120,
)
line = next(
    (
        x[len("__EVIDENCE__") :]
        for x in r.stdout.decode("utf-8").splitlines()
        if x.startswith("__EVIDENCE__")
    ),
    None,
)
if line:
    dump(folder / "component" / "migration-jobs.json", json.loads(line))
    for job in json.loads(line):
        print(job["conversation_id"], job.get("orchestrator_rationale"), job.get("status"))
else:
    print("Inspection failed with code", r.returncode)
raise SystemExit(r.returncode or (0 if line else 1))
