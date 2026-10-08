@echo off
setlocal
cd /d "%~dp0"
rem A closed window can leave the backend or the frontend of an earlier run alive, and several
rem copies would then share the ports and answer at random with different code. Stop them first.
call :stop
if exist backend\.env (
  for /f "usebackq eol=# tokens=1,* delims==" %%A in ("backend\.env") do set "%%A=%%~B"
)
rem Turns on the idle-room sweeper, in the SERVER process: that is where the heartbeats are, so it is
rem the only process that can tell who is still connected.
set GENAI_SWEEP=1
rem Every start begins with no game open: close_rooms deletes the rooms of the last session.
rem Both processes share this console, so Ctrl+C stops the backend and the frontend together.
start "" /b cmd /c "cd backend && uv run python manage.py migrate && uv run python manage.py close_rooms && uv run python manage.py runserver 8000"
pushd frontend
bun run dev
popd
rem The frontend has stopped: stop the backend with it.
call :stop
endlocal
goto :eof

:stop
rem This project's Django server (the Python of backend\.venv running runserver) and its Vite
rem dev server (run from frontend\). Their uv and bun parents exit on their own once these are gone.
powershell -NoProfile -Command "$root = (Get-Location).Path; Get-CimInstance Win32_Process | Where-Object { ($_.CommandLine -like ('*' + $root + '\backend\*') -and $_.CommandLine -like '*runserver*') -or ($_.Name -in 'vite.exe','node.exe' -and $_.CommandLine -like ('*' + $root + '\frontend\*')) } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
goto :eof
