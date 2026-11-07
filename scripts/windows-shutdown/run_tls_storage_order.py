#!/usr/bin/env python3
"""Observe GCC emutls release versus cleanup callbacks without accessing freed storage.

Exit zero means all six cases matched the explicitly requested diagnostic order.
For --expected-order storage-first, that is a reproduction of an unsafe ordering,
not a passing storage-lifetime contract or validation of the shutdown fix.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import re
import shutil
import sys

from prepare_runtime import SOURCES, guard_source
from pthread_provenance import isolated_pthread_archives, pthread_link_proof, pthread_provider_paths
from run_runtime import command, digest


VARIANTS = ("archive", "baseline", "guarded")
MODES = ("pthread", "foreign")
SOURCE_REFERENCES = {
    "gcc_emutls": "https://github.com/gcc-mirror/gcc/blob/7ff47281ce4f3699185b06a3430968eac2a5b0c6/libgcc/emutls.c",
    "mingw_v9_thread": "https://github.com/mingw-w64/mingw-w64/blob/v9.0.0/mingw-w64-libraries/winpthreads/src/thread.c",
    "reverse_key_order_patch": "https://github.com/mingw-w64/mingw-w64/commit/8e06daa36dfcea4bb491acf4b350658f40738f02",
}


def assess(result: dict, *, variant: str, mode: str, expected_order: str) -> dict:
    """A clean exit alone cannot establish either observation or lifetime safety."""
    observation = None
    parse_error = None
    try:
        lines = [line for line in result["stdout"].splitlines() if line.strip()]
        if len(lines) != 1:
            raise ValueError("Expected one complete JSON observation")
        observation = json.loads(lines[0])
        if not isinstance(observation, dict):
            raise ValueError("Observation is not a JSON object")
    except (ValueError, TypeError, KeyError) as error:
        parse_error = str(error)

    exited_zero = not result["timed_out"] and result["returncode"] == 0
    checks = {
        "process_exited_zero": exited_zero,
        "not_terminated_by_supervisor": not result["terminated_by_supervisor"],
        "empty_stderr": not result["stderr"].strip(),
        "valid_json_observation": parse_error is None,
        "known_case": variant in VARIANTS and mode in MODES,
    }
    lifetime_order = "unproven"
    key_order = "unproven"
    if parse_error is None:
        exact_integers = {
            "schema": 1, "worker_error": 0, "storage_touches": 2,
            "cleanup_registrations": 1, "keys_created": 2, "keys_deleted": 2,
            "sequence": 5, "release_calls": 1, "cleanup_callbacks": 1,
            "low_key_callbacks": 1, "high_key_callbacks": 1, "dll_destructors": 1,
        }
        checks["exact_callback_and_cleanup_counts"] = all(
            type(observation.get(name)) is int and observation[name] == expected
            for name, expected in exact_integers.items()
        )
        checks["requested_worker_mode"] = observation.get("mode") == mode
        checks["native_wait_confirmed"] = observation.get("native_wait_confirmed") is True
        checks["actual_unload_confirmed"] = observation.get("actual_unload_confirmed") is True
        checks["worker_identified"] = type(observation.get("worker_id")) is int and observation["worker_id"] > 0
        low, high = observation.get("low_key"), observation.get("high_key")
        checks["public_keys_are_distinct_and_increasing"] = (
            type(low) is int and type(high) is int and 0 <= low < high
        )
        address_names = ("object_address", "allocation_base", "observed_release_address")
        addresses_valid = all(
            isinstance(observation.get(name), str) and
            re.fullmatch(r"0x[0-9a-fA-F]+", observation[name]) for name in address_names
        )
        if addresses_valid:
            object_address, base, released = [int(observation[name], 16) for name in address_names]
            addresses_valid = base > 0 and object_address == base + 8 and object_address % 8 == 0 and released == base
        checks["exact_known_emutls_allocation_released"] = bool(addresses_valid)
        event_names = ("release_enter_event", "release_leave_event", "cleanup_event", "low_key_event", "high_key_event")
        events = [observation.get(name) for name in event_names]
        events_valid = all(type(value) is int for value in events) and sorted(events) == [1, 2, 3, 4, 5]
        checks["unique_complete_event_sequence"] = events_valid
        if events_valid:
            enter, leave, callback, key_low, key_high = events
            checks["release_return_observed"] = leave == enter + 1
            if leave < callback:
                lifetime_order = "storage-first"
            elif callback < enter:
                lifetime_order = "callback-first"
            key_order = "ascending" if key_low < key_high else "descending"
            checks["callback_and_allocator_counter_agreement"] = (
                type(observation.get("callbacks_at_release")) is int and
                type(observation.get("completed_releases_at_callback")) is int and
                observation["callbacks_at_release"] == int(lifetime_order == "callback-first") and
                observation["completed_releases_at_callback"] == int(lifetime_order == "storage-first")
            )
            checks["entire_sequence_matches_traversal"] = (
                events == [1, 2, 3, 4, 5] if lifetime_order == "storage-first" else
                events == [4, 5, 3, 2, 1] if lifetime_order == "callback-first" else False
            )
        else:
            checks["release_return_observed"] = False
            checks["callback_and_allocator_counter_agreement"] = False
            checks["entire_sequence_matches_traversal"] = False

    observation_valid = all(checks.values())
    expectation_met = observation_valid and lifetime_order == expected_order
    contract_passed = observation_valid and lifetime_order == "callback-first"
    return {
        "variant": variant, "mode": mode, "expected_order": expected_order,
        "observation": observation, "parse_error": parse_error, "checks": checks,
        "observation_valid": observation_valid, "observed_order": lifetime_order,
        "public_key_order": key_order, "diagnostic_expectation_met": expectation_met,
        "process_exit_passed": exited_zero,
        "storage_lifetime_contract_passed": contract_passed,
        "classification": (
            "failed-observation" if not observation_valid else
            "unexpected-order" if not expectation_met else
            "reproduced-storage-release-before-cleanup" if not contract_passed else
            "observed-cleanup-before-storage-release"
        ),
        **result,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cxx", required=True)
    parser.add_argument("--baseline-runtime-source", type=Path, required=True)
    parser.add_argument("--guarded-runtime-source", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--expected-order", choices=("storage-first", "callback-first"), required=True)
    parser.add_argument("--pthread-library-dir", type=Path,
                        help="Optional isolated directory containing both libpthread.a and libwinpthread.a")
    parser.add_argument("--expected-compiler-version", default="10.4.0")
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("This probe requires actual Windows, not Wine or Linux")
    if args.expected_compiler_version != "10.4.0":
        parser.error("The storage allocation layout is pinned to GCC 10.4.0")
    if not 0 < args.timeout <= 120:
        parser.error("timeout must be in (0,120]")
    output = args.output_dir.resolve()
    if output.exists() and any(output.iterdir()):
        parser.error("output directory must be absent or empty; previous observations cannot be reused")
    output.mkdir(parents=True, exist_ok=True)
    source_root = Path(__file__).resolve().parent
    fixture = source_root / "tls_storage_order"
    sources = {
        "archive": None,
        "baseline": args.baseline_runtime_source.resolve(strict=True),
        "guarded": args.guarded_runtime_source.resolve(strict=True),
    }
    summary = {
        "purpose": "observe TLS allocation release and GCC cleanup callback ordering",
        "expected_order": args.expected_order,
        "original_shutdown_fix_assessed": False,
        "spatial_workload_assessed": False,
        "normal_exit_is_not_lifetime_safety": True,
        "platform": platform.platform(), "python": sys.version,
        "source_references": SOURCE_REFERENCES,
        "source_sha256": {str(path): digest(path) for path in (
            fixture / "probe.cpp", fixture / "probe.h", fixture / "host.c",
            Path(__file__).resolve(), source_root / "run_runtime.py", source_root / "prepare_runtime.py",
            source_root / "pthread_provenance.py",
            sources["baseline"], sources["guarded"],
        )},
        "commands": [], "builds": [], "link_proofs": {}, "results": [],
        "diagnostic_expectation_met": False, "storage_lifetime_contract_passed": False,
    }

    def save() -> None:
        (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    def build(argv: list[str], stem: str) -> dict:
        result = command(argv, output=output, stem=stem)
        summary["builds"].append({"name": stem, **result})
        if result["returncode"] != 0 or result["timed_out"]:
            raise RuntimeError(f"Build failed: {stem}")
        return result

    try:
        if digest(sources["baseline"]) != SOURCES["gcc10-atexit_thread.cc"][3]:
            raise RuntimeError("Baseline must be the exact pinned GCC 10.4 fallback source")
        if sources["guarded"].read_bytes() != guard_source(sources["baseline"].read_text(encoding="utf-8")).encode("utf-8"):
            raise RuntimeError("Guarded input differs from the existing shutdown guard")
        cxx_found = shutil.which(args.cxx)
        if not cxx_found:
            raise RuntimeError(f"Compiler not found: {args.cxx}")
        cxx = str(Path(cxx_found).resolve())
        summary["compiler_binary"] = {"path": cxx, "sha256": digest(Path(cxx))}
        identified = {}
        for name, option in (("version", "-dumpfullversion"), ("target", "-dumpmachine")):
            result = command([cxx, option], output=output, stem=f"compiler-{name}")
            summary["commands"].append(result)
            if result["returncode"] != 0:
                raise RuntimeError(f"Cannot identify compiler {name}")
            identified[name] = result["stdout"].strip()
        if identified["version"] != args.expected_compiler_version or not identified["target"].startswith("x86_64-w64-mingw32"):
            raise RuntimeError("The selected compiler is not the requested Windows x64 MinGW compiler")
        summary["compiler_identity"] = identified
        summary["runtime_archives"] = {}
        summary["compiler_default_runtime_archives"] = {}
        pthread_directory = args.pthread_library_dir.resolve(strict=True) if args.pthread_library_dir else None
        private_archives = isolated_pthread_archives(pthread_directory) if pthread_directory else {}
        summary["selected_pthread_library_dir"] = str(pthread_directory) if pthread_directory else None
        for name in ("libstdc++.a", "libgcc.a", "libpthread.a", "libwinpthread.a"):
            result = command([cxx, f"-print-file-name={name}"], output=output, stem=f"compiler-{name}")
            summary["commands"].append(result)
            if result["returncode"] != 0:
                raise RuntimeError(f"Cannot locate {name}")
            path = Path(result["stdout"].strip()).resolve(strict=True)
            summary["compiler_default_runtime_archives"][name] = {"path": str(path), "sha256": digest(path)}
            if pthread_directory and name in ("libpthread.a", "libwinpthread.a"):
                selected = command([cxx, f"-L{pthread_directory}", f"-print-file-name={name}"],
                                   output=output, stem=f"selected-{name}")
                summary["commands"].append(selected)
                if selected["returncode"] != 0:
                    raise RuntimeError(f"Cannot locate selected {name}")
                path = Path(selected["stdout"].strip()).resolve(strict=True)
                if path != private_archives[name]:
                    raise RuntimeError(f"Compiler selected {name} outside the isolated directory")
            summary["runtime_archives"][name] = {"path": str(path), "sha256": digest(path)}
        tool_paths = {}
        for name in ("nm", "objdump"):
            candidate = Path(cxx).with_name(Path(cxx).name.replace("g++", name))
            tool = shutil.which(str(candidate))
            if not tool:
                raise RuntimeError(f"Matching {name} unavailable: {candidate}")
            tool_paths[name] = tool

        host = output / "storage-order-host.exe"
        build([cxx, "-x", "c", "-std=c11", "-O0", "-g", "-Wall", "-Wextra", "-static",
               str(fixture / "host.c"), "-o", str(host)], "build-host")
        fixture_object = output / "storage-order.o"
        build([cxx, "-std=c++11", "-O0", "-g", "-Wall", "-Wextra", "-pthread", "-c",
               str(fixture / "probe.cpp"), "-o", str(fixture_object)], "compile-probe")
        undefined = command([tool_paths["nm"], "--undefined-only", str(fixture_object)],
                            output=output, stem="probe-undefined-symbols")
        if undefined["returncode"] != 0 or not re.search(r"\bU\s+__emutls_get_address\s*$", undefined["stdout"], re.MULTILINE):
            raise RuntimeError("Compiler did not emit actual emulated TLS access for the probe")
        summary["compiler_emutls_access_proven"] = True
        dlls = {}
        for variant, runtime_source in sources.items():
            dll = output / f"storage-order-{variant}.dll"
            link_map = output / f"storage-order-{variant}.map"
            argv = [cxx, "-shared", "-static", "-static-libgcc", "-static-libstdc++", "-pthread", str(fixture_object)]
            if pthread_directory:
                argv.insert(1, f"-L{pthread_directory}")
            if runtime_source:
                runtime_object = output / f"{variant}-runtime.o"
                build([cxx, "-std=c++11", "-O0", "-g", "-Wall", "-Wextra", "-pthread", "-c",
                       str(runtime_source), "-o", str(runtime_object)], f"compile-{variant}-runtime")
                argv.append(str(runtime_object))
            argv.extend(["-Wl,--wrap=free", "-Wl,--no-undefined", f"-Wl,-Map,{link_map}", "-o", str(dll)])
            build(argv, f"build-{variant}")
            symbols = command([tool_paths["nm"], "-C", "--defined-only", str(dll)], output=output, stem=f"symbols-{variant}")
            disassembly = command([tool_paths["objdump"], "-d", "-C", "--disassemble=emutls_destroy", str(dll)],
                                  output=output, stem=f"emutls-destroy-{variant}")
            map_text = link_map.read_text(encoding="utf-8", errors="replace")
            symbol_counts = {
                name: len(re.findall(r"^\s*[0-9a-fA-F]+\s+[Tt]\s+" + re.escape(name) + r"\s*$",
                                     symbols["stdout"], flags=re.MULTILINE))
                for name in ("__cxa_thread_atexit", "__emutls_get_address", "emutls_destroy", "__wrap_free",
                             "pthread_key_create", "pthread_key_delete", "_pthread_cleanup_dest")
            }
            archive_fallback = re.search(r"libstdc\+\+\.a\((?:libstdc\+\+_a-)?atexit_thread\.(?:o|obj)\)", map_text) is not None
            wrapped_branches = len(re.findall(r"\b(?:callq?|jmpq?)\s+[^\n]*<__wrap_free>", disassembly["stdout"]))
            thread_proof = pthread_link_proof(map_text, symbols["stdout"], [
                summary["runtime_archives"][name]["path"] for name in ("libpthread.a", "libwinpthread.a")
            ])
            proof = {
                "symbol_counts": symbol_counts,
                "archive_fallback_present": archive_fallback,
                "selected_source_object_present": runtime_source is None or f"{variant}-runtime.o" in map_text,
                "emutls_destroy_branches_to_wrapped_free": wrapped_branches,
                "pthread_archive_providers": thread_proof["pthread_archive_providers"],
                "only_selected_pthread_archives": thread_proof["only_selected_pthread_archives"],
                "pthread_provider_proof": thread_proof,
                "link_map": str(link_map), "link_map_sha256": digest(link_map),
                "symbols_log": symbols["stdout_path"], "disassembly_log": disassembly["stdout_path"],
            }
            proof["passed"] = (symbols["returncode"] == 0 and disassembly["returncode"] == 0 and
                               all(count == 1 for count in symbol_counts.values()) and
                               archive_fallback == (variant == "archive") and
                               proof["selected_source_object_present"] and wrapped_branches >= 1 and
                               thread_proof["passed"])
            summary["link_proofs"][variant] = proof
            if not proof["passed"]:
                raise RuntimeError(f"{variant}: cannot prove selected runtime and emutls allocator interception")
            dlls[variant] = dll
        summary["binary_sha256"] = {str(path): digest(path) for path in (host, *dlls.values())}
        summary["selected_archives_unchanged_after_linking"] = all(
            digest(Path(record["path"])) == record["sha256"] for record in summary["runtime_archives"].values()
        )
        if not summary["selected_archives_unchanged_after_linking"]:
            raise RuntimeError("A selected runtime archive changed while linking the comparison")
        save()
        for mode in MODES:
            for variant, dll in dlls.items():
                result = command([str(host), str(dll), mode], output=output,
                                 stem=f"{mode}-{variant}", timeout=args.timeout)
                assessed = assess(result, variant=variant, mode=mode, expected_order=args.expected_order)
                summary["results"].append(assessed)
                save()
                print(f"{variant}/{mode}: {assessed['classification']}", flush=True)
        exact_cases = {(result["variant"], result["mode"]) for result in summary["results"]} == {
            (variant, mode) for variant in VARIANTS for mode in MODES
        } and len(summary["results"]) == 6
        summary["runtime_archives_after_execution"] = {
            name: {"path": record["path"], "sha256": digest(Path(record["path"]))}
            for name, record in summary["runtime_archives"].items()
        }
        summary["selected_archives_unchanged_after_execution"] = (
            summary["runtime_archives_after_execution"] == summary["runtime_archives"]
        )
        if not summary["selected_archives_unchanged_after_execution"]:
            raise RuntimeError("A selected runtime archive changed during the storage-order cases")
        summary["diagnostic_expectation_met"] = exact_cases and all(result["diagnostic_expectation_met"] for result in summary["results"])
        summary["storage_lifetime_contract_passed"] = exact_cases and all(result["storage_lifetime_contract_passed"] for result in summary["results"])
        save()
        return 0 if summary["diagnostic_expectation_met"] else 1
    except Exception as error:
        summary["error"] = f"{type(error).__name__}: {error}"
        save()
        print(summary["error"], file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
