@echo off
REM Fake adb entry: forward MaaCore adb commands to fake_adb.py
REM MaaCore invokes us via AsstConnect(adb_path=this file), then runs "<adb_path> -s <serial> ..."
REM We invoke python fake_adb.py with all arguments forwarded.

setlocal
set "DIR=%~dp0"
python "%DIR%fake_adb.py" %*
exit /b %ERRORLEVEL%