@echo off
REM entropy-arb record-only runner (BTC) - restart loop
cd /d C:\Users\rfo\Desktop\flowbot\entropy-arb
:loop
python main.py --record-only --symbol BTC --hedge lighter-rh --config config_BTC.yaml --no-dashboard >> logs\BTC\runner.log 2>&1
timeout /t 30 /nobreak >nul
goto loop
