import json, sys

rows = json.load(open(sys.argv[1]))
PROBE = {"probe_alpha", "probe_beta", "probe_gamma"}
by = {}
for r in rows:
    by.setdefault((r["pattern"], r["module"], r["handler"]), {})[r["mode"]] = r

splat, enu, param = [], [], []
for (pat, mod, h), m in sorted(by.items()):
    r, s = m["real"], m["stub"]
    rok = isinstance(r["keys"], list)
    sok = isinstance(s["keys"], list)
    leaks = sok and bool(PROBE & set(r_ for r_ in s["keys"]))
    if leaks:
        splat.append((pat, h, s["keys"], r["keys"] if rok else r["status"]))
    elif rok:
        enu.append((pat, h, r["keys"]))
    else:
        param.append((pat, h, r["status"]))

print("EMPIRICAL SPLIT (from the harness, not AST): splat=%d enumerated=%d needs-param=%d"
      % (len(splat), len(enu), len(param)))
print("\n== splat (stub leaked its own probe keys => seam measures the stub) ==")
for pat, h, sk, rk in splat:
    print(f"  {pat:50} {h}")
    print(f"      stub keys: {sk}")
print("\n== enumerated (real call answered; key set is the handler's own) ==")
for pat, h, k in enu:
    print(f"  {pat:50} {h}")
    print(f"      {k}")
print("\n== needs a required parameter before it answers at all ==")
for pat, h, st in param:
    print(f"  {pat:50} {h}")
    print(f"      {st}")
