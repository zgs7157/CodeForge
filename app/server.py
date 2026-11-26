"""CodeForge 本地服务：GUI + 铸造任务调度 + 模型进程管理。全部走本机，不需要任何 API Key。"""
from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import traceback
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parent))

import forge
import llm
import agent as agent_mod
import catalog as catalog_mod
from forge import Forge, LEVELS, LANGS

ROOT = Path(__file__).resolve().parent.parent
WEB = Path(__file__).resolve().parent / "web"
PROJECTS = ROOT / "projects"
HTTP_PORT = int(os.environ.get("CODEFORGE_WEB_PORT", "8765"))

TASKS: dict[str, dict] = {}
AGENTS: dict[str, object] = {}
APPROVALS: dict[str, dict] = {}
ENGINE = {"state": "idle", "message": "未启动", "model": ""}

# ---------------------------------------------------------------- 悬浮窗状态
HUD = {"mode": "idle", "title": "", "step": 0, "total": 0, "message": "",
       "status": "idle", "t0": 0.0, "elapsed": 0.0}
SWITCH = {"id": None, "phase": "idle", "pct": 0.0, "msg": ""}
CODE_TOTAL = 9
CODE_STEPS = {"plan": 1, "generate": 2, "project": 3, "static": 4,
              "testgen": 5, "test": 6, "repair": 7, "review": 8, "done": 9}


def hud_start(mode, title, total):
    HUD.update({"mode": mode, "title": (title or "")[:80], "step": 0,
                "total": total or CODE_TOTAL, "message": "准备中…",
                "status": "running", "t0": time.time(), "elapsed": 0.0})


def hud_step(step, message, status="running"):
    HUD.update({"step": step, "message": (message or "")[:120], "status": status})


def hud_finish(status, message):
    HUD.update({"status": status, "message": (message or "")[:120],
                "mode": HUD.get("mode", "idle") if status != "ok" else HUD.get("mode", "idle"),
                "elapsed": round(time.time() - (HUD.get("t0") or time.time()), 1)})


def hud_snapshot():
    el = (time.time() - HUD["t0"]) if HUD.get("status") in ("running", "pending") and HUD.get("t0") else HUD.get("elapsed", 0)
    return {"mode": HUD.get("mode", "idle"), "title": HUD.get("title", ""),
            "step": HUD.get("step", 0), "total": HUD.get("total", 0),
            "message": HUD.get("message", ""), "status": HUD.get("status", "idle"),
            "elapsed": round(el or 0, 1), "engine": ENGINE["state"]}


def engine_boot(log=print):
    ENGINE["state"] = "loading"
    ENGINE["message"] = "正在加载本地模型…"
    try:
        msg = llm.SERVER.ensure(log=log)
        llm.SERVER.warm(log=log)
        ENGINE["state"] = "ready"
        ENGINE["message"] = msg
        ENGINE["model"] = llm.model_names()[0] if llm.model_names() else ""
    except Exception as e:
        ENGINE["state"] = "error"
        ENGINE["message"] = str(e)[:800]


def run_model_switch(mid):
    """下载（可选）→ 写配置 → 重启引擎。全过程进度同步到悬浮窗和 GUI 模型面板。"""
    entry = next((e for e in catalog_mod.CATALOG if e["id"] == mid), None)
    if not entry:
        SWITCH.update({"id": mid, "phase": "error", "msg": "没有这个模型"})
        return
    label = f"{entry['name']} {entry['quant']}"
    hud_start("code", f"切换模型：{label}", 3)
    hud_step(1, f"准备 {label}")
    try:
        if entry["file"] not in catalog_mod.local_files():
            SWITCH.update({"id": mid, "phase": "download", "pct": 0.0, "msg": "开始下载…"})

            def prog(done, total):
                pct = (done / total * 100) if total else 0
                SWITCH.update({"pct": round(pct, 1),
                               "msg": f"下载中 {done/1e9:.2f}/{total/1e9:.2f} GB"})
                hud_step(1, f"下载 {label} {pct:.0f}%")

            path = catalog_mod.download(entry, prog)
        else:
            path = catalog_mod.MODELS / entry["file"]
            SWITCH.update({"id": mid, "phase": "download", "pct": 100.0, "msg": "本地已有，跳过下载"})
        SWITCH.update({"phase": "apply", "msg": "写入配置…"})
        hud_step(2, "写入配置…")
        catalog_mod.apply_model(entry)
        SWITCH.update({"phase": "restart", "msg": "重启推理引擎（约 30 秒）…"})
        hud_step(3, "重启推理引擎…")
        llm.SERVER.stop()
        time.sleep(1.5)
        msg = llm.SERVER.ensure()
        llm.SERVER.warm()
        names = llm.model_names() or []
        if names and entry["file"] not in " ".join(names):
            raise RuntimeError(f"引擎加载的不是 {entry['file']}（实际：{', '.join(names)}）")
        ENGINE.update({"state": "ready", "message": msg, "model": str(path)})
        SWITCH.update({"phase": "done", "pct": 100.0, "msg": f"已切换到 {label}"})
        hud_finish("ok", f"模型已切换到 {label}")
    except Exception as e:
        ENGINE.update({"state": "error", "message": str(e)[:400]})
        SWITCH.update({"phase": "error", "msg": str(e)[:200]})
        hud_finish("fail", "模型切换失败")


def open_hud():
    """拉起悬浮窗进程（pythonw，无控制台）。重复拉起会因端口占用自行退出。"""
    try:
        hud = Path(__file__).resolve().parent / "hud.py"
        pw = Path(sys.executable).with_name("pythonw.exe")
        exe = str(pw) if pw.exists() else str(sys.executable)
        flags = getattr(subprocess, "DETACHED_PROCESS", 0)
        subprocess.Popen([exe, "-X", "utf8", str(hud)], close_fds=True,
                         creationflags=flags, cwd=str(ROOT))
        return True
    except Exception:
        return False


def project_list():
    items = []
    if PROJECTS.exists():
        for p in sorted(PROJECTS.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if p.is_dir():
                meta = {}
                mf = p / "codeforge.json"
                if mf.exists():
                    try:
                        meta = json.loads(mf.read_text(encoding="utf-8"))
                    except Exception:
                        meta = {}
                items.append({
                    "name": p.name, "path": str(p),
                    "time": time.strftime("%Y-%m-%d %H:%M", time.localtime(p.stat().st_mtime)),
                    "level": meta.get("level_label", ""),
                    "tests": meta.get("tests", {}),
                    "zip": (p.with_suffix(".zip").exists()),
                })
    return items


def run_task(task_id, requirement, language, level):
    t = TASKS[task_id]
    q = t["q"]

    def emit(ev):
        q.put(ev)
        idx = CODE_STEPS.get(ev.get("phase"))
        if idx:
            hud_step(idx, ev.get("message", ""),
                     "fail" if ev.get("phase") == "error" else "running")

    hud_start("code", requirement, CODE_TOTAL)
    try:
        if llm.health() is None:
            q.put({"phase": "log", "status": "info", "message": "推理引擎未就绪，先加载模型…"})
            llm.SERVER.ensure(log=lambda m: q.put({"phase": "log", "status": "info", "message": m}))
        f = Forge(requirement, language=language, level=level, emit=emit)
        t["forge"] = f
        result = f.run()
        t["result"] = result
        hud_finish("ok", f"铸造完成 · {result.get('elapsed', 0)}s · {result.get('level_label', '')}")
        q.put({"phase": "result", "status": "ok", "message": "铸造完成", "result": result})
    except Exception as e:
        tb = traceback.format_exc()
        t["error"] = str(e)
        hud_finish("fail", "铸造出错：" + str(e)[:80])
        q.put({"phase": "error", "status": "fail", "message": str(e), "detail": tb[-2500:]})
    finally:
        t["finished"] = True
        q.put({"phase": "__end__", "status": "ok", "message": ""})


def run_agent(task_id, goal, level, policy):
    t = TASKS[task_id]
    q = t["q"]

    def emit(ev):
        q.put(ev)
        ph, msg = ev.get("phase"), ev.get("message", "")
        if ph == "plan":
            hud_step(0, msg)
        elif ph == "generate":
            m = re.search(r"第\s*(\d+)\s*/\s*(\d+)\s*步", msg)
            if m:
                HUD["total"] = int(m.group(2))
                hud_step(int(m.group(1)), msg)
        elif ph == "thought":
            hud_step(HUD.get("step", 0), msg)
        elif ph == "action":
            hud_step(HUD.get("step", 0), msg,
                     "fail" if ev.get("status") == "fail" else "running")
        elif ph == "approval":
            hud_step(HUD.get("step", 0), "等你批准：" + msg, "pending")
        elif ph == "done":
            hud_step(HUD.get("step", 0), msg, "ok")

    hud_start("agent", goal, agent_mod.MAX_STEPS.get(level, 6))

    def approver(action, args):
        """把动作挂起，等前端点允许/拒绝。"""
        rid = uuid.uuid4().hex[:10]
        ev = threading.Event()
        APPROVALS[rid] = {"event": ev, "decision": None, "task": task_id}
        q.put({"phase": "approval", "status": "pending", "message": f"请求批准：{action}",
               "request": {"id": rid, "action": action,
                           "args": {k: (str(v)[:500]) for k, v in (args or {}).items()}}})
        ev.wait(timeout=900)
        info = APPROVALS.pop(rid, {})
        return info.get("decision") == "allow"

    try:
        if llm.health() is None:
            q.put({"phase": "log", "status": "info", "message": "推理引擎未就绪，先加载模型…"})
            llm.SERVER.ensure(log=lambda m: q.put({"phase": "log", "status": "info", "message": m}))
        ag = agent_mod.Agent(goal, level=level, emit=emit, approver=approver, policy=policy)
        AGENTS[task_id] = ag
        t["agent"] = ag
        res = ag.run()
        t["result"] = res
        hud_finish("ok" if res.get("ok") else "fail", res.get("summary", "")[:110])
        q.put({"phase": "result", "status": "ok", "message": "Agent 结束", "result": res})
    except Exception as e:
        tb = traceback.format_exc()
        t["error"] = str(e)
        hud_finish("fail", "Agent 出错：" + str(e)[:80])
        q.put({"phase": "error", "status": "fail", "message": str(e), "detail": tb[-2500:]})
    finally:
        t["finished"] = True
        q.put({"phase": "__end__", "status": "ok", "message": ""})


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "CodeForge/1.0"

    def log_message(self, fmt, *args):
        pass

    # ------------------------------------------------------------ 工具
    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_file_bytes(self, data: bytes, ctype: str, download_name=None):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if download_name:
            self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
        self.end_headers()
        self.wfile.write(data)

    def read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    # ------------------------------------------------------------ GET
    def do_GET(self):
        u = urlparse(self.path)
        path, qs = u.path, parse_qs(u.query)
        try:
            if path in ("/", "/index.html"):
                return self.send_file_bytes((WEB / "index.html").read_bytes(), "text/html; charset=utf-8")
            if path == "/favicon.ico":
                fav = WEB / "favicon.ico"
                if fav.exists():
                    return self.send_file_bytes(fav.read_bytes(), "image/x-icon")
                return self.send_json({"error": "no icon"}, 404)
            if path.startswith("/static/"):
                name = path[len("/static/"):].replace("..", "")
                f = WEB / name
                if f.exists():
                    ctype = {"css": "text/css", "js": "application/javascript"}.get(f.suffix.lstrip("."), "text/plain")
                    return self.send_file_bytes(f.read_bytes(), ctype + "; charset=utf-8")
                return self.send_json({"error": "not found"}, 404)
            if path == "/api/models":
                try:
                    data = catalog_mod.recommend()
                except Exception as e:
                    return self.send_json({"error": str(e)}, 500)
                data["switch"] = SWITCH
                data["engine"] = ENGINE
                return self.send_json(data)
            if path == "/api/hud":
                snap = hud_snapshot()
                snap["online"] = True
                return self.send_json(snap)
            if path == "/api/state":
                return self.send_json({
                    "engine": ENGINE,
                    "levels": LEVELS,
                    "languages": LANGS,
                    "projects": project_list(),
                    "gpu": self.gpu_info(),
                })
            if path == "/api/events":
                return self.stream_events(qs.get("id", [""])[0])
            if path == "/api/file":
                return self.api_file(qs)
            if path == "/api/download":
                return self.api_download(qs)
            if path == "/api/projects":
                return self.send_json({"projects": project_list()})
            return self.send_json({"error": "unknown"}, 404)
        except BrokenPipeError:
            pass
        except Exception as e:
            try:
                self.send_json({"error": str(e)}, 500)
            except Exception:
                pass

    def gpu_info(self):
        try:
            out = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.used,memory.total",
                                  "--format=csv,noheader,nounits"], capture_output=True, text=True,
                                 timeout=6, encoding="utf-8", errors="ignore")
            if out.returncode == 0 and out.stdout.strip():
                name, used, total = [x.strip() for x in out.stdout.strip().splitlines()[0].split(",")]
                return {"name": name, "used": int(used), "total": int(total)}
        except Exception:
            pass
        return {}

    def stream_events(self, task_id):
        task = TASKS.get(task_id)
        if not task:
            return self.send_json({"error": "任务不存在"}, 404)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        q = task["q"]
        # 先补发历史事件，刷新页面也不会丢
        history = task.get("history", [])
        for ev in history:
            self.wfile.write(b"data: " + json.dumps(ev, ensure_ascii=False).encode("utf-8") + b"\n\n")
        self.wfile.flush()
        while True:
            try:
                ev = q.get(timeout=20)
            except queue.Empty:
                try:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                except Exception:
                    return
                continue
            if ev.get("phase") == "__end__":
                return
            task["history"].append(ev)
            try:
                self.wfile.write(b"data: " + json.dumps(ev, ensure_ascii=False).encode("utf-8") + b"\n\n")
                self.wfile.flush()
            except Exception:
                return

    def _project_dir(self, qs) -> Path | None:
        name = (qs.get("project", [""])[0] or "").strip()
        if not name or "/" in name or "\\" in name or ".." in name:
            return None
        p = PROJECTS / name
        return p if p.is_dir() else None

    def api_file(self, qs):
        pdir = self._project_dir(qs)
        if not pdir:
            return self.send_json({"error": "项目不存在"}, 404)
        rel = (qs.get("path", [""])[0] or "").strip()
        target = (pdir / rel).resolve()
        if not str(target).startswith(str(pdir.resolve())) or not target.is_file():
            return self.send_json({"error": "文件不存在"}, 404)
        data = target.read_text(encoding="utf-8", errors="ignore")
        return self.send_json({"path": rel, "content": data, "size": target.stat().st_size})

    def api_download(self, qs):
        pdir = self._project_dir(qs)
        if not pdir:
            return self.send_json({"error": "项目不存在"}, 404)
        zp = pdir.with_suffix(".zip")
        if not zp.exists():
            return self.send_json({"error": "还没生成压缩包"}, 404)
        return self.send_file_bytes(zp.read_bytes(), "application/zip", zp.name)

    # ------------------------------------------------------------ POST
    def do_POST(self):
        u = urlparse(self.path)
        path, qs = u.path, parse_qs(u.query)
        try:
            if path == "/api/run":
                body = self.read_body()
                req = (body.get("requirement") or "").strip()
                if not req:
                    return self.send_json({"error": "需求不能为空"}, 400)
                if ENGINE["state"] != "ready":
                    threading.Thread(target=engine_boot, daemon=True).start()
                tid = uuid.uuid4().hex[:10]
                TASKS[tid] = {"id": tid, "q": queue.Queue(), "history": [], "finished": False,
                              "created": time.time(), "forge": None, "result": None,
                              "requirement": req}
                threading.Thread(target=run_task,
                                 args=(tid, req, body.get("language") or "auto", body.get("level") or "standard"),
                                 daemon=True).start()
                return self.send_json({"id": tid})
            if path == "/api/agent":
                body = self.read_body()
                goal = (body.get("goal") or "").strip()
                if not goal:
                    return self.send_json({"error": "目标不能为空"}, 400)
                tid = uuid.uuid4().hex[:10]
                TASKS[tid] = {"id": tid, "q": queue.Queue(), "history": [], "finished": False,
                              "created": time.time(), "agent": None, "result": None,
                              "requirement": goal, "mode": "agent"}
                threading.Thread(target=run_agent,
                                 args=(tid, goal, body.get("level") or "standard",
                                       body.get("policy") or "ask"), daemon=True).start()
                return self.send_json({"id": tid})
            if path == "/api/agent/approve":
                body = self.read_body()
                rid = (body.get("id") or "").strip()
                info = APPROVALS.get(rid)
                if not info:
                    return self.send_json({"error": "该请求已失效"}, 404)
                info["decision"] = "allow" if body.get("decision") == "allow" else "deny"
                if body.get("allow_all"):
                    info["allow_all"] = True
                    ag = AGENTS.get(info.get("task"))
                    if ag is not None:
                        ag.allow_all = True
                info["event"].set()
                return self.send_json({"ok": True})
            if path == "/api/hud/open":
                open_hud()
                return self.send_json({"ok": True})
            if path == "/api/model/switch":
                body = self.read_body()
                mid = body.get("id")
                if not mid:
                    return self.send_json({"error": "缺少模型 id"}, 400)
                threading.Thread(target=run_model_switch, args=(mid,), daemon=True).start()
                return self.send_json({"ok": True, "id": mid})
            if path == "/api/model":
                if llm.health() is None:
                    ENGINE.update({"state": "loading", "message": "正在加载本地模型…"})
                    threading.Thread(target=engine_boot, daemon=True).start()
                else:
                    ENGINE.update({"state": "ready",
                                   "message": f"推理引擎已在运行（{', '.join(llm.model_names()) or '模型已加载'}）",
                                   "model": llm.model_names()[0] if llm.model_names() else ENGINE.get("model", "")})
                return self.send_json({"ok": True, "engine": ENGINE})
            if path == "/api/model/stop":
                llm.SERVER.stop()
                ENGINE.update({"state": "idle", "message": "已停止", "model": ""})
                return self.send_json({"ok": True})
            if path == "/api/open":
                pdir = self._project_dir(qs)
                if not pdir:
                    return self.send_json({"error": "项目不存在"}, 404)
                os.startfile(str(pdir))  # noqa: S606
                return self.send_json({"ok": True})
            if path == "/api/run_file":
                pdir = self._project_dir(qs)
                body = self.read_body()
                if not pdir:
                    return self.send_json({"error": "项目不存在"}, 404)
                entry = body.get("entry") or "main.py"
                if entry.endswith(".py"):
                    cmd = [sys.executable, "-X", "utf8", entry]
                elif entry.endswith(".js"):
                    cmd = ["node", entry]
                else:
                    return self.send_json({"error": "该文件不支持直接运行"}, 400)
                try:
                    r = subprocess.run(cmd, cwd=str(pdir), capture_output=True, text=True,
                                       encoding="utf-8", errors="ignore", timeout=25)
                    return self.send_json({"ok": True, "code": r.returncode,
                                           "output": ((r.stdout or "") + (r.stderr or ""))[-3000:]})
                except subprocess.TimeoutExpired:
                    return self.send_json({"ok": False, "error": "运行超时，可能是交互式程序"})
            return self.send_json({"error": "unknown"}, 404)
        except Exception as e:
            try:
                self.send_json({"error": str(e)}, 500)
            except Exception:
                pass


def engine_monitor():
    """每 8 秒对齐一次真实状态，避免界面显示「就绪」但其实引擎早挂了。"""
    while True:
        time.sleep(8)
        try:
            alive = llm.health() is not None
            if ENGINE["state"] == "ready" and not alive:
                ENGINE.update({"state": "idle", "message": "推理引擎未运行", "model": ""})
            elif ENGINE["state"] == "idle" and alive:
                names = llm.model_names()
                ENGINE.update({"state": "ready",
                               "message": f"推理引擎已在运行（{', '.join(names) or '模型已加载'}）",
                               "model": names[0] if names else ""})
        except Exception:
            pass


def main():
    PROJECTS.mkdir(parents=True, exist_ok=True)
    if os.environ.get("CODEFORGE_NO_HUD") != "1":
        open_hud()
    threading.Thread(target=engine_boot, daemon=True).start()
    threading.Thread(target=engine_monitor, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", HTTP_PORT), Handler)
    url = f"http://127.0.0.1:{HTTP_PORT}/"
    print(f"CodeForge 已启动：{url}")
    edge = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
    if os.environ.get("CODEFORGE_NO_BROWSER") != "1":
        try:
            if edge.exists():
                subprocess.Popen([str(edge), f"--app={url}", "--window-size=1500,960",
                                  "--disable-features=msEdgeTranslate"], close_fds=True)
            else:
                webbrowser.open(url)
        except Exception:
            webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        llm.SERVER.stop()


if __name__ == "__main__":
    main()
