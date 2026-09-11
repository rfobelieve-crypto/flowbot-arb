@echo off
REM §1.25 宇宙級分鐘錄製器（150 個配對、一個行程、三條 WS）。
REM 純錄製：不 import Engine、不碰憑證、不可能下單。
cd /d C:\Users\rfo\Desktop\flowbot\arb\engine
python tools\record_universe.py >> logs\universe_run.log 2>&1
