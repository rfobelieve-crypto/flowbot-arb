@echo off
REM entropy-arb record-only runner (ZEC) - restart loop
cd /d C:\Users\rfo\Desktop\flowbot\arb\engine
:loop
python main.py --record-only --symbol ZEC --hedge lighter-rh --config config_ZEC.yaml --no-dashboard >> logs\ZEC\runner.log 2>&1
timeout /t 30 /nobreak >nul
goto loop
