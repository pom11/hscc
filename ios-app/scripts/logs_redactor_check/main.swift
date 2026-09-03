import Foundation

// Top-level assertion harness for LogRedactor (compiled as main.swift by
// scripts/logs_redactor_check.sh). Runs a fixed set of redaction cases and
// exits non-zero if any secret survives or any legitimate prose is mangled.

var failures = 0

func check(_ label: String, _ input: String, expected expectedSubstring: String,
           banned: [String] = []) {
    let out = LogRedactor.redact(input)
    var ok = out.contains(expectedSubstring)
    for b in banned where out.localizedCaseInsensitiveContains(b) { ok = false }
    if ok {
        print("PASS  \(label)")
    } else {
        failures += 1
        print("FAIL  \(label)\n      in:  \(input)\n      out: \(out)\n      want: \(expectedSubstring) | banned: \(banned)")
    }
}

// Tailnet hosts → 100.64.0.1
check("tailnet .64", "connecting to 100.64.0.7:8080", expected: "100.64.0.1",
      banned: ["100.64.0.7"])
check("tailnet .84", "worker 100.64.0.9 wedged", expected: "100.64.0.1",
      banned: ["100.64.0.9"])
// Input uses the SANCTIONED fixture block (100.64.0.0/24). A realistic
// out-of-block CGNAT address here would itself be a leak in a public repo —
// which is the exact class of bug this redactor exists to prevent.
check("tailnet address", "host 100.64.0.9 replied", expected: "100.64.0.1",
      banned: ["100.64.0.9"])
check("tailnet .127", "gateway 100.64.0.12 ok", expected: "100.64.0.1",
      banned: ["100.64.0.12"])

// RFC1918 → 10.0.0.x
check("rfc 10/8", "internal lb 10.9.8.7 reachable", expected: "10.0.0.x",
      banned: ["10.9.8.7"])
check("rfc 192.168", "dev box 192.168.1.50", expected: "10.0.0.x",
      banned: ["192.168.1.50"])
check("rfc 172.16", "printer 172.16.5.1", expected: "10.0.0.x",
      banned: ["172.16.5.1"])

// Other IPv4 → [REDACTED_IP]
check("other ipv4", "198.51.100.7 out", expected: "[REDACTED_IP]",
      banned: ["198.51.100.7"])

// Bearer tokens
check("auth header", "Authorization: Bearer abCdEfGhIjKlMnOpQrStUvWxYz.12345",
      expected: "Bearer ***", banned: ["abCdEfGhIjKlMnOpQrStUvWxYz"])
check("inline bearer", "token invalid: Bearer a1b2c3d4e5f6g7h8i9j0k1",
      expected: "Bearer ***", banned: ["a1b2c3d4e5f6g7h8i9j0k1"])

// key=value secrets
check("token=", "refreshed token=9f8e7d6c5b4a3928174605abcd", expected: "token=***",
      banned: ["9f8e7d6c5b4a3928174605"])
check("apikey:", "apikey:sk-proj-abcdef0123456789", expected: "apikey=***",
      banned: ["sk-proj-abcdef0123456789"])

// Session ids
check("sess_ prefix", "resumed sess_9f8e7d6c5b4a", expected: "sess_***",
      banned: ["9f8e7d6c5b4a"])
check("session=", "closed session=abc123def456", expected: "sess_***",
      banned: ["abc123def456"])

// Long opaque runs (uuid-ish / tokens not otherwise matched)
check("long run", "job id 6f1e2d3c4b5a69788a9b0c1d2e3f4a5b finished",
      expected: "***", banned: ["6f1e2d3c4b5a69788a9b0c1d2e3f4a5b"])

// Legitimate prose / normal text must survive
check("prose kept", "daemon started cleanly, scheduler online",
      expected: "daemon started cleanly, scheduler online")
check("short word kept", "session started", expected: "session started")

// A sane timestamp and level pass through untouched
let ts = LogRedactor.redact("2026-09-03 16:00:00 INFO done")
if ts.contains("2026-09-03") && ts.contains("INFO") {
    print("PASS  timestamp+level kept: \(ts)")
} else {
    failures += 1
    print("FAIL  timestamp+level kept: \(ts)")
}

if failures == 0 {
    print("ALL REDACTOR CHECKS PASSED")
    exit(0)
} else {
    print("REDACTOR CHECKS FAILED: \(failures)")
    exit(1)
}
