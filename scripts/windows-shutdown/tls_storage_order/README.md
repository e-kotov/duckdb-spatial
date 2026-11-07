# TLS allocation and cleanup ordering probe

This is a separate, deterministic observation of the GCC 10.4 emulated TLS
allocation lifetime. It does not replace or modify the existing 12-case native
shutdown fixture, and it does not run Spatial or establish the cause of an R
shutdown hang.

## What is measured

Each fresh native process loads one DLL and creates one worker, using either
the DLL's `pthread_create` or the C host's `CreateThread`. The worker touches
one real compiler-generated `thread_local` allocation twice, then registers a
cleanup callback through the exact GCC `__cxa_thread_atexit` implementation.
Two public pthread keys, created afterward, independently show whether key
destructors run in ascending or descending order.

The TLS object is trivially destructible. No member destructor is registered
for it: invoking such a destructor after its storage was released could itself
be undefined behavior even if the destructor did not read `this`. Instead,
the explicitly registered cleanup callback receives live C-host state. The
observer and callbacks touch only that state; they do not read the TLS object,
allocate memory, use CRT logging, or wait.

The compiler must emit `__emutls_get_address`. For the probe's eight-byte size
and alignment, the pinned [GCC 10.4 allocation implementation](https://github.com/gcc-mirror/gcc/blob/7ff47281ce4f3699185b06a3430968eac2a5b0c6/libgcc/emutls.c)
returns the allocation base plus eight bytes. The worker publishes that base
as an integer while the object is alive. It never reads an allocator header or
performs pointer arithmetic outside the object.

GNU ld's `--wrap=free` observes the matching release and always calls the real
allocator once. It records both entry and return, with the completion event
written only after `free` returns. The linked `emutls_destroy` disassembly must
contain a branch to the wrapper, and the process must observe exactly one
matching release. A missing callback or an inactive wrapper is a failure,
never evidence that storage remained alive.

The host waits on a duplicated native worker HANDLE in both modes. It also
joins the pthread worker where applicable, checks exact callback counts,
deletes only its two public keys, verifies the DLL's normal global destructor,
and confirms the DLL actually unloaded. Its state remains alive through all
these operations. Success requires normal process exit, independently of the
reported ordering.

## Distinguishing the experiments

The runner uses the original GCC archive, the exact rebuilt unmodified source,
and the existing shutdown-guarded source. It compiles the same probe object
once and links all three variants against the selected pthread archives.

| Expected observation | Full event order | Meaning |
| --- | --- | --- |
| `storage-first` | Release entry, release return, GCC callback, lower key, higher key | Reproduces storage release before the GCC cleanup callback. A successful diagnostic still has `storage_lifetime_contract_passed: false`. |
| `callback-first` | Higher key, lower key, GCC callback, release entry, release return | Establishes cleanup-before-release for this bounded probe. |

The old MinGW traversal is ascending. The accepted [MinGW commit 8e06daa](https://github.com/mingw-w64/mingw-w64/commit/8e06daa36dfcea4bb491acf4b350658f40738f02)
reverses traversal to preserve this GCC fallback's destructor-before-release
ordering. Comparing exact-original, rebuilt-original, and reverse-order
pthread archives can distinguish the ordering change from rebuilding alone.
The shutdown guard does not modify key traversal, so it is expected to retain
the same storage order as the matching unguarded source.

The required `--expected-order` is deliberate: exit zero means all six cases
matched that explicit diagnostic expectation. The summary separately reports
whether the storage-lifetime contract passed. Unknown orders, a timeout,
forced termination, nonzero exit, skipped release, duplicate callback,
incomplete unload, or unproven runtime provenance fail the diagnostic.

## Running on Windows

Prepare the existing hash-checked inputs with `prepare_runtime.py`. Then run:

```powershell
python scripts/windows-shutdown/run_tls_storage_order.py `
  --cxx C:/rtools42/x86_64-w64-mingw32.static.posix/bin/g++.exe `
  --baseline-runtime-source build/windows-shutdown/runtime/baseline.cc `
  --guarded-runtime-source build/windows-shutdown/runtime/guarded.cc `
  --expected-order storage-first `
  --output-dir build/tls-storage-order/original
```

To compare an isolated rebuilt pthread runtime, additionally pass
`--pthread-library-dir <prepared-arm>/lib`. That directory must contain both
`libpthread.a` and `libwinpthread.a`; the runner prepends it with `-L` and
rejects any pthread archive provider outside the selected directory. It
records both the installed default hashes and the selected hashes, checks
unique runtime symbol definitions and link maps, and verifies the selected
archives were unchanged after linking and after the native cases. It never
modifies installed archives.

Use a fresh output directory for each invocation. Repeat with the three
prepared pthread arms, using `storage-first` for original and rebuilt-original
and `callback-first` for the reverse-order patch. Each invocation retains
commands, stdout/stderr, object files, hosts, DLLs, hashes, maps, disassembly,
and a JSON summary.

Local gate tests run with:

```text
python -m unittest discover -s scripts/windows-shutdown -p test_tls_storage_order.py -v
```

These Python tests validate rejection of misleading records. They do not
compile or execute the native fixture. The native probe requires actual
Windows and the specified GCC toolchain.

## Existing cleanup contracts against the same archives

The unchanged 12-case fixture can additionally use the same private directory:

```powershell
python scripts/windows-shutdown/run_runtime.py `
  --cxx C:/rtools42/x86_64-w64-mingw32.static.posix/bin/g++.exe `
  --baseline-runtime-source build/windows-shutdown/runtime/baseline.cc `
  --guarded-runtime-source build/windows-shutdown/runtime/guarded.cc `
  --pthread-library-dir <prepared-arm>/lib `
  --output-dir build/tls-cleanup-controls/<arm> `
  --timeout 10 --cycles 16 --workers 8
```

This optional argument adds `-L` to archive lookup and link commands, verifies
the actual compiler-selected paths, proves only the selected pthread archive
providers occur in each DLL's link map, and rehashes the archives after linking
and after the native cases. The shared helper is loaded lazily by
`run_runtime.py` only when this option is supplied. Its original default
commands, C/C++ fixture, 12-case matrix and classifications are unchanged.

Run the controls separately for each prepared pthread arm. They retain the
normal `std::thread`, foreign-thread, automatic TLS destructor, main-thread,
global destructor, `atexit` and actual-unload checks. The two unguarded
abandoned-reader cases must still reproduce their diagnostic shutdown hangs;
these are still failed software exits. The reverse key-order change alone
does not remove the explicitly abandoned key-table reader.

These results and the storage-order observations are complementary. Do not
replace the original archived nine-case full comparison or later twelve-case
run with this new evidence. Private-provider rejection tests run with:

```text
python -m unittest discover -s scripts/windows-shutdown -p test_pthread_provenance.py -v
```

## Limits

- The probe observes real emutls storage release and the real GCC cleanup list,
  but deliberately avoids executing a C++ member destructor on freed storage.
  It establishes the ordering hazard without causing that undefined behavior.
- One trivial TLS object, one registered callback and two public keys do not
  exhaust reentrant destructor registration, all object types or every DLL
  lifetime pattern.
- Wrapper instrumentation changes the linked DLL. It brackets the original
  `free` and adds counter updates; it does not delay release or change key order.
- A successful reverse-order result would validate the bounded lifetime
  contract, not prove that this hazard caused the recorded shutdown deadlock.
  The independent abandoned-reader and actual R workloads remain necessary.
