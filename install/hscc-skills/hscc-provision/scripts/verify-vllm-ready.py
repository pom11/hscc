#!/usr/bin/env python3
"""Verify vLLM readiness across cluster nodes before assigning tasks."""
import json, os, subprocess, sys, time, urllib.request

def check_host(ip, model, timeout=30):
    """Check if vLLM is serving on a host. Returns (ok, content_sample)."""
    try:
        payload = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": "say hi"}],
            "max_tokens": 5
        }).encode()
        req = urllib.request.Request(
            f"http://{ip}:8000/v1/chat/completions",
            data=payload,
            headers={"Content-Type": "application/json"}
        )
        resp = urllib.request.urlopen(req, timeout=timeout)
        data = json.loads(resp.read())
        msg = data.get("choices", [{}])[0].get("message", {})
        content = msg.get("content", "")
        reasoning = msg.get("reasoning")
        # Bug check: content is null but reasoning has text
        if not content and reasoning:
            return False, f"BUG: content=null, reasoning={reasoning[:50]}"
        return True, content[:50].strip()
    except Exception as e:
        return False, str(e)

if __name__ == "__main__":
    hosts = sys.argv[1:]  # e.g. 192.0.2.11 192.0.2.12
    if not hosts:
        print("Usage: verify-vllm-ready.py <ip1> [ip2] ...")
        sys.exit(1)
    
    model = "Qwen/Qwen3.6-35B-A3B-FP8"
    print(f"Checking vLLM readiness for {model}...")
    print()
    
    results = []
    for ip in hosts:
        print(f"  {ip} ... ", end="", flush=True)
        ok, sample = check_host(ip, model)
        status = f"OK: {sample}" if ok else f"FAIL: {sample}"
        print(status)
        results.append((ip, ok, sample))
    
    print()
    all_ok = all(r[1] for r in results)
    print(f"{'ALL READY' if all_ok else 'SOME FAILED'}")
    sys.exit(0 if all_ok else 1)
