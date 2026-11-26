"""CodeForge 悬浮窗：常驻桌面右上角，实时显示 Agent/铸造任务「正在干什么、第几步」。

独立进程（pythonw 启动，无控制台），数据从 8765 的 /api/hud 拉取，不占用主界面的事件流。
双击卡片打开主界面，右上角 ✕ 关闭，按住卡片任意处可拖动。
"""
from __future__ import annotations

import ctypes
import json
import re
import socket
import sys
import threading
import time
import tkinter as tk
import urllib.request
import webbrowser

BASE = "http://127.0.0.1:8765"
LOCK_PORT = 8767
TRANSPARENT = "#010203"
BG = "#161922"
LINE = "#2b3140"
FG = "#e9edf8"
MUTED = "#8b93a7"
ACCENT = "#7c5cff"
OK = "#34d399"
BAD = "#f87171"
WARN = "#f5a524"

POLL_MS = 350

# ---------------------------------------------------------------- DPI
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)          # 每显示器 DPI 感知
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

try:
    S = ctypes.windll.user32.GetDpiForSystem() / 96.0
except Exception:
    S = 1.0


def P(v: float) -> int:
    return max(1, int(round(v * S)))


W, H = P(404), P(134)


def _round_rect(canvas, x1, y1, x2, y2, r, **kw):
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return canvas.create_polygon(pts, smooth=True, **kw)


def _clip(text: str, n: int) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


class Hud:
    def __init__(self):
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=TRANSPARENT)
        try:
            self.root.attributes("-transparentcolor", TRANSPARENT)
        except Exception:
            self.root.configure(bg=BG)
        self.root.attributes("-alpha", 1.0)
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.root.geometry(f"{W}x{H}+{sw - W - P(24)}+{P(96)}")

        self.cv = tk.Canvas(self.root, width=W, height=H, bg=TRANSPARENT,
                            highlightthickness=0, bd=0)
        self.cv.pack(fill="both", expand=True)
        _round_rect(self.cv, 1, 1, W - 1, H - 1, P(18), fill=BG, outline=LINE, width=1)
        self.bar = self.cv.create_rectangle(P(1), P(20), P(5), H - P(20),
                                            fill=MUTED, outline="")

        self.dot = self.cv.create_oval(P(20), P(20), P(30), P(30), fill=MUTED, outline="")
        f_title = ("Microsoft YaHei UI", P(10), "bold")
        f_mid = ("Microsoft YaHei UI", P(9))
        f_small = ("Microsoft YaHei UI", P(8))
        f_num = ("Consolas", P(9))

        self.t_head = self.cv.create_text(P(38), P(25), anchor="w", text="CodeForge · 待命",
                                          fill=MUTED, font=f_title)
        self.t_step = self.cv.create_text(W - P(38), P(25), anchor="e", text="", fill=MUTED,
                                          font=f_num)
        self.t_goal = self.cv.create_text(P(20), P(52), anchor="w", text="", fill=MUTED,
                                          font=f_small)
        self.t_msg = self.cv.create_text(P(20), P(77), anchor="w", text="没有正在处理的任务",
                                         fill=MUTED, font=f_mid)

        self.cv.create_rectangle(P(20), P(98), W - P(20), P(106), fill="#20242f", outline="")
        self.track = (P(20), W - P(20))
        self.prog = self.cv.create_rectangle(P(20), P(98), P(20), P(106), fill=ACCENT, outline="")
        self.t_foot = self.cv.create_text(P(20), P(120), anchor="w", text="双击打开主界面",
                                          fill=MUTED, font=f_small)

        self.close = self.cv.create_text(W - P(22), P(22), text="✕", fill=MUTED, font=f_small)
        self.cv.tag_bind(self.close, "<Button-1>", lambda e: self.root.destroy())
        self.cv.tag_bind(self.close, "<Enter>", lambda e: self.cv.itemconfig(self.close, fill=BAD))
        self.cv.tag_bind(self.close, "<Leave>", lambda e: self.cv.itemconfig(self.close, fill=MUTED))

        self.cv.bind("<Button-1>", self._press)
        self.cv.bind("<B1-Motion>", self._drag)
        self.cv.bind("<Double-Button-1>", lambda e: webbrowser.open(BASE))

        self.data = {}
        threading.Thread(target=self._poll, daemon=True).start()
        self.root.after(POLL_MS, self._tick)
        self.root.mainloop()

    # -------------------------------------------------------------- 拖动
    def _press(self, e):
        self._dx = e.x_root - self.root.winfo_x()
        self._dy = e.y_root - self.root.winfo_y()

    def _drag(self, e):
        self.root.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

    # -------------------------------------------------------------- 数据
    def _poll(self):
        while True:
            try:
                with urllib.request.urlopen(BASE + "/api/hud", timeout=2) as r:
                    self.data = json.load(r)
            except Exception:
                self.data = {"online": False}
            time.sleep(POLL_MS / 1000)

    def _set_progress(self, pct):
        x1, x2 = self.track
        self.cv.coords(self.prog, x1, P(98), x1 + (x2 - x1) * pct, P(106))

    def _tick(self):
        d = self.data or {}
        online = bool(d) and d.get("online") is not False

        if not online:
            self.cv.itemconfig(self.dot, fill=MUTED)
            self.cv.itemconfig(self.t_head, text="CodeForge · 服务未启动", fill=BAD)
            self.cv.itemconfig(self.t_step, text="")
            self.cv.itemconfig(self.t_goal, text="")
            self.cv.itemconfig(self.t_msg, text="双击这里启动，或双击桌面图标", fill=MUTED)
            self.cv.itemconfig(self.t_foot, text="端口 8765 无响应", fill=MUTED)
            self.cv.itemconfig(self.bar, fill=MUTED)
            self._set_progress(0)
            self.root.after(POLL_MS, self._tick)
            return

        mode = d.get("mode", "idle")
        status = d.get("status", "idle")
        step, total = int(d.get("step") or 0), int(d.get("total") or 0)
        busy = mode in ("agent", "code") and status in ("running", "pending")

        color = {"running": ACCENT, "pending": WARN, "ok": OK, "fail": BAD}.get(status, MUTED)
        label = {"agent": "电脑 Agent", "code": "代码工坊"}.get(mode, "待命")

        self.cv.itemconfig(self.dot, fill=color)
        self.cv.itemconfig(self.bar, fill=color)
        self.cv.itemconfig(self.t_head, text=f"CodeForge · {label}",
                           fill=FG if busy else MUTED)
        self.cv.itemconfig(self.t_step, text=f"{step}/{total} 步" if total else "")
        self.cv.itemconfig(self.t_goal, text=_clip(d.get("title", ""), 30))
        msg = re.sub(r"^第\s*\d+\s*/\s*\d+\s*步[：:]\s*", "", d.get("message") or "")
        self.cv.itemconfig(self.t_msg, text=_clip(msg or "没有正在处理的任务", 30),
                           fill=FG if busy else MUTED)

        el = int(d.get("elapsed") or 0)
        if status == "pending":
            foot = "⚠ 等你批准这个操作 · 双击打开主界面"
        elif busy and el:
            foot = f"已用时 {el}s · 双击打开主界面"
        elif status == "ok" and el:
            foot = f"完成 · 用时 {el}s · 双击打开主界面"
        elif status == "fail" and el:
            foot = f"已停止 · 用时 {el}s · 双击打开主界面"
        else:
            foot = "双击打开主界面"
        self.cv.itemconfig(self.t_foot, text=foot, fill=WARN if status == "pending" else MUTED)

        pct = (step / total) if total else (1.0 if status in ("ok", "fail") else 0.0)
        self._set_progress(max(0.0, min(1.0, pct)))
        self.cv.itemconfig(self.prog, fill=color)
        self.root.after(POLL_MS, self._tick)


def single_instance() -> bool:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", LOCK_PORT))
        s.listen(1)
    except OSError:
        return False
    globals()["_lock"] = s
    return True


if __name__ == "__main__":
    if "--check" in sys.argv:
        sys.exit(0)
    if not single_instance():
        sys.exit(0)
    Hud()
