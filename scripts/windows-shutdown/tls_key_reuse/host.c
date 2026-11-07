#include "probe.h"

#include <stdio.h>
#include <string.h>

static int fail(const char *message) {
    fprintf(stderr, "KEY REUSE PROBE FAILURE: %s (Win32 error %lu)\n", message, GetLastError());
    fflush(stderr);
    /* A setup failure must never be turned into a passing cleanup observation. */
    TerminateProcess(GetCurrentProcess(), 90);
    return 90;
}

typedef struct ForeignContext {
    OrderTouchFn touch;
} ForeignContext;

static DWORD WINAPI foreign_worker(LPVOID argument) {
    ForeignContext *context = (ForeignContext *)argument;
    return context->touch() ? 0 : 1;
}

int main(int argc, char **argv) {
    StorageOrderState state = {0};
    HMODULE module;
    HMODULE remaining = NULL;
    OrderInitializeFn initialize;
    OrderTouchFn touch;
    OrderRunPthreadFn run_pthread;
    OrderDeleteKeysFn delete_keys;
    DWORD worker_exit = STILL_ACTIVE;
    int is_foreign;

    if (argc != 4 || (strcmp(argv[2], "pthread") && strcmp(argv[2], "foreign")) ||
        (strcmp(argv[3], "normal") && strcmp(argv[3], "key-reuse"))) {
        fprintf(stderr, "usage: key-reuse-host DLL {pthread|foreign} {normal|key-reuse}\n");
        return 2;
    }
    is_foreign = strcmp(argv[2], "foreign") == 0;
    state.reuse_reserved_key = strcmp(argv[3], "key-reuse") == 0;
    state.structure_bytes = sizeof(state);
    module = LoadLibraryA(argv[1]);
    if (!module) {
        return fail("LoadLibrary");
    }
    initialize = (OrderInitializeFn)GetProcAddress(module, "OrderInitialize");
    touch = (OrderTouchFn)GetProcAddress(module, "OrderTouch");
    run_pthread = (OrderRunPthreadFn)GetProcAddress(module, "OrderRunPthread");
    delete_keys = (OrderDeleteKeysFn)GetProcAddress(module, "OrderDeleteKeys");
    if (!initialize || !touch || !run_pthread || !delete_keys || !initialize(&state)) {
        return fail("exports or initialization");
    }

    if (is_foreign) {
        ForeignContext context = {touch};
        HANDLE thread = CreateThread(NULL, 0, foreign_worker, &context, 0, NULL);
        if (!thread || WaitForSingleObject(thread, 15000) != WAIT_OBJECT_0 ||
            !GetExitCodeThread(thread, &worker_exit) || worker_exit != 0 || !CloseHandle(thread)) {
            return fail("foreign worker completion");
        }
    } else if (!run_pthread()) {
        return fail("pthread worker join");
    }

    /* In both modes, independently wait on the real native HANDLE. Its signal
     * includes DLL_THREAD_DETACH callbacks. Read counters only after this wait. */
    if (!state.native_worker_handle ||
        WaitForSingleObject(state.native_worker_handle, 0) != WAIT_OBJECT_0 ||
        GetThreadId(state.native_worker_handle) != (DWORD)state.worker_id ||
        !GetExitCodeThread(state.native_worker_handle, &worker_exit) || worker_exit != 0 ||
        !CloseHandle(state.native_worker_handle)) {
        return fail("native handle did not confirm worker termination");
    }
    if (state.worker_error || state.storage_touches != 2 || state.cleanup_registrations != 1 ||
        state.keys_created != 2 || state.release_calls != 1 || state.cleanup_callbacks != 1 ||
        state.low_key_callbacks != 1 || state.high_key_callbacks != 1 || state.sequence != 5 ||
        state.key_create_calls != 5 || state.key_create_phase_mask != 62 || state.key_create_phase != 0 ||
        state.reserved_key_created != 1 || state.reserved_key_deleted != 1 || state.setup_sequence != 4 ||
        state.reserve_created_event != 1 || state.storage_ready_event != 2 ||
        state.reserve_deleted_event != (state.reuse_reserved_key ? 3 : 4) ||
        state.cleanup_registered_event != (state.reuse_reserved_key ? 4 : 3) ||
        state.reserved_key != 0 || state.emutls_key != 1 ||
        state.cleanup_key != (state.reuse_reserved_key ? 0 : 2) ||
        state.low_key != (state.reuse_reserved_key ? 2 : 0) || state.high_key != 3 ||
        state.dll_destructors != 0 || state.allocation_base == 0 ||
        state.observed_release_address != state.allocation_base || !delete_keys()) {
        return fail("incomplete or invalid observation");
    }

    if (!FreeLibrary(module) || state.dll_destructors != 1 || state.worker_error ||
        state.keys_deleted != 2 || state.sequence != 5 || state.release_calls != 1 ||
        state.cleanup_callbacks != 1 || state.low_key_callbacks != 1 || state.high_key_callbacks != 1 ||
        GetModuleHandleExA(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS |
                              GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                          (LPCSTR)touch, &remaining)) {
        return fail("DLL cleanup or actual unload");
    }

    printf("{\"schema\":1,\"mode\":\"%s\",\"scenario\":\"%s\",\"worker_id\":%lu,"
           "\"native_wait_confirmed\":true,\"actual_unload_confirmed\":true,"
           "\"reserved_key\":%llu,\"emutls_key\":%llu,\"cleanup_key\":%llu,"
           "\"reserved_key_created\":%ld,\"reserved_key_deleted\":%ld,"
           "\"key_create_calls\":%ld,\"key_create_phase_mask\":%ld,\"key_create_phase\":%ld,"
           "\"setup_sequence\":%ld,\"reserve_created_event\":%ld,\"storage_ready_event\":%ld,"
           "\"reserve_deleted_event\":%ld,\"cleanup_registered_event\":%ld,"
           "\"worker_error\":%ld,\"storage_touches\":%ld,\"cleanup_registrations\":%ld,"
           "\"keys_created\":%ld,\"keys_deleted\":%ld,\"low_key\":%llu,\"high_key\":%llu,"
           "\"object_address\":\"0x%llx\",\"allocation_base\":\"0x%llx\","
           "\"observed_release_address\":\"0x%llx\",\"sequence\":%ld,\"release_calls\":%ld,"
           "\"release_enter_event\":%ld,\"release_leave_event\":%ld,\"callbacks_at_release\":%ld,"
           "\"cleanup_callbacks\":%ld,\"cleanup_event\":%ld,\"completed_releases_at_callback\":%ld,"
           "\"low_key_callbacks\":%ld,\"low_key_event\":%ld,"
           "\"high_key_callbacks\":%ld,\"high_key_event\":%ld,\"dll_destructors\":%ld}\n",
           argv[2], argv[3], (DWORD)state.worker_id,
           (unsigned long long)state.reserved_key, (unsigned long long)state.emutls_key,
           (unsigned long long)state.cleanup_key, state.reserved_key_created, state.reserved_key_deleted,
           state.key_create_calls, state.key_create_phase_mask, state.key_create_phase,
           state.setup_sequence, state.reserve_created_event, state.storage_ready_event,
           state.reserve_deleted_event, state.cleanup_registered_event,
           state.worker_error, state.storage_touches,
           state.cleanup_registrations, state.keys_created, state.keys_deleted,
           (unsigned long long)state.low_key, (unsigned long long)state.high_key,
           (unsigned long long)state.object_address, (unsigned long long)state.allocation_base,
           (unsigned long long)state.observed_release_address, state.sequence, state.release_calls,
           state.release_enter_event, state.release_leave_event, state.callbacks_at_release,
           state.cleanup_callbacks, state.cleanup_event, state.completed_releases_at_callback,
           state.low_key_callbacks, state.low_key_event, state.high_key_callbacks,
           state.high_key_event, state.dll_destructors);
    return 0;
}
