@echo off
REM MON - HMM candidate, generated from measurements (2026-09-14).
REM CLI --symbol is the HL leg (MON); the Lighter leg (MON)
REM is hedge.symbol in the yaml. config.py ignores entropy.symbol.
REM MODE: LIVE. THIS SENDS REAL ORDERS.
REM
REM Shadow was retired on 2026-09-14 and the reason is structural, not a
REM preference: in shadow, `shadow_decisions` and `quotes_cancelled` are
REM equal line for line and `quotes_rested` is permanently 0, because
REM maker_rested needs an OPEN status back from the venue. So M2 (fill
REM rate) has a zero denominator, M3 has no fills to mark out, and M4 has
REM no real round trip -- the three gates that decide whether this works
REM are STRUCTURALLY unmeasurable there. A ten-hour clean shadow run told
REM us nothing about whether live would even start (it would not: the HL
REM SDK was not installed).
REM
REM STOP ORDER MATTERS:
REM   1. echo flat > logs\MON\control.cmd   (while the engine is alive)
REM   2. comment this member out of ops\arb_watchdog.ps1
REM   3. only then kill the processes
REM   4. wait past one watchdog cycle (>5 min) and re-check -- an
REM      immediately-empty process list is NOT evidence
REM Comments ASCII ONLY - UTF-8 bytes make cmd.exe skip lines.
cd /d C:\Users\rfo\Desktop\flowbot\arb\engine
:loop
python main.py --symbol MON --hedge lighter --config config_MON.yaml --no-dashboard >> logs\MON\runner.log 2>&1
timeout /t 30 /nobreak >nul
goto loop
