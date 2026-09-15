@echo off
REM Discord watcher for the HMM live engine. Lives in arb since 2026-09-15
REM (moved from flow_system/research/ops; the user moved all of HMM here).
REM Watcher: ops/hmm_watch.py  (transition-only: silent when fine)
REM Delivery: ops/alert.py, arb's own ARB_DISCORD_WEBHOOK_URL. Reads nothing
REM from flow_system.
REM
REM NO --pair ON PURPOSE. The target is derived from the watchdog's $Members
REM minus engine/logs/stop/*.stop, via ops/live_hmm.py -- the same function
REM flow_system's freshness_board imports. On 2026-09-14 a hardcoded target
REM burned us four times in one day.
REM
REM ASCII ONLY. cmd.exe reads .bat in the OEM codepage (cp950 here); UTF-8
REM CJK in comments shifts its byte-offset bookkeeping and it SKIPS LINES
REM while still exiting 0. CRLF line endings for the same reason.
REM
REM Launched by ops\run_hidden.vbs (wscript.exe has no console). Proof it ran
REM is the ARTIFACT: new lines in results\hmm_watch.log. Never LastTaskResult.
setlocal
set ROOT=C:\Users\rfo\Desktop\flowbot\arb
set PYTHONIOENCODING=utf-8
cd /d "%ROOT%"
python "%ROOT%\ops\hmm_watch.py" >> "%ROOT%\results\hmm_watch.log" 2>&1
endlocal
