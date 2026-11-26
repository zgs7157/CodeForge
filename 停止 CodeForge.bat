@echo off
chcp 65001 >nul
echo 正在停止 CodeForge...
taskkill /f /im llama-server.exe >nul 2>nul
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8765" ^| findstr LISTENING') do taskkill /f /pid %%a >nul 2>nul
echo 已停止（如果窗口还开着，直接关掉即可）。
timeout /t 2 >nul
