@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 goto python
py -3 -c "import sys, tkinter; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 goto python
py -3 "%~dp0valorant_party_joiner.py"
if errorlevel 1 goto failed
exit /b 0

:python
where python >nul 2>nul
if errorlevel 1 goto missing
python -c "import sys, tkinter; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 goto missing
python "%~dp0valorant_party_joiner.py"
if errorlevel 1 goto failed
exit /b 0

:missing
echo Python 3.10 or newer with tkinter is required.
echo Install the normal Windows Python package from https://www.python.org/downloads/windows/
echo Include the Python launcher and Tcl/Tk support, then run this file again.
pause
exit /b 1

:failed
echo The tool could not start. The error is shown above.
pause
exit /b 1
