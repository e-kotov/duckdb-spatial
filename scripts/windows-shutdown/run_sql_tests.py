#!/usr/bin/env python3
"""Run unchanged GDAL/GEOS/PROJ SQL tests against the exact experimental DLL.

This supplies an explicit input list because upstream Spatial currently does
not register its test directory for MinGW. No existing SQL tests or ordinary
test configuration are modified. Passing requires every selected test to run,
the load-path assertion to succeed, and the native process to exit normally.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import psutil


# This file is already empty in the pinned upstream tree d332807f4ffdcc4d.
# Keep executing and recording it; it cannot produce an assertion. No other
# zero-assertion file is accepted, and the complete suite must still contribute
# all 130 statements observed in the unchanged selected tests + provenance check.
KNOWN_EMPTY_TEST = "test/sql/geos/equals.test"
EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
MIN_STATEMENT_PASSES = 130


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical(path: str | Path, source_root: Path) -> str:
    value = Path(path)
    if not value.is_absolute():
        value = source_root / value
    return os.path.normcase(str(value.resolve()))


def verified_empty_tests(selected: list[Path], source_root: Path) -> dict[str, dict]:
    verified = {}
    for path in selected:
        if path.stat().st_size != 0:
            continue
        relative = path.relative_to(source_root).as_posix()
        digest = sha256(path)
        if relative != KNOWN_EMPTY_TEST or digest != EMPTY_SHA256:
            raise ValueError(f"Unexpected empty SQL test source: {relative}")
        verified[canonical(path, source_root)] = {
            "path": relative, "bytes": 0, "sha256": digest,
            "upstream_git_blob": "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391",
        }
    return verified


def event_passed(event: dict, source_root: Path, known_empty: dict[str, dict]) -> bool:
    passes = event.get("passes")
    name = event.get("name")
    return (event.get("status") == "ok" and event.get("fails") == 0
            and type(passes) is int and passes >= 0 and isinstance(name, str)
            and (passes > 0 or canonical(name, source_root) in known_empty)
            and event.get("skip-mode", 0) == 0 and not event.get("partial-skip-reasons"))


def read_events(log: Path) -> tuple[list[dict], list[str]]:
    events, errors = [], []
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        if "[TEST_EVENT] " not in line:
            continue
        payload = line.split("[TEST_EVENT] ", 1)[1]
        try:
            event = json.loads(payload)
            if not isinstance(event, dict):
                raise ValueError("event is not a JSON object")
            events.append(event)
        except (ValueError, json.JSONDecodeError) as error:
            errors.append(f"{error}: {payload[:200]}")
    return events, errors


def stop_process_tree(process: subprocess.Popen) -> list[dict]:
    """Record an actual timeout, then terminate only this owned process tree."""
    snapshot = []
    try:
        parent = psutil.Process(process.pid)
        processes = parent.children(recursive=True) + [parent]
    except psutil.NoSuchProcess:
        return snapshot
    for child in processes:
        try:
            snapshot.append(child.as_dict(attrs=["pid", "ppid", "name", "status", "num_threads"]))
            child.kill()
        except psutil.NoSuchProcess:
            pass
    psutil.wait_procs(processes, timeout=10)
    process.wait(timeout=10)
    return snapshot


def run(args: argparse.Namespace) -> dict:
    source_root = args.source_root.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    runner = args.runner.resolve(strict=True)
    extension = args.extension.resolve(strict=True)
    repository_extension = args.repository_extension.resolve(strict=True)
    extension_hash = sha256(extension)
    if sha256(repository_extension) != extension_hash:
        raise ValueError("The test repository DLL does not match the archived experiment arm")

    selected = sorted((source_root / "test/sql/gdal").glob("*.test"))
    selected += sorted((source_root / "test/sql/geos").glob("*.test"))
    selected += [source_root / "test/sql/proj.test"]
    if len(selected) < 3 or any(not path.is_file() for path in selected):
        raise ValueError("Expected unchanged GDAL, GEOS and PROJ SQL test sources")
    known_empty = verified_empty_tests(selected, source_root)

    # A statically linked Spatial instance cannot pass this assertion. Direct
    # LOAD can leave duckdb_extensions().install_path empty, so the supervisor
    # verifies the actual mapped DLL path during the brief handshake below.
    # The expected file has already been checked against the archived hash.
    provenance = output / "candidate-binary-provenance.test"
    provenance.write_text(
        "require spatial\n\nquery I\n"
        "SELECT count(*) FROM duckdb_extensions()\n"
        "WHERE extension_name = 'spatial' AND loaded\n"
        "AND coalesce(install_mode, '') <> 'STATICALLY_LINKED';\n"
        "----\n1\n\nsleep 1 second\n", encoding="utf-8", newline="\n")
    tests = [provenance] + selected
    expected = {canonical(path, source_root) for path in tests}
    inputs = output / "sql-inputs.txt"
    inputs.write_text("\n".join(path.resolve().as_posix() for path in tests) + "\n",
                      encoding="utf-8", newline="\n")
    command = [str(runner), "-f", str(inputs), "--require", "spatial",
               "--emit-test-events", "--test-dir", str(source_root)]
    (output / "command.json").write_text(json.dumps(command, indent=2) + "\n", encoding="utf-8")
    log = output / "unittest.log"
    start = time.monotonic()
    tests_finished_at = None
    timeout_reason = None
    process_snapshot = []
    observed_modules: set[str] = set()
    observation_errors: set[str] = set()
    with log.open("wb") as stream:
        process = subprocess.Popen(command, cwd=source_root, stdout=stream, stderr=subprocess.STDOUT)
        try:
            while process.poll() is None:
                elapsed = time.monotonic() - start
                try:
                    for mapping in psutil.Process(process.pid).memory_maps():
                        if Path(mapping.path).name.lower() == "spatial.duckdb_extension":
                            observed_modules.add(canonical(mapping.path, source_root))
                except psutil.NoSuchProcess:
                    pass
                except psutil.AccessDenied as error:
                    if process.poll() is None:
                        observation_errors.add(str(error))
                events, _ = read_events(log)
                terminal = {canonical(event["name"], source_root) for event in events
                            if event.get("event") == "end" and isinstance(event.get("name"), str)}
                if expected.issubset(terminal) and tests_finished_at is None:
                    tests_finished_at = time.monotonic()
                if tests_finished_at is not None and time.monotonic() - tests_finished_at >= args.exit_grace:
                    # The tests returned, but this alone cannot identify the native
                    # wait site. Do not label it a captured loader-lock deadlock.
                    timeout_reason = "timeout_after_test_completion"
                    break
                if elapsed >= args.timeout:
                    timeout_reason = "suite_timeout"
                    break
                time.sleep(0.2)
            if timeout_reason:
                process_snapshot = stop_process_tree(process)
            else:
                process.wait()
        finally:
            # A supervisor exception must not leave its owned native process
            # alive and change the state seen by subsequent experiment arms.
            if process.poll() is None:
                stop_process_tree(process)

    events, event_errors = read_events(log)
    terminal_events = [event for event in events if event.get("event") == "end"]
    terminal_names = [canonical(event["name"], source_root) for event in terminal_events
                      if isinstance(event.get("name"), str)]
    all_tests_reported = set(terminal_names) == expected and len(terminal_names) == len(expected)
    statement_passes = sum(event.get("passes", 0) for event in terminal_events)
    all_tests_ok = (all(event_passed(event, source_root, known_empty) for event in terminal_events)
                    and statement_passes >= MIN_STATEMENT_PASSES)
    binary_unchanged = sha256(repository_extension) == extension_hash
    observed_exact_binary = observed_modules == {canonical(repository_extension, source_root)}
    passed = (timeout_reason is None and process.returncode == 0
              and not event_errors and all_tests_reported and all_tests_ok and binary_unchanged
              and observed_exact_binary and not observation_errors)
    result = {
        "all_passed": passed,
        "reason": timeout_reason or ("passed" if passed else "test_or_provenance_failure"),
        "exit_code": process.returncode,
        "elapsed_seconds": round(time.monotonic() - start, 3),
        "extension": str(extension), "extension_sha256": extension_hash,
        "repository_extension": str(repository_extension), "binary_unchanged": binary_unchanged,
        "observed_exact_binary": observed_exact_binary,
        "observed_spatial_modules": sorted(observed_modules),
        "module_observation_errors": sorted(observation_errors),
        "runner": str(runner), "runner_sha256": sha256(runner),
        "expected_test_files": len(tests), "upstream_test_files": len(selected),
        "all_tests_reported": all_tests_reported, "all_tests_ok": all_tests_ok,
        "statement_passes": statement_passes,
        "minimum_statement_passes": MIN_STATEMENT_PASSES,
        "statement_failures": sum(event.get("fails", 0) for event in terminal_events),
        "verified_empty_test_files": list(known_empty.values()),
        "tests": terminal_events, "event_errors": event_errors,
        "timeout_processes": process_snapshot,
        "source_files": [{"path": str(path.relative_to(source_root)), "sha256": sha256(path)}
                         for path in selected],
    }
    (output / "summary.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runner", type=Path, required=True)
    parser.add_argument("--extension", type=Path, required=True)
    parser.add_argument("--repository-extension", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--exit-grace", type=float, default=30)
    args = parser.parse_args()
    if args.timeout <= 0 or args.exit_grace <= 0:
        parser.error("timeouts must be positive")
    try:
        result = run(args)
    except Exception as error:
        args.output.mkdir(parents=True, exist_ok=True)
        result = {"all_passed": False, "reason": "harness_error", "error": repr(error)}
        (args.output / "summary.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0 if result["all_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
