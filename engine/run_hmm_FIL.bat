@echo off
REM ===========================================================================
REM FIL (Filecoin) - HMM LIVE. THIS ONE SENDS REAL ORDERS.  2026-09-14
REM ===========================================================================
REM
REM Replaces MET. MET was not a parameter problem: its median trade slice on
REM Lighter is $0.27 while BOTH venues have a $10 minimum order, so 93% of
REM fills could not be hedged at all. The HL leg was never reached once in
REM 20 minutes and 17 fills - the whole run was one-legged and directional.
REM
REM Why FIL instead (measured 2026-09-14 from 581,597 Lighter trades):
REM   median trade slice  $100    vs the $10 hedge minimum   -> 10x headroom
REM   Lighter half-spread 8.4 bps (G1 wants >= 5)
REM   Lighter volume      $1.31M/day    HL core volume $10.9M/day
REM   name match FIL/FIL, price deviation 0.19% -> same asset, verified
REM Our whole clip is $15 and the median taker slice is $100, so a single
REM taker normally lifts the entire quote -> the immediate hedge to HL fires
REM on the first fill. That will be the FIRST real order this system has ever
REM sent to Hyperliquid (userFills on the account was literally 0).
REM
REM WHAT STOPS IT, no Telegram needed:
REM   echo flat  > logs\FIL\control.cmd    close everything, stay paused
REM   echo pause > logs\FIL\control.cmd    stop opening, keep hedging
REM Per-pair path on purpose: the default control.cmd is shared by every
REM engine and the reader truncates it, so a flat would land on a random one.
REM A HALT cannot be lifted from here, by design - it needs a restart.
REM If a residual ends up below the venue minimum:
REM   python tools\flatten_residual.py --symbol FIL --config config_FIL.yaml
REM
REM Risk gates, generated from measurement by arblib/make_hmm_config.py and
REM re-checked against the engine's own loader before this file was written:
REM   clip $15 = 14.88 base   max_net_base 11.5 base ($11.6)
REM     >= 10.0 hedgeable  AND  < 14.88 one clip  AND  > 3 quanta
REM   $60/leg, max_account_gross 70, max_daily_loss 10, M5 stale->HALT
REM
REM Comments ASCII ONLY - UTF-8 bytes make cmd.exe skip lines (2026-09-13).
cd /d C:\Users\rfo\Desktop\flowbot\arb\engine
:loop
python main.py --symbol FIL --hedge lighter --config config_FIL.yaml --no-dashboard >> logs\FIL\runner.log 2>&1
timeout /t 30 /nobreak >nul
goto loop
