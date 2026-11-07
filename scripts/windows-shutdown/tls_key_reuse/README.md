# Bounded low-key reuse experiment

This is a separate native experiment for a specific safety question about the
accepted MinGW reverse-key traversal change. It does not change the six-arm
Spatial/R comparison, rebuild the complete toolchain, or establish the cause of
the recorded R shutdown hang.

The existing native experiment proves the usual initialization sequence:
emutls registers a storage-release key, GCC registers its callback key next,
and descending numeric traversal runs callbacks first. The question here is
whether a legally freed lower key can make numeric order differ from creation
order. The source predicts that it can. Until the new workflow executes, this
is a source-derived regression hypothesis, not a reproduced regression.

## Exact paired setup

Each case starts with a newly loaded DLL in a fresh process. The worker first
creates a public key with a null destructor and requires that its actual key
is 0. It then accesses one trivial, aligned compiler-generated `thread_local`
object, which initializes the real GCC emutls runtime and allocates storage.

| Scenario | Reserved-key deletion | Actual keys required by the probe | Later public callback keys |
| --- | --- | --- | --- |
| `normal` | After GCC cleanup registration | reserved 0, emutls 1, GCC cleanup 2 | 0 and 3 |
| `key-reuse` | After storage allocation, before GCC cleanup registration | reserved 0, emutls 1, GCC cleanup 0 | 2 and 3 |

Both cases create and delete the same reservation. The sole setup difference
is its deletion point. This can occur in normal application code: storage for a
C++ TLS object is obtained before its constructor runs, and that constructor
can delete an older application key before the compiler registers the object's
destructor. Deleting a valid key with no per-thread value is permitted; the
probe does so before cleanup begins, outside the runtime's destructor locks.

The reserved key is deleted exactly once. In `key-reuse`, the later GCC key
uses the same numeric slot, but the fixture never deletes that slot again.
Only the two separate public callback keys are deleted after worker completion.

`__wrap_pthread_key_create` forwards each call unchanged to the real runtime
and records the actual returned key in one of five setup phases. The driver
requires all five calls and phase bits, exact key identities, and the recorded
deletion/registration sequence. A runtime that allocates different keys fails
the observation; the gate does not silently relabel it as a successful reuse.

## Independent observations and safe callbacks

The fixture retains the existing non-destructive observation technique:

- The TLS object is trivially destructible. No member destructor is called on
  potentially freed storage.
- The explicit GCC callback and both public pthread callbacks receive live
  host-owned state, never the TLS object's address.
- The worker reads and validates the TLS value only during setup while the
  allocation is alive. Later observations compare integer addresses only.
- `__wrap_free` forwards every original release exactly once and records entry
  and return for the known real emutls allocation. It performs no allocation,
  I/O, TLS lookup, or freed-memory access.
- The key-creation observer similarly performs no key API call of its own,
  allocation, I/O, or TLS lookup beyond forwarding the original operation.
- The host retains its DLL reference and state through worker completion,
  deletion of the public keys, the global destructor, and verified unload.

Five exit events are measured: release entry/return, GCC cleanup callback, and
the two public callbacks. The lifetime order comes only from the actual release
and GCC callback events. Public ascending/descending traversal comes only from
the two public callback events. **One result is never inferred from the other.**
The public key 0 straddles the runtime keys in `normal`, so the earlier
storage-only probe's entire five-event sequence is intentionally not reused.

The source-predicted outcomes are:

| Private pthread family | Public traversal | `normal` lifetime | `key-reuse` lifetime |
| --- | --- | --- | --- |
| Original installed archive copies | Ascending | Storage before callback | Callback before storage |
| Unmodified v9 source rebuild | Ascending | Storage before callback | Callback before storage |
| Accepted reverse-order backport | Descending | Callback before storage | Storage before callback |

A successful diagnostic can therefore confirm an unsafe lifetime order in
the patched runtime. Every storage-before-callback case remains a failed
lifetime contract even when its host process exits normally.

## Runtime and completion gates

The workflow uses the same pinned actions, Windows image, Rtools42/GCC 10.4,
immutable runtime sources, ABI preparation, and three private pthread families
as the completed ordering experiment. `verify_pthread_build.py` is reused
byte-for-byte from the reviewed six-arm experiment helper. Its focused test
fixture also includes the Windows short-TEMP-path canonicalization from
`bf3256b770c8a5d7924a2eb97a0ce085ec258071`; production path and hash verification
are unchanged. There is no replacement provenance policy for this probe.

Every family runs both scenarios with pthread and foreign (`CreateThread`)
workers against archive GCC, exact baseline GCC source, and the unchanged
final-shutdown guard: 12 cases per family, **36 cases in total**. Each case uses
a fresh host process. Foreign workers and pthread workers must both reach a
signaled real native thread handle, including completion of native thread
detach. The host then checks both public callbacks, the GCC callback, one real
storage release, one global destructor, and actual DLL unload.

Before linking and after all cases, the driver rechecks every installed input
and all six private archives against the preparation manifest. It also records
the unchanged installed defaults separately from selected private archives.
Each DLL must prove its actual private archive/member provider and unique
strong pthread definitions, intended GCC provider, real compiler emutls
references, and a disassembled `emutls_destroy` branch to the wrapped free.
Flags and a declared library directory alone do not satisfy the link gate.

The Python driver continues across failed observations. The PowerShell
collector continues across nonzero family drivers. Each case is bounded at
30 seconds; the collector receives 25 minutes so even 36 timeouts can be
retained, with 45 minutes for the complete job. Before/after integrity reports,
sources, binaries, objects, maps, disassembly, raw observations, and summaries
are uploaded with 90-day retention, including failure runs.

The workflow triggers only on `test/windows-tls-key-reuse-20261107` and its job
requires `e-kotov/duckdb-spatial`. It has read-only repository permissions.

## Separate pre-existing re-registration limitation

This experiment prioritizes low-key reuse because the source predicts a case
that ascending traversal handles safely and descending traversal does not.
A second source-derived limitation must be kept separate:

1. MinGW clears the current GCC key value before invoking its callback.
2. GCC 10.4 `run(void *)` walks only the captured `elt::next` list. It does not
   reread the pthread key while draining that list.
3. If a running destructor initializes another TLS object and registers a
   new callback, GCC stores that callback in a fresh key list for a later
   pthread destructor round.
4. Descending traversal can release the entire emutls array in the current
   round before that new callback runs next round. Ascending traversal also
   has this problem for the later registration.

That is a pre-existing wider lifetime limitation left unresolved by reversal,
not evidence that reversal introduced it. The existing GCC shutdown guard
does not change either `run()` function or emutls release behavior. The
matched Spatial experiment remains useful even if this separate limitation
exists; its outcome must not be described as universal TLS lifetime safety.

A small future probe could use two trivial TLS allocations and live host
state: the first callback obtains the second allocation and registers a
second callback; the observer then records whether the second actual free
returns before the second callback. It must track allocation generations if
the allocator reuses the first address, and neither callback may inspect
freed storage. That follow-up is not implemented or run by this workflow.

## Source basis

- [Accepted MinGW change `8e06daa`](https://github.com/mingw-w64/mingw-w64/commit/8e06daa36dfcea4bb491acf4b350658f40738f02): signed descending loop index and reverse numeric key traversal.
- [Pinned MinGW v9 `thread.c`](https://github.com/mingw-w64/mingw-w64/blob/acc9b9d9eb63a13d8122cbac4882eb5f4ee2f679/mingw-w64-libraries/winpthreads/src/thread.c): key allocation/deletion search position and `_pthread_cleanup_dest`.
- [Pinned GCC 10.4 `emutls.c`](https://github.com/gcc-mirror/gcc/blob/7ff47281ce4f3699185b06a3430968eac2a5b0c6/libgcc/emutls.c): actual per-thread allocation and release.
- [Pinned GCC 10.4 `atexit_thread.cc`](https://github.com/gcc-mirror/gcc/blob/7ff47281ce4f3699185b06a3430968eac2a5b0c6/libstdc%2B%2B-v3/libsupc%2B%2B/atexit_thread.cc): cleanup registration, captured callback list, and key lifetime.

Focused gate checks can run locally:

```text
python -m unittest discover -s scripts/windows-shutdown -p test_tls_key_reuse.py -v
```

Those checks test observation rejection and classification. Actual key reuse,
storage release, callback execution, and unload require the Windows workflow.
