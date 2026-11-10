#!/usr/bin/env python3
"""Distributed client for Rotor-Cuda (stdlib only).

Loop:  ask server for a range  ->  run Rotor on it  ->  report result  ->  ask for the next range.

Rotor exit-code contract (see Main.cpp):
    0  = whole range scanned, nothing found   -> POST /api/client/complete
    10 = key found (written to the -o file)   -> POST /api/client/found
    other = crash / interrupted               -> NOT reported; the server keeps the range
                                                 and its lease simply expires.

This client ONLY talks to the server given by --server. No other host is contacted.
"""
import argparse
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

# ============================ 1. HTTP ======================================


class Api:
    def __init__(self, server, token):
        self.server = server.rstrip("/")
        self.token = token

    def post(self, path, body, timeout=30):
        req = urllib.request.Request(
            self.server + path, data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json",
                     **({"Authorization": "Bearer " + self.token} if self.token else {})})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())

    def post_retry(self, path, body, tries=None, delay=10):
        """Retry on network/5xx errors. tries=None -> forever. 4xx raises immediately."""
        n = 0
        while True:
            try:
                return self.post(path, body)
            except urllib.error.HTTPError as e:
                if e.code < 500:
                    raise
                err = f"HTTP {e.code}"
            except (urllib.error.URLError, socket.timeout, ConnectionError, TimeoutError) as e:
                err = str(e)
            n += 1
            if tries is not None and n >= tries:
                raise RuntimeError(f"server unreachable: {err}")
            print(f"[client] server error ({err}), retry in {delay}s", flush=True)
            time.sleep(delay)


# ============================ 2. ROTOR RUNNER ==============================


def build_cmd(a, rng, outfile):
    """Command line for Rotor. Range end +1 so the boundary key is never skipped."""
    end = format(int(rng["end"], 16) + 1, "X")
    cmd = [a.rotor, "-m", "address", "--coin", "BTC", "--range", f"{rng['start']}:{end}", "-o", outfile]
    cmd += ["-b"] if a.both else []
    cmd += ["-t", str(a.threads)] if a.cpu else ["-g", "--gpui", a.gpu_ids]
    cmd += a.extra.split() if a.extra else []
    cmd.append(rng["target"])
    return cmd


def parse_found(outfile):
    if not os.path.exists(outfile):
        return []
    txt = open(outfile).read()
    return sorted(set(m.upper() for m in re.findall(r"Priv \(HEX\):\s*([0-9A-Fa-f]+)", txt)))


def heartbeat(api, rng, stop):
    while not stop.wait(max(30, rng.get("lease_seconds", 1800) // 3)):
        try:
            api.post("/api/client/heartbeat", {"job_id": rng["job_id"], "range_id": rng["range_id"]}, timeout=10)
        except Exception:
            pass


def save_backup(path, rng, keys):
    with open(path, "a") as f:
        for k in keys:
            f.write(f"{time.ctime()} target={rng['target']} job={rng['job_id']} range={rng['range_id']} priv_hex={k}\n")
    os.chmod(path, 0o600)


# ============================ 3. MAIN LOOP =================================


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    here = os.path.dirname(os.path.abspath(__file__))
    ap.add_argument("--server", default=os.environ.get("RANGE_SERVER"), help="https://xxx.up.railway.app")
    ap.add_argument("--token", default=os.environ.get("CLIENT_TOKEN", ""), help="CLIENT_TOKEN of the server (if set)")
    ap.add_argument("--worker", default=socket.gethostname(), help="worker name shown in the admin UI")
    ap.add_argument("--rotor", default=os.path.join(here, "..", "Rotor-Cuda", "Rotor"))
    ap.add_argument("--cpu", action="store_true", help="use CPU threads instead of GPU")
    ap.add_argument("--threads", type=int, default=os.cpu_count() or 1)
    ap.add_argument("--gpu-ids", default="0", help="e.g. 0 or 0,1")
    ap.add_argument("--both", action="store_true", help="also search uncompressed keys (2x slower)")
    ap.add_argument("--extra", default="", help="extra Rotor args, e.g. '--gpux 256,256'")
    ap.add_argument("--exit-when-idle", action="store_true", help="quit instead of polling when no job")
    ap.add_argument("--workdir", default=os.path.expanduser("~/.rotor-client"))
    a = ap.parse_args()
    if not a.server:
        ap.error("--server is required")
    os.makedirs(a.workdir, exist_ok=True)
    if not os.path.exists(a.rotor):
        sys.exit(f"Rotor binary not found: {a.rotor} (run install.sh)")

    api = Api(a.server, a.token)
    child = {"p": None}

    def on_sig(*_):
        if child["p"] and child["p"].poll() is None:
            child["p"].send_signal(signal.SIGINT)
        print("\n[client] bye", flush=True)
        os._exit(130)
    signal.signal(signal.SIGINT, on_sig)
    signal.signal(signal.SIGTERM, on_sig)

    while True:
        try:
            rng = api.post_retry("/api/client/next", {"worker": a.worker})
        except urllib.error.HTTPError as e:
            sys.exit(f"[client] server rejected request: HTTP {e.code} (check --token)")
        if rng["status"] != "range":
            print(f"[client] idle: {rng.get('reason')}", flush=True)
            if a.exit_when_idle:
                return
            time.sleep(rng.get("retry_after", 60))
            continue

        print(f"\n[client] job {rng['job_id']} range #{rng['range_id']}: {rng['start']} -> {rng['end']}  target {rng['target']}", flush=True)
        outfile = os.path.join(a.workdir, f"found_{rng['job_id']}_{rng['range_id']}.txt")
        if os.path.exists(outfile):
            os.remove(outfile)

        stop = threading.Event()
        threading.Thread(target=heartbeat, args=(api, rng, stop), daemon=True).start()
        try:
            child["p"] = subprocess.Popen(build_cmd(a, rng, outfile))
            code = child["p"].wait()
        finally:
            stop.set()

        keys = parse_found(outfile)
        if code == 10 or keys:
            print(f"\n[client] KEY FOUND ({len(keys)}) - saving local backup then reporting", flush=True)
            save_backup(os.path.join(a.workdir, "FOUND_KEYS_BACKUP.txt"), rng, keys)
            for k in keys:
                try:
                    r = api.post_retry("/api/client/found", {"job_id": rng["job_id"], "range_id": rng["range_id"],
                                                             "worker": a.worker, "private_key": k})
                    print(f"[client] server accepted key: {r}", flush=True)
                except urllib.error.HTTPError as e:
                    print(f"[client] server rejected key {k}: HTTP {e.code} (false positive?)", flush=True)
            if not keys:
                print("[client] exit code 10 but no key in output file; range NOT reported", flush=True)
            # NOTE: if the server rejected the key (false positive) the range is left on the server
            # (not deleted), so it is simply handed out again later.
        elif code == 0:
            api.post_retry("/api/client/complete", {"job_id": rng["job_id"], "range_id": rng["range_id"], "worker": a.worker})
            print("[client] range done, reported to server", flush=True)
        else:
            print(f"[client] Rotor exited with code {code}: range NOT reported, retrying in 15s", flush=True)
            time.sleep(15)


if __name__ == "__main__":
    main()
