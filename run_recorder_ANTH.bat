@echo off
REM entropy-arb record-only runner (ANTH) - restart loop
cd /d C:\Users\rfo\Desktop\flowbot\arb\engine
:loop
python main.py --record-only --symbol ANTH --hedge lighter-rh --config config_ANTH.yaml --no-dashboard >> logs\ANTH\runner.log 2>&1
timeout /t 30 /nobreak >nul
goto loop
