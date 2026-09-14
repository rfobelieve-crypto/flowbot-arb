@echo off
REM CHIP - HMM candidate, generated from measurements (2026-09-14).
REM CLI --symbol is the HL leg (CHIP); the Lighter leg (CHIP)
REM is hedge.symbol in the yaml. config.py ignores entropy.symbol.
REM MODE: --shadow. Full strategy runs, NOTHING is sent (structure, not
REM discipline: shadow never calls init_signer, and _blocked is the one
REM send boundary every order path asks).
REM G2 in arblib/hmm_screen.py reads this run's shadow.csv.
REM TO GO LIVE: remove the shadow flag. Needs the user to say so again.
REM Comments ASCII ONLY - UTF-8 bytes make cmd.exe skip lines.
cd /d C:\Users\rfo\Desktop\flowbot\arb\engine
:loop
python main.py --shadow --symbol CHIP --hedge lighter --config config_CHIP.yaml --no-dashboard >> logs\CHIP\runner.log 2>&1
timeout /t 30 /nobreak >nul
goto loop
