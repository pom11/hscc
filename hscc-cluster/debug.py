"""hscc-cluster read-only debug handlers."""
try:
    from . import clusterlib as cl  # package context (runtime)
except ImportError:
    import clusterlib as cl  # direct import context (tests)


def vllm_logs(args, **kwargs):
    node = args.get("node", cl.HEAD)
    # real serve logs live inside the container at /tmp/sparkrun_serve.log
    inner = cl.ssh_cmd(node,
        "docker ps --format '{{.Names}}' | grep -i sparkrun | head -1 | "
        "xargs -I{} docker exec {} sh -c 'tail -n 200 /tmp/sparkrun_serve.log' 2>/dev/null",
        timeout=25)
    docker = cl.ssh_cmd(node,
        "docker ps --format '{{.Names}}' | grep -i sparkrun | head -1 | "
        "xargs -I{} docker logs --tail 80 {} 2>&1", timeout=25)
    return {"node": node, "serve_log_tail": inner["stdout"][-4000:],
            "docker_log_tail": docker["stdout"][-2000:]}


def node_diagnostics(args, **kwargs):
    node = args.get("node", cl.HEAD)
    dmesg = cl.ssh_cmd(node, "sudo dmesg | tail -n 60", timeout=20)
    fd = cl.ssh_cmd(node, "cat /proc/sys/fs/file-nr", timeout=10)
    disk = cl.ssh_cmd(node, "df -h / /mnt/nas 2>/dev/null", timeout=10)
    docker = cl.ssh_cmd(node, "docker ps --format '{{.Names}} {{.Status}}'", timeout=10)
    gpu = cl.ssh_cmd(node, "nvidia-smi --query-gpu=temperature.gpu,utilization.gpu,"
                           "ecc.errors.uncorrected.aggregate.total "
                           "--format=csv,noheader 2>/dev/null", timeout=15)
    blob = dmesg["stdout"]
    return {"node": node,
            "oom_detected": "Out of memory" in blob or "oom-kill" in blob.lower(),
            "dmesg_tail": blob[-3000:], "file_nr": fd["stdout"].strip(),
            "disk": disk["stdout"], "docker": docker["stdout"], "gpu": gpu["stdout"]}


def nas_diagnose(args, **kwargs):
    node = args.get("node", cl.NODES[0])
    # fresh-mount probe: read a sentinel under /mnt/nas
    probe = cl.ssh_cmd(node, "ls /mnt/nas >/dev/null 2>&1 && echo probe-ok || echo probe-fail",
                       timeout=15)
    exports = cl.ssh_cmd(cl.NAS_HOST, "cat /etc/exports 2>/dev/null; exportfs -v 2>/dev/null",
                         timeout=15)
    err = (probe["stderr"] + probe["stdout"]).lower()
    if "stale" in err or probe["code"] == 32:
        verdict = "stale"
    elif "probe-ok" in probe["stdout"]:
        verdict = "healthy"
    else:
        verdict = "unreachable"
    return {"node": node, "verdict": verdict,
            "probe": probe["stdout"].strip(),
            "qnap_exports": exports["stdout"][-2000:]}
