@echo off
REM ===========================================================================
REM MET (Meteora) - HMM LIVE. THIS ONE SENDS REAL ORDERS.  2026-09-14
REM ===========================================================================
REM
REM Authorised by the user on 2026-09-14, in his own words, asking to skip
REM further shadow and verify on the live book. That is the second saying-so
REM the 9th override requires; the first one only reached record-only.
REM (Comments here are ASCII only, so the Chinese original lives in the
REM commit message and in CLAUDE.md, not in this file.)
REM
REM WHY LIVE AND NOT MORE SHADOW -- this is the whole reason, so it is here
REM and not in a doc nobody opens:
REM   shadow_decisions == quotes_cancelled EXACTLY, and quotes_rested == 0.
REM   maker_rested needs an OPEN status back from the venue, and shadow never
REM   sends, so M2 (fill rate), M3 (markout) and M4 (latency) are all
REM   STRUCTURALLY unmeasurable here. Waiting longer cannot produce them.
REM   Quant Arb's "Analysing Real Fills" (2026-05-27) collected 35,000 fills
REM   by deliberately ping-ponging a live book for exactly this reason.
REM
REM WHAT STOPS IT, and none of this needs Telegram:
REM   echo flat  > logs\MET\control.cmd    close everything, stay paused
REM   echo pause > logs\MET\control.cmd    stop opening, keep hedging
REM   The per-pair path is deliberate: the default control.cmd is shared by
REM   all 18 engines and the reader truncates it, so a flat would land on a
REM   random one. Checked before going live, not after.
REM   A HALT cannot be lifted from here, by design. It needs a restart.
REM
REM The risk gates are in config_MET.yaml and were audited on 2026-09-14:
REM   $15/order, $60/leg, max_net_base 16 base units (~$3.74, under one clip
REM   so a fully failed hedge trips it), max_daily_loss_usd 10,
REM   max_account_gross_usd 70 (this is what makes a second live market
REM   fail the B3 budget check), M5 stale->HALT, unexplained_position_halt.
REM
REM Comments ASCII ONLY - UTF-8 bytes make cmd.exe skip lines (2026-09-13).
cd /d C:\Users\rfo\Desktop\flowbot\arb\engine
:loop
if exist logs\stop\%~n0.stop goto end
python main.py --symbol MET --hedge lighter --config config_MET.yaml --no-dashboard >> logs\MET\runner.log 2>&1
timeout /t 30 /nobreak >nul
goto loop
:end
