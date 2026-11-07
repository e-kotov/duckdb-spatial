"""Shared read-only checks for optional isolated Windows pthread archives."""

from __future__ import annotations

import ntpath
from pathlib import Path
import re


ARCHIVE_NAMES = ("libpthread.a", "libwinpthread.a")
ARCHIVE_PATTERN = r"lib(?:winpthread|pthread)(?:\.dll)?\.a"


def normalized_library_path(path: str) -> str:
    return ntpath.normcase(ntpath.normpath(path.strip()))


def pthread_member_provider_paths(map_text: str) -> list[str]:
    members = re.findall(r"([A-Za-z]:[^\r\n()]*" + ARCHIVE_PATTERN + r")\(", map_text)
    return sorted({normalized_library_path(path) for path in members})


def pthread_provider_paths(map_text: str) -> list[str]:
    """Normalize Windows archive paths from GNU ld member and LOAD entries."""
    loaded = re.findall(r"^\s*LOAD\s+([^\r\n]*" + ARCHIVE_PATTERN + r")\s*$", map_text, re.MULTILINE)
    return sorted(set(pthread_member_provider_paths(map_text)) |
                  {normalized_library_path(path) for path in loaded})


def isolated_pthread_archives(directory: Path) -> dict[str, Path]:
    """Require actual private files and exclude shadowing of unrelated runtimes."""
    directory = directory.resolve(strict=True)
    if not directory.is_dir():
        raise ValueError("The isolated pthread library path must be a directory")
    unexpected = [path.name for path in directory.iterdir()
                  if path.suffix.lower() in (".a", ".lib", ".dll") and path.name.lower() not in ARCHIVE_NAMES]
    if unexpected:
        raise ValueError(f"Isolated pthread directory contains unrelated link libraries: {unexpected}")
    selected = {name: (directory / name).resolve(strict=True) for name in ARCHIVE_NAMES}
    if any(path.parent != directory or not path.is_file() for path in selected.values()):
        raise ValueError("Both pthread archives must be real files inside the isolated directory")
    return selected


def pthread_link_proof(map_text: str, symbols_text: str, selected_paths: list[str]) -> dict:
    providers = pthread_provider_paths(map_text)
    member_providers = pthread_member_provider_paths(map_text)
    expected = {normalized_library_path(path) for path in selected_paths}
    definitions = {
        name: len(re.findall(r"^\s*[0-9a-fA-F]+\s+[Tt]\s+" + re.escape(name) + r"\s*$",
                             symbols_text, flags=re.MULTILINE))
        for name in ("pthread_key_create", "pthread_key_delete", "_pthread_cleanup_dest")
    }
    only_selected = bool(providers) and set(providers).issubset(expected)
    return {
        "pthread_archive_providers": providers,
        "pthread_member_providers": member_providers,
        "expected_pthread_archives": sorted(expected),
        "pthread_symbol_definitions": definitions,
        "only_selected_pthread_archives": only_selected,
        "passed": only_selected and bool(member_providers) and all(count == 1 for count in definitions.values()),
    }
