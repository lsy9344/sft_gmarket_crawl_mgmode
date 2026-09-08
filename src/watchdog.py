"""
Watchdog: full_sweep.py 모니터링 + 행잉 시 자동 재시작
로그 파일에 3분간 새 항목이 없으면 프로세스를 kill하고 재시작.
"""

import subprocess
import sys
import time
import os
from pathlib import Path
from datetime import datetime

sys.stdout.reconfigure(line_buffering=True)

BASE_DIR = Path(__file__).parent.parent
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

STALL_TIMEOUT = 1500  # 25 minutes - must exceed max cooldown (20 min extended)
CHECK_INTERVAL = 30  # check every 30 seconds
MAX_RESTARTS = 20


def get_log_mtime(log_path):
    if log_path.exists():
        return os.path.getmtime(log_path)
    return 0


def start_sweep():
    log_name = f"fullsweep_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    log_path = LOG_DIR / log_name
    log_file = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-X", "utf8", str(BASE_DIR / "src" / "full_sweep.py")],
        stdout=log_file,
        stderr=subprocess.STDOUT,
        cwd=str(BASE_DIR),
        creationflags=0x00000008,
    )
    print(f"[WATCHDOG] Started full_sweep PID={proc.pid}, log={log_name}")
    return proc, log_path, log_file


def main():
    restarts = 0
    proc, log_path, log_file = start_sweep()
    last_mtime = time.time()

    while restarts < MAX_RESTARTS:
        time.sleep(CHECK_INTERVAL)

        if proc.poll() is not None:
            print(f"[WATCHDOG] Process exited (code={proc.returncode}). Restarting...")
            log_file.close()
            restarts += 1
            time.sleep(30)
            proc, log_path, log_file = start_sweep()
            last_mtime = time.time()
            continue

        current_mtime = get_log_mtime(log_path)
        if current_mtime > last_mtime:
            last_mtime = current_mtime
        else:
            stall_duration = time.time() - last_mtime
            if stall_duration > STALL_TIMEOUT:
                print(f"[WATCHDOG] STALLED for {stall_duration:.0f}s. Killing PID={proc.pid}...")
                proc.kill()
                proc.wait()
                log_file.close()
                restarts += 1
                print(f"[WATCHDOG] Restart #{restarts}. Waiting 60s before restart...")
                time.sleep(60)
                proc, log_path, log_file = start_sweep()
                last_mtime = time.time()

    print(f"[WATCHDOG] Max restarts ({MAX_RESTARTS}) reached. Giving up.")


if __name__ == "__main__":
    main()
