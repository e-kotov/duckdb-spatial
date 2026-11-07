#include "probe.h"

#include <cxxabi.h>
#include <pthread.h>
#include <type_traits>

namespace {
StorageOrderState *state = nullptr;
pthread_key_t low_key;
pthread_key_t high_key;

/* No destructor is registered for this object. Calling a member destructor
 * after its storage was freed would itself be invalid, even if the destructor
 * did not read a member. The explicit GCC callback below instead receives the
 * live host state and never receives this object's address. */
struct alignas(8) Storage {
    uintptr_t value;
};
static_assert(sizeof(void *) == 8, "The allocation-layout check requires Windows x64");
static_assert(sizeof(Storage) == 8 && alignof(Storage) == 8, "Expected one aligned word");
static_assert(std::is_trivially_destructible<Storage>::value, "No implicit destructor allowed");
thread_local Storage storage;

LONG Event(StorageOrderState *context) {
    return InterlockedIncrement(&context->sequence);
}

void Error(StorageOrderState *context) {
    InterlockedExchange(&context->worker_error, 1);
}

bool OnWorker(StorageOrderState *context) {
    return (DWORD)InterlockedCompareExchange(&context->worker_id, 0, 0) == GetCurrentThreadId();
}

void CleanupCallback(void *argument) {
    StorageOrderState *context = static_cast<StorageOrderState *>(argument);
    if (context != state || !OnWorker(context)) {
        Error(state);
        return;
    }
    if (InterlockedIncrement(&context->cleanup_callbacks) != 1) {
        Error(context);
    }
    context->completed_releases_at_callback = context->release_leave_event != 0;
    context->cleanup_event = Event(context);
}

void LowKeyCallback(void *argument) {
    StorageOrderState *context = static_cast<StorageOrderState *>(argument);
    if (context != state || !OnWorker(context)) {
        Error(state);
        return;
    }
    if (InterlockedIncrement(&context->low_key_callbacks) != 1) {
        Error(context);
    }
    context->low_key_event = Event(context);
}

void HighKeyCallback(void *argument) {
    StorageOrderState *context = static_cast<StorageOrderState *>(argument);
    if (context != state || !OnWorker(context)) {
        Error(state);
        return;
    }
    if (InterlockedIncrement(&context->high_key_callbacks) != 1) {
        Error(context);
    }
    context->high_key_event = Event(context);
}

struct UnloadSentinel {
    ~UnloadSentinel() {
        if (state) {
            InterlockedIncrement(&state->dll_destructors);
        }
    }
} unload_sentinel;
} // namespace

extern "C" void __real_free(void *);

/* GNU ld redirects unresolved free references in the static libgcc emutls
 * object here. The runner must also prove that call in the linked disassembly.
 * The original free always executes once. No allocation, CRT I/O, TLS access,
 * pointer dereference, or deliberate delay is introduced by this observer. */
extern "C" void __wrap_free(void *allocation) {
    bool matched = false;
    if (state && OnWorker(state) && state->allocation_base != 0 &&
        reinterpret_cast<uintptr_t>(allocation) == state->allocation_base) {
        matched = true;
        if (InterlockedIncrement(&state->release_calls) != 1) {
            Error(state);
        }
        state->observed_release_address = reinterpret_cast<uintptr_t>(allocation);
        state->callbacks_at_release = state->cleanup_callbacks;
        state->release_enter_event = Event(state);
    }
    __real_free(allocation);
    if (matched) {
        /* Record completion after free returned. Do not inspect allocation. */
        state->release_leave_event = Event(state);
    }
}

extern "C" __declspec(dllexport) int OrderInitialize(StorageOrderState *context) {
    if (state || !context || context->structure_bytes != sizeof(StorageOrderState)) {
        return 0;
    }
    state = context;
    return pthread_self() != 0;
}

extern "C" __declspec(dllexport) int OrderTouch() {
    if (!state || state->storage_touches != 0) {
        return 0;
    }
    InterlockedExchange(&state->worker_id, (LONG)GetCurrentThreadId());
    if (!DuplicateHandle(GetCurrentProcess(), GetCurrentThread(), GetCurrentProcess(),
                         &state->native_worker_handle, 0, FALSE, DUPLICATE_SAME_ACCESS)) {
        Error(state);
        return 0;
    }

    Storage *object = &storage;
    object->value = 0x51704f5244455255ULL;
    state->object_address = reinterpret_cast<uintptr_t>(object);
    /* GCC 10.4 libgcc/emutls.c, align <= sizeof(void *): allocates size+8 and
     * returns base+8. Derive the base as an integer while storage is alive;
     * never read the hidden header or do out-of-object pointer arithmetic. */
    state->allocation_base = state->object_address - sizeof(void *);
    state->storage_touches = 1;
    if (reinterpret_cast<uintptr_t>(&storage) != state->object_address ||
        storage.value != 0x51704f5244455255ULL ||
        state->object_address % alignof(Storage) != 0) {
        Error(state);
        return 0;
    }
    state->storage_touches = 2;

    /* Obtain the actual TLS allocation before registering with GCC, matching
     * the compiler-generated emutls-first, cleanup-key-second sequence. */
    if (__cxxabiv1::__cxa_thread_atexit(CleanupCallback, state, nullptr) != 0) {
        Error(state);
        return 0;
    }
    state->cleanup_registrations = 1;

    /* Public-key order is an independent traversal control. Allocate these
     * after both runtime keys, so they do not alter that initialization order. */
    if (pthread_key_create(&low_key, LowKeyCallback) != 0) {
        Error(state);
        return 0;
    }
    state->keys_created = 1;
    state->low_key = static_cast<uint64_t>(low_key);
    if (pthread_key_create(&high_key, HighKeyCallback) != 0) {
        Error(state);
        return 0;
    }
    state->keys_created = 2;
    state->high_key = static_cast<uint64_t>(high_key);
    if (state->low_key >= state->high_key ||
        pthread_setspecific(low_key, state) != 0 || pthread_setspecific(high_key, state) != 0) {
        Error(state);
        return 0;
    }
    return 1;
}

namespace {
void *PthreadWorker(void *) {
    return OrderTouch() ? nullptr : reinterpret_cast<void *>(static_cast<uintptr_t>(1));
}
} // namespace

extern "C" __declspec(dllexport) int OrderRunPthread() {
    pthread_t worker;
    void *result = nullptr;
    if (pthread_create(&worker, nullptr, PthreadWorker, nullptr) != 0) {
        return 0;
    }
    return pthread_join(worker, &result) == 0 && result == nullptr;
}

extern "C" __declspec(dllexport) int OrderDeleteKeys() {
    if (!state || state->keys_created != 2 || state->keys_deleted != 0 ||
        pthread_key_delete(low_key) != 0 || pthread_key_delete(high_key) != 0) {
        return 0;
    }
    state->keys_deleted = 2;
    return 1;
}
