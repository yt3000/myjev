"""MyJev 生命周期入口（docs/04 §1 实现）。

  python -m myjev.serve --role admin            前台运行管理后台 (:8090)
  python -m myjev.serve --role runtime          前台运行服务后台 (:8091)
  python -m myjev.serve serve-all --daemon      守护启动两进程（推荐）
  python -m myjev.serve stop-all                双路停止（pid 优先、端口兜底，幂等）
  python -m myjev.serve status                  两进程状态 + epoch 回执
  python -m myjev.serve --role admin --daemon   单角色守护
"""
from __future__ import annotations

import argparse
import atexit
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent      # myjev/
DATA = ROOT / "data"
IS_WIN = os.name == "nt"
PORTS = {"admin": 8090, "runtime": 8091}


def pid_file(role: str) -> Path:
    return DATA / f"{role}.pid"


def log_file(role: str) -> Path:
    return DATA / f"{role}.log"


def _port_busy(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.6)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _read_pid(role: str) -> int | None:
    try:
        return int(pid_file(role).read_text().strip())
    except Exception:
        return None


def _pid_alive(pid: int) -> bool:
    try:
        if IS_WIN:
            r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                               capture_output=True, text=True)
            return str(pid) in r.stdout
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _kill_pid(pid: int) -> bool:
    try:
        if IS_WIN:
            return subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                                  capture_output=True).returncode == 0
        os.kill(pid, 15)
        time.sleep(0.5)
        try:
            os.kill(pid, 0)
            os.kill(pid, 9)
        except OSError:
            pass
        return True
    except Exception:
        return False


def _pids_on_port(port: int) -> list[int]:
    out: set[int] = set()
    try:
        if IS_WIN:
            r = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                               capture_output=True, text=True, errors="ignore")
            for line in r.stdout.splitlines():
                c = line.split()
                if len(c) >= 5 and c[0].startswith("TCP") and f":{port}" in c[1] \
                        and c[3].upper() == "LISTENING":
                    out.add(int(c[4]))
        else:
            r = subprocess.run(["lsof", f"-tiTCP:{port}", "-sTCP:LISTEN"],
                               capture_output=True, text=True)
            out = {int(x) for x in r.stdout.split() if x.strip().isdigit()}
    except Exception:
        pass
    return sorted(out)


def _write_pid(role: str):
    DATA.mkdir(exist_ok=True)
    pid_file(role).write_text(str(os.getpid()))

    def _clean():
        try:
            if _read_pid(role) == os.getpid():
                pid_file(role).unlink(missing_ok=True)
        except Exception:
            pass
    atexit.register(_clean)


def _http_get(url: str, timeout: float = 3.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def daemon_launch(role: str, port: int, auto_train: bool) -> bool:
    if _port_busy(port):
        print(f"[serve] {role} 端口 {port} 已被占用（残留实例？）——先 stop-all")
        return False
    DATA.mkdir(exist_ok=True)
    logf = open(log_file(role), "a", encoding="utf-8", errors="replace")
    args = [sys.executable, "-m", "myjev.serve", "--role", role, "--foreground"]
    if not auto_train:
        args.append("--no-auto-train")
    flags, kw = 0, {}
    if IS_WIN:
        flags = 0x00000008 | 0x00000200
    else:
        kw["start_new_session"] = True
    subprocess.Popen(args, stdout=logf, stderr=subprocess.STDOUT,
                     stdin=subprocess.DEVNULL, creationflags=flags, cwd=str(ROOT), **kw)
    return True


def cmd_stop() -> int:
    killed = []
    for role in ("runtime", "admin"):
        pid = _read_pid(role)
        if pid and _pid_alive(pid) and _kill_pid(pid):
            killed.append(f"{role}:pid={pid}")
        port = PORTS[role]
        for p in _pids_on_port(port):
            if _kill_pid(p):
                killed.append(f"{role}:port={p}")
        pid_file(role).unlink(missing_ok=True)
    time.sleep(0.6)
    busy = [r for r, pt in PORTS.items() if _port_busy(pt)]
    if busy:
        print(f"[stop] 警告：仍有监听 {busy}（可能需要管理员权限）")
        return 1
    print(f"[stop] 已停止 {killed or '（无运行实例）'}，8090/8091 空闲")
    return 0


def cmd_status() -> int:
    rc = 0
    for role in ("admin", "runtime"):
        pid = _read_pid(role)
        alive = bool(pid and _pid_alive(pid)) and _port_busy(PORTS[role])
        extra = ""
        if alive:
            h = _http_get(f"http://127.0.0.1:{PORTS[role]}/api/health") or \
                _http_get(f"http://127.0.0.1:{PORTS[role]}/health")
            if role == "runtime" and h:
                extra = f" epoch={h.get('version_epoch')} loaded={h.get('loaded_epoch')} 模型:{list(h.get('loaded', {}))}"
            elif h:
                extra = f" 任务:{h.get('tasks')}"
        print(f"[status] {role:8s} :{PORTS[role]}  {'运行中 pid=' + str(pid) + extra if alive else '未运行'}")
        rc = rc or (0 if alive else 3)
    return rc


def run_foreground(role: str, auto_train: bool) -> int:
    import uvicorn
    if _port_busy(PORTS[role]):
        print(f"[serve] 端口 {PORTS[role]} 被占用——stop-all 或改端口。")
        return 3
    if role == "runtime":
        from .runtime_app import create_runtime_app
        app = create_runtime_app(ROOT)
    else:
        from .admin_app import create_admin_app
        app = create_admin_app(ROOT)
        if auto_train:
            db = app.state.db
            for t, eng in app.state.engines.items():
                if not eng.active:
                    print(f"[serve] 首次启动：训练 {t}（{eng.title}）…")
                    res = eng.train(note="首次自动训练")
                    db.publish(t, res["version"], note="初始发布")
                    eng.reload_registry()
    _write_pid(role)
    print(f"[serve] {role} → http://127.0.0.1:{PORTS[role]}  pid={os.getpid()}")
    uvicorn.run(app, host="127.0.0.1", port=PORTS[role], log_level="warning")
    return 0


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser("myjev.serve")
    ap.add_argument("action", nargs="?", default="run",
                    choices=["run", "serve-all", "stop-all", "status"])
    ap.add_argument("--role", default="admin", choices=["admin", "runtime"])
    ap.add_argument("--daemon", action="store_true")
    ap.add_argument("--foreground", action="store_true",
                    help="(内部) 由 daemon 拉起时标记")
    ap.add_argument("--no-auto-train", action="store_true")
    args = ap.parse_args()
    auto = not args.no_auto_train

    if args.action == "stop-all":
        return cmd_stop()
    if args.action == "status":
        return cmd_status()
    if args.action == "serve-all":
        if args.daemon:
            ok_a = daemon_launch("admin", PORTS["admin"], auto)
            time.sleep(1.5)
            ok_r = daemon_launch("runtime", PORTS["runtime"], False)
            print("[serve] 等待就绪（首次含训练，最长 3 分钟）…")
            deadline = time.time() + 180
            while time.time() < deadline:
                if _port_busy(8090) and _port_busy(8091):
                    print("[serve] ✓ 控制台 http://127.0.0.1:8090 | 推理 :8091 | 日志 data/admin.log data/runtime.log")
                    return 0
                time.sleep(2)
            print("[serve] ✗ 未在期限内就绪，查看 data/*.log")
            return 1
        print("[serve] 前台 serve-all 不支持，请用 --daemon 或分别 --role 前台")
        return 2
    # run
    if args.daemon and not args.foreground:
        return 0 if daemon_launch(args.role, PORTS[args.role], auto) else 1
    return run_foreground(args.role, auto)


if __name__ == "__main__":
    sys.exit(main())
