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
REM HOW TO STOP IT (one action, not a remembered sequence):
REM   1. echo flat > logs\MON\control.cmd   (while the engine is alive - it needs the signer)
REM   2. type nul > logs\stop\%~n0.stop
REM      Both restarters read that file: this loop exits at the gate
REM      below, and arb_watchdog.ps1 will not relaunch. Delete it to
REM      resume. Killing python alone does NOT stop anything - the cmd
REM      wrapper relaunches it 30s later.
REM   2026-09-15: this launcher had NO gate (the generator template was
REM   missed when af4a19e patched the others), so the .stop file placed
REM   after the 17:40 flat did not stop the loop, and two cmd wrappers were
REM   running. Both were ended by the user at 18:2x; the gate was added
REM   only after that, because cmd reads a running .bat by byte offset.
REM Comments ASCII ONLY - UTF-8 bytes make cmd.exe skip lines.
cd /d C:\Users\rfo\Desktop\flowbot\arb\engine
:loop
if exist logs\stop\%~n0.stop goto end
python main.py --symbol MON --hedge lighter --config config_MON.yaml --no-dashboard >> logs\MON\runner.log 2>&1
timeout /t 30 /nobreak >nul
goto loop
:end
