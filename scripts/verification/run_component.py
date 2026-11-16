"""Execute one reviewed component probe and save sanitized evidence."""

import argparse
import json
import subprocess
from pathlib import Path

from verify_runtime import ART, PROJECT, REPO, dump

p = argparse.ArgumentParser()
p.add_argument("script")
p.add_argument("--run", required=True)
a = p.parse_args()
script = ART / a.script
result = subprocess.run(
    ["docker", "compose", "-p", PROJECT, "exec", "-T", "banking-agent", "python", "-"],
    cwd=REPO,
    input=script.read_bytes(),
    capture_output=True,
    timeout=300,
)
stdout = result.stdout.decode("utf-8", errors="replace")
evidence = next(
    (
        line[len("__EVIDENCE__") :]
        for line in stdout.splitlines()
        if line.startswith("__EVIDENCE__")
    ),
    None,
)
target = Path(a.run) / "component" / (script.stem + ".json")
if evidence:
    dump(target, json.loads(evidence))
    print("Saved " + str(target))
else:
    dump(
        target,
        {
            "exit_code": result.returncode,
            "stdout": stdout,
            "stderr": result.stderr.decode("utf-8", errors="replace"),
        },
    )
    print("Probe failed; inspect " + str(target))
raise SystemExit(result.returncode or (0 if evidence else 1))
