"""Run the repository checks and preserve commands, outcomes and evidence hashes."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts/verification"
PROJECT = "abl-stage-verification"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--run", required=True, help="New evidence directory relative to repository"
    )
    parser.add_argument("--unit-only", action="store_true")
    parser.add_argument("--private-only", action="store_true")
    args = parser.parse_args()
    folder = (ROOT / args.run).resolve()
    if not folder.is_relative_to(ROOT / "docs/verification/evidence"):
        parser.error("--run must be below docs/verification/evidence")
    folder.mkdir(parents=True, exist_ok=False)
    os.chdir(ROOT)
    sys.path.insert(0, str(SCRIPTS))
    from verify_runtime import clean

    rows = []

    def save(name, data):
        (folder / name).write_text(
            json.dumps(clean(data), ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def command(name, argv, timeout=900):
        print("Running " + name, flush=True)
        started = datetime.now(UTC).isoformat()
        try:
            result = subprocess.run(
                argv,
                cwd=ROOT,
                capture_output=True,
                timeout=timeout,
                env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
            )
            output = result.stdout.decode("utf-8", errors="replace") + result.stderr.decode(
                "utf-8", errors="replace"
            )
            code = result.returncode
        except subprocess.TimeoutExpired as exc:
            output = (exc.stdout or b"").decode("utf-8", errors="replace") + "\nTIMEOUT"
            code = 124
        (folder / (name + ".log")).write_text(clean(output), encoding="utf-8")
        rows.append(
            {
                "step": name,
                "argv": argv,
                "started_utc": started,
                "exit_code": code,
                "log": name + ".log",
            }
        )
        save("execution.json", rows)
        print(f"{name}: exit {code}", flush=True)
        return code

    sources = [
        p
        for dirname in ("services", "tests", "scripts", "infra")
        for p in (ROOT / dirname).rglob("*")
        if p.is_file()
        and not any(x in p.parts for x in ("runtime", "__pycache__", ".pytest_cache"))
    ]
    sources += [
        ROOT / n
        for n in ("docker-compose.yml", "Dockerfile", "requirements.txt", "requirements-dev.txt")
    ]
    save(
        "source-manifest.json",
        {
            "created_utc": datetime.now(UTC).isoformat(),
            "timezone": "Europe/Moscow",
            "commit": subprocess.check_output(
                ["git", "-c", "safe.directory=" + ROOT.as_posix(), "rev-parse", "HEAD"], text=True
            ).strip(),
            "working_tree": subprocess.check_output(
                ["git", "-c", "safe.directory=" + ROOT.as_posix(), "status", "--porcelain"],
                text=True,
            ),
            "python": sys.version,
            "project": PROJECT,
            "sha256": {p.relative_to(ROOT).as_posix(): sha(p) for p in sorted(sources)},
        },
    )
    (folder / "HOW_TO_REPEAT.md").write_text(
        "# Повторение этого прогона\n\n"
        "Подготовка и ограничения: [общая инструкция](../../HOW_TO_REPEAT.md).\n\n"
        "Из корня репозитория, с новым именем каталога:\n\n```powershell\n"
        ".venv/Scripts/python.exe scripts/verification/reproduce.py "
        "--run docs/verification/evidence/run-NEW"
        + (" --unit-only" if args.unit_only else " --private-only" if args.private_only else "")
        + "\n```\n\n"
        "Точные команды и коды завершения: `execution.json`. Версия и хеши исходников: "
        "`source-manifest.json`. Контрольные суммы доказательств: `SHA256SUMS.json`.\n"
        "Код завершения 0 означает завершение сценария, а не отсутствие уязвимостей. "
        "HTTP-опыты и компонентные подстановки различаются в поле scope.\n",
        encoding="utf-8",
    )
    command("dependencies", [sys.executable, "-m", "pip", "freeze"])
    if not args.private_only:
        command(
            "pytest",
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "--basetemp=" + str(SCRIPTS / "runtime" / (folder.name + "-pytest")),
                "--junitxml=" + str(folder / "pytest.xml"),
            ],
        )
    command("ruff", [sys.executable, "-m", "ruff", "check", "services", "tests", "scripts"])
    if not args.unit_only:
        compose = [
            "docker",
            "compose",
            "-p",
            PROJECT,
            "-f",
            "docker-compose.yml",
            "-f",
            "scripts/verification/compose.models.yml",
        ]
        command("containers", compose + ["ps", "--all", "--format", "json"])
        command("images", compose + ["images", "--format", "json"])
        stages = [
            ("baseline", "verify_runtime.py", ["--baseline"]),
            ("memory", "continue_experiments.py", []),
            ("ordinary", "check_shared_dialog.py", []),
            ("private", "check_normal_memory.py", []),
        ]
        if args.private_only:
            stages = [stages[-1]]
        for label, script, extra in stages:
            command(
                label, [sys.executable, str(SCRIPTS / script), "--run", str(folder / label), *extra]
            )
        for script in (
            ()
            if args.private_only
            else (
                "check_action_component.py",
                "check_boundaries_component.py",
                "check_egress_component.py",
                "check_remaining_component.py",
                "check_contract_component.py",
            )
        ):
            command(
                Path(script).stem,
                [sys.executable, str(SCRIPTS / "run_component.py"), script, "--run", str(folder)],
                timeout=360,
            )
        command(
            "migration-jobs",
            [
                sys.executable,
                str(SCRIPTS / "inspect_memory_jobs.py"),
                "--run",
                str(folder / ("private" if args.private_only else "ordinary")),
            ],
        )
    missing = []
    for p in folder.rglob("results.json"):
        for item in json.loads(p.read_text(encoding="utf-8")):
            for link in item.get("evidence", []):
                if not (p.parent / link).is_file():
                    missing.append(str(p.parent / link))
    save(
        "audit.json",
        {"missing_evidence": missing, "failed_steps": [r["step"] for r in rows if r["exit_code"]]},
    )
    save(
        "SHA256SUMS.json",
        {
            p.relative_to(folder).as_posix(): sha(p)
            for p in sorted(folder.rglob("*"))
            if p.is_file() and p.name != "SHA256SUMS.json"
        },
    )
    print("Evidence: " + str(folder), flush=True)
    return int(bool(missing or any(r["exit_code"] for r in rows)))


if __name__ == "__main__":
    raise SystemExit(main())
