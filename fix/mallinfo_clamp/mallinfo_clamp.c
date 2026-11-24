/*
 * mallinfo() clamp shim for CasparCG + CEF 142
 *
 * CEF (Chromium base/trace_event/malloc_dump_provider.cc ReportMallinfoStats)
 * calls legacy 32-bit mallinfo() and does checked_cast<size_t>(arena + hblkhd).
 * When the process has > 2 GiB of glibc malloc memory the int sum overflows,
 * the CHECK fails and the whole casparcg process dies (SIGILL in MemoryInfra).
 *
 * This library only overrides mallinfo(): it reads the 64-bit mallinfo2() and
 * clamps the values so that every field and arena+hblkhd stay within INT_MAX.
 * malloc/free are NOT touched.
 *
 * build: gcc -O2 -shared -fPIC -o libmallinfo_clamp.so mallinfo_clamp.c
 * use:   LD_PRELOAD=/path/libmallinfo_clamp.so
 */
#include <malloc.h>
#include <limits.h>

static int clamp(size_t v, size_t max) { return (int)(v > max ? max : v); }

struct mallinfo mallinfo(void)
{
    struct mallinfo2 m = mallinfo2();
    struct mallinfo r;
    const size_t half = INT_MAX / 2;
    /* arena + hblkhd must not overflow int */
    size_t arena = m.arena, hblkhd = m.hblkhd;
    if (arena + hblkhd > INT_MAX) {
        if (arena > half && hblkhd > half) { arena = half; hblkhd = half; }
        else if (arena > half)  arena  = INT_MAX - hblkhd;
        else                    hblkhd = INT_MAX - arena;
    }
    r.arena    = (int)arena;
    r.hblkhd   = (int)hblkhd;
    r.ordblks  = clamp(m.ordblks,  INT_MAX);
    r.smblks   = clamp(m.smblks,   INT_MAX);
    r.hblks    = clamp(m.hblks,    INT_MAX);
    r.usmblks  = clamp(m.usmblks,  INT_MAX);
    r.fsmblks  = clamp(m.fsmblks,  INT_MAX);
    r.uordblks = clamp(m.uordblks, INT_MAX);
    r.fordblks = clamp(m.fordblks, INT_MAX);
    r.keepcost = clamp(m.keepcost, INT_MAX);
    return r;
}
