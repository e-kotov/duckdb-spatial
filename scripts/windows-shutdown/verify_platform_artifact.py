#!/usr/bin/env python3
"""Verify artifacts after the unchanged, strict upstream platform test suite.

This does not replace or rerun that suite. It checks complete test registration,
the original extension footer, the loaded file's hash, native loader evidence,
and normal CLI process exit. No metadata checks are disabled.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import subprocess
import sys
import tarfile


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def sha256(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def unpack_support(artifact: Path, destination: Path) -> Path:
    bundles = list((artifact / 'test-support').glob('*/test-support.tar.gz'))
    require(len(bundles) == 1, 'Expected exactly one original test-support bundle')
    destination.mkdir()
    with tarfile.open(bundles[0], 'r:gz') as bundle:
        for member in bundle.getmembers():
            path = PurePosixPath(member.name)
            require(not path.is_absolute() and '..' not in path.parts, 'Unsafe bundle path')
            target = destination.joinpath(*path.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                require(member.isfile(), 'Unexpected link or special file in test support')
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.extractfile(member) as source, target.open('wb') as output:
                    shutil.copyfileobj(source, output)
                target.chmod(member.mode & 0o777)
    return destination / 'release'


def read_footer(path: Path) -> dict:
    # Exact layout in pinned DuckDB src/main/extension/extension_load.cpp:
    # eight reversed 32-byte fields, followed by a 256-byte signature.
    with path.open('rb') as stream:
        stream.seek(-512, os.SEEK_END)
        footer = stream.read(512)
    fields = [footer[offset:offset + 32].rstrip(b'\0').decode('ascii')
              for offset in range(0, 256, 32)][::-1]
    return dict(zip(('magic', 'platform', 'duckdb_version', 'extension_version', 'abi'), fields))


def run_process(command: list[str], env: dict, cwd: Path, output: Path,
                name: str, summary: dict, sql: str | None = None) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(command, input=sql, text=True, encoding='utf-8',
                                errors='replace', capture_output=True, env=env, cwd=cwd, timeout=60)
    except subprocess.TimeoutExpired as error:
        summary[name] = {'command': command, 'timed_out': True, 'returncode': None}
        for label, content in (('stdout', error.stdout), ('stderr', error.stderr)):
            if isinstance(content, bytes):
                content = content.decode('utf-8', errors='replace')
            (output / f'{name}-{label}.log').write_text(content or '', encoding='utf-8')
        raise RuntimeError(f'{name} did not exit normally within 60 seconds') from error
    summary[name] = {'command': command, 'timed_out': False, 'returncode': result.returncode}
    (output / f'{name}-stdout.log').write_text(result.stdout, encoding='utf-8')
    (output / f'{name}-stderr.log').write_text(result.stderr, encoding='utf-8')
    return result


def verify(args: argparse.Namespace, summary: dict) -> None:
    source = args.source.resolve(strict=True)
    artifact = args.artifact.resolve(strict=True)
    output = args.output.resolve(strict=True)
    require((args.platform == 'linux_amd64' and sys.platform == 'linux'
             and platform.machine().lower() in ('x86_64', 'amd64'))
            or (args.platform == 'osx_arm64' and sys.platform == 'darwin'
                and platform.machine().lower() == 'arm64'), 'Artifact must run on its native platform')
    extension = (artifact / 'spatial.duckdb_extension').resolve(strict=True)
    require(extension.is_relative_to(artifact), 'Extension escaped the downloaded artifact directory')
    summary.update(platform=args.platform, extension=str(extension),
                   extension_sha256=sha256(extension), footer=read_footer(extension))
    build = unpack_support(artifact, output / 'support')
    cli, unittest = build / 'duckdb', build / 'test/unittest'
    summary.update(cli_sha256=sha256(cli), unittest_sha256=sha256(unittest))
    env = os.environ.copy()
    env['LD_LIBRARY_PATH' if sys.platform == 'linux' else 'DYLD_LIBRARY_PATH'] = str(build / 'src')

    expected = {path.relative_to(source).as_posix() for path in (source / 'test').rglob('*')
                if path.is_file() and path.suffix in ('.test', '.test_slow')}
    require(bool(expected), 'No original Spatial SQL test files found')
    listed = run_process([str(unittest), '--test-dir', str(source), '--list-tests', 'test/*'],
                         env, source, output, 'registration', summary)
    rows = listed.stdout.splitlines()
    require(bool(rows) and rows[0] == 'name\tgroup', 'Missing upstream test-list header')
    registered = [row.split('\t', 1)[0] for row in rows[1:] if row]
    summary['registration'].update(expected_count=len(expected), registered_count=len(registered),
                                   missing=sorted(expected - set(registered)),
                                   extra=sorted(set(registered) - expected))
    # Catch returns the number of tests for listing (164 for the pinned sources),
    # unlike actual test/CLI execution, which must return zero.
    require(listed.returncode in (0, len(registered) % 256), 'Test listing did not exit normally')
    require(set(registered) == expected and len(registered) == len(expected),
            'Registered tests do not exactly match every original Spatial SQL test')

    cache = output / 'extension-cache'
    cache.mkdir()
    quote = lambda path: str(path).replace("'", "''")
    # The deprecated singular setting prints a warning to CLI stdout. Use the
    # supported list setting so the provenance stream remains strict JSON.
    sql = ("PRAGMA version;\nPRAGMA platform;\n"
           "SET autoinstall_known_extensions = false;\n"
           "SET autoload_known_extensions = false;\n"
           f"SET extension_directories = ['{quote(cache)}'];\n"
           f"FORCE INSTALL '{quote(extension)}';\nLOAD spatial;\n"
           "SELECT extension_name, loaded, installed, install_path, install_mode, "
           "current_setting('allow_extensions_metadata_mismatch') AS metadata_mismatch_allowed "
           "FROM duckdb_extensions() WHERE extension_name = 'spatial';\n"
           "SELECT ST_AsText(ST_Point(1, 2)) AS point;\n")
    (output / 'load.sql').write_text(sql, encoding='utf-8')
    # These diagnostics report actual native library loading; they do not alter
    # metadata validation, symbol binding, or the ordinary test suite.
    env['LD_DEBUG' if sys.platform == 'linux' else 'DYLD_PRINT_LIBRARIES'] = (
        'files' if sys.platform == 'linux' else '1')
    result = run_process([str(cli), '-unsigned', '-bail', '-json', ':memory:'],
                         env, source, output, 'load', summary, sql)

    footer = summary['footer']
    require(footer['magic'] == '4' and footer['abi'] == 'CPP', 'Unexpected extension metadata format')
    require(footer['duckdb_version'] == 'v2.0.0' and footer['platform'] == args.platform,
            f'Incorrect extension version/platform metadata: {footer}')
    require(result.returncode == 0, 'Local INSTALL/LOAD or CLI process exit failed; see load-stderr.log')
    decoder, remaining, documents = json.JSONDecoder(), result.stdout.strip(), []
    while remaining:
        value, end = decoder.raw_decode(remaining)
        documents.append(value)
        remaining = remaining[end:].lstrip()
    require(len(documents) == 4 and all(isinstance(item, list) and len(item) == 1 for item in documents),
            'Unexpected CLI provenance output')
    version, runtime_platform, loaded, point = [item[0] for item in documents]
    summary.update(engine=version, runtime_platform=runtime_platform, loaded_extension=loaded, smoke=point)
    require(version['library_version'] == 'v2.0.0' and version['source_id'] == 'd54cbbe3df',
            'CLI is not the recorded v2.0.0 DuckDB build')
    require(runtime_platform['platform'] == args.platform, 'CLI reports a different extension platform')
    require(loaded['loaded'] and loaded['installed'] and loaded['install_mode'] != 'STATICALLY_LINKED',
            'Spatial was not loaded from the supplied local artifact')
    require(loaded['metadata_mismatch_allowed'] is False, 'Extension metadata validation was disabled')
    installed = Path(loaded['install_path']).resolve(strict=True)
    require(installed.is_relative_to(cache), 'Spatial was loaded from outside the isolated install directory')
    summary['installed_sha256'] = sha256(installed)
    require(summary['installed_sha256'] == summary['extension_sha256'] == sha256(extension),
            'The installed or source Spatial binary differs from the downloaded artifact')
    require(point['point'] == 'POINT (1 2)', 'Loaded Spatial function returned an unexpected result')

    if sys.platform == 'linux':
        pattern = r'^\s*\d+:\s+calling init:\s+(.+\.duckdb_extension)\s*$'
    else:
        pattern = r'^(?:dyld\[\d+\]:\s+(?:<[^>]+>\s+)?|dyld:\s+loaded:\s+)(/.+\.duckdb_extension)\s*$'
    observed = {str(Path(path).resolve()) for path in re.findall(pattern, result.stderr, re.MULTILINE)
                if Path(path).name == 'spatial.duckdb_extension'}
    summary['observed_spatial_modules'] = sorted(observed)
    require(observed == {str(installed)}, 'Native loader trace did not identify exactly the installed Spatial binary')
    summary['all_passed'] = True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--artifact', type=Path, required=True)
    parser.add_argument('--platform', choices=('linux_amd64', 'osx_arm64'), required=True)
    parser.add_argument('--source', type=Path, default=Path('.'))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    summary = {'all_passed': False, 'purpose': 'Artifact provenance after the strict upstream test suite'}
    try:
        verify(args, summary)
    except Exception as error:
        summary['error'] = f'{type(error).__name__}: {error}'
    (args.output / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(summary, indent=2))
    return 0 if summary['all_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
