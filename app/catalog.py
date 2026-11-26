"""硬件探测 + 模型目录 + 自动推荐。

思路：先量出显存/内存/CPU，再从候选 GGUF 目录里挑「能全量塞进显存里质量最高的那个」，
塞不下的降级（部分卸载，速度掉一半以上），显存太小的退到小参数模型。
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parent.parent
MODELS = ROOT / "models"
CONFIG = ROOT / "config.json"

# --------------------------------------------------------------- 探测


def _nvidia():
    exe = Path(r"C:\Windows\System32\nvidia-smi.exe")
    if not exe.exists():
        return None
    try:
        out = subprocess.run([str(exe), "--query-gpu=name,memory.total,driver_version",
                              "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=8).stdout.strip()
        if not out:
            return None
        line = out.splitlines()[0]
        name, vram, drv = [x.strip() for x in line.split(",")]
        return {"name": name, "vram_mb": int(float(vram)), "driver": drv}
    except Exception:
        return None


def probe() -> dict:
    ram = psutil.virtual_memory()
    disk = psutil.disk_usage(str(ROOT)) if ROOT.exists() else None
    cpu = os.environ.get("PROCESSOR_IDENTIFIER", "")
    cores = psutil.cpu_count(logical=True) or 0
    gpu = _nvidia()
    return {
        "gpu": gpu,
        "vram_mb": (gpu or {}).get("vram_mb", 0),
        "ram_gb": round(ram.total / 1e9, 1),
        "ram_free_gb": round(ram.available / 1e9, 1),
        "cpu": cpu,
        "cores": cores,
        "disk_free_gb": round(disk.free / 1e9, 1) if disk else 0,
    }


# --------------------------------------------------------------- 目录
# quality 是相对打分（同档位里越大越强）；size_gb 是 Q4/Q5/Q8 量化后的实际体积
CATALOG = [
    {
        "id": "qwen-coder-1.5b-q4",
        "name": "Qwen2.5-Coder-1.5B",
        "params": "1.5B", "quant": "Q4_K_M", "size_gb": 1.05, "quality": 42,
        "repo": "Qwen/Qwen2.5-Coder-1.5B-Instruct-GGUF",
        "file": "qwen2.5-coder-1.5b-instruct-q4_k_m.gguf",
        "note": "4G 显存以下的保底方案：飞快，但复杂需求容易写错",
    },
    {
        "id": "qwen-coder-3b-q4",
        "name": "Qwen2.5-Coder-3B",
        "params": "3B", "quant": "Q4_K_M", "size_gb": 2.0, "quality": 55,
        "repo": "Qwen/Qwen2.5-Coder-3B-Instruct-GGUF",
        "file": "qwen2.5-coder-3b-instruct-q4_k_m.gguf",
        "note": "8G 内存 / 4G 显存的最优解，速度约为 7B 的两倍",
    },
    {
        "id": "qwen-coder-7b-q4",
        "name": "Qwen2.5-Coder-7B",
        "params": "7B", "quant": "Q4_K_M", "size_gb": 4.47, "quality": 78,
        "repo": "Qwen/Qwen2.5-Coder-7B-Instruct-GGUF",
        "file": "qwen2.5-coder-7b-instruct-q4_k_m.gguf",
        "note": "6G 显存（GTX 1060/1660）的甜点，代码质量与速度平衡",
    },
    {
        "id": "qwen-coder-7b-q5",
        "name": "Qwen2.5-Coder-7B",
        "params": "7B", "quant": "Q5_K_M", "size_gb": 5.34, "quality": 82,
        "repo": "Qwen/Qwen2.5-Coder-7B-Instruct-GGUF",
        "file": "qwen2.5-coder-7b-instruct-q5_k_m.gguf",
        "note": "8G 显存可以全量塞下，比 Q4 略强；6G 卡会很挤",
    },
    {
        "id": "deepseek-v2-lite-q4",
        "name": "DeepSeek-Coder-V2-Lite",
        "params": "16B MoE（激活 2.4B）",
        "quant": "Q4_K_M", "size_gb": 9.6, "quality": 86,
        "repo": "deepseek-ai/DeepSeek-Coder-V2-Lite-Instruct-GGUF",
        "file": "DeepSeek-Coder-V2-Lite-Instruct-Q4_K_M.gguf",
        "note": "MoE 结构，激活参数少所以跑得动，需要 12G 显存才舒服",
    },
    {
        "id": "qwen-coder-14b-q4",
        "name": "Qwen2.5-Coder-14B",
        "params": "14B", "quant": "Q4_K_M", "size_gb": 8.9, "quality": 90,
        "repo": "Qwen/Qwen2.5-Coder-14B-Instruct-GGUF",
        "file": "qwen2.5-coder-14b-instruct-q4_k_m.gguf",
        "note": "12G 显存档位的首选，长任务与自修复明显更稳",
    },
    {
        "id": "qwen-coder-32b-q4",
        "name": "Qwen2.5-Coder-32B",
        "params": "32B", "quant": "Q4_K_M", "size_gb": 19.6, "quality": 97,
        "repo": "Qwen/Qwen2.5-Coder-32B-Instruct-GGUF",
        "file": "qwen2.5-coder-32b-instruct-q4_k_m.gguf",
        "note": "24G 显存才玩得转，本机级别天花板",
    },
]

# 显存里还要留出图形界面/系统占用的余量
VRAM_OVERHEAD_MB = 620


def local_files() -> dict:
    out = {}
    if MODELS.exists():
        for p in MODELS.glob("*.gguf"):
            out[p.name] = round(p.stat().st_size / 1e9, 2)
    return out


def config_model() -> str:
    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
        return cfg.get("model_file", "")
    except Exception:
        return ""


def evaluate(entry, hw) -> dict:
    vram_budget = max(0, hw["vram_mb"] - VRAM_OVERHEAD_MB) / 1024.0   # GB
    need = entry["size_gb"] * 1.03
    if vram_budget <= 0:
        fit, mode = False, "cpu"
    elif need <= vram_budget:
        fit, mode = True, "full"
    elif need <= vram_budget * 2.6:
        fit, mode = False, "partial"
    else:
        fit, mode = False, "no"
    files = local_files()
    installed = entry["file"] in files
    score = entry["quality"] * (1.0 if mode == "full" else 0.62 if mode == "partial" else 0.18)
    # 内存紧张时略微打压大模型（mmap 会反复换页）
    if entry["size_gb"] > hw["ram_gb"] * 0.45:
        score *= 0.82
    return {"fit": fit, "load_mode": mode, "installed": installed, "score": round(score, 1),
            "vram_budget_gb": round(vram_budget, 2)}


def recommend(hw=None) -> dict:
    hw = hw or probe()
    rows = []
    for e in CATALOG:
        ev = evaluate(e, hw)
        rows.append({**{k: e[k] for k in ("id", "name", "params", "quant", "size_gb",
                                           "quality", "repo", "file", "note")}, **ev})
    usable = [r for r in rows if r["load_mode"] in ("full", "partial")]
    best = max(usable, key=lambda r: r["score"]) if usable else min(rows, key=lambda r: r["size_gb"])
    for r in rows:
        r["recommended"] = r["id"] == best["id"]
        r["current"] = Path(config_model()).name == r["file"] if config_model() else False
    rows.sort(key=lambda r: (-r["score"], r["size_gb"]))
    return {"hardware": hw, "recommended": best["id"], "models": rows,
            "current_file": Path(config_model()).name if config_model() else ""}


# --------------------------------------------------------------- 下载
def download(entry, on_progress=None) -> Path:
    """从 ModelScope 拉到 models 目录。on_progress(done_bytes, total_bytes)。"""
    MODELS.mkdir(parents=True, exist_ok=True)
    target = MODELS / entry["file"]
    if target.exists():
        return target
    url = f"https://modelscope.cn/models/{entry['repo']}/resolve/master/{entry['file']}"
    tmp = target.with_suffix(".part")
    req = urllib.request.Request(url, headers={"User-Agent": "CodeForge/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = r.read(1024 * 512)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if on_progress:
                on_progress(done, total)
    if total and tmp.stat().st_size < total:
        raise IOError(f"下载不完整：{tmp.stat().st_size}/{total}")
    tmp.replace(target)
    return target


def apply_model(entry) -> dict:
    """把 config.json 的 model_file 指过去（只改配置，重启引擎由调用方做）。"""
    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception:
        cfg = {}
    cfg["model_file"] = entry["file"]
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "model_file": cfg["model_file"], "at": time.time()}


if __name__ == "__main__":
    print(json.dumps(recommend(), ensure_ascii=False, indent=2))
