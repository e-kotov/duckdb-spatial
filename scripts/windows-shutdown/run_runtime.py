#!/usr/bin/env python3
"""Compare the exact GCC fallback and its shutdown guard on Windows x64.

This is a mechanism/cleanup-contract experiment, not the Spatial workload test.
The abandoned-reader fixture deliberately leaves a detached worker inside a TLS
destructor before ExitProcess. An unmodified runtime timeout is recorded as a
successful diagnostic reproduction and an unsuccessful software exit. Only the
guarded build is required to exit successfully for that scenario.

The C host has no C++ runtime workload. Each DLL statically links the selected
compiler's runtime, with either its original archive member, the supplied
unmodified fallback source, or the supplied guarded source. Normal controls
check real C++ thread_local destructor counts, user atexit callbacks, and global
destructors across joined DLL-created and foreign workers, main-thread exit,
and repeated FreeLibrary.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import time


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def command(argv: list[str], *, output: Path, stem: str, timeout: float = 180,
            exit_marker_path: Path | None = None) -> dict:
    started = time.monotonic()
    process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    timed_out = False
    exit_request_seen = None
    timeout_stage = None
    try:
        if exit_marker_path is None:
            stdout, stderr = process.communicate(timeout=timeout)
        else:
            # Fixture output is bounded to a few lines. Observe the file marker
            # so a slow startup never consumes the shutdown grace period.
            deadline = started + max(30.0, timeout)
            while process.poll() is None:
                now = time.monotonic()
                if exit_request_seen is None and exit_marker_path.exists():
                    markers = exit_marker_path.read_text(encoding="utf-8").splitlines()
                    if "PROCESS_EXIT_REQUEST" in markers:
                        exit_request_seen = now
                        deadline = now + timeout
                if now >= deadline:
                    timeout_stage = "after-exit-request" if exit_request_seen else "workload-or-setup"
                    raise subprocess.TimeoutExpired(argv, timeout)
                time.sleep(0.025)
            stdout, stderr = process.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        timed_out = True
        # All fixture children are single native processes, without launchers.
        # This forced termination is recorded, never represented as exit zero.
        process.kill()
        stdout, stderr = process.communicate(timeout=30)
    stdout_path = output / f"{stem}.stdout.log"
    stderr_path = output / f"{stem}.stderr.log"
    stdout_path.write_bytes(stdout)
    stderr_path.write_bytes(stderr)
    return {
        "command": argv,
        "pid": process.pid,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "exit_request_observed_seconds": (
            round(exit_request_seen - started, 3) if exit_request_seen is not None else None
        ),
        "timeout_stage": timeout_stage,
        "timed_out": timed_out,
        "terminated_by_supervisor": timed_out,
        "returncode": None if timed_out else process.returncode,
        "returncode_after_termination": process.returncode if timed_out else None,
        "stdout": stdout.decode("utf-8", errors="replace"),
        "stderr": stderr.decode("utf-8", errors="replace"),
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
    }


def assess(result: dict, *, variant: str, mode: str, markers: list[str],
           cycles: int, workers: int) -> dict:
    """Check cleanup evidence, not just exit codes or a printed success label."""
    counts = {name: markers.count(name) for name in (
        "CPP_TLS_DTOR", "GLOBAL_DTOR", "ATEXIT_CALLBACK", "UNLOAD_OK",
        "FOREIGN_WORKERS_JOINED", "FOREIGN_UNLOAD_OK",
        "PROCESS_EXIT_REQUEST", "BLOCKED_DTOR_ENTER", "BLOCKED_DTOR_LEAVE",
        "BLOCK_SETUP_ERROR",
    )}
    expected_hang = mode == "abandoned-reader" and variant in ("archive", "baseline")
    exited_zero = not result["timed_out"] and result["returncode"] == 0
    checks = {"no_setup_error": counts["BLOCK_SETUP_ERROR"] == 0}
    if mode in ("unload", "foreign-unload"):
        foreign = mode == "foreign-unload"
        unload_marker = "FOREIGN_UNLOAD_OK" if foreign else "UNLOAD_OK"
        other_unload_marker = "UNLOAD_OK" if foreign else "FOREIGN_UNLOAD_OK"
        checks.update({
            "process_exited_zero": exited_zero,
            "tls_destructors": counts["CPP_TLS_DTOR"] == cycles * workers,
            "global_destructors": counts["GLOBAL_DTOR"] == cycles,
            "atexit_callbacks": counts["ATEXIT_CALLBACK"] == cycles,
            "host_verified_actual_unloads": counts[unload_marker] == 1,
            "correct_worker_creation_mode": counts[other_unload_marker] == 0 and
            counts["FOREIGN_WORKERS_JOINED"] == (cycles if foreign else 0),
            "no_process_exit_fixture": counts["PROCESS_EXIT_REQUEST"] == 0,
            "no_abandoned_reader": counts["BLOCKED_DTOR_ENTER"] == 0,
        })
    elif mode == "main-exit":
        checks.update({
            "process_exited_zero": exited_zero,
            "tls_destructor": counts["CPP_TLS_DTOR"] == 1,
            "global_destructor": counts["GLOBAL_DTOR"] == 1,
            "atexit_callback": counts["ATEXIT_CALLBACK"] == 1,
            "process_exit_requested": counts["PROCESS_EXIT_REQUEST"] == 1,
            "no_abandoned_reader": counts["BLOCKED_DTOR_ENTER"] == 0,
        })
    else:
        checks.update({
            "destructor_entered": counts["BLOCKED_DTOR_ENTER"] == 1,
            "destructor_did_not_return": counts["BLOCKED_DTOR_LEAVE"] == 0,
            "process_exit_requested": counts["PROCESS_EXIT_REQUEST"] == 1,
            "no_other_tls_workload": counts["CPP_TLS_DTOR"] == 0,
        })
        if expected_hang:
            checks.update({
                "shutdown_hang_observed": result["timed_out"] and
                result["timeout_stage"] == "after-exit-request",
                "global_cleanup_not_reached": counts["GLOBAL_DTOR"] == 0,
                "atexit_cleanup_not_reached": counts["ATEXIT_CALLBACK"] == 0,
            })
        else:
            checks.update({
                "process_exited_zero": exited_zero,
                "global_destructor_preserved": counts["GLOBAL_DTOR"] == 1,
                "atexit_callback_preserved": counts["ATEXIT_CALLBACK"] == 1,
            })
    met = all(checks.values())
    return {
        "variant": variant,
        "mode": mode,
        "expected_hang": expected_hang,
        "marker_counts": counts,
        "checks": checks,
        "experiment_expectation_met": met,
        "software_exit_passed": exited_zero and met,
        "classification": (
            "expected-diagnostic-hang" if expected_hang and met else
            "passed-clean-exit-and-contract" if met else "failed-experiment"
        ),
        **result,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cxx", required=True, help="Exact Rtools42 g++.exe executable")
    parser.add_argument("--baseline-runtime-source", type=Path, required=True)
    parser.add_argument("--guarded-runtime-source", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--expected-compiler-version", default="10.4.0")
    parser.add_argument("--timeout", type=float, default=10)
    parser.add_argument("--cycles", type=int, default=16)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("This experiment must run on actual Windows; it is not a Wine/Linux test.")
    if not 0 < args.timeout <= 120 or not 0 < args.cycles <= 64 or not 0 < args.workers <= 128:
        parser.error("timeout must be in (0,120], cycles in [1,64], workers in [1,128]")

    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    fixture = Path(__file__).resolve().parent / "runtime_tests"
    sources = {
        "archive": None,
        "baseline": args.baseline_runtime_source.resolve(strict=True),
        "guarded": args.guarded_runtime_source.resolve(strict=True),
    }
    summary = {
        "purpose": "runtime mechanism and cleanup-contract experiment",
        "spatial_reproducer_validated": False,
        "platform": platform.platform(),
        "python": sys.version,
        "workload": {"cycles": args.cycles, "workers": args.workers, "timeout": args.timeout},
        "source_sha256": {str(path): digest(path) for path in (
            fixture / "fixture.cpp", fixture / "host.c", fixture / "fixture.h",
            sources["baseline"], sources["guarded"], Path(__file__).resolve(),
        )},
        "compiler_commands": [],
        "builds": [],
        "results": [],
        "candidate_validated_for_runtime_fixture": False,
    }

    def save() -> None:
        (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    try:
        cxx_found = shutil.which(args.cxx)
        if not cxx_found:
            raise RuntimeError(f"Compiler not found: {args.cxx}")
        cxx = str(Path(cxx_found).resolve())
        version = command([cxx, "-dumpfullversion"], output=output, stem="compiler-version")
        target = command([cxx, "-dumpmachine"], output=output, stem="compiler-target")
        archive = command([cxx, "-print-file-name=libstdc++.a"], output=output, stem="compiler-archive")
        summary["compiler_commands"].extend([version, target, archive])
        if any(item["returncode"] != 0 for item in (version, target, archive)):
            raise RuntimeError("Could not identify the selected compiler and runtime")
        if version["stdout"].strip() != args.expected_compiler_version:
            raise RuntimeError("Compiler version differs from the explicitly selected runtime baseline")
        if not target["stdout"].strip().startswith("x86_64-w64-mingw32"):
            raise RuntimeError("The selected compiler is not the required Windows x64 MinGW target")
        runtime_archive = Path(archive["stdout"].strip()).resolve(strict=True)
        summary["runtime_archive"] = {"path": str(runtime_archive), "sha256": digest(runtime_archive)}
        summary["compiler_binary"] = {"path": cxx, "sha256": digest(Path(cxx))}
        summary["thread_runtime_archives"] = {}
        for library_name in ("libpthread.a", "libwinpthread.a"):
            identified = command([cxx, f"-print-file-name={library_name}"], output=output,
                                 stem=f"compiler-{library_name}")
            summary["compiler_commands"].append(identified)
            if identified["returncode"] != 0:
                raise RuntimeError(f"Could not identify {library_name}")
            library_path = Path(identified["stdout"].strip()).resolve(strict=True)
            summary["thread_runtime_archives"][library_name] = {
                "path": str(library_path), "sha256": digest(library_path),
            }
        nm_candidate = Path(cxx).with_name(Path(cxx).name.replace("g++", "nm"))
        nm = shutil.which(str(nm_candidate))
        if not nm:
            raise RuntimeError(f"Matching nm executable not found: {nm_candidate}")

        host = output / "runtime-host.exe"
        build_host = command([
            cxx, "-x", "c", "-std=c11", "-O0", "-g", "-Wall", "-Wextra", "-static",
            str(fixture / "host.c"), "-o", str(host),
        ], output=output, stem="build-host")
        summary["builds"].append({"variant": "host", **build_host})
        if build_host["returncode"] != 0:
            raise RuntimeError("Native C host failed to compile")

        fixture_object = output / "fixture.o"
        compile_fixture = command([
            cxx, "-std=c++11", "-O0", "-g", "-Wall", "-Wextra", "-pthread", "-c",
            str(fixture / "fixture.cpp"), "-o", str(fixture_object),
        ], output=output, stem="compile-fixture")
        summary["builds"].append({"variant": "fixture-object", **compile_fixture})
        if compile_fixture["returncode"] != 0:
            raise RuntimeError("Native C++ fixture failed to compile")

        dlls = {}
        summary["link_proofs"] = {}
        for variant, runtime_source in sources.items():
            dll = output / f"runtime-{variant}.dll"
            link_map = output / f"runtime-{variant}.map"
            argv = [
                cxx, "-std=c++11", "-O0", "-g", "-Wall", "-Wextra", "-pthread",
                "-shared", "-static", "-static-libgcc", "-static-libstdc++",
                str(fixture_object),
            ]
            if runtime_source:
                runtime_object = output / f"{variant}-runtime.o"
                compile_runtime = command([
                    cxx, "-std=c++11", "-O0", "-g", "-Wall", "-Wextra", "-pthread", "-c",
                    str(runtime_source), "-o", str(runtime_object),
                ], output=output, stem=f"compile-{variant}-runtime")
                summary["builds"].append({"variant": f"{variant}-runtime-object", **compile_runtime})
                if compile_runtime["returncode"] != 0:
                    raise RuntimeError(f"{variant} fallback runtime source failed to compile")
                argv.append(str(runtime_object))
            argv.extend(["-Wl,--no-undefined", f"-Wl,-Map,{link_map}", "-o", str(dll)])
            built = command(argv, output=output, stem=f"build-{variant}")
            summary["builds"].append({"variant": variant, **built})
            if built["returncode"] != 0:
                raise RuntimeError(f"{variant} runtime fixture failed to compile")
            dlls[variant] = dll
            symbols = command([nm, "-C", "--defined-only", str(dll)], output=output,
                              stem=f"symbols-{variant}")
            map_text = link_map.read_text(encoding="utf-8", errors="replace")
            definitions = re.findall(r"^\s*[0-9a-fA-F]+\s+[Tt]\s+__cxa_thread_atexit\s*$",
                                     symbols["stdout"], flags=re.MULTILINE)
            archive_fallback = re.search(
                r"libstdc\+\+\.a\((?:libstdc\+\+_a-)?atexit_thread\.o\)", map_text
            ) is not None
            selected_object = runtime_source is None or f"{variant}-runtime.o" in map_text
            proof = {
                "nm_returncode": symbols["returncode"],
                "symbol_log": symbols["stdout_path"],
                "link_map": str(link_map),
                "cxa_thread_atexit_definitions": len(definitions),
                "libstdcxx_archive_fallback_present": archive_fallback,
                "selected_runtime_object_present": selected_object,
                "passed": symbols["returncode"] == 0 and len(definitions) == 1 and
                archive_fallback == (variant == "archive") and selected_object,
            }
            summary["link_proofs"][variant] = proof
            if not proof["passed"]:
                raise RuntimeError(f"{variant}: cannot prove which fallback implementation was linked")
        summary["binary_sha256"] = {str(path): digest(path) for path in (host, *dlls.values())}
        save()

        for mode in ("unload", "foreign-unload", "main-exit", "abandoned-reader"):
            for variant, dll in dlls.items():
                stem = f"{mode}-{variant}"
                marker_path = output / f"{stem}.markers.log"
                marker_path.unlink(missing_ok=True)
                result = command([
                    str(host), str(dll), mode, str(marker_path), str(args.cycles), str(args.workers),
                ], output=output, stem=stem, timeout=args.timeout, exit_marker_path=marker_path)
                markers = marker_path.read_text(encoding="utf-8").splitlines() if marker_path.exists() else []
                assessed = assess(result, variant=variant, mode=mode, markers=markers,
                                  cycles=args.cycles, workers=args.workers)
                assessed["marker_path"] = str(marker_path)
                summary["results"].append(assessed)
                save()
                print(f"{variant}/{mode}: {assessed['classification']} "
                      f"({result['elapsed_seconds']:.3f}s)", flush=True)
        summary["candidate_validated_for_runtime_fixture"] = (
            len(summary["results"]) == 12 and
            all(result["experiment_expectation_met"] for result in summary["results"])
        )
        save()
        return 0 if summary["candidate_validated_for_runtime_fixture"] else 1
    except Exception as error:
        summary["error"] = f"{type(error).__name__}: {error}"
        save()
        print(summary["error"], file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
