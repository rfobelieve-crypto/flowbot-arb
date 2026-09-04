@echo off
REM entropy-arb record-only runner (NEAR) - restart loop
cd /d C:\Users\rfo\Desktop\flowbot\arb\engine
:loop
python main.py --record-only --symbol NEAR --hedge lighter-rh --config config_NEAR.yaml --no-dashboard >> logs\NEAR\runner.log 2>&1
timeout /t 30 /nobreak >nul
goto loop
