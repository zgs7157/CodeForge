"""本地大模型接入层：管理 llama.cpp server 进程，并提供 chat 接口。

整个方案不依赖任何云端 API Key，模型在本机显存/内存里跑。
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = ROOT / "models"
LLAMA_DIR = ROOT / "llama"
LOG_DIR = ROOT / "logs"
CONFIG_FILE = ROOT / "config.json"

HOST = "127.0.0.1"
PORT = int(os.environ.get("CODEFORGE_PORT", "18080"))
BASE = f"http://{HOST}:{PORT}"


class LLMError(RuntimeError):
    pass


def read_config() -> dict:
    defaults = {
        "model_file": "",
        "ngl": 99,
        "ctx": 8192,
        "threads": 6,
        "port": PORT,
        "auto_start": True,
    }
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        defaults.update(cfg or {})
    except Exception:
        pass
    return defaults


def save_config(d: dict) -> None:
    CONFIG_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def health(timeout: float = 2.5):
    """返回 None 表示没起来；返回 dict 表示健康。"""
    try:
        with urllib.request.urlopen(BASE + "/health", timeout=timeout) as r:
            body = r.read().decode("utf-8", "ignore")
            try:
                return json.loads(body)
            except Exception:
                return {"status": "ok", "raw": body}
    except Exception:
        return None


def model_names() -> list[str]:
    try:
        with urllib.request.urlopen(BASE + "/v1/models", timeout=3) as r:
            data = json.loads(r.read().decode("utf-8", "ignore"))
        return [m.get("id", "?") for m in data.get("data", [])]
    except Exception:
        return []


def list_gguf() -> list[Path]:
    if not MODELS_DIR.exists():
        return []
    files = [p for p in MODELS_DIR.glob("*.gguf") if p.stat().st_size > 50 * 1024 * 1024]
    return sorted(files, key=lambda p: p.stat().st_size, reverse=True)


def pick_model() -> Path | None:
    cfg = read_config()
    name = (cfg.get("model_file") or "").strip()
    if name:
        for cand in (MODELS_DIR / name, MODELS_DIR / Path(name).name):
            if cand.exists() and cand.stat().st_size > 50 * 1024 * 1024:
                return cand
        # 配置里指的文件没了，退回按体积挑，但不静默
        print(f"[llm] 配置的模型不存在：{name}，改为自动挑选")
    items = list_gguf()
    return items[0] if items else None


def _wait_health(proc: subprocess.Popen, seconds: int, log=print) -> bool:
    t0 = time.time()
    while time.time() - t0 < seconds:
        if proc.poll() is not None:
            log(f"推理进程提前退出，code={proc.returncode}")
            return False
        if health() is not None:
            return True
        time.sleep(1.5)
    return False


class Server:
    """常驻的 llama-server 进程管理器。"""

    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.model: Path | None = None
        self.lock = threading.Lock()

    # ---------------- 进程管理 ----------------
    def ensure(self, log=print) -> str:
        with self.lock:
            if health() is not None:
                return f"推理引擎已在运行（{', '.join(model_names()) or '模型已加载'}）"
            exe = LLAMA_DIR / "llama-server.exe"
            if not exe.exists():
                raise LLMError(f"找不到推理引擎：{exe}，请先运行 安装引擎.bat 或解压 llama 包")
            model = pick_model()
            if model is None:
                raise LLMError("models 目录下没有可用的 .gguf 模型文件")
            cfg = read_config()
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            size_gb = model.stat().st_size / 1024 ** 3
            # 6G 显存：7B Q4 用 4096 上下文更稳，小模型给足上下文
            ctx = int(cfg.get("ctx") or (6144 if size_gb > 3.4 else 8192))
            ngl = int(cfg.get("ngl", 99))
            base_args = [
                str(exe), "-m", str(model),
                "--host", HOST, "--port", str(PORT),
                "-t", str(int(cfg.get("threads") or max(4, (os.cpu_count() or 4) - 1))),
                "-c", str(ctx),
                "-b", "512", "-ub", "512",
                "--repeat-penalty", "1.05",
                "-np", "1",
            ]
            attempts = [(ngl, ctx), (max(8, ngl // 3), min(ctx, 4096))]
            last_err = ""
            for idx, (g, c) in enumerate(attempts, 1):
                log(f"启动推理引擎（GPU 层 {g}，上下文 {c}）…")
                logf = open(LOG_DIR / f"llama-server-{idx}.log", "a", encoding="utf-8", errors="ignore")
                args = list(base_args)
                args[args.index("-c") + 1] = str(c)
                args += ["-ngl", str(g)]
                flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                proc = subprocess.Popen(args, stdout=logf, stderr=subprocess.STDOUT, creationflags=flags)
                if _wait_health(proc, 240, log):
                    self.proc = proc
                    self.model = model
                    log(f"模型加载完成：{model.name}")
                    return f"模型已就绪：{model.name}"
                try:
                    proc.kill()
                except Exception:
                    pass
                tail = ""
                try:
                    tail = (LOG_DIR / f"llama-server-{idx}.log").read_text(encoding="utf-8", errors="ignore")[-1200:]
                except Exception:
                    pass
                last_err = tail
                log("第一次加载失败，降低显存占用重试…" if idx == 1 else "加载失败")
            raise LLMError("推理引擎启动失败。最后日志：\n" + last_err[-900:])

    def _pids_on_port(self) -> list[int]:
        """占用 PORT 的进程（可能是别的入口拉起的 llama-server）。"""
        pids: list[int] = []
        try:
            import psutil
            for c in psutil.net_connections(kind="inet"):
                if c.laddr and getattr(c.laddr, "port", None) == PORT and c.pid:
                    pids.append(c.pid)
        except Exception:
            pids = []
        if not pids:
            try:
                out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                                     timeout=8, creationflags=subprocess.CREATE_NO_WINDOW).stdout
                for line in out.splitlines():
                    parts = line.split()
                    if len(parts) >= 5 and parts[1].startswith(f"127.0.0.1:{PORT}"):
                        pids.append(int(parts[-1]))
            except Exception:
                pass
        return pids

    def stop(self) -> None:
        with self.lock:
            if self.proc and self.proc.poll() is None:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            self.proc = None
            # 兜底：不管谁拉起来的引擎，占着端口就杀掉，否则切换模型等于没切
            for pid in self._pids_on_port():
                try:
                    import psutil
                    name = psutil.Process(pid).name().lower()
                    if "llama" in name:
                        psutil.Process(pid).kill()
                except Exception:
                    pass
        for _ in range(24):
            if health() is None:
                return
            time.sleep(0.5)

    def warm(self, log=print) -> None:
        """空跑一次极短请求，确保权重真的进了显存。"""
        self.chat([{"role": "user", "content": "回复一个字：好"}], max_tokens=8, temperature=0.0, timeout=180)

    # ---------------- 推理 ----------------
    def chat(self, messages, temperature=0.2, top_p=0.9, max_tokens=2048,
             stop=None, timeout=900) -> str:
        payload = {
            "messages": messages,
            "temperature": temperature,
            "top_p": top_p,
            "max_tokens": max_tokens,
            "stream": False,
            "cache_prompt": True,
        }
        if stop:
            payload["stop"] = stop
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            BASE + "/v1/chat/completions",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = json.loads(r.read().decode("utf-8", "ignore"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")[:400]
            raise LLMError(f"推理请求失败 HTTP {e.code}: {detail}")
        except Exception as e:
            raise LLMError(f"推理请求失败：{e}")
        try:
            return body["choices"][0]["message"]["content"]
        except Exception:
            raise LLMError(f"推理返回格式异常：{str(body)[:300]}")


SERVER = Server()
