@echo off
REM GRAM - HMM candidate, generated from measurements (2026-09-14).
REM CLI --symbol is the HL leg (GRAM); the Lighter leg (GRAM)
REM is hedge.symbol in the yaml. config.py ignores entropy.symbol.
REM MODE: --record-only. No strategy, no quotes, no keepalive; it only
REM feeds logs/<pair>/minutes.csv. Ran in shadow mode until 2026-09-15.
REM Shadow was retired by the user on 2026-09-14 ("only run live"):
REM it structurally cannot measure M2/M3/M4 (quotes_rested is always 0
REM because it never sends), and it was the main consumer of the shared
REM IP budget (_http_keepalive_loop starts only when not record_only).
REM WARNING: do NOT reach live by deleting the mode flag. There is no
REM --record-only fallback here, so a bare launch sends REAL orders.
REM Going live means a generated config + the user saying so again.
REM G2 in arblib/hmm_screen.py reads the LIVE maker.csv, not shadow.
REM Comments ASCII ONLY - UTF-8 bytes make cmd.exe skip lines.
cd /d C:\Users\rfo\Desktop\flowbot\arb\engine
:loop
if exist logs\stop\%~n0.stop goto end
python main.py --record-only --symbol GRAM --hedge lighter --config config_GRAM.yaml --no-dashboard >> logs\GRAM\runner.log 2>&1
timeout /t 30 /nobreak >nul
goto loop
:end
