# arb_watchdog.ps1 — relaunch any member of the §0.75 recording family that
# is not running. Runs every 5 min from the EntropyArbWatchdog scheduled task.
#
# Why (2026-09-03): the whole family died at 21:09 +0800 from one interrupt
# (runner.log ends in ^C) and stayed dead 46 minutes. The 30-second restart
# loop inside each .bat only heals "the python child died"; when the console
# that hosts the loop is taken out, the loop dies with it and nothing
# restarts anything. This is the layer above that loop.
#
# Rules:
#   * presence is judged by PROCESS COMMAND LINE, never by file mtime — a
#     stalled-but-alive process is the recorder's own problem to log, and
#     launching a second copy on top of a live one is the duplicate-scanner
#     bug of 2026-09-03 (two scanners hitting the same public API = the rate
#     limiting we spent a day on). One member, one process, or relaunch.
#   * each member is launched exactly the way the Startup-folder launcher
#     does it (its own minimized console via the same .bat), so the heal
#     path and the boot path are the same path.
#   * 存活比對**不綁 --record-only**（2026-09-13）：HMM 翻 live 之後指令列
#     沒有那個旗標，綁著它就會讓看門狗判定 live 引擎已死並開第二個 —— 兩個
#     引擎在同一個帳戶上對同一個市場報價。符號簽章帶尾空白，已足夠唯一。
#   * every decision is one line in the log, including "all alive" — a
#     watchdog that only writes when it acts looks dead when things are fine
#     (the degradation-guard rule, freshness_board.py registry note).
param([switch]$DryRun)

$Root = 'C:\Users\rfo\Desktop\flowbot\arb\engine'
$Log  = 'C:\Users\rfo\Desktop\flowbot\arb\results\arb_watchdog.log'

# member name -> (command-line signature, launcher .bat)
$Members = [ordered]@{
  'SNDK'    = @('--symbol SNDK ', 'run_recorder.bat')
  'NBIS'    = @('--symbol NBIS ', 'run_recorder_NBIS.bat')
  'ANTH'    = @('--symbol ANTH ', 'run_recorder_ANTH.bat')
  'BTC'     = @('--symbol BTC ',  'run_recorder_BTC.bat')
  'ZEC'     = @('--symbol ZEC ',  'run_recorder_ZEC.bat')
  'NEAR'    = @('--symbol NEAR ', 'run_recorder_NEAR.bat')
  'HYPE'    = @('--symbol HYPE ', 'run_recorder_HYPE.bat')
  'GOLD_LL' = @('--symbol XAU ',  'run_recorder_GOLD_LL.bat')
  'NVDA_LL' = @('--symbol NVDA ', 'run_recorder_NVDA_LL.bat')
  # MET —— HMM 候選的 record-only 錄製器（2026-09-14，TODO §1.41b）。
  # 它要回答 GMX 死掉的那一關：premium 會不會震盪（判準在
  # arblib/hmm_screen.py，跑之前凍結）。**record-only，不送單。**
  'MET'     = @('--symbol MET ',  'run_recorder_MET.bat')
  # HMM（對沖做市）Stage 1。現在是 record-only；翻 live 只改 .bat。
  'HMM_GMX' = @('--symbol GMX ', 'run_hmm_GMX.bat')
  # 'scanner' 2026-09-13 退出這張表 —— 掃描器搬到 Railway 了（docs/DEPLOY.md §6）。
  # 搬家的理由是 per-IP 的 WAF 預算：掃描器一支 65 次/分，是十支引擎合計的十六倍，
  # 而它擋住的是**引擎的重連**，那時引擎手上有部位。**本機不可以再起第二支** ——
  # 兩支就是 2026-09-03 的 duplicate-scanner bug，也正是這整條路的起點。
  # 取代它的不是一個行程，是下面的 scan_pull（拉回產物）。
  # 2026-09-11 §1.25：宇宙級錄製器。它不是 main.py,所以比對式要另一條
  # （見下面的 -or）。少了這一條它死掉就沒人拉起來,而它錄的是
  # **不可回填**的分鐘資料。
  'universe' = @('record_universe.py', 'run_recorder_universe.bat')
}

$procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
         ForEach-Object { [string]$_.CommandLine }
$stamp = (Get-Date).ToUniversalTime().ToString('yyyy-MM-dd HH:mm')
$dead = @()
foreach ($name in $Members.Keys) {
  $sig, $bat = $Members[$name]
  $alive = @($procs | Where-Object { $_ -like "*main.py*$sig*" -or ($name -in 'universe' -and $_ -like "*$sig*") }).Count
  if ($alive -ge 1) { continue }
  $dead += $name
  if (-not $DryRun) {
    Start-Process -FilePath (Join-Path $Root $bat) -WorkingDirectory $Root -WindowStyle Minimized
  }
}
$line = if ($dead.Count -eq 0) { "$stamp UTC  all $($Members.Count) alive" }
        elseif ($DryRun)      { "$stamp UTC  DRY-RUN would relaunch: $($dead -join ',')" }
        else                  { "$stamp UTC  RELAUNCHED: $($dead -join ',')" }
Add-Content -Path $Log -Value $line -Encoding UTF8
Write-Output $line

# B6 (2026-09-13): add up the per-process risk budgets of everything in
# $Members that runs LIVE, grouped by the account each leg settles on.
# Every ceiling inside the engine (cap_usd, max_gross_usd) is per process, and
# one process trades one symbol -- so N markets means N processes sharing one
# Lighter account and one HL account, and N processes each honouring $1,000
# can put $5,000 on one account. This is the launch-time half of that check;
# the runtime half reads the account back from the venue itself
# (engine/entropy_arb/account.py). It runs HERE because this file is the
# registry of what the machine launches, and because a guard nobody schedules
# is a guard that does not exist (mistake.md 2026-09-01 / 2026-09-11).
#
# It writes results/account_budget.json, which flow_system's freshness board
# reads as a json_flag -- so the VERDICT is what turns red, and a crash here
# turns red too because the flag goes stale. The exit code is not the
# judgement (mistake.md 2026-08-26).
$Eng = 'C:\Users\rfo\Desktop\flowbot\arb\engine'
try {
  $out = & python (Join-Path $Eng 'tools\account_budget.py') 2>&1
  $bad = @($out | Where-Object { "$_" -like '*RED*' -or "$_" -like '*紅*' })
  $budline = if ($bad.Count -eq 0) { "$stamp UTC  account budget OK" }
             else { "$stamp UTC  account budget RED: $($bad -join ' | ')" }
} catch {
  $budline = "$stamp UTC  account budget CHECK FAILED: $($_.Exception.Message)"
}
Add-Content -Path $Log -Value $budline -Encoding UTF8
Write-Output $budline

# 掃描器的產物拉回本機（2026-09-13，docs/DEPLOY.md §6）。
#
# 為什麼掛在這裡而不是另開一個排程：這台機器上「每 5 分鐘、隱藏視窗」的心跳
# 只有這一個，而 schtasks 建的工作**預設會彈視窗**（mistake.md 2026-09-06，
# 每 5 分鐘彈一次、修它花掉一個 session）。重用一條已經驗過的隱藏路徑，
# 比新增一條要再驗一次的便宜。
#
# 放在重啟迴圈**之後**：拉取再慢也不可以延遲「引擎死了要拉起來」那件事。
# 鎖檔擋重疊：遠端每日 CSV 約 100 MB，第一次拉會久，而看門狗每 5 分鐘回來。
#
# 判準是 results/scan_pull_last.json（本機位元組有沒有在長），不是這裡的
# 退出碼 —— Railway 活著但拉取斷了，本機資料會靜靜地停在昨天而十個消費者
# 一個都不會報錯（mistake.md 2026-08-29）。
$Arb  = 'C:\Users\rfo\Desktop\flowbot\arb'
$Lock = Join-Path $Arb 'results\.scan_pull.lock'
try {
  $stale = (Test-Path $Lock) -and
           ((Get-Date) - (Get-Item $Lock).LastWriteTime).TotalMinutes -gt 30
  if ((Test-Path $Lock) -and -not $stale) {
    $pullline = "$stamp UTC  scan_pull SKIPPED (上一次還在跑)"
  } else {
    New-Item -ItemType File -Path $Lock -Force | Out-Null
    # SCAN_URL / SCAN_TOKEN 從 arb/.env 讀（它被 gitignore，值不進 argv）。
    Select-String -Path (Join-Path $Arb '.env') -Pattern '^SCAN_(URL|TOKEN)=' |
      ForEach-Object {
        $kv = $_.Line -split '=', 2
        Set-Item -Path ("Env:" + $kv[0]) -Value $kv[1].Trim()
      }
    # 明寫 UTF-8 再抓輸出：scan_pull 吐的是 UTF-8，而 PowerShell 預設用
    # 主控台碼頁（cp950）解它 -> 中文變亂碼 -> log 裡那一行讀不懂。
    # 讀不懂的 log 行等於沒記（engine/main.py 的 logging 註解、
    # mistake.md 2026-09-11 的鏡像：那次是讀，這次是寫）。
    $prevEnc = [Console]::OutputEncoding
    try {
      [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
      $env:PYTHONIOENCODING = 'utf-8'
      $out = & python (Join-Path $Arb 'tools\scan_pull.py') 2>&1
    } finally { [Console]::OutputEncoding = $prevEnc }
    $pullline = "$stamp UTC  scan_pull: " + (($out | Select-Object -Last 1) -replace '\s+', ' ')
    Remove-Item $Lock -Force -ErrorAction SilentlyContinue
  }
} catch {
  $pullline = "$stamp UTC  scan_pull FAILED: $($_.Exception.Message)"
  Remove-Item $Lock -Force -ErrorAction SilentlyContinue
}
Add-Content -Path $Log -Value $pullline -Encoding UTF8
Write-Output $pullline
