#ifndef DUCKDB_SPATIAL_WINDOWS_SHUTDOWN_FIXTURE_H
#define DUCKDB_SPATIAL_WINDOWS_SHUTDOWN_FIXTURE_H

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>

#ifndef _WIN64
#error This diagnostic fixture is for the captured Windows x64 runtime.
#endif

/* Owned by the C host, so counters remain readable after FreeLibrary. */
typedef struct FixtureState {
    volatile LONG cpp_tls_dtors;
    volatile LONG global_dtors;
    volatile LONG atexit_calls;
    volatile LONG blocked_entered;
    volatile LONG blocked_left;
    volatile LONG worker_error;
    HANDLE log_file;
    HANDLE worker_start;
    HANDLE destructor_started;
    HANDLE destructor_release;
} FixtureState;

typedef int (*FixtureInitializeFn)(FixtureState *);
typedef int (*FixtureJoinedFn)(unsigned int);
typedef int (*FixtureTouchMainFn)(void);
typedef int (*FixtureBlockDestructorFn)(void);

#endif
