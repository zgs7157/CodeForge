"""CodeForge 安装程序（自带 GUI 向导）。

做的事：探测硬件 → 推荐模型 → 选目录 → 解包 payload → 按需下载模型 → 建快捷方式 →
写卸载信息（注册表 HKCU 卸载项 + 卸载.bat）。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox

APP = "CodeForge"
TITLE = "CodeForge 铸码工坊"
VER = "1.0.0"
DEFAULT_DIR = r"D:\APP\CodeForge"
HERE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
PAYLOAD = HERE / "payload.zip"

BG = "#141821"
CARD = "#1a2030"
FG = "#e9edf8"
MUT = "#8b93a7"
ACC = "#7c5cff"
OK = "#34d399"
ERR = "#f87171"

MODELS = [
    {"id": "qwen-coder-1.5b-q4", "name": "Qwen2.5-Coder-1.5B Q4_K_M", "gb": 1.05, "q": 42,
     "repo": "Qwen/Qwen2.5-Coder-1.5B-Instruct-GGUF", "file": "qwen2.5-coder-1.5b-instruct-q4_k_m.gguf"},
    {"id": "qwen-coder-3b-q4", "name": "Qwen2.5-Coder-3B Q4_K_M", "gb": 2.00, "q": 55,
     "repo": "Qwen/Qwen2.5-Coder-3B-Instruct-GGUF", "file": "qwen2.5-coder-3b-instruct-q4_k_m.gguf"},
    {"id": "qwen-coder-7b-q4", "name": "Qwen2.5-Coder-7B Q4_K_M", "gb": 4.47, "q": 78,
     "repo": "Qwen/Qwen2.5-Coder-7B-Instruct-GGUF", "file": "qwen2.5-coder-7b-instruct-q4_k_m.gguf"},
    {"id": "qwen-coder-14b-q4", "name": "Qwen2.5-Coder-14B Q4_K_M", "gb": 8.90, "q": 90,
     "repo": "Qwen/Qwen2.5-Coder-14B-Instruct-GGUF", "file": "qwen2.5-coder-14b-instruct-q4_k_m.gguf"},
    {"id": "deepseek-v2-lite-q4", "name": "DeepSeek-Coder-V2-Lite Q4_K_M", "gb": 9.60, "q": 86,
     "repo": "deepseek-ai/DeepSeek-Coder-V2-Lite-Instruct-GGUF",
     "file": "DeepSeek-Coder-V2-Lite-Instruct-Q4_K_M.gguf"},
]


# ------------------------------------------------------------------ 硬件
def probe_hw() -> dict:
    import ctypes

    class MEM(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

    m = MEM()
    m.dwLength = ctypes.sizeof(MEM)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
    ram_gb = m.ullTotalPhys / 1e9
    vram = 0
    gpu = ""
    exe = Path(r"C:\Windows\System32\nvidia-smi.exe")
    if exe.exists():
        try:
            out = subprocess.run([str(exe), "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                                 capture_output=True, text=True, timeout=8,
                                 creationflags=subprocess.CREATE_NO_WINDOW).stdout.strip()
            if out:
                gpu, v = [x.strip() for x in out.splitlines()[0].split(",")]
                vram = int(float(v))
        except Exception:
            pass
    return {"ram_gb": round(ram_gb, 1), "vram_mb": vram, "gpu": gpu}


def pick_model(hw, prefer_id=None):
    if prefer_id:
        for m in MODELS:
            if m["id"] == prefer_id:
                return m
    budget = max(0, hw["vram_mb"] - 620) / 1024
    best, best_score = MODELS[0], -1
    for m in MODELS:
        fit = m["gb"] * 1.03 <= budget
        partial = m["gb"] * 1.03 <= budget * 2.6
        score = m["q"] * (1.0 if fit else 0.62 if partial else 0.18)
        if m["gb"] > hw["ram_gb"] * 0.45:
            score *= 0.82
        if score > best_score:
            best, best_score = m, score
    return best


# ------------------------------------------------------------------ 安装动作
def detect_running() -> list:
    """返回正在运行的 CodeForge 组件名：引擎 / 服务 / 悬浮窗。"""
    found = []
    try:
        import psutil
        for p in psutil.process_iter(["name", "cmdline"]):
            try:
                nm = (p.info.get("name") or "").lower()
                cl = " ".join(p.info.get("cmdline") or [])
                if nm == "llama-server.exe":
                    found.append(f"引擎 PID {p.pid}")
                elif "hud.py" in cl and "python" in nm:
                    found.append(f"悬浮窗 PID {p.pid}")
            except Exception:
                continue
        for c in psutil.net_connections(kind="inet"):
            if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port == 8765:
                found.append(f"本地服务 PID {c.pid}")
    except Exception:
        out = subprocess.run(["tasklist", "/fi", "imagename eq llama-server.exe"], capture_output=True,
                             text=True, creationflags=subprocess.CREATE_NO_WINDOW).stdout
        if "llama-server" in out.lower():
            found.append("引擎")
    return found


def stop_running(log=None):
    if log:
        log("先关闭正在运行的 CodeForge…")
    subprocess.run(["taskkill", "/f", "/im", "llama-server.exe"], capture_output=True,
                   creationflags=subprocess.CREATE_NO_WINDOW)
    ps = ("Get-NetTCPConnection -LocalPort 8765,8767 -ErrorAction SilentlyContinue | ForEach-Object "
          "{ Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True,
                   creationflags=subprocess.CREATE_NO_WINDOW)
    ps2 = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*hud.py*' } | "
           "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps2], capture_output=True,
                   creationflags=subprocess.CREATE_NO_WINDOW)
    time.sleep(2)


def make_shortcut(target_bat: Path, lnk: Path, icon: Path, desc: str):
    ps = (
        "$s=(New-Object -ComObject WScript.Shell);"
        f"$l=$s.CreateShortcut('{lnk}');"
        f"$l.TargetPath='{target_bat}';"
        f"$l.WorkingDirectory='{target_bat.parent}';"
        f"$l.IconLocation='{icon},0';"
        "$l.WindowStyle=7;"
        f"$l.Description='{desc}';"
        "$l.Save()"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True,
                   creationflags=subprocess.CREATE_NO_WINDOW)


def write_uninstaller(target: Path, registry: bool = True):
    bat = target / "卸载 CodeForge.bat"
    desk = Path(os.environ["USERPROFILE"]) / "Desktop" / f"{APP} 铸码工坊.lnk"
    smlnk = Path(os.environ["APPDATA"]) / r"Microsoft\Windows\Start Menu\Programs" / f"{APP} 铸码工坊.lnk"
    lines = [
        "@echo off",
        "chcp 65001 >nul",
        # 复制自身到 temp 再执行，才能把自己所在的目录删干净
        'if /i "%~1"=="--run" goto run',
        'copy "%~f0" "%TEMP%\\CodeForge_uninstall.bat" >nul 2>nul',
        'start "" /min "%TEMP%\\CodeForge_uninstall.bat" --run',
        "exit /b",
        "",
        ":run",
        "echo 正在卸载 CodeForge...",
        "taskkill /f /im llama-server.exe >nul 2>nul",
        'powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort 8765,8767 -ErrorAction SilentlyContinue '
        '| ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }"',
        'powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '
        '\'*hud.py*\' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"',
        'powershell -NoProfile -Command "Remove-Item '
        "'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\CodeForge'"
        ' -Recurse -Force -ErrorAction SilentlyContinue"',
        f'powershell -NoProfile -Command "Remove-Item \'{desk}\',\'{smlnk}\' -Force -ErrorAction SilentlyContinue"',
        "timeout /t 2 >nul",
        f'rmdir /s /q "{target}"',
        'del "%~f0"',
        "",
    ]
    bat.write_text("\n".join(lines), encoding="utf-8", newline="\r\n")
    if not registry:
        return
    key = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\CodeForge"
    subprocess.run(["reg", "add", key, "/v", "DisplayName", "/t", "REG_SZ", "/d", TITLE, "/f"],
                   capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    subprocess.run(["reg", "add", key, "/v", "DisplayVersion", "/t", "REG_SZ", "/d", VER, "/f"],
                   capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    subprocess.run(["reg", "add", key, "/v", "InstallLocation", "/t", "REG_SZ", "/d", str(target), "/f"],
                   capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    subprocess.run(["reg", "add", key, "/v", "UninstallString", "/t", "REG_SZ", "/d", str(bat), "/f"],
                   capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    subprocess.run(["reg", "add", key, "/v", "DisplayIcon", "/t", "REG_SZ",
                    "/d", str(target / "assets" / "codeforge.ico"), "/f"],
                   capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)


def download_model(model, dest: Path, cb):
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / model["file"]
    if target.exists():
        cb(1.0, "本地已有该模型，跳过下载")
        return target
    url = f"https://modelscope.cn/models/{model['repo']}/resolve/master/{model['file']}"
    tmp = target.with_suffix(".part")
    req = urllib.request.Request(url, headers={"User-Agent": "CodeForge-Setup/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = r.read(1024 * 512)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            cb(done / total if total else 0, f"下载模型 {done/1e9:.2f}/{total/1e9:.2f} GB（{done/total*100:.0f}%）")
    tmp.replace(target)
    return target


# ------------------------------------------------------------------ 安装主流程
def do_install(target: Path, model: dict, opt_model: bool, opt_desktop: bool, opt_startmenu: bool,
               log, progress, registry: bool = True) -> Path:
    log(f"安装目录：{target}")
    target.mkdir(parents=True, exist_ok=True)
    running = detect_running()
    if running:
        log("检测到 CodeForge 正在运行（" + "、".join(running) + "），先关闭以免文件被占用")
        stop_running()
    log("解包程序文件…")
    progress(0.08)
    if not PAYLOAD.exists():
        raise RuntimeError("安装包损坏：找不到 payload.zip")
    with zipfile.ZipFile(PAYLOAD) as z:
        for member in z.infolist():
            try:
                z.extract(member, target)
            except PermissionError:
                raise RuntimeError(f"文件被占用解不开：{member.filename}（请先完全退出 CodeForge 再安装）")
    log(f"程序文件已解包到 {target}")
    progress(0.45)

    mf = target / "models" / model["file"]
    if opt_model:
        if mf.exists():
            log("模型已存在，跳过下载")
        else:
            log(f"开始下载 {model['name']}（{model['gb']} GB）…")
            download_model(model, target / "models",
                           lambda p, s: (progress(0.45 + 0.5 * p), log(s)))
    else:
        log("跳过模型下载，首次启动时会用「模型库」补上")

    cfg = target / "config.json"
    try:
        c = json.loads(cfg.read_text(encoding="utf-8"))
    except Exception:
        c = {}
    c["model_file"] = model["file"]
    cfg.write_text(json.dumps(c, ensure_ascii=False, indent=2), encoding="utf-8")

    progress(0.96)
    log("建快捷方式 / 写卸载信息…")
    icon = target / "assets" / "codeforge.ico"
    bat = target / "启动 CodeForge.bat"
    desc = f"{TITLE} · 本地 AI 代码工坊 + 电脑 Agent"
    if opt_desktop:
        make_shortcut(bat, Path(os.environ["USERPROFILE"]) / "Desktop" / f"{APP} 铸码工坊.lnk", icon, desc)
    if opt_startmenu:
        sm = Path(os.environ["APPDATA"]) / r"Microsoft\Windows\Start Menu\Programs"
        make_shortcut(bat, sm / f"{APP} 铸码工坊.lnk", icon, desc)
    write_uninstaller(target, registry=registry)
    progress(1.0)
    log("安装完成")
    return target


# ------------------------------------------------------------------ 界面
class Setup(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{TITLE} 安装程序 v{VER}")
        self.configure(bg=BG)
        self.resizable(False, False)
        w, h = 820, 620
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"{w}x{h}+{(sw-w)//2}+{max(0,(sh-h)//2-40)}")
        self.hw = probe_hw()
        self.rec = pick_model(self.hw)
        self.model_var = tk.StringVar(value=self.rec["id"])
        self.dir_var = tk.StringVar(value=DEFAULT_DIR)
        self.opt_desktop = tk.BooleanVar(value=True)
        self.opt_startmenu = tk.BooleanVar(value=True)
        self.opt_model = tk.BooleanVar(value=True)
        self._busy = False
        self._frame = tk.Frame(self, bg=BG)
        self._frame.pack(fill="both", expand=True)
        self.page_welcome()

    def clear(self):
        for w in self._frame.winfo_children():
            w.destroy()

    def _title(self, text, sub=""):
        tk.Label(self._frame, text=text, bg=BG, fg=FG, font=("Microsoft YaHei UI", 18, "bold")).pack(
            anchor="w", padx=28, pady=(24, 2))
        if sub:
            tk.Label(self._frame, text=sub, bg=BG, fg=MUT, font=("Microsoft YaHei UI", 9)).pack(anchor="w", padx=28)

    # ---------------- 第一页
    def page_welcome(self):
        self.clear()
        # 先把底部操作栏占好位置，避免控件过多被挤掉
        bar = tk.Frame(self._frame, bg=BG)
        bar.pack(side="bottom", fill="x", padx=26, pady=(6, 14))
        tk.Button(bar, text="开始安装", command=self.start_install, bg=ACC, fg="white", relief="flat",
                  font=("Microsoft YaHei UI", 10, "bold"), padx=26, pady=8).pack(side="right")
        tk.Button(bar, text="取消", command=self.destroy, bg=CARD, fg=FG, relief="flat",
                  font=("Microsoft YaHei UI", 10), padx=18, pady=8).pack(side="right", padx=(0, 10))
        tk.Label(bar, text=f"需要约 {self.rec['gb'] + 0.2:.1f} GB 磁盘空间", bg=BG, fg=MUT,
                 font=("Microsoft YaHei UI", 8)).pack(side="left")

        self._title("安装 CodeForge 铸码工坊",
                    "完全本地运行的 AI 代码工坊 + 电脑 Agent，不需要任何 API Key")
        hw = self.hw
        info = f"检测到：{hw['gpu'] or '无独显'} · 显存 {hw['vram_mb']} MB · 内存 {hw['ram_gb']} GB"
        tk.Label(self._frame, text=info, bg=BG, fg=ACC, font=("Microsoft YaHei UI", 9)).pack(
            anchor="w", padx=28, pady=(4, 10))

        row = tk.Frame(self._frame, bg=BG)
        row.pack(fill="x", padx=28)
        tk.Label(row, text="安装位置", bg=BG, fg=MUT, font=("Microsoft YaHei UI", 9)).pack(anchor="w")
        line = tk.Frame(row, bg=BG)
        line.pack(fill="x", pady=(2, 0))
        e = tk.Entry(line, textvariable=self.dir_var, bg=CARD, fg=FG, insertbackground=FG,
                     relief="flat", font=("Microsoft YaHei UI", 9))
        e.pack(side="left", fill="x", expand=True, ipady=5)
        tk.Button(line, text="浏览…", command=self.browse, bg=CARD, fg=FG, relief="flat",
                  font=("Microsoft YaHei UI", 9), padx=12).pack(side="left", padx=(8, 0))

        tk.Label(self._frame, text="模型（按你的显卡自动推荐，装完还能在「模型库」里换）", bg=BG, fg=MUT,
                 font=("Microsoft YaHei UI", 9)).pack(anchor="w", padx=28, pady=(12, 3))
        box = tk.Frame(self._frame, bg=CARD)
        box.pack(fill="x", padx=28, ipady=4)
        for m in MODELS:
            budget = max(0, self.hw["vram_mb"] - 620) / 1024
            fit = "全量进显存" if m["gb"] * 1.03 <= budget else ("部分卸载·慢" if m["gb"] * 1.03 <= budget * 2.6 else "显存不足")
            tag = " ← 推荐" if m["id"] == self.rec["id"] else ""
            tk.Radiobutton(box, text=f"{m['name']}  ({m['gb']} GB · {fit}){tag}",
                           variable=self.model_var, value=m["id"], bg=CARD, fg=FG,
                           selectcolor="#2a3040", activebackground=CARD, activeforeground=FG,
                           font=("Microsoft YaHei UI", 9), anchor="w").pack(fill="x", padx=8)

        opts = tk.Frame(self._frame, bg=BG)
        opts.pack(fill="x", padx=28, pady=(10, 0))
        for var, text in ((self.opt_desktop, "创建桌面快捷方式"),
                          (self.opt_startmenu, "创建开始菜单项"),
                          (self.opt_model, "安装时下载所选模型（不勾则稍后首次启动再下载）")):
            tk.Checkbutton(opts, text=text, variable=var, bg=BG, fg=MUT, selectcolor=CARD,
                           activebackground=BG, activeforeground=FG,
                           font=("Microsoft YaHei UI", 9), anchor="w").pack(fill="x")

    def browse(self):
        d = filedialog.askdirectory(initialdir="D:\\APP" if Path("D:\\APP").exists() else "C:\\")
        if d:
            self.dir_var.set(str(Path(d) / APP if Path(d).name != APP else d))

    # ---------------- 安装
    def start_install(self):
        if self._busy:
            return
        target = Path(self.dir_var.get().strip())
        if target.drive.upper() == "C:":
            if not messagebox.askyesno("装到 C 盘？", "建议装在 D 盘（模型动辄几个 GB）。仍然继续吗？"):
                return
        if target.exists() and any(target.iterdir()) and not messagebox.askyesno(
                "目录非空", f"{target} 已存在且有文件，继续会覆盖同名文件。继续吗？"):
            return
        self._busy = True
        self.page_progress()
        threading.Thread(target=self.install_worker, args=(target,), daemon=True).start()

    def page_progress(self):
        self.clear()
        self._title("正在安装", "解包程序文件 → 下载模型 → 建快捷方式")
        self.lbl = tk.Label(self._frame, text="准备中…", bg=BG, fg=MUT,
                            font=("Microsoft YaHei UI", 9), anchor="w")
        self.lbl.pack(fill="x", padx=28, pady=(16, 6))
        self.bar = tk.Canvas(self._frame, height=8, bg="#20242f", highlightthickness=0)
        self.bar.pack(fill="x", padx=28)
        self.fill = self.bar.create_rectangle(0, 0, 0, 8, fill=ACC, outline="")
        self.logbox = tk.Text(self._frame, height=9, bg="#0f131c", fg="#9fb0c8", relief="flat",
                              font=("Consolas", 8))
        self.logbox.pack(fill="both", expand=True, padx=28, pady=12)

    def log(self, s):
        self.logbox.insert("end", s + "\n")
        self.logbox.see("end")
        self.lbl.config(text=s)

    def progress(self, pct):
        w = self.bar.winfo_width() or 660
        self.bar.coords(self.fill, 0, 0, w * max(0, min(1, pct)), 8)

    def install_worker(self, target: Path):
        try:
            model = pick_model(self.hw, self.model_var.get())
            do_install(target, model, self.opt_model.get(), self.opt_desktop.get(),
                       self.opt_startmenu.get(), self.log, self.progress)
            self._uninstall_ready = True
            self.after(200, lambda: self.page_done(target))
        except Exception as e:
            self.log(f"安装失败：{e}")
            self.after(200, lambda: messagebox.showerror("安装失败", str(e)))
            self._busy = False

    # ---------------- 完成页
    def page_done(self, target: Path):
        self._busy = False
        self.clear()
        self._title("安装完成", f"已装到 {target}")
        tk.Label(self._frame, text="双击桌面「CodeForge 铸码工坊」就能用；"
                                   "首次启动会加载模型，右上角指示灯变绿即可。",
                 bg=BG, fg=MUT, justify="left", font=("Microsoft YaHei UI", 9),
                 wraplength=620).pack(anchor="w", padx=28, pady=(14, 0))
        bar = tk.Frame(self._frame, bg=BG)
        bar.pack(side="bottom", fill="x", padx=28, pady=18)

        def launch():
            subprocess.Popen(["cmd", "/c", "start", "", str(target / "启动 CodeForge.bat")],
                             creationflags=subprocess.DETACHED_PROCESS)
            self.destroy()

        tk.Button(bar, text="立即启动", command=launch, bg=ACC, fg="white", relief="flat",
                  font=("Microsoft YaHei UI", 10, "bold"), padx=24, pady=8).pack(side="right")
        tk.Button(bar, text="完成", command=self.destroy, bg=CARD, fg=FG, relief="flat",
                  font=("Microsoft YaHei UI", 10), padx=24, pady=8).pack(side="right", padx=(0, 10))


def cli_silent(argv) -> int:
    """无界面安装：--silent --dir=... [--model=id] [--no-model] [--no-desktop]
    [--no-startmenu] [--no-reg]"""
    kv = {}
    for a in argv:
        if a.startswith("--"):
            k, _, v = a[2:].partition("=")
            kv[k] = v or "1"
    target = Path(kv.get("dir") or DEFAULT_DIR)
    hw = probe_hw()
    model = pick_model(hw, kv.get("model") or None)
    print(f"[hw] {hw} -> model {model['id']}", flush=True)
    try:
        do_install(target, model, "no-model" not in kv, "no-desktop" not in kv,
                   "no-startmenu" not in kv, lambda s: print(s, flush=True),
                   lambda p: None, registry="no-reg" not in kv)
        print("SILENT_INSTALL_OK", flush=True)
        return 0
    except Exception as e:
        print(f"SILENT_INSTALL_FAIL {e}", flush=True)
        return 1


if __name__ == "__main__":
    if "--silent" in sys.argv:
        sys.exit(cli_silent(sys.argv[1:]))
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass
    app = Setup()
    app.mainloop()
