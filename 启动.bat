@echo off
rem ============================================================================
rem  CREW V4 portable launcher
rem ----------------------------------------------------------------------------
rem  ASCII-ONLY + CRLF, both load-bearing. cmd.exe reads a .bat byte by byte and
rem  re-seeks while executing; a multi-byte character can be split across a read
rem  boundary so a comment gets truncated and its tail EXECUTED as a command.
rem
rem  Everything this app needs ships next to this file:
rem      python\     the interpreter (Python 3.13 embeddable, 10MB)
rem      packages\   third-party dependencies
rem      app\        the application itself
rem  packages\ is put on sys.path by  python\python313._pth  -- NOT by an
rem  environment variable, so this whole folder can be renamed or moved anywhere.
rem
rem  The target machine therefore needs NOTHING installed: no Python, no pip,
rem  no network. Unpack and double-click.
rem ============================================================================
chcp 65001 >nul
setlocal
cd /d "%~dp0"

if not exist "python\python.exe" goto :broken
rem  No .env check: the app starts without a key and asks for one in the
rem  settings page on first launch. The old check refused to start, which left
rem  users editing a text file before they could even see the interface.

rem  A native window needs pywebview (bundled) AND the Edge WebView2 runtime
rem  (ships with any current Edge). When the window cannot open, main.py falls
rem  back to the default browser -- and that path NEEDS a console, because the
rem  console is how it tells the user what happened and how Ctrl+C reaches it.
rem  So the check below decides which interpreter to start. It is not a path
rem  check: "the files are there" and "it imports" are different questions.
"python\python.exe" -c "import webview" >nul 2>nul
if errorlevel 1 goto :browser_mode

rem  Native window: no console at all, which is what a desktop app should look
rem  like. main.py then blocks until the window is closed, and anything that goes
rem  wrong lands in  instance\v4_win.log  -- with no console that file is the only
rem  place to look.
rem  First launch keeps a console: a startup failure would otherwise be a
rem  silent no-op (pythonw has no window). main.py writes instance\.started-ok
rem  once it is serving, and the console stops appearing after that.
if exist "instance\.started-ok" goto :silent
echo First launch: keeping this window open so any error is visible.
echo It will not appear again after a successful start.
echo.
"python\python.exe" "main.py"
exit /b %ERRORLEVEL%

:silent
start "" "python\pythonw.exe" "main.py"
exit /b 0

:browser_mode
echo [!] This copy cannot open its own window, so the app will open in your
echo     default browser instead. Everything else works the same.
echo.
echo     KEEP THIS WINDOW OPEN while you use the app, and press Ctrl+C here when
echo     you are done (closing this window stops the app too).
echo.
echo     To get the native window, install the Microsoft Edge WebView2 runtime:
echo         https://developer.microsoft.com/microsoft-edge/webview2/
echo.
rem  No `start` here: main.py's output must stay readable in this console and
rem  Ctrl+C must reach the process.
"python\python.exe" "main.py"
exit /b %ERRORLEVEL%

:broken
echo [!] python\python.exe is missing, so this copy is incomplete.
echo     Please unpack the WHOLE archive again -- do not move files out of it.
echo.
pause
exit /b 1
