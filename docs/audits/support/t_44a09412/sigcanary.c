/*
 * sigcanary.c — calibrate the "is this signal blocked in the kernel mask" read.
 *
 * t_44a09412: the stalled process is /bin/sh (bash 3.2), which cannot be
 * attached by a debugger on this host and which RESETS its inherited signal
 * mask at startup (measured: a harness exec'd from a process that blocked
 * INT+QUIT+TSTP behaved exactly like a clean-mask harness). So the mask has to
 * be read behaviourally, and a behavioural read is only worth what its
 * calibration is worth: this tool is a target whose mask we control, so the
 * probe's two answers are pinned against known ground.
 *
 * usage:
 *   sigcanary <blocked-signum-list|none>   park, with SIGQUIT/SIGTTIN/... set
 *
 * The parked process prints its own state, then pauses. SIGQUIT and SIGTTIN are
 * the canaries because bash 3.2 has NEITHER a handler nor an ignore for them
 * (readable in sigprobe's `caught`/`ignored`), so their DEFAULT action
 * (terminate / stop) is externally observable: delivered => the mask let it
 * through, no effect => the mask held it.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <signal.h>
#include <unistd.h>

int
main(int argc, char **argv)
{
  sigset_t s;
  const char *list;
  char *dup, *tok;

  if (argc != 2) {
    fprintf(stderr, "usage: %s none | 3,21\n", argv[0]);
    return 2;
  }
  list = argv[1];
  sigemptyset(&s);
  if (strcmp(list, "none") != 0) {
    dup = strdup(list);
    for (tok = strtok(dup, ","); tok; tok = strtok(NULL, ","))
      sigaddset(&s, (int)strtol(tok, NULL, 10));
    if (sigprocmask(SIG_BLOCK, &s, NULL) != 0) { perror("sigprocmask"); return 1; }
  }
  printf("pid=%d blocked=%s\n", (int)getpid(), list);
  fflush(stdout);
  pause();
  return 0;
}
