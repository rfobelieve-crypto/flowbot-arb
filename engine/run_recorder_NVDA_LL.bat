@echo off
REM entropy-arb record-only runner - NVDA_LL: lighter NVDA vs lighter-rh NVDA (zero-fee control, 2026-09-04)
cd /d C:\Users\rfo\Desktop\flowbot\arb\engine
:loop
python main.py --record-only --symbol NVDA --hedge lighter-rh --config config_NVDA_LL.yaml --no-dashboard >> logs\NVDA_LL\runner.log 2>&1
timeout /t 30 /nobreak >nul
goto loop
