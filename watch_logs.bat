@echo off
echo Watching pipeline logs (Ctrl+C to stop)...
echo.
powershell -Command "Get-Content logs\logs.txt -Wait -Tail 30"
