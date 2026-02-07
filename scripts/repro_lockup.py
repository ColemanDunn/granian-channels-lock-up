#!/usr/bin/env python3
"""
Reproduce a Granian + Django Channels lockup quickly and repeatably.

This repo (and this script) exists to find the actual root cause of the lockup,
not just mitigate symptoms.

Behavior:
- Starts granian on a free port (auto-increments if taken)
- Runs an HTTP + WebSocket load driver
- Detects lockup via HTTP timeouts and/or a WebSocket recv-timeout streak
- Writes per-run artifacts to scripts/repro_artifacts/run_<id>_p<port>/
- Deletes pass artifacts automatically (unless --keep-pass-logs)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

import requests
import websocket


REPO_ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS_DIR = REPO_ROOT / "scripts" / "repro_artifacts"


def _now_id() -> str:
    # Timestamp-only ID (HHMMSS) + pid to avoid collisions.
    return f"{time.strftime('%H%M%S')}_{os.getpid()}"


def _find_free_port(host: str, base_port: int, max_tries: int) -> int:
    for port in range(base_port, base_port + max_tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"no free port found in range {base_port}-{base_port + max_tries - 1}")


def _stop_process(proc: subprocess.Popen, *, name: str, timeout_s: float = 3.0) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=timeout_s)
        return
    except Exception:
        pass
    try:
        proc.terminate()
        proc.wait(timeout=timeout_s)
        return
    except Exception:
        pass
    try:
        proc.kill()
        proc.wait(timeout=timeout_s)
    except Exception:
        print(f"[repro_lockup] failed to stop {name} pid={proc.pid}", file=sys.stderr)


def _wait_http_ok(url: str, *, timeout_s: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            resp = requests.get(url, timeout=1.0)
            if 200 <= resp.status_code < 500:
                return True
        except requests.RequestException:
            pass
        time.sleep(0.1)
    return False


def _try_sample(pid: int, seconds: int, out_path: Path) -> None:
    if seconds <= 0:
        return
    try:
        subprocess.run(
            ["sample", str(pid), str(seconds), "-file", str(out_path)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return


def _pids_listening_on(port: int) -> list[int]:
    try:
        result = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return []
    pids: list[int] = []
    for token in result.stdout.split():
        try:
            pids.append(int(token))
        except ValueError:
            continue
    return sorted(set(pids))


@dataclass(frozen=True)
class RunResult:
    run_id: str
    port: int
    run_dir: str
    start_error: str | None
    lockup_count: int
    elapsed_seconds: float
    server_returncode: int | None
    granian_source: str


class LockupTracker:
    def __init__(self, stop_event: threading.Event, *, max_lockups: int, run_dir: Path) -> None:
        self._lock = threading.Lock()
        self._count = 0
        self._stop_event = stop_event
        self._max_lockups = max_lockups
        self._run_dir = run_dir
        self._last_capture_at = 0.0

    def count(self) -> int:
        with self._lock:
            return self._count

    def _append(self, title: str, lines: list[str]) -> None:
        path = self._run_dir / "capture.log"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"\n=== {title} ===\n")
            for line in lines:
                fh.write(line.rstrip() + "\n")

    def _capture(self, reason: str, *, port: int, sample_seconds: int) -> None:
        now = time.monotonic()
        with self._lock:
            if (now - self._last_capture_at) < 1.0:
                return
            self._last_capture_at = now

        ts = time.strftime("%H:%M:%S")
        pids = _pids_listening_on(port)
        self._append(f"{ts} lockup reason={reason} pids={pids}", [])
        if pids and sample_seconds > 0:
            out_path = self._run_dir / f"sample_{pids[0]}_{int(time.time() * 1000)}.txt"
            _try_sample(pids[0], sample_seconds, out_path)
            self._append("sample", [f"sampled pid={pids[0]} seconds={sample_seconds} -> {out_path.name}"])

    def record(self, reason: str, *, port: int, sample_seconds: int) -> None:
        with self._lock:
            self._count += 1
            count = self._count
        self._append("event", [f"{time.strftime('%H:%M:%S')} lockup #{count} detected ({reason})"])
        self._capture(reason, port=port, sample_seconds=sample_seconds)
        if self._max_lockups > 0 and count >= self._max_lockups:
            self._stop_event.set()


class Stats:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._c = Counter()

    def inc(self, key: str, n: int = 1) -> None:
        with self._lock:
            self._c[key] += n

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._c)


def websocket_worker(
    stop_event: threading.Event,
    tracker: LockupTracker,
    *,
    stats: Stats,
    ws_url: str,
    http_timeout_s: float,
    recv_timeout_s: float,
    streak_limit: int,
    send_interval_s: float,
    port: int,
    sample_seconds: int,
    logger: logging.Logger,
) -> None:
    counter = 0
    streak = 0
    while not stop_event.is_set():
        ws = websocket.WebSocket()
        try:
            logger.info("ws connect url=%s", ws_url)
            ws.connect(ws_url, timeout=http_timeout_s)
            ws.settimeout(recv_timeout_s)
            while not stop_event.is_set():
                payload = {"type": "ping", "count": counter}
                ws.send(json.dumps(payload))
                stats.inc("ws_send")
                try:
                    _ = ws.recv()
                    stats.inc("ws_recv")
                    streak = 0
                except websocket.WebSocketTimeoutException:
                    stats.inc("ws_recv_timeout")
                    streak += 1
                    if streak >= streak_limit:
                        tracker.record("ws-timeout-streak", port=port, sample_seconds=sample_seconds)
                        streak = 0
                counter += 1
                time.sleep(send_interval_s)
        except (websocket.WebSocketException, ConnectionError, OSError):
            stats.inc("ws_error")
            time.sleep(0.2)
        finally:
            try:
                ws.close()
            except websocket.WebSocketException:
                pass


def http_worker(
    stop_event: threading.Event,
    tracker: LockupTracker,
    *,
    stats: Stats,
    http_url: str,
    timeout_s: float,
    interval_s: float,
    port: int,
    sample_seconds: int,
    logger: logging.Logger,
) -> None:
    while not stop_event.is_set():
        try:
            resp = requests.get(http_url, timeout=timeout_s)
            resp.raise_for_status()
            stats.inc("http_ok")
        except requests.Timeout:
            stats.inc("http_timeout")
            tracker.record("http-timeout", port=port, sample_seconds=sample_seconds)
        except requests.RequestException:
            stats.inc("http_error")
            pass
        if stop_event.is_set():
            break
        time.sleep(interval_s)


def status_worker(
    stop_event: threading.Event,
    *,
    logger: logging.Logger,
    stats: Stats,
    tracker: LockupTracker,
    interval_s: float,
) -> None:
    if interval_s <= 0:
        return
    while not stop_event.is_set():
        snap = stats.snapshot()
        logger.info("status lockups=%s stats=%s", tracker.count(), snap)
        time.sleep(interval_s)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Reproduce Granian + Channels lockup with minimal moving parts.")

    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--base-port", type=int, default=8000)
    p.add_argument("--max-port-tries", type=int, default=50)

    p.add_argument("--max-seconds", type=float, default=60.0)
    p.add_argument("--lockup-max", type=int, default=1)

    p.add_argument("--http-workers", type=int, default=2)
    p.add_argument("--ws-workers", type=int, default=2)

    p.add_argument("--http-interval-seconds", type=float, default=0.05)
    p.add_argument("--http-timeout-seconds", type=float, default=5.0)

    p.add_argument("--ws-recv-timeout-seconds", type=float, default=0.5)
    p.add_argument("--ws-timeout-streak", type=int, default=5)
    p.add_argument("--send-interval-seconds", type=float, default=0.2)

    p.add_argument("--sample-seconds", type=int, default=1, help="macOS only; 0 disables.")
    p.add_argument("--status-interval-seconds", type=float, default=5.0, help="0 disables periodic status logs.")
    p.add_argument("--log-level", default="info", choices=["critical", "error", "warning", "info", "debug"])
    p.add_argument("--keep-pass-logs", action="store_true")
    p.add_argument("--run-id", default="", help="Default is a timestamp-based id.")

    p.add_argument("--granian-source", default="installed", choices=["installed", "editable"])
    p.add_argument("--granian-editable-path", default="./granian")

    p.add_argument("--granian-workers", type=int, default=4)
    p.add_argument("--granian-runtime-threads", type=int, default=1)
    p.add_argument("--granian-runtime-blocking-threads", type=int, default=2, help="0 omits the flag (auto)")
    p.add_argument("--granian-blocking-threads", type=int, default=1, help="0 omits the flag (auto)")
    p.add_argument("--granian-runtime-mode", default="auto", choices=["auto", "mt", "st"])
    p.add_argument("--granian-loop", default="auto", choices=["auto", "asyncio", "uvloop", "rloop", "winloop"])
    p.add_argument("--granian-task-impl", default="asyncio", choices=["asyncio", "rust"])
    p.add_argument(
        "--granian-log-level",
        default="info",
        choices=["critical", "error", "warning", "warn", "info", "debug", "notset"],
    )

    return p.parse_args()


def main() -> int:
    args = parse_args()

    run_id = args.run_id or _now_id()
    port = _find_free_port(args.host, args.base_port, args.max_port_tries)
    run_dir = ARTIFACTS_DIR / f"run_{run_id}_p{port}"
    run_dir.mkdir(parents=True, exist_ok=True)

    http_url = f"http://{args.host}:{port}/api/users/"
    ws_url = f"ws://{args.host}:{port}/ws/echo/"

    # Per-run logging: file in the run dir + stdout.
    logger = logging.getLogger("repro_lockup")
    logger.setLevel(getattr(logging, args.log_level.upper()))
    logger.propagate = False
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    fh = logging.FileHandler(run_dir / "driver.log", encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    logger.info("run start run_id=%s port=%s http_url=%s ws_url=%s", run_id, port, http_url, ws_url)

    meta = {
        "run_id": run_id,
        "started_at": time.strftime("%H:%M:%S"),
        "port": port,
        "http_url": http_url,
        "ws_url": ws_url,
        "granian_source": args.granian_source,
    }
    (run_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")

    server_log = run_dir / "granian.log"
    start_error: str | None = None
    server_proc: subprocess.Popen | None = None
    exit_code = 0

    server_cmd: list[str] = ["uv", "run", "--no-sync"]
    if args.granian_source == "editable":
        server_cmd += ["--with-editable", args.granian_editable_path]
    server_cmd += [
        "granian",
        "--interface",
        "asginl",
        "--host",
        args.host,
        "--port",
        str(port),
        "--workers",
        str(args.granian_workers),
        "--runtime-threads",
        str(args.granian_runtime_threads),
    ]
    if args.granian_runtime_blocking_threads:
        server_cmd += ["--runtime-blocking-threads", str(args.granian_runtime_blocking_threads)]
    if args.granian_blocking_threads:
        server_cmd += ["--blocking-threads", str(args.granian_blocking_threads)]
    server_cmd += [
        "--runtime-mode",
        args.granian_runtime_mode,
        "--loop",
        args.granian_loop,
        "--task-impl",
        args.granian_task_impl,
        "--log-level",
        args.granian_log_level,
        "--no-access-log",
        "config.asgi:application",
    ]
    (run_dir / "server_cmd.json").write_text(json.dumps(server_cmd, indent=2) + "\n", encoding="utf-8")

    stop_event = threading.Event()
    tracker = LockupTracker(stop_event, max_lockups=args.lockup_max, run_dir=run_dir)
    stats = Stats()

    started = time.monotonic()
    try:
        with server_log.open("w", encoding="utf-8") as server_fh:
            server_proc = subprocess.Popen(
                server_cmd,
                cwd=REPO_ROOT,
                stdout=server_fh,
                stderr=subprocess.STDOUT,
            )

        if not _wait_http_ok(http_url, timeout_s=20.0):
            start_error = "server_not_ready"
            exit_code = 2
            return exit_code

        threads: list[threading.Thread] = []
        status_t = threading.Thread(
            target=status_worker,
            args=(stop_event,),
            kwargs={
                "logger": logger,
                "stats": stats,
                "tracker": tracker,
                "interval_s": args.status_interval_seconds,
            },
            daemon=True,
        )
        status_t.start()
        threads.append(status_t)

        for _ in range(max(args.ws_workers, 0)):
            t = threading.Thread(
                target=websocket_worker,
                args=(stop_event, tracker),
                kwargs={
                    "stats": stats,
                    "ws_url": ws_url,
                    "http_timeout_s": args.http_timeout_seconds,
                    "recv_timeout_s": args.ws_recv_timeout_seconds,
                    "streak_limit": args.ws_timeout_streak,
                    "send_interval_s": args.send_interval_seconds,
                    "port": port,
                    "sample_seconds": args.sample_seconds,
                    "logger": logger,
                },
                daemon=True,
            )
            t.start()
            threads.append(t)

        for _ in range(max(args.http_workers, 0)):
            t = threading.Thread(
                target=http_worker,
                args=(stop_event, tracker),
                kwargs={
                    "stats": stats,
                    "http_url": http_url,
                    "timeout_s": args.http_timeout_seconds,
                    "interval_s": args.http_interval_seconds,
                    "port": port,
                    "sample_seconds": args.sample_seconds,
                    "logger": logger,
                },
                daemon=True,
            )
            t.start()
            threads.append(t)

        while not stop_event.is_set() and (time.monotonic() - started) < args.max_seconds:
            time.sleep(0.2)
        stop_event.set()
        for t in threads:
            t.join(timeout=2.0)
    finally:
        elapsed = time.monotonic() - started
        if server_proc is not None:
            _stop_process(server_proc, name="server")

        lockup_count = tracker.count()
        logger.info("run end lockups=%s elapsed_s=%.3f stats=%s", lockup_count, elapsed, stats.snapshot())
        if start_error is None and lockup_count > 0 and exit_code == 0:
            exit_code = 1
        result = RunResult(
            run_id=run_id,
            port=port,
            run_dir=str(run_dir),
            start_error=start_error,
            lockup_count=lockup_count,
            elapsed_seconds=elapsed,
            server_returncode=server_proc.returncode if server_proc is not None else None,
            granian_source=args.granian_source,
        )
        (run_dir / "result.json").write_text(json.dumps(asdict(result), indent=2) + "\n", encoding="utf-8")

        if start_error is None and lockup_count == 0 and not args.keep_pass_logs:
            shutil.rmtree(run_dir, ignore_errors=True)

        print(json.dumps(asdict(result)))

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
