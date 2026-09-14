@echo off
REM HMM (hedged market making) GMX: quote on Lighter, hedge on HL core.
REM
REM MODE: --shadow (2026-09-13). The FULL strategy runs and NOTHING is
REM sent. That is structure, not discipline: shadow never calls
REM init_signer(), and _blocked() is the one send boundary that every
REM order path asks before it can put anything on an exchange.
REM
REM Why the mode changed: the previous flag did not run the strategy
REM loop at all (engine.py, tasks are only added when not record-only),
REM so it could not answer the one question that blocks go-live --
REM the Lighter GMX book updates every 9.5-14s while staleness_sec is
REM 10.0, and max_stale_episodes=5 HALTs the whole session when flat.
REM The minute recorder keeps running here (recorder.enabled=true),
REM so the arb family CSV does not gap.
REM
REM TO GO LIVE: remove the shadow flag from the python line below.
REM That needs the user to say so again (CLAUDE.md, 9th override).
REM
REM Comments in this file are ASCII ONLY. UTF-8 bytes throw off the
REM byte-offset bookkeeping cmd.exe uses to read a .bat and it silently
REM skips lines, exit code 0 (mistake.md 2026-09-13).
cd /d C:\Users\rfo\Desktop\flowbot\arb\engine
:loop
if exist logs\stop\%~n0.stop goto end
python main.py --shadow --symbol GMX --hedge lighter --config config_HMM_GMX.yaml --no-dashboard >> logs\GMX\runner.log 2>&1
timeout /t 30 /nobreak >nul
goto loop
:end
