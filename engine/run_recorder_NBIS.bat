@echo off
REM entropy-arb record-only runner (NBIS) - restart loop
cd /d C:\Users\rfo\Desktop\flowbot\arb\engine
:loop
python main.py --record-only --symbol NBIS --hedge lighter --config config_NBIS.yaml --no-dashboard >> logs\NBIS\runner.log 2>&1
timeout /t 30 /nobreak >nul
goto loop
