@echo off
REM HMM(對沖做市) GMX —— 掛單 Lighter / 對沖 HL core
REM 目前是 --record-only：不送任何單。翻 live = 拿掉那個旗標，
REM 而那要使用者另外說一次（CLAUDE.md 第 9 次 override）。
cd /d C:\Users\rfo\Desktop\flowbot\arb\engine
:loop
python main.py --record-only --symbol GMX --hedge lighter --config config_HMM_GMX.yaml --no-dashboard >> logs\GMX\runner.log 2>&1
timeout /t 30 /nobreak >nul
goto loop
