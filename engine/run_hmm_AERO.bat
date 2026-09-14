@echo off
REM ===========================================================================
REM AERO (Aerodrome) - HMM LIVE. THIS ONE SENDS REAL ORDERS.  2026-09-14
REM ===========================================================================
REM
REM The fourth market today, but the FIRST one picked with rulers that can
REM reproduce the three answers we already paid for. GMX, MET and FIL were
REM each picked with a screen that was biased toward passing:
REM   G1 threshold was 5.0 bps while break-even is 7.4 (HL half-spread 2.5
REM      plus fees 4.90) - so it passed markets that lose by construction
REM   G3 counted "minutes with a trade", which dust flow satisfies perfectly
REM   M3 measured markout against the FILL price, so a flat market read
REM      +half-spread; MET read +30.26 and PASSED while the truth was -3.72
REM
REM AERO cleared all five gates on arblib/hmm_universe.py (128 markets), and
REM that screen's self-checks now reproduce the live outcomes:
REM   MET fails G4 ($0.27 slice) and G5 (4.7% taker minority)  <- matches 121:0
REM   FIL fails G1 (2.40 bps net)                              <- matches 0 fills
REM
REM Why AERO specifically - it is best on the two things that killed MET:
REM   net edge        4.51 bps (highest of the six that passed)
REM   taker minority  47.4%    (nearly two-sided; MET was 6.9%)
REM   median slice    $39.93   (4x the $10 hedge minimum; our whole clip is $15,
REM                             so a median taker trade lifts the entire quote
REM                             and the hedge to HL fires on the first fill)
REM   Lighter $227k/day, HL core $889k/day, both venues name AERO, 5x lev
REM
REM WHAT STOPS IT, no Telegram needed:
REM   echo flat  > logs\AERO\control.cmd    close everything, stay paused
REM   echo pause > logs\AERO\control.cmd    stop opening, keep hedging
REM STOP ORDER MATTERS (this was got wrong on 2026-09-14 and the watchdog
REM relaunched a live engine for 2h21m unattended):
REM   1. echo flat, wait for it to settle
REM   2. comment this member out of ops\arb_watchdog.ps1
REM   3. only then kill the processes
REM   4. wait past one watchdog cycle (>5 min) and check it is still gone -
REM      an immediately-empty process list is NOT evidence
REM If a residual ends up below the venue minimum:
REM   python tools\flatten_residual.py --symbol AERO --config config_AERO.yaml
REM
REM Risk gates, generated from measurement and re-checked against the
REM engine's own loader before this file was written:
REM   clip $15 = 26.33 base    max_net_base 23 base ($13.10)
REM     >= 20.0 hedgeable  AND  < 26.33 one clip  AND  > 3 quanta
REM   tol 2 base >= 1 quantum, $60/leg, account gross 70, daily loss 10,
REM   M5 stale->HALT, unexplained_position_halt
REM
REM Comments ASCII ONLY - UTF-8 bytes make cmd.exe skip lines (2026-09-13).
cd /d C:\Users\rfo\Desktop\flowbot\arb\engine
:loop
python main.py --symbol AERO --hedge lighter --config config_AERO.yaml --no-dashboard >> logs\AERO\runner.log 2>&1
timeout /t 30 /nobreak >nul
goto loop
