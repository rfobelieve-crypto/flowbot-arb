@echo off
REM entropy-arb record-only runner - GOLD_LL: lighter XAU vs lighter-rh XAU (zero-fee control, 2026-09-04)
cd /d C:\Users\rfo\Desktop\flowbot\arb\engine
:loop
python main.py --record-only --symbol XAU --hedge lighter-rh --config config_GOLD_LL.yaml --no-dashboard >> logs\GOLD_LL\runner.log 2>&1
timeout /t 30 /nobreak >nul
goto loop
