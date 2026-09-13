@echo off
REM MET record-only recorder (HMM candidate, 2026-09-14).
REM Answers the gate GMX failed: does the premium oscillate?
REM Criteria frozen in arblib/hmm_screen.py before the run.
REM RECORD-ONLY. Any order-sending mode needs another override.
REM Comments ASCII ONLY - UTF-8 bytes make cmd.exe skip lines.
cd /d C:\Users\rfo\Desktop\flowbot\arb\engine
:loop
python main.py --record-only --symbol MET --hedge lighter --config config_MET.yaml --no-dashboard >> logs\MET\runner.log 2>&1
timeout /t 30 /nobreak >nul
goto loop
