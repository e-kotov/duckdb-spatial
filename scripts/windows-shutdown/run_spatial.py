#!/usr/bin/env python3
"""Run the pinned R reproduction with a specific unsigned Spatial DLL.

The original supervisor is imported unchanged. Its success/quit-hang/R-error
classification remains strict. Only the local extension loader and provenance
checks are adapted in the R reproduction; SQL workload and cleanup stay intact.
Each fresh process gets its own extension directory. A hang always exits this
driver nonzero, including when it is useful baseline evidence.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
import difflib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
from types import SimpleNamespace

from prepare_runtime import SOURCES, replace_once


CONFIG = '''  config <- list(allow_unsigned_extensions = "true",
                 extension_directory = file.path(getwd(), "extensions"),
                 autoinstall_known_extensions = "false",
                 autoload_known_extensions = "false")
  if (one_thread) config$threads <- "1"
  drv <- duckdb::duckdb(shared_home = FALSE, config = config)'''

LOAD = '''  extension <- normalizePath(Sys.getenv("SPATIAL_TEST_EXTENSION"),
                             winslash = "/", mustWork = TRUE)
  # Windows can retain the mapped DLL after database shutdown. Install the
  # candidate once into this fresh process's directory; never replace it while
  # mapped. The remaining iterations reuse that exact copy, as INSTALL does in
  # the original reproduction's populated default extension directory.
  if (iteration == 1L) {
    DBI::dbExecute(conn, paste0("INSTALL ", as.character(DBI::dbQuoteString(conn, extension))))
  }
  DBI::dbExecute(conn, "LOAD spatial;")
  loaded <- DBI::dbGetQuery(conn, paste0(
    "SELECT extension_name, loaded, installed, install_path, installed_from, ",
    "extension_version, install_mode FROM duckdb_extensions() ",
    "WHERE extension_name = 'spatial'"))
  stopifnot(nrow(loaded) == 1L, isTRUE(loaded$loaded), isTRUE(loaded$installed),
            loaded$install_mode != "STATICALLY_LINKED")
  loaded$install_path <- normalizePath(loaded$install_path, winslash = "/", mustWork = TRUE)
  expected_dir <- normalizePath("extensions", winslash = "/", mustWork = TRUE)
  stopifnot(startsWith(tolower(loaded$install_path), paste0(tolower(expected_dir), "/")))
  stopifnot(identical(unname(tools::md5sum(loaded$install_path)),
                      unname(tools::md5sum(extension))))
  loaded$source_path <- extension
  loaded$source_md5 <- unname(tools::md5sum(extension))
  loaded$engine_version <- DBI::dbGetQuery(conn, "SELECT version() AS version")$version
  loaded$platform <- DBI::dbGetQuery(conn, "PRAGMA platform")$platform
  loaded$threads <- DBI::dbGetQuery(conn, "SELECT current_setting('threads') AS n")$n
  stopifnot(loaded$engine_version == "v2.0.0", loaded$platform == "windows_amd64_mingw")
  stopifnot(if (one_thread) loaded$threads == 1 else loaded$threads >= 2)
  write.csv(loaded, file.path(log_dir, paste0("loaded-extension-", iteration, ".csv")),
            row.names = FALSE)
  .windows_checkpoint("LOCAL SPATIAL LOADED: ", loaded$install_path)'''

SMOKE = '''
  # Additional independent GEOS/PROJ/core checks. The raw reproduction does not
  # include these, so differences in its hang frequency remain interpretable.
  query <- function(sql) DBI::dbGetQuery(conn, sql)
  stopifnot(query("SELECT ST_AsText(ST_Point(1, 2)) AS value")$value == "POINT (1 2)")
  stopifnot(query(paste0("SELECT ST_Area(ST_Intersection(",
    "ST_GeomFromText('POLYGON ((0 0, 2 0, 2 2, 0 2, 0 0))'), ",
    "ST_GeomFromText('POLYGON ((1 0, 3 0, 3 2, 1 2, 1 0))'))) AS area"))$area == 2)
  projected <- query(paste0("SELECT ST_X(g) AS x, ST_Y(g) AS y FROM (SELECT ",
    "ST_Transform(ST_Point(12, 55), 'EPSG:4326', 'EPSG:3857', always_xy := true) AS g)"))
  stopifnot(abs(projected$x - 1335833.8895192828) < 0.01,
            abs(projected$y - 7361866.113051188) < 0.01)
  aggregate <- query(paste0("SELECT count(*) AS n, ",
    "sum(ST_Area(ST_Buffer(ST_Point(i % 101, i % 97), 1))) AS a ",
    "FROM range(100000) AS t(i)"))
  stopifnot(aggregate$n == 100000, is.finite(aggregate$a),
            aggregate$a > 300000, aggregate$a < 320000)
  .windows_checkpoint("SPATIAL SMOKE RETURNED")
'''


def sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def verify_inputs(inputs: Path) -> None:
    for name in ("windows-native-geometry.R", "windows-test-supervisor.py", "countries.geojson"):
        actual = sha256(inputs / name)
        if actual != SOURCES[name][3]:
            raise ValueError(f"Pinned input changed: {name}: {actual}")


def adapt_reproducer(source: str, smoke: bool = False) -> str:
    source = replace_once(source, "print(sessionInfo())", '''print(sessionInfo())
stopifnot(as.character(getRversion()) == "4.6.2",
          as.character(packageVersion("DBI")) == "1.3.0",
          as.character(packageVersion("duckdb")) == "2.0.0")''')
    source = replace_once(source, '''  drv <- duckdb::duckdb(shared_home = FALSE,
                       config = if (one_thread) list(threads = "1") else list())''', CONFIG)
    source = replace_once(source, '  DBI::dbExecute(conn, "INSTALL spatial; LOAD spatial;")', LOAD)
    if smoke:
        anchor = '  DBI::dbExecute(conn, "SET geometry_always_xy = true")'
        source = replace_once(source, anchor, anchor + "\n" + SMOKE)
    return source


def clean_environment(extension: Path) -> dict[str, str]:
    # The process can be captured in a later debugging run. Keep CI/repository
    # credentials and user R startup files out of these diagnostic children.
    allowed = {
        "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "COMSPEC",
        "TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "HOMEDRIVE",
        "HOMEPATH", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "OS",
        "PROCESSOR_ARCHITECTURE", "PROCESSOR_IDENTIFIER", "NUMBER_OF_PROCESSORS",
        "R_HOME", "R_ARCH", "R_LIBS", "R_LIBS_USER", "R_LIBS_SITE", "R_USER",
        "RTOOLS42_HOME", "RTOOLS45_HOME", "RTOOLS46_HOME", "R_LIB_FOR_PAK",
        "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
        "LANG", "LC_ALL", "TZ", "CI", "GITHUB_ACTIONS",
    }
    result = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    result.update(SPATIAL_TEST_EXTENSION=str(extension), R_PROFILE_USER=os.devnull,
                  R_ENVIRON_USER=os.devnull, R_PROFILE=os.devnull, R_ENVIRON=os.devnull)
    return result


def inspect_provenance(case: Path, expected_sha: str) -> list[dict]:
    files = sorted((case / "diagnostics").glob("loaded-extension-*.csv"))
    if len(files) != 3:
        raise ValueError(f"Expected all three load records, found {len(files)}")
    records = []
    extension_root = (case / "extensions").resolve()
    for path in files:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
        if len(rows) != 1:
            raise ValueError(f"Invalid load record: {path}")
        row = rows[0]
        installed = Path(row["install_path"]).resolve(strict=True)
        if not installed.is_relative_to(extension_root):
            raise ValueError(f"Extension came from outside isolated repository: {installed}")
        actual = sha256(installed)
        if actual != expected_sha:
            raise ValueError(f"Loaded Spatial copy differs: {actual}, expected {expected_sha}")
        if row["loaded"] != "TRUE" or row["installed"] != "TRUE":
            raise ValueError("Spatial was not both loaded and installed")
        row["sha256"] = actual
        records.append(row)
    return records


def save(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")


def run_case(supervisor, inputs: Path, output: Path, extension_sha: str,
             thread_mode: str, attempt: int, smoke: bool = False) -> dict:
    kind = "smoke" if smoke else "raw"
    case = output / f"{kind}-{thread_mode}-{attempt:03d}"
    case.mkdir(parents=True, exist_ok=False)
    (case / "diagnostics").mkdir()
    input_dir = case / "inst" / "spatial"
    input_dir.mkdir(parents=True)
    shutil.copy2(inputs / "countries.geojson", input_dir / "countries.geojson")
    original = (inputs / "windows-native-geometry.R").read_text(encoding="utf-8")
    adapted = adapt_reproducer(original, smoke)
    runner = case / "reproducer.R"
    runner.write_text(adapted, encoding="utf-8", newline="\n")
    (case / "reproducer-adaptation.patch").write_text("".join(difflib.unified_diff(
        original.splitlines(keepends=True), adapted.splitlines(keepends=True),
        fromfile="pinned/windows-native-geometry.R", tofile="local/reproducer.R")),
        encoding="utf-8", newline="\n")
    supervisor.ROOT = case
    supervisor.OUT = case / "diagnostics"
    supervisor.RUNNER = runner
    label = "raw--all--base--" + ("t1" if thread_mode == "control" else "t0")
    previous = Path.cwd()
    try:
        os.chdir(case)
        result = supervisor.invoke(label, limit=180, quit_grace=30)
    finally:
        os.chdir(previous)
    result.update(thread_mode=thread_mode, attempt=attempt, case_kind=kind,
                  directory=str(case), adapted_reproducer_sha256=sha256(runner))
    # Retain the process outcome even if provenance is incomplete (e.g. an R
    # error before loading). Such a case can never count as fix evidence.
    try:
        result["loaded_extensions"] = inspect_provenance(case, extension_sha)
        result["provenance_passed"] = True
    except (OSError, ValueError, KeyError) as error:
        result["provenance_passed"] = False
        result["provenance_error"] = str(error)
    result["software_passed"] = result["reason"] == "success" and result["provenance_passed"]
    # The supervisor has waited for a normal exit or terminated and waited for
    # the owned process tree. Keep the exact arm DLL once, not 100+ installed
    # copies that could exhaust the hosted runner's disk during this matrix.
    installed_dir = case / "extensions"
    if installed_dir.exists():
        try:
            shutil.rmtree(installed_dir)
            result["verified_installed_copy_removed"] = result["provenance_passed"]
        except OSError as error:
            result["installed_copy_removal_error"] = str(error)
    save(case / "result.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extension", type=Path, required=True)
    parser.add_argument("--inputs", type=Path, default=Path("build/windows-shutdown-inputs"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--attempts", type=int, default=30)
    parser.add_argument("--control-attempts", type=int, default=5)
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("This experiment must run on real Windows; Wine/Linux is not validation")
    if args.attempts < 1 or args.control_attempts < 1:
        parser.error("Both attempt counts must be positive")
    if not shutil.which("Rscript"):
        parser.error("Rscript is not on PATH")
    inputs = args.inputs.resolve(strict=True)
    extension = args.extension.resolve(strict=True)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if (output / "summary.json").exists():
        parser.error("Output already contains a previous run; use a fresh directory")
    verify_inputs(inputs)
    extension_sha = sha256(extension)
    spec = importlib.util.spec_from_file_location("pinned_supervisor", inputs / "windows-test-supervisor.py")
    supervisor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(supervisor)
    env = clean_environment(extension)
    supervisor.subprocess = SimpleNamespace(
        Popen=lambda *a, **kw: subprocess.Popen(*a, env=env, stdin=subprocess.DEVNULL, **kw),
        run=lambda *a, **kw: subprocess.run(*a, env=env, **kw),
    )
    result = {
        "platform": platform.platform(), "native_windows": True,
        "extension": str(extension), "extension_sha256": extension_sha,
        "requested": {"default_attempts": args.attempts, "control_attempts": args.control_attempts},
        "supervisor_sha256": sha256(inputs / "windows-test-supervisor.py"),
        "original_reproducer_sha256": sha256(inputs / "windows-native-geometry.R"),
        "results": [], "all_passed": False,
    }
    save(output / "summary.json", result)
    # Do all requested attempts, including after a baseline hang. No fail-fast
    # sampling and no converting a terminated/hung child into an exit success.
    for thread_mode, count in (("default", args.attempts), ("control", args.control_attempts)):
        for attempt in range(1, count + 1):
            row = run_case(supervisor, inputs, output, extension_sha, thread_mode, attempt)
            result["results"].append(row)
            save(output / "summary.json", result)
    result["smoke"] = run_case(supervisor, inputs, output, extension_sha, "default", 1, smoke=True)
    if sha256(extension) != extension_sha:
        raise ValueError("Source Spatial binary changed during the experiment")
    result["counts"] = dict(Counter(row["reason"] for row in result["results"]))
    result["all_passed"] = all(row["software_passed"] for row in result["results"]) and result["smoke"]["software_passed"]
    save(output / "summary.json", result)
    print(json.dumps({k: v for k, v in result.items() if k not in ("results", "smoke")}, indent=2))
    return 0 if result["all_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
