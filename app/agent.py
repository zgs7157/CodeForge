"""Computer Agent：让本地模型真正操作这台电脑。

流程是 ReAct 式的：模型每次只输出一个 JSON 动作，执行后把结果回喂，直到 finish。
每个动作都要经过审批闸门（可逐条批准，也可一键全部允许）；危险命令即使全允许也会被拦。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROJECTS_DIR = ROOT / "projects"
PY = sys.executable

MAX_STEPS = {"fast": 4, "standard": 6, "deep": 9, "ultra": 12}

# 这些命令无论是否"全部允许"都必须逐条确认
DANGEROUS = [
    r"\bformat\b", r"\bdiskpart\b", r"\bremove-item\b.*-recurse", r"\bdel\s+/s", r"\brmdir\s+/s",
    r"\breg\s+delete", r"\bshutdown\b", r"\bRestart-Computer\b", r"\bStop-Computer\b",
    r"\bRemove-Item\b.*\bC:\\", r"\bClear-Disk\b", r"\bReset-ComputerMachinePassword\b",
    r"\bnet\s+user\b.*/delete", r"\bicacls\b.*/deny", r"\bbcdedit\b",
]

ACTION_HELP = """可用动作（每次只输出一个 JSON，不要输出别的文字）：
{"thought":"这一步想干什么","action":"shell","args":{"command":"要执行的 PowerShell 命令"}}
{"thought":"...","action":"write_file","args":{"path":"相对路径","content":"文件内容"}}
{"thought":"...","action":"read_file","args":{"path":"相对路径"}}
{"thought":"...","action":"list_dir","args":{"path":"."}}
{"thought":"...","action":"run_python","args":{"path":"main.py","args":["--x","1"]}}
{"thought":"...","action":"open_path","args":{"path":"要打开的文件/文件夹/网址"}}
{"thought":"...","action":"finish","args":{"summary":"最终交待"}}
"""

SYSTEM = (
    "你是运行在用户 Windows 10 电脑上的本地 Agent，可以直接操作这台机器来完成用户的目标。\n"
    "规则：\n"
    "1. 每次只输出一个 JSON 动作，格式严格，不要输出解释、Markdown 或代码块标记。\n"
    "2. shell 用 PowerShell 语法；命令要短、可验证；不要执行会破坏系统的命令。\n"
    "3. 所有文件操作都限定在当前工作目录（相对路径）。\n"
    "4. 做完就调用 finish，并在 summary 里说明做了什么、结果如何。\n"
    "5. 如果某个动作被拒绝，换个更安全的做法，不要重复被拒的动作。\n\n"
    + ACTION_HELP
)


def _slug(text: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text.encode("ascii", "ignore").decode()).strip("-").lower()
    return s[:20] or "agent"


def parse_action(text: str) -> dict | None:
    """从模型输出里抠出第一个完整的 JSON 对象。"""
    t = text.strip()
    t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.M).strip()
    start = t.find("{")
    if start < 0:
        return None
    depth, in_str, esc = 0, False, False
    for i, ch in enumerate(t[start:], start):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(t[start:i + 1])
                except Exception:
                    return None
    return None


class Agent:
    def __init__(self, goal: str, level: str = "standard", emit=None, approver=None, policy="ask"):
        self.goal = (goal or "").strip()
        self.max_steps = MAX_STEPS.get(level, 6)
        self.level = level
        self.emit_cb = emit or (lambda ev: None)
        self.approver = approver or (lambda action, args: True)
        self.policy = policy            # ask / auto
        self.allow_all = policy == "auto"
        self.cancelled = False
        ts = time.strftime("%Y%m%d-%H%M%S")
        self.workdir = PROJECTS_DIR / f"{ts}-agent-{_slug(self.goal)}"
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.history: list[dict] = []
        self.steps: list[dict] = []

    # ------------------------------------------------------------ 事件
    def emit(self, phase, status, message, detail="", **extra):
        ev = {"phase": phase, "status": status, "message": message, "detail": detail,
              "ts": time.time(), **extra}
        try:
            self.emit_cb(ev)
        except Exception:
            pass

    def log(self, message, detail=""):
        self.emit("log", "info", message, detail)

    # ------------------------------------------------------------ 执行
    def _rel(self, path: str) -> Path:
        p = (self.workdir / (path or ".")).resolve()
        if not str(p).startswith(str(self.workdir.resolve())):
            raise PermissionError("只能操作当前工作目录里的文件")
        return p

    def execute(self, action: str, args: dict) -> str:
        try:
            if action == "shell":
                return self.do_shell(args.get("command", ""))
            if action == "write_file":
                p = self._rel(args.get("path", ""))
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(args.get("content", ""), encoding="utf-8", newline="\n")
                return f"已写入 {p.relative_to(self.workdir)}（{len(args.get('content','').splitlines())} 行）"
            if action == "read_file":
                p = self._rel(args.get("path", ""))
                if not p.exists():
                    return f"文件不存在：{p}"
                return p.read_text(encoding="utf-8", errors="ignore")[:4000]
            if action == "list_dir":
                p = self._rel(args.get("path", "."))
                return "\n".join(x.name + ("/" if x.is_dir() else "") for x in sorted(p.iterdir())[:60])
            if action == "run_python":
                p = self._rel(args.get("path", "main.py"))
                cmd = [PY, "-X", "utf8", str(p.name)] + [str(a) for a in (args.get("args") or [])]
                r = subprocess.run(cmd, cwd=str(self.workdir), capture_output=True, text=True,
                                   encoding="utf-8", errors="ignore", timeout=120)
                return f"返回码 {r.returncode}\n{(r.stdout or '')[-1500:]}\n{(r.stderr or '')[-1200:]}"
            if action == "open_path":
                target = args.get("path", "")
                if re.match(r"^https?://", target):
                    os.startfile(target)
                else:
                    p = Path(target)
                    if not p.is_absolute():
                        p = self._rel(target)
                    os.startfile(str(p))
                return f"已打开 {target}"
            if action == "finish":
                return "FINISH"
            return f"未知动作：{action}"
        except subprocess.TimeoutExpired:
            return "命令超时（120s），可能卡住了"
        except Exception as e:
            return f"执行失败：{e}"

    def do_shell(self, command: str) -> str:
        if not command.strip():
            return "空命令"
        r = subprocess.run(["powershell", "-NoProfile", "-Command", command], cwd=str(self.workdir),
                           capture_output=True, text=True, encoding="utf-8", errors="ignore", timeout=90)
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        return f"返回码 {r.returncode}\n" + (out[:2500] if out else "（无输出）")

    def is_dangerous(self, action: str, args: dict) -> bool:
        if action == "shell":
            cmd = (args.get("command") or "").lower()
            return any(re.search(p, cmd, re.I) for p in DANGEROUS)
        return False

    # ------------------------------------------------------------ 主循环
    def run(self) -> dict:
        self.emit("plan", "ok", f"Agent 已接管，工作目录：{self.workdir.name}")
        self.log(f"目标：{self.goal}")
        sys_ctx = f"当前工作目录：{self.workdir}\n系统：Windows 10，PowerShell，Python 3.13 可用（命令用 py -3.13）。"
        self.history = [{"role": "user", "content": f"{sys_ctx}\n\n用户目标：{self.goal}"}]

        from llm import SERVER
        for i in range(self.max_steps):
            ev_ctx = (self.history[-1]["content"].split("观察：")[-1] if i else "")
            self.emit("generate", "running", f"第 {i + 1}/{self.max_steps} 步：模型正在决策…")
            text = SERVER.chat([{"role": "system", "content": SYSTEM}] + self.history,
                               temperature=0.2, max_tokens=600, timeout=600)
            act = parse_action(text)
            if not act:
                self.log("模型输出无法解析成动作", text[:400])
                self.history.append({"role": "assistant", "content": text[:400]})
                self.history.append({"role": "user", "content": "观察：输出格式不正确，请只输出一个合法 JSON 动作。"})
                continue
            thought = str(act.get("thought", ""))[:200]
            action = act.get("action", "")
            args = act.get("args") or {}
            self.emit("thought", "ok", f"想法：{thought or '（未说明）'}")
            step = {"step": i + 1, "thought": thought, "action": action, "args": args}
            self.steps.append(step)

            if action == "finish":
                summary = str(args.get("summary", "") or thought)
                self.emit("done", "ok", f"Agent 完成：{summary}")
                self.finish_report(summary)
                return {"ok": True, "summary": summary, "steps": self.steps,
                        "workdir": str(self.workdir)}

            title = self.action_title(action, args)
            self.emit("action", "running", f"请求执行：{title}", json.dumps(args, ensure_ascii=False)[:800],
                      action=action, args=args)

            need_approval = self.is_dangerous(action, args) or not self.allow_all
            if need_approval:
                ok = self.approver(action, args)
                if not ok:
                    self.emit("action", "fail", f"已拒绝：{title}")
                    self.history.append({"role": "assistant", "content": json.dumps({"action": action, "args": args}, ensure_ascii=False)})
                    self.history.append({"role": "user", "content": "观察：用户拒绝了这个动作，不要重试，改别的做法或直接 finish。"})
                    continue

            result = self.execute(action, args)
            step["result"] = result[:600]
            self.emit("action", "ok", f"执行完成：{title}", result[:1500])
            self.history.append({"role": "assistant", "content": json.dumps({"action": action, "args": args}, ensure_ascii=False)})
            self.history.append({"role": "user", "content": "观察：" + result[:1200] + "\n请继续下一个动作，或 finish。"})
            # 控制历史长度，8G 机器别把上下文撑爆
            if len(self.history) > 9:
                self.history = self.history[:1] + self.history[-8:]

        summary = "达到步数上限，已停止"
        self.emit("done", "fail", summary)
        self.finish_report(summary)
        return {"ok": False, "summary": summary, "steps": self.steps, "workdir": str(self.workdir)}

    def action_title(self, action: str, args: dict) -> str:
        if action == "shell":
            return "执行命令：" + str(args.get("command", ""))[:160]
        if action == "write_file":
            return "写入文件：" + str(args.get("path", ""))
        if action == "read_file":
            return "读取文件：" + str(args.get("path", ""))
        if action == "list_dir":
            return "列出目录：" + str(args.get("path", "."))
        if action == "run_python":
            return "运行 Python：" + str(args.get("path", ""))
        if action == "open_path":
            return "打开：" + str(args.get("path", ""))
        return action

    def finish_report(self, summary: str) -> None:
        try:
            report = {
                "goal": self.goal,
                "summary": summary,
                "steps": self.steps,
                "finished": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            (self.workdir / "agent_report.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            lines = [f"# Agent 执行记录\n", f"目标：{self.goal}\n", f"结论：{summary}\n", "## 步骤"]
            for s in self.steps:
                lines.append(f"{s['step']}. [{s['action']}] {s.get('thought','')}")
                if s.get("args"):
                    lines.append(f"   - 参数：`{json.dumps(s['args'], ensure_ascii=False)[:200]}`")
                if s.get("result"):
                    lines.append(f"   - 结果：{s['result'][:200]}")
            (self.workdir / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        except Exception:
            pass
