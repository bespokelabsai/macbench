/* setuid-root launcher: starts the CAVE RESCUE service once (idempotent). Takes no input: the environment,
 * open files and resource limits of the caller are all reset, so the service never inherits them. */
#define _GNU_SOURCE
#include <unistd.h>
#include <stdlib.h>
#include <sched.h>
#include <stdio.h>
#include <sys/resource.h>
#include <sys/stat.h>
int main(void) {
    if (setgid(0) || setuid(0)) return 1;
    clearenv();
    setenv("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", 1);
    struct rlimit inf = {RLIM_INFINITY, RLIM_INFINITY}, nofile = {4096, 4096}, nocore = {0, 0};
    int big[] = {RLIMIT_FSIZE, RLIMIT_CPU, RLIMIT_AS, RLIMIT_DATA, RLIMIT_STACK, RLIMIT_NPROC};
    for (unsigned i = 0; i < sizeof big / sizeof big[0]; i++) setrlimit(big[i], &inf);
    setrlimit(RLIMIT_STACK, &(struct rlimit){8 << 20, RLIM_INFINITY});
    setrlimit(RLIMIT_NOFILE, &nofile); setrlimit(RLIMIT_CORE, &nocore);
    int more[] = {RLIMIT_SIGPENDING, RLIMIT_MEMLOCK, RLIMIT_MSGQUEUE, RLIMIT_RTPRIO, RLIMIT_LOCKS};
    for (unsigned i = 0; i < sizeof more / sizeof more[0]; i++) setrlimit(more[i], &inf);
    for (int fd = 3; fd < 65536; fd++) close(fd);
    /* no scheduling, CPU or OOM handicap inherited from the caller */
    struct sched_param sp = {0};
    sched_setscheduler(0, SCHED_OTHER, &sp);
    setpriority(PRIO_PROCESS, 0, 0);
    cpu_set_t all; CPU_ZERO(&all); for (int c = 0; c < CPU_SETSIZE; c++) CPU_SET(c, &all);
    sched_setaffinity(0, sizeof all, &all);
    FILE *f = fopen("/proc/self/oom_score_adj", "w"); if (f) { fputs("0", f); fclose(f); }
    umask(022);
    execl("/bin/bash", "bash", "/opt/caverescue/start.sh", (char *)0);
    return 1;
}
