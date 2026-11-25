# Must NOT trip: the documented placeholder (t_3480956c)

The scrub rewrites a real home path to the documented placeholder. `<` and `>` are
not path-name characters, so the guard cannot match any line below — which is the
whole reason the replacement is spelled this way and not `$HOME` or `<path>`.

    gateway socket: /Users/<operator>/.hermes/gateway.sock
    suite home:     /home/<operator>/.hscc/suite
    interpreter:    "/Users/<operator>/.hermes/hermes-agent/venv/bin/python"
    json:           {"home": "/home/<operator>/work"}

Tilde-relative and system paths never named an operator and are out of scope:

    ~/.hermes/gateway.sock
    /usr/local/bin/python3
    /private/var/folders/9x/T/pytest-of-someone
    file:///Users/<operator>/index.html
