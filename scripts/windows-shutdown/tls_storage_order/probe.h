#ifndef WINDOWS_TLS_STORAGE_ORDER_PROBE_H
#define WINDOWS_TLS_STORAGE_ORDER_PROBE_H

#include <stdint.h>
#include <windows.h>

/* This state belongs to the C host. It remains alive through the native thread
 * wait, pthread join (where applicable), and final FreeLibrary. */
typedef struct StorageOrderState {
    DWORD structure_bytes;
    HANDLE native_worker_handle;
    volatile LONG worker_id;
    volatile LONG worker_error;
    uintptr_t object_address;
    uintptr_t allocation_base;
    uintptr_t observed_release_address;
    uint64_t low_key;
    uint64_t high_key;
    LONG storage_touches;
    LONG cleanup_registrations;
    LONG keys_created;
    LONG keys_deleted;
    volatile LONG sequence;
    volatile LONG release_calls;
    LONG release_enter_event;
    LONG release_leave_event;
    LONG callbacks_at_release;
    volatile LONG cleanup_callbacks;
    LONG cleanup_event;
    LONG completed_releases_at_callback;
    volatile LONG low_key_callbacks;
    LONG low_key_event;
    volatile LONG high_key_callbacks;
    LONG high_key_event;
    volatile LONG dll_destructors;
} StorageOrderState;

typedef int (*OrderInitializeFn)(StorageOrderState *);
typedef int (*OrderTouchFn)(void);
typedef int (*OrderRunPthreadFn)(void);
typedef int (*OrderDeleteKeysFn)(void);

#endif
