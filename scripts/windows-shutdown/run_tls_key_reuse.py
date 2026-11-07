#!/usr/bin/env python3
"""Observe whether legal low-key reuse reverses the runtime lifetime ordering.

Exit zero means all 12 normal/key-reuse observations match the explicitly
recorded diagnostic expectations and all input-integrity checks pass. Expected
storage-first observations remain failed lifetime contracts. This does not
assess the R shutdown hang or validate a production runtime change.
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
from prepare_pthread_archives import PINNED_INSTALLED
from run_runtime import command, digest
from verify_pthread_build import ARMS, inspect_link, inspect_preparation


VARIANTS = ("archive", "baseline", "guarded")
MODES = ("pthread", "foreign")
SCENARIOS = ("normal", "key-reuse")
SOURCE_REFERENCES = {
    "gcc_emutls": "https://github.com/gcc-mirror/gcc/blob/7ff47281ce4f3699185b06a3430968eac2a5b0c6/libgcc/emutls.c",
    "mingw_v9_thread": "https://github.com/mingw-w64/mingw-w64/blob/v9.0.0/mingw-w64-libraries/winpthreads/src/thread.c",
    "reverse_key_order_patch": "https://github.com/mingw-w64/mingw-w64/commit/8e06daa36dfcea4bb491acf4b350658f40738f02",
}


def expectations(pthread_arm: str, scenario: str) -> tuple[str, str]:
    if pthread_arm not in ARMS or scenario not in SCENARIOS:
        raise ValueError("Unknown private pthread family or setup scenario")
    descending = pthread_arm == "reverse-order"
    callback_first = descending != (scenario == "key-reuse")
    return ("callback-first" if callback_first else "storage-first",
            "descending" if descending else "ascending")


def assess(result: dict, *, variant: str, mode: str, scenario: str,
           expected_order: str, expected_traversal: str) -> dict:
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

    exited_zero = (result["timed_out"] is False and type(result["returncode"]) is int
                   and result["returncode"] == 0)
    checks = {
        "process_exited_zero": exited_zero,
        "not_terminated_by_supervisor": result["terminated_by_supervisor"] is False,
        "empty_stderr": not result["stderr"].strip(),
        "valid_json_observation": parse_error is None,
        "known_case": variant in VARIANTS and mode in MODES and scenario in SCENARIOS,
    }
    lifetime_order = "unproven"
    key_order = "unproven"
    if parse_error is None:
        exact_integers = {
            "schema": 1, "worker_error": 0, "storage_touches": 2,
            "cleanup_registrations": 1, "keys_created": 2, "keys_deleted": 2,
            "sequence": 5, "release_calls": 1, "cleanup_callbacks": 1,
            "low_key_callbacks": 1, "high_key_callbacks": 1, "dll_destructors": 1,
            "reserved_key_created": 1, "reserved_key_deleted": 1,
            "key_create_calls": 5, "key_create_phase_mask": 62, "key_create_phase": 0,
            "setup_sequence": 4, "reserve_created_event": 1, "storage_ready_event": 2,
        }
        checks["exact_callback_and_cleanup_counts"] = all(
            type(observation.get(name)) is int and observation[name] == expected
            for name, expected in exact_integers.items()
        )
        checks["requested_worker_mode"] = observation.get("mode") == mode
        checks["requested_setup_scenario"] = observation.get("scenario") == scenario
        checks["native_wait_confirmed"] = observation.get("native_wait_confirmed") is True
        checks["actual_unload_confirmed"] = observation.get("actual_unload_confirmed") is True
        checks["worker_identified"] = type(observation.get("worker_id")) is int and observation["worker_id"] > 0
        low, high = observation.get("low_key"), observation.get("high_key")
        checks["public_keys_are_distinct_and_increasing"] = (
            type(low) is int and type(high) is int and 0 <= low < high
        )
        expected_keys = {
            "reserved_key": 0, "emutls_key": 1,
            "cleanup_key": 0 if scenario == "key-reuse" else 2,
            "low_key": 2 if scenario == "key-reuse" else 0, "high_key": 3,
        }
        checks["actual_runtime_and_public_key_allocation"] = all(
            type(observation.get(name)) is int and observation[name] == value
            for name, value in expected_keys.items()
        )
        expected_setup = {"reserve_deleted_event": 3 if scenario == "key-reuse" else 4,
                          "cleanup_registered_event": 4 if scenario == "key-reuse" else 3}
        checks["reservation_deleted_at_requested_point"] = all(
            type(observation.get(name)) is int and observation[name] == value
            for name, value in expected_setup.items()
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
            # The public keys straddle the runtime keys in the paired control,
            # but follow both runtime keys in key-reuse. Measure these orders
            # independently; never infer lifetime safety from public traversal.
            checks["callback_is_outside_allocator_call"] = lifetime_order != "unproven"
        else:
            checks["release_return_observed"] = False
            checks["callback_and_allocator_counter_agreement"] = False
            checks["callback_is_outside_allocator_call"] = False

    observation_valid = all(checks.values())
    expectation_met = (observation_valid and lifetime_order == expected_order
                       and key_order == expected_traversal)
    contract_passed = observation_valid and lifetime_order == "callback-first"
    return {
        **result,
        "variant": variant, "mode": mode, "scenario": scenario,
        "expected_order": expected_order, "expected_traversal": expected_traversal,
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
    }



def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cxx", required=True)
    parser.add_argument("--baseline-runtime-source", type=Path, required=True)
    parser.add_argument("--guarded-runtime-source", type=Path, required=True)
    parser.add_argument("--preparation", type=Path, required=True)
    parser.add_argument("--pthread-arm", choices=ARMS, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("This probe requires actual Windows, not Wine or Linux")
    if not 0 < args.timeout <= 120:
        parser.error("timeout must be in (0,120]")
    output = args.output_dir.resolve()
    preparation_directory = args.preparation.resolve(strict=True)
    if output.is_relative_to(preparation_directory):
        parser.error("Observation output must not be inside the immutable preparation")
    if output.exists() and any(output.iterdir()):
        parser.error("Output directory must be absent or empty; previous observations cannot be reused")
    output.mkdir(parents=True, exist_ok=True)
    source_root = Path(__file__).resolve().parent
    fixture = source_root / "tls_key_reuse"
    sources = {
        "archive": None,
        "baseline": args.baseline_runtime_source.resolve(strict=True),
        "guarded": args.guarded_runtime_source.resolve(strict=True),
    }
    summary = {
        "purpose": "compare actual storage/callback ordering with and without legal low-key reuse",
        "pthread_arm": args.pthread_arm,
        "expected_orders": {scenario: {"lifetime": expectations(args.pthread_arm, scenario)[0],
                                       "public_traversal": expectations(args.pthread_arm, scenario)[1]}
                            for scenario in SCENARIOS},
        "original_shutdown_fix_assessed": False,
        "spatial_workload_assessed": False,
        "normal_exit_is_not_lifetime_safety": True,
        "platform": platform.platform(), "python": sys.version,
        "source_references": SOURCE_REFERENCES,
        "source_sha256": {str(path): digest(path) for path in (
            fixture / "probe.cpp", fixture / "probe.h", fixture / "host.c",
            Path(__file__).resolve(), source_root / "run_runtime.py", source_root / "prepare_runtime.py",
            source_root / "pthread_provenance.py", source_root / "prepare_pthread_archives.py",
            source_root / "verify_pthread_build.py", sources["baseline"], sources["guarded"],
        )},
        "commands": [], "builds": [], "link_proofs": {}, "results": [],
        "diagnostic_expectation_met": False, "storage_lifetime_contract_passed": False,
        "input_integrity_passed": False,
    }

    def save() -> None:
        (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    def build(argv: list[str], stem: str) -> dict:
        result = command(argv, output=output, stem=stem)
        summary["builds"].append({"name": stem, **result})
        if result["returncode"] != 0 or result["timed_out"]:
            raise RuntimeError(f"Build failed: {stem}")
        return result

    save()
    try:
        preparation = inspect_preparation(preparation_directory)
        summary["preparation_before"] = preparation
        (output / "preparation-before.json").write_text(json.dumps(preparation, indent=2) + "\n", encoding="utf-8")
        variant_inputs = preparation["variants"][args.pthread_arm]
        private_archives = variant_inputs["archives"]
        pthread_directory = Path(variant_inputs["library_dir"])
        if digest(sources["baseline"]) != SOURCES["gcc10-atexit_thread.cc"][3]:
            raise RuntimeError("Baseline must be the exact pinned GCC 10.4 fallback source")
        if sources["guarded"].read_bytes() != guard_source(sources["baseline"].read_text(encoding="utf-8")).encode("utf-8"):
            raise RuntimeError("Guarded input differs from the existing shutdown guard")
        cxx_found = shutil.which(args.cxx)
        if not cxx_found:
            raise RuntimeError(f"Compiler not found: {args.cxx}")
        cxx = str(Path(cxx_found).resolve())
        summary["compiler_binary"] = {"path": cxx, "sha256": digest(Path(cxx))}
        if summary["compiler_binary"] != preparation["compiler"]:
            raise RuntimeError("Driver compiler differs from the prepared pinned compiler")
        identified = {}
        for name, option in (("version", "-dumpfullversion"), ("target", "-dumpmachine")):
            result = command([cxx, option], output=output, stem=f"compiler-{name}")
            summary["commands"].append(result)
            if result["returncode"] != 0 or result["timed_out"]:
                raise RuntimeError(f"Cannot identify compiler {name}")
            identified[name] = result["stdout"].strip()
        if identified["version"] != "10.4.0" or not identified["target"].startswith("x86_64-w64-mingw32"):
            raise RuntimeError("The selected compiler is not the pinned Windows x64 MinGW compiler")
        summary["compiler_identity"] = identified
        summary["runtime_archives"] = {}
        summary["compiler_default_runtime_archives"] = {}
        summary["selected_pthread_library_dir"] = str(pthread_directory)
        for name in ("libstdc++.a", "libgcc.a", "libpthread.a", "libwinpthread.a"):
            result = command([cxx, f"-print-file-name={name}"], output=output, stem=f"compiler-{name}")
            summary["commands"].append(result)
            if result["returncode"] != 0 or result["timed_out"]:
                raise RuntimeError(f"Cannot locate installed {name}")
            path = Path(result["stdout"].strip()).resolve(strict=True)
            default = {"path": str(path), "sha256": digest(path)}
            if default["sha256"] != PINNED_INSTALLED[name]:
                raise RuntimeError(f"Installed {name} differs from the pinned runtime")
            summary["compiler_default_runtime_archives"][name] = default
            if name in private_archives:
                path = Path(private_archives[name]["path"])
            summary["runtime_archives"][name] = {"path": str(path), "sha256": digest(path)}
        tool_paths = {}
        for name in ("nm", "objdump"):
            candidate = Path(cxx).with_name(Path(cxx).name.replace("g++", name))
            tool = shutil.which(str(candidate))
            if not tool:
                raise RuntimeError(f"Matching {name} unavailable: {candidate}")
            tool_paths[name] = tool

        host = output / "key-reuse-host.exe"
        build([cxx, "-x", "c", "-std=c11", "-O0", "-g", "-Wall", "-Wextra", "-static",
               str(fixture / "host.c"), "-o", str(host)], "build-host")
        fixture_object = output / "key-reuse.o"
        build([cxx, "-std=c++11", "-O0", "-g", "-Wall", "-Wextra", "-pthread", "-c",
               str(fixture / "probe.cpp"), "-o", str(fixture_object)], "compile-probe")
        undefined = command([tool_paths["nm"], "--undefined-only", str(fixture_object)],
                            output=output, stem="probe-undefined-symbols")
        required_references = ("__emutls_get_address", "__cxa_thread_atexit", "__real_free", "__real_pthread_key_create")
        if undefined["returncode"] != 0 or not all(
            re.search(r"\bU\s+" + re.escape(name) + r"\s*$", undefined["stdout"], re.MULTILINE)
            for name in required_references
        ):
            raise RuntimeError("Probe did not reference actual compiler emutls/GCC cleanup and original wrapped functions")
        summary["compiler_emutls_access_proven"] = True
        dlls = {}
        for variant, runtime_source in sources.items():
            dll = output / f"key-reuse-{variant}.dll"
            link_map = output / f"key-reuse-{variant}.map"
            argv = [cxx, f"-L{pthread_directory}", "-shared", "-static", "-static-libgcc", "-static-libstdc++",
                    "-pthread", str(fixture_object)]
            if runtime_source:
                runtime_object = output / f"{variant}-runtime.o"
                build([cxx, "-std=c++11", "-O0", "-g", "-Wall", "-Wextra", "-pthread", "-c",
                       str(runtime_source), "-o", str(runtime_object)], f"compile-{variant}-runtime")
                argv.append(str(runtime_object))
            argv.extend(["-Wl,--wrap=free", "-Wl,--wrap=pthread_key_create", "-Wl,--no-undefined",
                         f"-Wl,-Map,{link_map}", "-o", str(dll)])
            build(argv, f"build-{variant}")
            symbols = command([tool_paths["nm"], "-C", "--defined-only", str(dll)], output=output, stem=f"symbols-{variant}")
            disassembly = command([tool_paths["objdump"], "-d", "-C", "--disassemble=emutls_destroy", str(dll)],
                                  output=output, stem=f"emutls-destroy-{variant}")
            map_text = link_map.read_text(encoding="utf-8", errors="replace")
            definition_types = {
                name: re.findall(r"^\s*[0-9a-fA-F]+\s+(\S)\s+" + re.escape(name) + r"\s*$",
                                 symbols["stdout"], flags=re.MULTILINE)
                for name in ("__cxa_thread_atexit", "__emutls_get_address", "emutls_destroy", "__wrap_free",
                             "__wrap_pthread_key_create", "pthread_key_create", "pthread_key_delete", "_pthread_cleanup_dest")
            }
            archive_fallback = re.search(r"libstdc\+\+\.a\([^)]*atexit_thread\.(?:o|obj)\)", map_text) is not None
            wrapped_branches = len(re.findall(r"\b(?:callq?|jmpq?)\s+[^\n]*<__wrap_free>", disassembly["stdout"]))
            thread_proof = inspect_link(map_text, symbols["stdout"], private_archives)
            proof = {
                "symbol_definition_types": definition_types,
                "archive_fallback_present": archive_fallback,
                "selected_source_object_present": runtime_source is None or f"{variant}-runtime.o" in map_text,
                "emutls_destroy_branches_to_wrapped_free": wrapped_branches,
                "pthread_provider_proof": thread_proof,
                "link_map": str(link_map), "link_map_sha256": digest(link_map),
                "symbols_log": symbols["stdout_path"], "symbols_sha256": digest(Path(symbols["stdout_path"])),
                "disassembly_log": disassembly["stdout_path"],
                "disassembly_sha256": digest(Path(disassembly["stdout_path"])),
            }
            proof["passed"] = (symbols["returncode"] == 0 and disassembly["returncode"] == 0
                               and not symbols["timed_out"] and not disassembly["timed_out"]
                               and all(kinds in (["T"], ["t"]) for kinds in definition_types.values())
                               and archive_fallback == (variant == "archive")
                               and proof["selected_source_object_present"] and wrapped_branches >= 1
                               and thread_proof["passed"])
            summary["link_proofs"][variant] = proof
            if not proof["passed"]:
                raise RuntimeError(f"{variant}: selected runtime/member or allocator interception is unproved")
            dlls[variant] = dll
        summary["binary_sha256"] = {str(path): digest(path) for path in (host, *dlls.values())}
        summary["selected_archives_unchanged_after_linking"] = all(
            digest(Path(record["path"])) == record["sha256"] for record in summary["runtime_archives"].values()
        )
        if not summary["selected_archives_unchanged_after_linking"]:
            raise RuntimeError("A selected runtime archive changed while linking")
        save()
        for scenario in SCENARIOS:
            expected_order, expected_traversal = expectations(args.pthread_arm, scenario)
            for mode in MODES:
                for variant, dll in dlls.items():
                    result = command([str(host), str(dll), mode, scenario], output=output,
                                     stem=f"{scenario}-{mode}-{variant}", timeout=args.timeout)
                    assessed = assess(result, variant=variant, mode=mode, scenario=scenario,
                                      expected_order=expected_order, expected_traversal=expected_traversal)
                    summary["results"].append(assessed)
                    save()
                    print(f"{scenario}/{variant}/{mode}: {assessed['classification']}; "
                          f"lifetime={assessed['observed_order']}; public={assessed['public_key_order']}", flush=True)
        pairs = [(row["scenario"], row["variant"], row["mode"]) for row in summary["results"]]
        exact_cases = len(pairs) == 12 and set(pairs) == {
            (scenario, variant, mode) for scenario in SCENARIOS for variant in VARIANTS for mode in MODES
        }
        summary["diagnostic_expectation_met"] = exact_cases and all(row["diagnostic_expectation_met"] for row in summary["results"])
        summary["all_software_exits_passed"] = exact_cases and all(row["process_exit_passed"] and row["observation_valid"] for row in summary["results"])
        summary["storage_lifetime_contract_passed"] = exact_cases and all(row["storage_lifetime_contract_passed"] for row in summary["results"])
    except Exception as error:
        summary["error"] = f"{type(error).__name__}: {error}"
        summary["diagnostic_expectation_met"] = False
        print(summary["error"], file=sys.stderr)
    finally:
        try:
            after = inspect_preparation(preparation_directory)
            summary["preparation_after"] = after
            (output / "preparation-after.json").write_text(json.dumps(after, indent=2) + "\n", encoding="utf-8")
            summary["input_integrity_passed"] = after == summary.get("preparation_before")
            for name in ("runtime_archives", "compiler_default_runtime_archives"):
                if name in summary:
                    rehashed = {key: {"path": record["path"], "sha256": digest(Path(record["path"]))}
                                for key, record in summary[name].items()}
                    summary[f"{name}_after_execution"] = rehashed
                    summary["input_integrity_passed"] = summary["input_integrity_passed"] and rehashed == summary[name]
            if not summary["input_integrity_passed"]:
                raise RuntimeError("Before/after installed-input and private-archive audit differs")
        except Exception as error:
            summary["integrity_error"] = f"{type(error).__name__}: {error}"
            summary["input_integrity_passed"] = False
            print(summary["integrity_error"], file=sys.stderr)
        summary["diagnostic_expectation_met"] = summary["diagnostic_expectation_met"] and summary["input_integrity_passed"]
        save()
    return 0 if summary["diagnostic_expectation_met"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
