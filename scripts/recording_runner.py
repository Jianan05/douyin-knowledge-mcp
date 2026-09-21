"""GUI runner used to capture a real public demo without exposing local paths."""

from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import tkinter as tk
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
TITLE = "DOUYIN PUBLIC DEMO"
DEMO_PYTHON = PROJECT / "runtime" / "python" / "python.exe"


def main() -> int:
    root = tk.Tk()
    root.title(TITLE)
    root.geometry("1160x680")
    root.configure(bg="#07131f")

    header = tk.Frame(root, bg="#0b1b29", padx=36, pady=24)
    header.pack(fill="x", padx=18, pady=(18, 0))
    tk.Label(
        header,
        text="DOUYIN KNOWLEDGE INGEST",
        bg="#0b1b29",
        fg="#48e0bd",
        font=("Segoe UI", 14, "bold"),
    ).pack(anchor="w")
    tk.Label(
        header,
        text="真人公开视频  →  本地转录 / OCR  →  人工审阅  →  confirmed 知识笔记",
        bg="#0b1b29",
        fg="#f4f7fa",
        font=("Microsoft YaHei UI", 22, "bold"),
    ).pack(anchor="w", pady=(8, 0))

    terminal = tk.Text(
        root,
        bg="#061019",
        fg="#eaf2f8",
        insertbackground="#eaf2f8",
        relief="flat",
        padx=26,
        pady=22,
        font=("Consolas", 14),
        wrap="word",
    )
    terminal.pack(fill="both", expand=True, padx=18, pady=18)
    terminal.tag_configure("command", foreground="#48e0bd")
    terminal.tag_configure("status", foreground="#67b7ff")
    terminal.insert("end", "> .\\runtime\\python\\python.exe public_media_demo.py --model tiny --brief\n\n", "command")
    terminal.insert("end", "准备公开夹具，不读取 Cookie、私人收藏或默认知识库……\n", "status")
    terminal.configure(state="disabled")

    def append(line: str, tag: str | None = None) -> None:
        terminal.configure(state="normal")
        terminal.insert("end", line, tag or ())
        terminal.see("end")
        terminal.configure(state="disabled")

    def run_demo() -> None:
        destination = Path(tempfile.mkdtemp(prefix="douyin-recording-run-"))
        command = [
            str(DEMO_PYTHON),
            str(PROJECT / "public_media_demo.py"),
            "--model", "tiny",
            "--output", str(destination),
            "--brief",
        ]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process = subprocess.Popen(
            command,
            cwd=PROJECT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=flags,
            env={
                **os.environ,
                "PYTHONUTF8": "1",
                "PYTHONWARNINGS": "ignore",
            },
        )
        assert process.stdout is not None
        for line in process.stdout:
            root.after(0, append, line)
        return_code = process.wait()
        if return_code == 0:
            root.after(0, append, "\n✓ 真实运行完成。\n", "command")
            root.after(25000, root.destroy)
        else:
            root.after(0, append, f"\n运行失败，退出码 {return_code}\n", "status")
            root.after(25000, root.destroy)

    root.after(1200, lambda: threading.Thread(target=run_demo, daemon=True).start())
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
