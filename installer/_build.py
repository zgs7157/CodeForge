"""打包安装程序：payload.zip + PyInstaller onefile。"""
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(r"D:\APP\CodeForge")
INS = ROOT / "installer"
PAYLOAD = INS / "payload.zip"

SKIP_DIRS = {"projects", "logs", "models", "__pycache__", "installer", ".git"}
SKIP_FILES = {"_c.js", "_check.py", "_mem.py", "_shot.py", "_switch.py", "_ensure.py",
              "_hud_shot.py", "_hud_open.py", "_demo.py"}


def build_payload():
    if PAYLOAD.exists():
        PAYLOAD.unlink()
    n = 0
    with zipfile.ZipFile(PAYLOAD, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in ROOT.rglob("*"):
            if not p.is_file():
                continue
            rel = p.relative_to(ROOT)
            if any(part in SKIP_DIRS for part in rel.parts):
                continue
            if p.name in SKIP_FILES or p.suffix == ".pyc":
                continue
            if p.suffix == ".gguf":
                continue
            z.write(p, rel.as_posix())
            n += 1
    print(f"payload.zip: {n} files, {PAYLOAD.stat().st_size/1e6:.1f} MB")


def build_exe():
    args = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--onefile", "--windowed",
            "--name", "CodeForge-Setup",
            "--icon", str(ROOT / "assets" / "codeforge.ico"),
            "--add-data", f"{PAYLOAD};.",
            "--distpath", str(INS / "dist"),
            "--workpath", str(INS / "build"),
            "--specpath", str(INS / "build"),
            str(INS / "installer.py")]
    r = subprocess.run(args, cwd=str(INS), capture_output=True, text=True, timeout=900)
    print(r.stdout[-1500:])
    if r.returncode != 0:
        print("STDERR", r.stderr[-2000:])
        return False
    exe = INS / "dist" / "CodeForge-Setup.exe"
    print("exe:", exe.exists(), f"{exe.stat().st_size/1e6:.1f} MB" if exe.exists() else "")
    return exe.exists()


if __name__ == "__main__":
    build_payload()
    ok = build_exe()
    sys.exit(0 if ok else 1)
