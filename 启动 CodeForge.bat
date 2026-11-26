@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "EDGE=C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
set "CODEFORGE_NO_BROWSER=1"

rem 8765 已在监听就只开窗口，不重复起服务
netstat -ano | findstr /c:":8765" | findstr /c:"LISTENING" >nul
if %errorlevel%==0 goto openwin

echo CodeForge 铸码工坊 正在启动，本地模型加载需要 30 秒左右…
where py >nul 2>nul
if %errorlevel%==0 (
    start "CodeForge 引擎" /min py -3.13 -X utf8 "%~dp0app\server.py"
) else (
    start "CodeForge 引擎" /min python -X utf8 "%~dp0app\server.py"
)
timeout /t 5 >nul

:openwin
if exist "%EDGE%" (
    start "" "%EDGE%" --app=http://127.0.0.1:8765/ --window-size=1500,950 --disable-features=msEdgeTranslate
) else (
    start "" http://127.0.0.1:8765/
)
exit /b 0
