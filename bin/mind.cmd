@echo off
rem mind — tiny launcher (Windows). Uses the project .venv if present,
rem else the py launcher, else python on PATH.
setlocal
set "SCRIPT_DIR=%~dp0"
set "VENV_PY=%SCRIPT_DIR%..\.venv\Scripts\python.exe"
if exist "%VENV_PY%" ( "%VENV_PY%" "%SCRIPT_DIR%mind.py" %* & exit /b )
where /q py
if not errorlevel 1 ( py -3 "%SCRIPT_DIR%mind.py" %* & exit /b )
python "%SCRIPT_DIR%mind.py" %*
