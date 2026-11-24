#include <malloc.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <pthread.h>
/* CEF ReportMallinfoStats 와 같은 계산: int arena + int hblkhd */
static void *worker(void *arg) {
    size_t n = (size_t)arg; char **p = malloc(n * sizeof(char*));
    for (size_t i = 0; i < n; i++) { size_t sz = (i % 3 == 0) ? 8u<<20 : (i % 3 == 1) ? 256u<<10 : 4096; p[i] = malloc(sz); memset(p[i], 1, sz); }
    for (size_t i = 0; i < n; i += 2) free(p[i]);  /* 조각화 */
    return p;
}
int main(void) {
    pthread_t t[8];
    for (int i = 0; i < 8; i++) pthread_create(&t[i], 0, worker, (void*)(size_t)700);
    for (int i = 0; i < 8; i++) pthread_join(t[i], 0);
    char *big[60]; for (int i = 0; i < 60; i++) { big[i] = malloc(16u<<20); memset(big[i], 2, 16u<<20); }
    struct mallinfo mi = mallinfo();
    struct mallinfo2 m2 = mallinfo2();
    int sum = mi.arena + mi.hblkhd;
    printf("mallinfo  arena=%d hblkhd=%d  int sum=%d  -> %s\n", mi.arena, mi.hblkhd, sum, sum < 0 ? "OVERFLOW (CEF would crash)" : "ok");
    printf("mallinfo2 arena=%zu hblkhd=%zu (true total %.0f MB)\n", m2.arena, m2.hblkhd, (m2.arena + m2.hblkhd) / 1048576.0);
    FILE *f = fopen("/proc/self/status", "r"); char l[256]; while (fgets(l, sizeof l, f)) if (!strncmp(l, "VmRSS", 5)) printf("%s", l);
    return 0;
}
