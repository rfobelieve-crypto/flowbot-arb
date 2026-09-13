@echo off
REM MET - HMM candidate (2026-09-14).
REM
REM MODE: --shadow. The FULL strategy runs and NOTHING is sent; that is
REM structure, not discipline (shadow never calls init_signer, and
REM _blocked is the single send boundary every order path asks).
REM
REM Why shadow and not record-only: G2 in arblib/hmm_screen.py now reads
REM the engine's OWN decisions from shadow.csv. Two attempts at computing
REM the side balance from minutes.csv both disagreed with the engine
REM (GMX: my number 34pc buy, the engine's 1.8pc), and tuning a metric
REM until the control goes green is fitting the instrument to the answer.
REM The minute recorder keeps running here, so G1/G3 do not gap.
REM
REM TO GO LIVE: remove the shadow flag. Needs the user to say so again.
REM Comments ASCII ONLY - UTF-8 bytes make cmd.exe skip lines.
cd /d C:\Users\rfo\Desktop\flowbot\arb\engine
:loop
python main.py --shadow --symbol MET --hedge lighter --config config_MET.yaml --no-dashboard >> logs\MET\runner.log 2>&1
timeout /t 30 /nobreak >nul
goto loop
