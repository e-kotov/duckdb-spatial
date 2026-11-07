/* Diagnostic ABI check only; this program neither creates nor modifies keys. */
#include <windows.h>
#include <pthread.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>
#include "thread.h"

int main(void) {
    _pthread_v value;
    size_t index, changed = 0, bitfield_offset = 0;
    unsigned int bitfield_mask = 0;
    const unsigned char *bytes = (const unsigned char *)&value;
    memset(&value, 0, sizeof(value));
    value.thread_noposix = 1;
    for (index = 0; index < sizeof(value); ++index) {
        if (bytes[index] != 0) {
            ++changed;
            bitfield_offset = index;
            bitfield_mask = bytes[index];
        }
    }
    printf("{\"pointer_size\":%llu,\"pthread_t_size\":%llu,"
           "\"pthread_key_t_size\":%llu,\"pthread_mutex_t_size\":%llu,"
           "\"pthread_spinlock_t_size\":%llu,\"pthread_rwlock_t_size\":%llu,"
           "\"DWORD_size\":%llu,\"LONG_size\":%llu,"
           "\"thread_size\":%llu,\"thread_h\":%llu,\"thread_keymax\":%llu,"
           "\"thread_keyval\":%llu,\"thread_keyval_set\":%llu,"
           "\"thread_spin_keys\":%llu,\"thread_tid\":%llu,"
           "\"thread_rwlc\":%llu,\"thread_ended\":%llu,"
           "\"foreign_bitfield_changed_bytes\":%llu,"
           "\"foreign_bitfield_offset\":%llu,\"foreign_bitfield_mask\":%u,"
           "\"rwlock_size\":%llu,\"rwlock_valid\":%llu,\"rwlock_busy\":%llu,"
           "\"rwlock_nex_count\":%llu,\"rwlock_nsh_count\":%llu,"
           "\"rwlock_ncomplete\":%llu,\"rwlock_mex\":%llu,"
           "\"rwlock_mcomplete\":%llu,\"rwlock_ccomplete\":%llu,"
           "\"destructor_iterations\":%u}\n",
           (unsigned long long)sizeof(void *),
           (unsigned long long)sizeof(pthread_t),
           (unsigned long long)sizeof(pthread_key_t),
           (unsigned long long)sizeof(pthread_mutex_t),
           (unsigned long long)sizeof(pthread_spinlock_t),
           (unsigned long long)sizeof(pthread_rwlock_t),
           (unsigned long long)sizeof(DWORD),
           (unsigned long long)sizeof(LONG),
           (unsigned long long)sizeof(_pthread_v),
           (unsigned long long)offsetof(_pthread_v, h),
           (unsigned long long)offsetof(_pthread_v, keymax),
           (unsigned long long)offsetof(_pthread_v, keyval),
           (unsigned long long)offsetof(_pthread_v, keyval_set),
           (unsigned long long)offsetof(_pthread_v, spin_keys),
           (unsigned long long)offsetof(_pthread_v, tid),
           (unsigned long long)offsetof(_pthread_v, rwlc),
           (unsigned long long)offsetof(_pthread_v, ended),
           (unsigned long long)changed,
           (unsigned long long)bitfield_offset, bitfield_mask,
           (unsigned long long)sizeof(rwlock_t),
           (unsigned long long)offsetof(rwlock_t, valid),
           (unsigned long long)offsetof(rwlock_t, busy),
           (unsigned long long)offsetof(rwlock_t, nex_count),
           (unsigned long long)offsetof(rwlock_t, nsh_count),
           (unsigned long long)offsetof(rwlock_t, ncomplete),
           (unsigned long long)offsetof(rwlock_t, mex),
           (unsigned long long)offsetof(rwlock_t, mcomplete),
           (unsigned long long)offsetof(rwlock_t, ccomplete),
           (unsigned int)PTHREAD_DESTRUCTOR_ITERATIONS);
    return 0;
}
