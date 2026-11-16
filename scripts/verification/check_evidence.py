"""Verify evidence hashes and result references without running the application."""

import hashlib
import json
import sys
from pathlib import Path

folder = Path(sys.argv[1]).resolve()
manifest = json.loads((folder / "SHA256SUMS.json").read_text(encoding="utf-8"))
errors = []
for name, expected in manifest.items():
    path = (folder / name).resolve()
    if not path.is_relative_to(folder) or not path.is_file():
        errors.append(name)
    elif hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        errors.append(name)
for path in folder.rglob("results.json"):
    for result in json.loads(path.read_text(encoding="utf-8")):
        for name in result.get("evidence", []):
            if not (path.parent / name).is_file():
                errors.append(str(path.parent / name))
print(json.dumps({"checked_files": len(manifest), "errors": errors}, indent=2))
raise SystemExit(bool(errors))
