"""Strip seeded Telegram credentials from non-default Hermes role profiles.

On Hermes 0.19+ the multiplex gateway refuses to poll the same bot token from
multiple profiles. Role profiles must NOT hold the Telegram bot credential —
only the `default` profile (the chat front-end) should.

This script idempotently comments out every active (uncommented) `TELEGRAM_*`
variable in the .env of each non-default profile. Lines already commented are
left untouched. Never deletes variables — comments them (reversible).

Best-effort: a missing profiles_dir or an unreadable/unwritable .env is skipped,
not fatal.
"""

import json
import os
import re


# Active (uncommented) TELEGRAM_* assignment.
#  Group 1 = leading whitespace, group 2 = the var assignment prefix.
_ACTIVE_RE = re.compile(r"^(\s*)(TELEGRAM[A-Z_]*\s*=)")


def strip_worker_telegram(profiles_dir=None):
    """Comment out TELEGRAM_* vars in every non-default role profile .env.

    Args:
        profiles_dir: path to ~/.hermes/profiles (default: auto-resolved).

    Returns:
        dict with keys:
            stripped  — list of profile names whose .env was modified.
            scanned   — total number of role profiles scanned.
            skipped_missing — True when profiles_dir does not exist.
    """
    if profiles_dir is None:
        profiles_dir = os.path.expanduser("~/.hermes/profiles")

    if not os.path.isdir(profiles_dir):
        return {"stripped": [], "skipped_missing": True}

    stripped = []
    scanned = 0

    try:
        entries = sorted(os.listdir(profiles_dir))
    except OSError:
        return {"stripped": [], "skipped_missing": True}

    for name in entries:
        if name == "default":
            continue

        profile_path = os.path.join(profiles_dir, name)
        if not os.path.isdir(profile_path):
            continue

        env_path = os.path.join(profile_path, ".env")
        if not os.path.isfile(env_path):
            continue

        scanned += 1

        try:
            with open(env_path, "r") as f:
                lines = f.readlines()
        except OSError:
            continue

        new_lines = []
        changed = False
        for line in lines:
            m = _ACTIVE_RE.match(line)
            if m:
                # Uncommented TELEGRAM_* assignment — comment it out.
                new_lines.append("# " + line)
                changed = True
            else:
                new_lines.append(line)

        if changed:
            try:
                with open(env_path, "w") as f:
                    f.writelines(new_lines)
                stripped.append(name)
            except OSError:
                pass

    return {"stripped": stripped, "scanned": scanned}


if __name__ == "__main__":
    result = strip_worker_telegram()
    print(json.dumps(result))
