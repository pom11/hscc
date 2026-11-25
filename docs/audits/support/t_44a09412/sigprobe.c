/*
#include <sys/time.h>
 * sigprobe.c — read a live process's BSD signal state with no debugger.
 *
 * t_44a09412: lldb cannot attach to /bin/sh (Apple harden-platform-binary), so
 * the kernel's own view of the process is the only signal state reachable on
 * this host: sysctl KERN_PROC_PID returns struct kinfo_proc, whose kp_proc
 * carries p_sigmask (blocked set), p_sigignore, p_sigcatch and p_siglist.
 *
 * The SELFTEST mode is not a convenience: p_sigmask/p_siglist are marked
 * DEPRECATED in <sys/proc.h>, so every number this prints is meaningless until
 * it has been shown to track a KNOWN state. SELFTEST forks a child that puts
 * SIGINT in the blocked set, SIGTERM in the caught set and SIGHUP in the
 * ignored set, sleeps, and the parent reads it back from outside.
 *
 * usage: sigprobe <pid>            read one pid
 *        sigprobe --selftest       validate the readout against a known state
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <signal.h>
#include <unistd.h>
#include <sys/sysctl.h>
#include <sys/wait.h>
#include <sys/proc.h>

/* Signals 1..32 live in __bits[0]; bit index is (signum - 1). */
static int
bit_of(const sigset_t *s, int sig)
{
  const unsigned int *w = (const unsigned int *)s;
  return (sig >= 1 && sig <= 32) ? ((w[0] >> (sig - 1)) & 1U) : -1;
}

static void
show(const char *label, const sigset_t *s)
{
  const unsigned int *w = (const unsigned int *)s;
  printf("  %-10s bits0=0x%08x  INT=%d TERM=%d HUP=%d USR1=%d CHLD=%d CONT=%d\n",
      label, w[0],
      bit_of(s, SIGINT), bit_of(s, SIGTERM), bit_of(s, SIGHUP),
      bit_of(s, SIGUSR1), bit_of(s, SIGCHLD), bit_of(s, SIGCONT));
}

static int
read_pid(pid_t pid)
{
  int mib[4] = { CTL_KERN, KERN_PROC, KERN_PROC_PID, pid };
  struct kinfo_proc kp;
  size_t len = sizeof(kp);

  memset(&kp, 0, sizeof(kp));
  if (sysctl(mib, 4, &kp, &len, NULL, 0) != 0) {
    perror("sysctl KERN_PROC_PID");
    return 1;
  }
  if (len == 0) {
    printf("pid %d: not found\n", (int)pid);
    return 1;
  }
  printf("pid=%d ppid=%d pgid=%d stat=%d comm=%s\n",
      (int)kp.kp_proc.p_pid, (int)kp.kp_eproc.e_ppid,
      (int)kp.kp_eproc.e_pgid, kp.kp_proc.p_stat, kp.kp_proc.p_comm);
  show("blocked", &kp.kp_proc.p_sigmask);
  {
    /* p_siglist is the DEPRECATED legacy pending int; read its low 32 bits. */
    sigset_t pending;
    memset(&pending, 0, sizeof(pending));
    memcpy(&pending, &kp.kp_proc.p_siglist, sizeof(int));
    show("pending ", &pending);
  }
  show("ignored ", &kp.kp_proc.p_sigignore);
  show("caught  ", &kp.kp_proc.p_sigcatch);
  return 0;
}

static void
noop_handler(int sig)
{
  (void)sig;
}

/*
 * Validate the PENDING readout, which is the field the whole verdict rests on.
 *
 * Child blocks SIGINT and parks. The parent then sends it SIGINT: because the
 * signal is blocked, XNU must retain it in the process pending set, so
 * `pending` must read INT=1 and keep reading it on every sample. If it reads 0
 * with a signal demonstrably blocked-and-sent, p_siglist is as dead as
 * p_sigmask looked and no stalled-harness reading from this tool means anything.
 */
static int
selftest_pending(void)
{
  sigset_t s;
  pid_t kid;
  int status, i;

  kid = fork();
  if (kid < 0) { perror("fork"); return 1; }
  if (kid == 0) {
    sigemptyset(&s);
    sigaddset(&s, SIGINT);
    sigprocmask(SIG_BLOCK, &s, NULL);
    pause();
    _exit(0);
  }
  usleep(400 * 1000);
  printf("selftest-pending: child %d blocks SIGINT; sending it SIGINT now\n",
      (int)kid);
  if (kill(kid, SIGINT) != 0) { perror("kill"); }
  usleep(200 * 1000);
  for (i = 0; i < 3; i++) {
    printf("--- sample %d (blocked+sent: INT must read 1 in `pending`) ---\n",
        i);
    read_pid(kid);
    usleep(300 * 1000);
  }
  kill(kid, SIGKILL);
  waitpid(kid, &status, 0);
  printf("selftest-pending verdict: INT=1 in `pending` across all three "
      "samples == readout live; INT=0 == p_siglist is dead\n");
  return 0;
}

/* Child puts a known state in place and parks; parent reads it from outside. */
static int
selftest(void)
{
  sigset_t s;
  pid_t kid;
  int status, rc;

  kid = fork();
  if (kid < 0) { perror("fork"); return 1; }
  if (kid == 0) {
    sigemptyset(&s);
    sigaddset(&s, SIGINT);              /* expect blocked INT=1  */
    sigprocmask(SIG_BLOCK, &s, NULL);
    signal(SIGHUP, SIG_IGN);            /* expect ignored HUP=1  */
    signal(SIGTERM, SIG_IGN);           /* two ignored, to tell   */
    signal(SIGUSR1, noop_handler);      /* expect caught USR1=1  */
    pause();
    _exit(0);
  }
  usleep(400 * 1000);
  printf("selftest: child %d KNOWN state = blocked{INT} ignored{HUP,TERM} "
      "caught{USR1}\n", (int)kid);
  rc = read_pid(kid);
  kill(kid, SIGKILL);
  waitpid(kid, &status, 0);
  printf("selftest verdict: INT must read 1 in `blocked`, HUP/TERM 1 in "
      "`ignored`, USR1 1 in `caught`; anything else and this tool is blind\n");
  return rc;
}

/*
 * Positive control: build a process whose kernel mask PROVABLY blocks a signal,
 * by blocking it here and exec'ing the target (execve preserves the mask). The
 * probe's answer for a blocked mask is only meaningful if we can manufacture
 * one and watch the probe call it what it is.
 *
 *   sigprobe --blockexec <signum> -- <cmd> [args...]
 */
static int
blockexec(int argc, char **argv)
{
  /* argv[0]=="--blockexec", argv[1]==signum, argv[2]=="--", argv[3..]=cmd */
  sigset_t s;
  int sig = (int)strtol(argv[2], NULL, 10);
  int i;

  if (sig < 1 || sig > 31 || strcmp(argv[3], "--") != 0 || argc < 5) {
    fprintf(stderr, "usage: %s --blockexec <signum> -- <cmd> [args...]\n",
        argv[0]);
    return 2;
  }
  sigemptyset(&s);
  sigaddset(&s, sig);
  if (sigprocmask(SIG_BLOCK, &s, NULL) != 0) { perror("sigprocmask"); return 1; }
  fprintf(stderr, "blockexec: blocked SIG%d in pid %d, exec'ing %s\n",
      sig, (int)getpid(), argv[4]);
  fflush(stderr);
  execvp(argv[4], &argv[4]);
  perror("execvp");
  (void)i;
  return 1;
}

/*
 * Manufacture a known starting signal state, then exec the target — so the
 * child bash 3.2 inherits exactly the dispositions/mask we ask for.
 *
 *   sigprobe --prep u2,u3,u20,b2 -- <cmd> [args...]
 *     uN = reset SIG<N> to SIG_DFL   (a non-interactive launcher shell leaves
 *                                     INT/QUIT/TSTP SIG_IGN, and bash 3.2 then
 *                                     CANNOT install a trap for them, so a
 *                                     "stalled" child built that way is testing
 *                                     the wrong thing)
 *     bN = add SIG<N> to the blocked mask (survives execve)
 *
 * This is the positive control: the probe's reading of "blocked" is only worth
 * something once it has been shown to say that about a mask we blocked by hand.
 */
static int
prep(int argc, char **argv)
{
  /* argv[1]=="--prep", argv[2]=list, argv[3]=="--", argv[4..]=cmd */
  sigset_t block;
  char *list, *tok;
  int i;

  if (argc < 6 || strcmp(argv[3], "--") != 0) {
    fprintf(stderr, "usage: %s --prep u2,u3,b20 -- <cmd> [args...]\n", argv[0]);
    return 2;
  }
  sigemptyset(&block);
  list = argv[2];
  for (tok = strtok(list, ","); tok != NULL; tok = strtok(NULL, ",")) {
    int sig = (int)strtol(tok + 1, NULL, 10);
    if (sig < 1 || sig > 31) {
      fprintf(stderr, "prep: bad signal %s\n", tok);
      return 2;
    }
    if (tok[0] == 'u') {
      if (signal(sig, SIG_DFL) == SIG_ERR) { perror("signal"); return 1; }
      fprintf(stderr, "prep: SIG%d -> SIG_DFL\n", sig);
    } else if (tok[0] == 'b') {
      sigaddset(&block, sig);
      fprintf(stderr, "prep: SIG%d -> blocked\n", sig);
    } else {
      fprintf(stderr, "prep: token must start with u or b: %s\n", tok);
      return 2;
    }
  }
  if (!sigemptyset(&block) && sigismember(&block, 0)) { /* keep gcc happy */ }
  {
    sigset_t cur;
    sigemptyset(&cur);
    sigprocmask(SIG_BLOCK, &block, &cur);
    (void)cur;
  }
  fflush(stderr);
  execvp(argv[4], &argv[4]);
  perror("execvp");
  for (i = 0; i < 0; i++) { }
  return 1;
}

int
main(int argc, char **argv)
{
  if (argc >= 2 && strcmp(argv[1], "--prep") == 0)
    return prep(argc, argv);
  if (argc >= 2 && strcmp(argv[1], "--blockexec") == 0)
    return blockexec(argc, argv);
  if (argc == 2 && strcmp(argv[1], "--selftest") == 0)
    return selftest();
  if (argc == 2 && strcmp(argv[1], "--selftest-pending") == 0)
    return selftest_pending();
  /* --sample <pid> <interval_ms> <count>: one process covers a whole stall
   * window, so a stalled ~10 s harness is caught even though the read itself
   * costs microseconds. Timestamped so the samples can be lined up against the
   * harness's own trace markers. */
  if (argc == 5 && strcmp(argv[1], "--sample") == 0) {
    pid_t pid = (pid_t)strtol(argv[2], NULL, 10);
    long iv_ms = strtol(argv[3], NULL, 10);
    int n = (int)strtol(argv[4], NULL, 10), i;
    for (i = 0; i < n; i++) {
      struct timeval tv;
      gettimeofday(&tv, NULL);
      printf("=== t=%ld.%03ld\n", (long)tv.tv_sec, (long)(tv.tv_usec / 1000));
      read_pid(pid);
      fflush(stdout);
      usleep((useconds_t)(iv_ms * 1000));
    }
    return 0;
  }
  if (argc != 2) {
    fprintf(stderr,
        "usage: %s <pid> | --selftest | --selftest-pending | "
        "--sample <pid> <interval_ms> <count>\n", argv[0]);
    return 2;
  }
  return read_pid((pid_t)strtol(argv[1], NULL, 10));
}
