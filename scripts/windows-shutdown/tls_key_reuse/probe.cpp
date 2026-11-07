#include "probe.h"

#include <cxxabi.h>
#include <pthread.h>
#include <type_traits>

namespace {
StorageOrderState *state = nullptr;
pthread_key_t low_key;
pthread_key_t high_key;
pthread_key_t reserved_key;

enum KeyCreatePhase {
    NoKeyCreate = 0, ReserveKey = 1, EmutlsKey = 2, CleanupKey = 3,
    PublicLowKey = 4, PublicHighKey = 5
};

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
extern "C" int __real_pthread_key_create(pthread_key_t *, void (*)(void *));

/* Forward every real key creation unchanged. The phase is set only around a
 * single setup operation on this worker; record the actual runtime key IDs,
 * rather than inferring allocation order from the public traversal callbacks.
 * No allocation, I/O, TLS access, or key API is used by this observer itself. */
extern "C" int __wrap_pthread_key_create(pthread_key_t *key, void (*destructor)(void *)) {
    int result = __real_pthread_key_create(key, destructor);
    if (!state || !OnWorker(state)) {
        return result;
    }
    LONG phase = InterlockedCompareExchange(&state->key_create_phase, 0, 0);
    if (result != 0 || !key || phase < ReserveKey || phase > PublicHighKey ||
        (phase == ReserveKey ? destructor != nullptr : destructor == nullptr)) {
        Error(state);
        return result;
    }
    LONG bit = 1L << phase;
    if (InterlockedOr(&state->key_create_phase_mask, bit) & bit) {
        Error(state);
        return result;
    }
    InterlockedIncrement(&state->key_create_calls);
    switch (phase) {
    case ReserveKey: state->reserved_key = static_cast<uint64_t>(*key); break;
    case EmutlsKey: state->emutls_key = static_cast<uint64_t>(*key); break;
    case CleanupKey: state->cleanup_key = static_cast<uint64_t>(*key); break;
    case PublicLowKey: state->low_key = static_cast<uint64_t>(*key); break;
    case PublicHighKey: state->high_key = static_cast<uint64_t>(*key); break;
    }
    return result;
}

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

    /* Reserve the first legal public key before the first emutls access. Both
     * scenarios make the same allocation; only its deletion timing differs.
     * A C++ TLS constructor can legally delete an earlier application key
     * between its emutls allocation and compiler-generated cleanup registration. */
    InterlockedExchange(&state->key_create_phase, ReserveKey);
    if (pthread_key_create(&reserved_key, nullptr) != 0 || reserved_key != 0) {
        Error(state);
        return 0;
    }
    InterlockedExchange(&state->key_create_phase, NoKeyCreate);
    state->reserved_key_created = 1;
    state->reserve_created_event = ++state->setup_sequence;

    InterlockedExchange(&state->key_create_phase, EmutlsKey);
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
    InterlockedExchange(&state->key_create_phase, NoKeyCreate);
    state->storage_ready_event = ++state->setup_sequence;

    if (state->reuse_reserved_key) {
        if (pthread_key_delete(reserved_key) != 0) {
            Error(state);
            return 0;
        }
        state->reserved_key_deleted = 1;
        state->reserve_deleted_event = ++state->setup_sequence;
    }

    /* The wrapping observer records GCC's actual chosen key. In the paired
     * control the reservation is still live; in key-reuse it is available. */
    InterlockedExchange(&state->key_create_phase, CleanupKey);
    if (__cxxabiv1::__cxa_thread_atexit(CleanupCallback, state, nullptr) != 0) {
        Error(state);
        return 0;
    }
    state->cleanup_registrations = 1;
    InterlockedExchange(&state->key_create_phase, NoKeyCreate);
    state->cleanup_registered_event = ++state->setup_sequence;

    if (!state->reuse_reserved_key) {
        if (pthread_key_delete(reserved_key) != 0) {
            Error(state);
            return 0;
        }
        state->reserved_key_deleted = 1;
        state->reserve_deleted_event = ++state->setup_sequence;
    }
    if (state->reserved_key != 0 || state->emutls_key != 1 ||
        (state->reuse_reserved_key ? state->cleanup_key != 0 : state->cleanup_key != 2)) {
        Error(state);
        return 0;
    }

    /* These callbacks measure traversal independently. In the control the
     * lower public key reuses slot 0, so their events need not surround the
     * runtime events in the same order as in the older storage-only probe. */
    InterlockedExchange(&state->key_create_phase, PublicLowKey);
    if (pthread_key_create(&low_key, LowKeyCallback) != 0) {
        Error(state);
        return 0;
    }
    state->keys_created = 1;
    InterlockedExchange(&state->key_create_phase, PublicHighKey);
    if (pthread_key_create(&high_key, HighKeyCallback) != 0) {
        Error(state);
        return 0;
    }
    state->keys_created = 2;
    InterlockedExchange(&state->key_create_phase, NoKeyCreate);
    if (state->low_key >= state->high_key ||
        state->key_create_calls != 5 || state->key_create_phase_mask != 62 ||
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
