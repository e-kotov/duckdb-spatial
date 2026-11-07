#include "fixture.h"

#include <cxxabi.h>
#include <cstdlib>
#include <pthread.h>
#include <thread>
#include <vector>

namespace {
FixtureState *state = nullptr;

void WriteMarker(const char *text, DWORD length) {
    DWORD written = 0;
    if (!state || !WriteFile(state->log_file, text, length, &written, nullptr) || written != length) {
        if (state) {
            InterlockedExchange(&state->worker_error, 1);
        }
    }
}

#define MARKER(text) WriteMarker(text "\n", sizeof(text "\n") - 1)

struct GlobalSentinel {
    ~GlobalSentinel() {
        if (state) {
            InterlockedIncrement(&state->global_dtors);
            MARKER("GLOBAL_DTOR");
        }
    }
} global_sentinel;

void UserAtExit() {
    InterlockedIncrement(&state->atexit_calls);
    MARKER("ATEXIT_CALLBACK");
}

struct CppTlsSentinel {
    ~CppTlsSentinel() {
        /* Deliberately independent of emutls object storage: this test checks
         * callback execution, not the separate old emutls ordering defect. */
        InterlockedIncrement(&state->cpp_tls_dtors);
        MARKER("CPP_TLS_DTOR");
    }
};

void TouchCppTls() {
    thread_local CppTlsSentinel sentinel;
    (void)&sentinel;
}

void BlockedDestructor(void *argument) {
    FixtureState *context = static_cast<FixtureState *>(argument);
    InterlockedIncrement(&context->blocked_entered);
    MARKER("BLOCKED_DTOR_ENTER");
    SetEvent(context->destructor_started);

    /* For the abandoned-reader test the C host calls ExitProcess while this
     * callback is running. GCC's fallback invokes us from winpthreads' worker
     * cleanup, outside the loader lock but inside _pthread_key_lock's read
     * section. ExitProcess terminates this worker before DLL static cleanup.
     * The event also permits failure cleanup without pretending this callback
     * completed during the expected-hang scenario. */
    if (WaitForSingleObject(context->destructor_release, INFINITE) != WAIT_OBJECT_0) {
        InterlockedExchange(&context->worker_error, 1);
    }
    InterlockedIncrement(&context->blocked_left);
    MARKER("BLOCKED_DTOR_LEAVE");
}
} // namespace

extern "C" __declspec(dllexport) int FixtureInitialize(FixtureState *context) {
    if (state || !context || context->log_file == INVALID_HANDLE_VALUE) {
        return 0;
    }
    state = context;

    /* The main thread must already own its pthread state before the worker
     * enters its blocked destructor. Otherwise run() could block allocating
     * that state at exit, obscuring the key-deletion defect under test. */
    if (!pthread_self()) {
        return 0;
    }
    return std::atexit(UserAtExit) == 0;
}

extern "C" __declspec(dllexport) int FixtureJoined(unsigned int count) {
    std::vector<std::thread> workers;
    try {
        workers.reserve(count);
        for (unsigned int index = 0; index < count; ++index) {
            workers.emplace_back(TouchCppTls);
        }
    } catch (...) {
        for (auto &worker : workers) {
            worker.join();
        }
        return 0;
    }
    for (auto &worker : workers) {
        worker.join();
    }
    return state->worker_error == 0;
}

extern "C" __declspec(dllexport) int FixtureTouchMain() {
    TouchCppTls();
    return state->worker_error == 0;
}

extern "C" __declspec(dllexport) int FixtureBlockDestructor() {
    try {
        std::thread worker([] {
            if (WaitForSingleObject(state->worker_start, INFINITE) != WAIT_OBJECT_0 ||
                __cxxabiv1::__cxa_thread_atexit(BlockedDestructor, state, nullptr) != 0) {
                InterlockedExchange(&state->worker_error, 1);
                MARKER("BLOCK_SETUP_ERROR");
                SetEvent(state->destructor_started);
            }
        });
        /* Complete detach before permitting cleanup to acquire winpthreads'
         * global lock. Otherwise detach itself could block before the host
         * reaches ExitProcess, which would test a different wait. */
        worker.detach();
        if (!SetEvent(state->worker_start)) {
            return 0;
        }
    } catch (...) {
        return 0;
    }
    return 1;
}
