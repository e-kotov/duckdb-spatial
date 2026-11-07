#!/usr/bin/env python3
"""Verify prepared pthread inputs and their actual providers in a Spatial DLL.

This is a read-only provenance check. It does not run a workload or turn any
failed native/R/SQL outcome into a passing software test.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PureWindowsPath
import re
import sys

from prepare_pthread_archives import (
    ARCHIVE_NAMES, PINNED_INSTALLED, PROVIDER_SYMBOLS, archive_members,
    member_manifest, verify_replacement,
)
from pthread_provenance import (
    ARCHIVE_PATTERN, isolated_pthread_archives, normalized_library_path, pthread_link_proof,
)


ARMS = ("original", "rebuilt-baseline", "reverse-order")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def relative_file(root: Path, name: str) -> Path:
    """Resolve manifest-relative Windows paths without permitting other inputs."""
    value = PureWindowsPath(name)
    require(not value.is_absolute() and not value.drive and ".." not in value.parts,
            f"Expected a relative preparation output path: {name}")
    path = root.joinpath(*value.parts).resolve(strict=True)
    require(path.is_relative_to(root) and path.is_file(), f"Output outside preparation: {name}")
    return path


def inspect_preparation(directory: Path) -> dict:
    """Recheck every archive and installed input against the retained manifest."""
    root = directory.resolve(strict=True)
    require(root.is_dir(), "Preparation must be a directory")
    manifest_path = root / "manifest.json"
    manifest_hash = digest(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    require(type(manifest.get("schema")) is int and manifest["schema"] == 1,
            "Unsupported preparation manifest schema")
    require(manifest.get("status") == "prepared" and
            manifest.get("native_preparation_passed") is True and
            manifest.get("installed_inputs_unchanged") is True,
            "Native preparation and installed-input integrity must have passed")
    require(manifest.get("layout", {}).get("passed") is True and
            manifest.get("installed_public_headers_match_v9_normalized") is True,
            "Native ABI and public-header checks must have passed")
    require(set(manifest.get("variants", {})) == set(ARMS), "Incomplete pthread arm set")
    installed = manifest.get("installed_inputs")
    require(isinstance(installed, dict) and bool(installed), "Missing installed input records")
    for path_text, record in installed.items():
        path = Path(record["path"]).resolve(strict=True)
        require(path == Path(path_text).resolve(strict=True) and path.is_file(),
                "Installed-input path disagrees with manifest key")
        require(record.get("unchanged") is True and
                record["before_sha256"] == record["after_sha256"] == digest(path),
                f"Installed input changed: {path}")
    compiler_record = manifest["tools"]["g++"]
    compiler_path = Path(compiler_record["path"]).resolve(strict=True)
    compiler_hash = digest(compiler_path)
    require(compiler_hash == compiler_record["before_sha256"] == PINNED_INSTALLED["g++.exe"] and
            str(compiler_path) in installed,
            "Prepared compiler no longer matches the pinned installed input")

    result = {
        "passed": False, "prep_manifest_sha256": manifest_hash,
        "preparation_directory": str(root), "variants": {},
        "installed_inputs_checked": len(installed), "installed_inputs_unchanged": True,
        "compiler": {"path": str(compiler_path), "sha256": compiler_hash},
    }
    original_members = {}
    for arm in ARMS:
        variant = manifest["variants"][arm]
        library_dir = (root / arm / "lib").resolve(strict=True)
        require(Path(variant["library_dir"]).resolve(strict=True) == library_dir,
                f"{arm}: library directory disagrees with preparation output")
        require(library_dir.is_relative_to(root), f"{arm}: library directory escapes preparation")
        archives = isolated_pthread_archives(library_dir)
        require(set(variant.get("archives", {})) == set(ARCHIVE_NAMES),
                f"{arm}: incomplete archive records")
        verified = {}
        for name in ARCHIVE_NAMES:
            path = archives[name]
            require(not (library_dir / name).is_symlink(), f"{arm}: archive must not be a symlink")
            record = variant["archives"][name]
            require(relative_file(root, record["path"]) == path,
                    f"{arm}/{name}: archive path disagrees with manifest")
            actual_hash = digest(path)
            require(actual_hash == record["sha256"], f"{arm}/{name}: archive hash changed")
            members = archive_members(path.read_bytes())
            require(member_manifest(members) == record["members"],
                    f"{arm}/{name}: archive member manifest changed")
            provider = record["thread_provider"]
            payload = dict(members).get(provider)
            require(payload is not None and hashlib.sha256(payload).hexdigest() == record["thread_object_sha256"],
                    f"{arm}/{name}: thread provider payload differs")
            if arm == "original":
                require(record.get("exact_installed_copy") is True and actual_hash == PINNED_INSTALLED[name],
                        f"{arm}/{name}: expected exact original installed archive")
                require(record.get("thread_global_symbols_match_rebuild") is True,
                        f"{arm}/{name}: missing native global-symbol comparison")
                original_members[name] = members
            else:
                require(record.get("all_other_member_payloads_identical") is True and
                        record.get("member_names_and_order_identical") is True,
                        f"{arm}/{name}: preparation did not prove isolated member replacement")
                obj_record = manifest["objects"][arm]
                obj = relative_file(root, obj_record["path"])
                require(digest(obj) == obj_record["sha256"] == record["thread_object_sha256"],
                        f"{arm}/{name}: rebuilt object no longer matches archive provider")
                verify_replacement(original_members[name], members, provider, obj.read_bytes())
            verified[name] = {"path": str(path), "sha256": actual_hash,
                              "thread_provider": provider,
                              "thread_object_sha256": record["thread_object_sha256"]}
        result["variants"][arm] = {"library_dir": str(library_dir), "archives": verified}
    require(digest(manifest_path) == manifest_hash, "Preparation manifest changed during verification")
    result["passed"] = True
    return result


def inspect_link(map_text: str, symbols_text: str, archives: dict) -> dict:
    """A search flag or LOAD entry alone cannot prove which implementation won."""
    base = pthread_link_proof(map_text, symbols_text,
                              [archives[name]["path"] for name in ARCHIVE_NAMES])
    pairs = sorted({(normalized_library_path(path), member) for path, member in
                    re.findall(r"([A-Za-z]:[^\r\n()]*" + ARCHIVE_PATTERN + r")\(([^()\r\n]+)\)",
                               map_text, flags=re.IGNORECASE)})
    loads = re.findall(r"^\s*LOAD\s+([^\r\n]*" + ARCHIVE_PATTERN + r")\s*$",
                       map_text, flags=re.MULTILINE | re.IGNORECASE)
    providers = {path for path, _ in pairs} | {normalized_library_path(path) for path in loads}
    expected = {normalized_library_path(archives[name]["path"]): archives[name]["thread_provider"]
                for name in ARCHIVE_NAMES}
    selected_thread = [(path, member) for path, member in pairs if expected.get(path) == member]
    definition_types = {
        name: re.findall(r"^\s*[0-9a-fA-F]+\s+([A-Za-z?])\s+" + re.escape(name) + r"\s*$",
                         symbols_text, flags=re.MULTILINE)
        for name in PROVIDER_SYMBOLS
    }
    definitions = {name: len(kinds) for name, kinds in definition_types.items()}
    passed = (base["passed"] and bool(providers) and providers.issubset(expected) and
              len(selected_thread) == 1 and all(kinds in (["T"], ["t"]) for kinds in definition_types.values()))
    return {
        **base, "passed": passed,
        "all_pthread_archive_providers": sorted(providers),
        "selected_thread_members": [{"archive": path, "member": member} for path, member in selected_thread],
        "expected_thread_members": expected,
        "pthread_api_definition_counts": definitions,
        "pthread_api_definition_types": definition_types,
        "exact_selected_thread_member": len(selected_thread) == 1,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preparation", type=Path, required=True)
    parser.add_argument("--check-preparation", action="store_true")
    parser.add_argument("--pthread-arm", choices=ARMS)
    parser.add_argument("--extension", type=Path)
    parser.add_argument("--link-map", type=Path)
    parser.add_argument("--symbols", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    build_args = (args.pthread_arm, args.extension, args.link_map, args.symbols)
    if args.check_preparation:
        if any(value is not None for value in build_args):
            parser.error("Preparation-only mode must not supply build inputs")
    elif any(value is None for value in build_args):
        parser.error("Build verification requires pthread arm, extension, link map and symbols")
    output = args.output.resolve()
    if output.is_relative_to(args.preparation.resolve()) or any(
            path is not None and output == path.resolve()
            for path in (args.extension, args.link_map, args.symbols)):
        parser.error("The report must not overwrite preparation or build inputs")
    report = {"passed": False, "pthread_arm": args.pthread_arm,
              "spatial_workload_assessed": False, "check_preparation_only": args.check_preparation}
    try:
        preparation = inspect_preparation(args.preparation)
        if args.check_preparation:
            report.update(preparation)
        else:
            variant = preparation["variants"][args.pthread_arm]
            map_bytes, symbols_bytes = args.link_map.read_bytes(), args.symbols.read_bytes()
            proof = inspect_link(map_bytes.decode("utf-8-sig"), symbols_bytes.decode("utf-8-sig"),
                                 variant["archives"])
            report.update({
                "extension_sha256": digest(args.extension),
                "prep_manifest_sha256": preparation["prep_manifest_sha256"],
                "selected_library_dir": variant["library_dir"],
                "archives": {name: {"path": info["path"], "sha256": info["sha256"]}
                             for name, info in variant["archives"].items()},
                "link_map_sha256": hashlib.sha256(map_bytes).hexdigest(),
                "symbols_sha256": hashlib.sha256(symbols_bytes).hexdigest(),
                "link_proof": proof,
            })
            require(proof["passed"], "Link map or unique APIs do not prove the selected pthread implementation")
            report["passed"] = True
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"passed": report["passed"], "output": str(args.output),
                      "error": report.get("error")}, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
