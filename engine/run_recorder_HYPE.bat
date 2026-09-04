@echo off
REM entropy-arb record-only runner (HYPE) - restart loop
cd /d C:\Users\rfo\Desktop\flowbot\arb\engine
:loop
python main.py --record-only --symbol HYPE --hedge lighter-rh --config config_HYPE.yaml --no-dashboard >> logs\HYPE\runner.log 2>&1
timeout /t 30 /nobreak >nul
goto loop
