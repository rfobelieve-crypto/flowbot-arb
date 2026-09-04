@echo off
REM entropy-arb record-only runner - restart loop (crash/network self-heal)
cd /d C:\Users\rfo\Desktop\flowbot\arb\engine
:loop
python main.py --record-only --symbol SNDK --hedge lighter-rh --no-dashboard >> logs\runner.log 2>&1
timeout /t 30 /nobreak >nul
goto loop
