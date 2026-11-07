#include "fixture.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int fail(const char *message) {
    fprintf(stderr, "FIXTURE FAILURE: %s (Win32 error %lu)\n", message, GetLastError());
    fflush(stderr);
    /* A failed fixture setup must remain a nonzero exit, even if a worker was
     * already blocked. This is never used to make a candidate test pass. */
    TerminateProcess(GetCurrentProcess(), 90);
    return 90;
}

static void marker(FixtureState *state, const char *text) {
    DWORD written = 0;
    DWORD length = (DWORD)strlen(text);
    if (!WriteFile(state->log_file, text, length, &written, NULL) || written != length) {
        fail("write marker");
    }
}

typedef struct ForeignWorkerContext {
    FixtureTouchMainFn touch_tls;
} ForeignWorkerContext;

static DWORD WINAPI foreign_worker(LPVOID argument) {
    ForeignWorkerContext *context = (ForeignWorkerContext *)argument;
    /* This export touches the calling thread's TLS, despite its name. Two
     * touches must still register exactly one destructor for this worker. */
    return context->touch_tls() && context->touch_tls() ? 0 : 1;
}

static const char *run_foreign_workers(ForeignWorkerContext *context, unsigned int count) {
    HANDLE threads[128];
    unsigned int index;

    /* Creating the workers in the C host bypasses the DLL's pthread_create
     * wrapper. Its runtime therefore cleans up their TLS through the foreign
     * thread DLL_THREAD_DETACH path. Keep the module and context alive until
     * every native thread handle has signaled, including its TLS callbacks. */
    for (index = 0; index < count; ++index) {
        threads[index] = CreateThread(NULL, 0, foreign_worker, context, 0, NULL);
        if (!threads[index]) {
            return "CreateThread foreign worker";
        }
    }
    for (index = 0; index < count; ++index) {
        DWORD exit_code = STILL_ACTIVE;
        if (WaitForSingleObject(threads[index], 15000) != WAIT_OBJECT_0) {
            return "wait for foreign worker and TLS cleanup";
        }
        if (!GetExitCodeThread(threads[index], &exit_code) || exit_code != 0) {
            return "foreign worker exit status";
        }
        if (!CloseHandle(threads[index])) {
            return "close foreign worker handle";
        }
    }
    return NULL;
}

static HMODULE load_fixture(const char *path, FixtureState *state,
                            FixtureJoinedFn *joined, FixtureTouchMainFn *touch_main,
                            FixtureBlockDestructorFn *block_destructor) {
    HMODULE module = LoadLibraryA(path);
    FixtureInitializeFn initialize;
    if (!module) {
        fail("LoadLibrary");
    }
    initialize = (FixtureInitializeFn)GetProcAddress(module, "FixtureInitialize");
    *joined = (FixtureJoinedFn)GetProcAddress(module, "FixtureJoined");
    *touch_main = (FixtureTouchMainFn)GetProcAddress(module, "FixtureTouchMain");
    *block_destructor = (FixtureBlockDestructorFn)GetProcAddress(module, "FixtureBlockDestructor");
    if (!initialize || !*joined || !*touch_main || !*block_destructor || !initialize(state)) {
        fail("fixture exports or initialization");
    }
    return module;
}

int main(int argc, char **argv) {
    FixtureState state = {0};
    FixtureJoinedFn joined = NULL;
    FixtureTouchMainFn touch_main = NULL;
    FixtureBlockDestructorFn block_destructor = NULL;
    HMODULE module;
    unsigned int cycles = 16;
    unsigned int workers = 8;
    unsigned int cycle;
    int foreign_unload;

    if (argc < 4 || argc > 6) {
        fprintf(stderr, "usage: host DLL {unload|foreign-unload|main-exit|abandoned-reader} LOG [CYCLES WORKERS]\n");
        return 2;
    }
    if (argc > 4) {
        cycles = (unsigned int)strtoul(argv[4], NULL, 10);
    }
    if (argc > 5) {
        workers = (unsigned int)strtoul(argv[5], NULL, 10);
    }
    if (!cycles || cycles > 64 || !workers || workers > 128) {
        return fail("invalid workload size");
    }
    state.log_file = CreateFileA(argv[3], GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE,
                                 NULL, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    state.worker_start = CreateEventA(NULL, TRUE, FALSE, NULL);
    state.destructor_started = CreateEventA(NULL, TRUE, FALSE, NULL);
    state.destructor_release = CreateEventA(NULL, TRUE, FALSE, NULL);
    if (state.log_file == INVALID_HANDLE_VALUE || !state.worker_start ||
        !state.destructor_started || !state.destructor_release) {
        return fail("initialize fixture handles");
    }

    foreign_unload = strcmp(argv[2], "foreign-unload") == 0;
    if (strcmp(argv[2], "unload") == 0 || foreign_unload) {
        for (cycle = 0; cycle < cycles; ++cycle) {
            HMODULE remaining = NULL;
            LONG expected_tls = (LONG)((cycle + 1) * workers);
            module = load_fixture(argv[1], &state, &joined, &touch_main, &block_destructor);
            if (foreign_unload) {
                ForeignWorkerContext context = {touch_main};
                const char *error = run_foreign_workers(&context, workers);
                if (error) {
                    return fail(error);
                }
            } else if (!joined(workers)) {
                return fail("joined worker execution");
            }
            if (state.cpp_tls_dtors != expected_tls) {
                return fail("joined worker TLS destructor count");
            }
            if (state.worker_error || state.global_dtors != (LONG)cycle ||
                state.atexit_calls != (LONG)cycle) {
                return fail("worker error or premature DLL cleanup");
            }
            if (foreign_unload) {
                marker(&state, "FOREIGN_WORKERS_JOINED\n");
            }
            if (!FreeLibrary(module)) {
                return fail("FreeLibrary");
            }
            if (state.global_dtors != (LONG)(cycle + 1) || state.atexit_calls != (LONG)(cycle + 1)) {
                return fail("FreeLibrary global or atexit callback count");
            }
            if (state.worker_error || state.cpp_tls_dtors != expected_tls) {
                return fail("worker error or additional TLS cleanup at unload");
            }
            if (GetModuleHandleExA(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS |
                                     GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                                 (LPCSTR)joined, &remaining)) {
                return fail("module remained loaded after FreeLibrary");
            }
        }
        if (state.worker_error) {
            return fail("worker error");
        }
        marker(&state, foreign_unload ? "FOREIGN_UNLOAD_OK\n" : "UNLOAD_OK\n");
        printf("%s cycles=%u workers=%u tls=%ld global=%ld atexit=%ld\n",
               foreign_unload ? "FOREIGN_UNLOAD_OK" : "UNLOAD_OK", cycles, workers,
               state.cpp_tls_dtors, state.global_dtors, state.atexit_calls);
        CloseHandle(state.destructor_release);
        CloseHandle(state.destructor_started);
        CloseHandle(state.worker_start);
        CloseHandle(state.log_file);
        return 0;
    }

    module = load_fixture(argv[1], &state, &joined, &touch_main, &block_destructor);
    (void)module;
    if (strcmp(argv[2], "main-exit") == 0) {
        if (!touch_main() || state.cpp_tls_dtors != 0) {
            return fail("main TLS registration");
        }
    } else if (strcmp(argv[2], "abandoned-reader") == 0) {
        if (!block_destructor() ||
            WaitForSingleObject(state.destructor_started, 15000) != WAIT_OBJECT_0 ||
            state.worker_error || state.blocked_entered != 1 || state.blocked_left != 0) {
            return fail("blocked destructor setup");
        }
    } else {
        return fail("unknown test mode");
    }

    marker(&state, "PROCESS_EXIT_REQUEST\n");
    if (!FlushFileBuffers(state.log_file)) {
        return fail("flush exit marker");
    }
    /* Keep state and the log handle valid until DLL cleanup has completed. */
    ExitProcess(0);
    return 91;
}
