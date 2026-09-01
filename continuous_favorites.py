"""安全地连续补齐抖音收藏；每轮仍由 ingest.py 负责去重和失败记录。"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import subprocess
import sys
import time
import traceback
from pathlib import Path


PROJECT = Path(__file__).resolve().parent
LIBRARY = Path(
    os.environ.get("DOUYIN_NOTES_DIR", str(Path.home() / "Desktop" / "DouyinNotes"))
).expanduser()
PROGRESS = LIBRARY / "_转录进度.md"
LOG = LIBRARY / "_连续续跑.log"
LOCK = LIBRARY / "_连续续跑.lock"
PID = LIBRARY / "_连续续跑.pid"
_LOG_LIMIT = 10 * 1024 * 1024
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|$)")


def _rotate_log(path: Path = LOG) -> None:
    if not path.exists() or path.stat().st_size < _LOG_LIMIT:
        return
    previous = path.with_suffix(path.suffix + ".1")
    previous.unlink(missing_ok=True)
    path.replace(previous)


def log(message: str, path: Path = LOG, *, echo: bool = True) -> None:
    """独立运行日志；只写程序回显，不主动读取或写入转录正文。"""
    clean = _ANSI.sub("", str(message)).rstrip("\r\n")
    if not clean:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    _rotate_log(path)
    stamp = dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        for line in clean.splitlines():
            handle.write(f"[{stamp}] {line}\n")
    if echo:
        print(clean, flush=True)


def acquire_instance_lock(path: Path = LOCK):
    """持有 Windows 文件锁直到进程退出，防止误启两个 GPU 转录任务。"""
    import msvcrt

    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        return None
    return handle


def read_progress(path: Path = PROGRESS) -> dict[str, int | str]:
    text = path.read_text(encoding="utf-8") if path.exists() else ""

    def number(label: str) -> int:
        match = re.search(rf"^- {re.escape(label)}：(\d+) 条$", text, re.MULTILINE)
        return int(match.group(1)) if match else -1

    status_match = re.search(r"^- 状态：\*\*(.+?)\*\*$", text, re.MULTILINE)
    total_match = re.search(r"^- 收藏清点：(\d+) 条$", text, re.MULTILINE)
    return {
        "status": status_match.group(1) if status_match else "未知",
        "total": int(total_match.group(1)) if total_match else -1,
        "failed": number("失败"),
        "completed": number("已完成"),
        "skipped": number("已跳过"),
    }


def run_cycle() -> int:
    command = [
        sys.executable,
        "-u",
        str(PROJECT / "ingest.py"),
        "--favorites",
        "--keep-collected",
        "--dir",
        str(LIBRARY),
    ]
    log(f"[连续续跑] 启动子进程：{' '.join(command)}")
    process = subprocess.Popen(
        command,
        cwd=PROJECT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    assert process.stdout is not None
    for line in process.stdout:
        log(line)
    return process.wait()


def main() -> int:
    parser = argparse.ArgumentParser(description="连续安全补齐抖音收藏")
    parser.add_argument(
        "--target-min",
        type=int,
        default=1,
        help="至少重新看到多少条收藏才允许判定补齐（默认 1；已知历史高水位时应显式传入）",
    )
    parser.add_argument(
        "--backoff-seconds",
        type=int,
        default=600,
        help="抖音只返回部分收藏时，两轮之间等待秒数",
    )
    args = parser.parse_args()
    target = max(1, args.target_min)
    cycle = 0
    lock_handle = acquire_instance_lock()
    if lock_handle is None:
        log("[连续续跑] 已有一个实例在运行，本次不重复启动。")
        return 2
    PID.write_text(str(os.getpid()), encoding="ascii")
    log(
        f"[连续续跑] 后台监督器启动；PID {os.getpid()}；"
        f"日志 {LOG}"
    )

    try:
        while True:
            cycle += 1
            log(
                f"[连续续跑] 第 {cycle} 轮开始；"
                f"完成条件：清点至少 {target} 条且失败 0。"
            )
            try:
                exit_code = run_cycle()
            except Exception:
                exit_code = 1
                log("[连续续跑] 启动或监督子进程异常：\n" + traceback.format_exc())
            state = read_progress()
            total = int(state["total"])
            if total > target:
                target = total
                log(f"[连续续跑] 收藏高水位更新为 {target} 条。")
            log(
                f"[连续续跑] 本轮退出码 {exit_code}；状态 {state['status']}；"
                f"清点 {total}；完成 {state['completed']}；"
                f"跳过 {state['skipped']}；失败 {state['failed']}。"
            )
            if (
                exit_code == 0
                and state["status"] == "完成"
                and total >= target
                and state["failed"] == 0
            ):
                log("[连续续跑] 已达到历史高水位且无失败，自动续跑完成。")
                return 0

            wait_s = max(60, args.backoff_seconds)
            log(
                f"[连续续跑] 当前清点尚未达到 {target} 条或仍有失败；"
                f"{wait_s // 60} 分钟后自动重试，无需人工提醒。"
            )
            time.sleep(wait_s)
    except KeyboardInterrupt:
        log("[连续续跑] 收到人工中断，已停止。")
        return 130
    finally:
        PID.unlink(missing_ok=True)
        lock_handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
