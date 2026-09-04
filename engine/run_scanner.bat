@echo off
REM entropy-arb cross-venue SCANNER (flow_system TODO 0.75b) - restart loop
cd /d C:\Users\rfo\Desktop\flowbot\arb\engine
:loop
python tools\scanner.py >> logs\scan\runner.log 2>&1
timeout /t 30 /nobreak >nul
goto loop
