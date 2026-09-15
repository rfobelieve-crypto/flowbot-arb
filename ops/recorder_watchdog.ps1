# recorder_watchdog.ps1 — arb 的常駐 WS 錄製器看門狗（2026-09-15 從 flow_system exit_paths_watchdog 移植）
#
# 管四支：recorders/hl_tape.py、hl_mid.py、lighter_tape.py、lighter_mid.py。
# 它們寫的資料（D:/flowbot_data/{hl,lighter}/...）是 HMM 工具在讀的，所以 2026-09-15
# 使用者決定「四支全搬」到 arb。D: 的資料路徑沒變，flow_system 的研究照樣從外面讀。
#
# **判準是產物不是進程**：旗標的 asof 超過 $StaleMin 分鐘、或 ok=false，就殺掉重啟。
# 一個卡在 WS 讀取上的殭屍進程，工作管理員看起來完全正常（flow_system mistake.md 2026-08-19）。
#
# **為什麼不併進 arb_watchdog.ps1**：那支只看行程在不在、從不殺。同一個行程掛兩個
# 判準相反的看門狗，其中一支會殺掉另一支剛拉起來的東西 —— 一個錄製器只能有一個看門狗。
#
# 三個從事故學來的細節（都在 flow_system 那支原檔的註解裡有經過）：
#   1. 讀旗標一定要 -Encoding UTF8（reason 是中文；cp950 讀壞 -> 殺掉健康行程，hl_mid 被殺過 70 次）
#   2. 讀不懂旗標時退回檔案 mtime 判斷，**預設不動手**
#   3. 錄製器啟動時自己先寫一份 ok=True 的「啟動中」旗標，否則這支會在第一次落盤前殺掉它
#
# 行程比對只用 'recorders\<name>.py' —— 不用檔名本身，否則會把 flow_system 那支同名的
# 舊行程當成「在跑」（搬家那天兩邊的檔名一樣）。
#
# 排程 Arb_RecorderWatchdog，每 5 分鐘，經 ops\run_hidden.vbs（不彈視窗）。

$ErrorActionPreference = 'Continue'
$Root = Split-Path -Parent $PSScriptRoot
$Log  = Join-Path $Root 'recorders\logs\watchdog.log'
$Py   = 'python'
$StaleMin = 20

New-Item -ItemType Directory -Force -Path (Split-Path $Log) | Out-Null
function Say($m) { "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $m" | Add-Content -Path $Log -Encoding UTF8 }

$Jobs = @(
  @{ name = 'hl_tape';      script = 'recorders\hl_tape.py';      flag = 'results\hl_tape_last.json';      log = 'recorders\logs\hl_tape.log' },
  @{ name = 'hl_mid';       script = 'recorders\hl_mid.py';       flag = 'results\hl_mid_last.json';       log = 'recorders\logs\hl_mid.log' },
  @{ name = 'lighter_tape'; script = 'recorders\lighter_tape.py'; flag = 'results\lighter_tape_last.json'; log = 'recorders\logs\lighter_tape.log' },
  @{ name = 'lighter_mid';  script = 'recorders\lighter_mid.py';  flag = 'results\lighter_mid_last.json';  log = 'recorders\logs\lighter_mid.log' }
)

# 單一 STOP 檔：recorders\stop\<name>.stop 存在就不拉起（跟 engine\logs\stop 同一個慣例）
$StopDir = Join-Path $Root 'recorders\stop'

# **重啟上限（2026-09-15，arb session 提出）**：每次重啟 = 一次 WS 重連 = 一次打在
# 同一個 IP 的連線額度上。Lighter 的 WAF 擋的正是重連，而 MON 的 Lighter 腿也在這個
# IP 上。一支崩潰迴圈的錄製器會在 MON 上線前把額度燒掉。所以 60 分鐘內最多拉起
# $RestartCap 次，超過就停手、寫一行 RESTART CAP —— 旗標會自然過期，flow_system
# 的新鮮度看板那一列會變紅，那才是該叫人的地方，不是無限重試。
$RestartCap = 3
$RestartWindowMin = 60
$RestartState = Join-Path $Root 'recorders\logs\restarts.json'
$restarts = @{}
if (Test-Path $RestartState) {
  try {
    $raw = Get-Content $RestartState -Raw -Encoding UTF8 | ConvertFrom-Json
    foreach ($p in $raw.PSObject.Properties) { $restarts[$p.Name] = @($p.Value) }
  } catch { $restarts = @{} }
}
$nowEpoch = [double][DateTimeOffset]::UtcNow.ToUnixTimeSeconds()

foreach ($j in $Jobs) {
  $script = Join-Path $Root $j.script
  if (-not (Test-Path $script)) { Say "$($j.name): script not found -> $script（跳過）"; continue }
  if (Test-Path (Join-Path $StopDir "$($j.name).stop")) { continue }

  $needle = '*' + $j.script + '*'
  $running = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
               Where-Object { $_.CommandLine -like $needle })

  $stale = $true
  $flagPath = Join-Path $Root $j.flag
  if (Test-Path $flagPath) {
    try {
      $f = Get-Content $flagPath -Raw -Encoding UTF8 | ConvertFrom-Json
      $age = (New-TimeSpan -Start ([datetime]::Parse($f.asof).ToUniversalTime()) -End ([datetime]::UtcNow)).TotalMinutes
      $stale = ($age -gt $StaleMin) -or (-not $f.ok)
      if ($stale) { Say "$($j.name): flag stale/not-ok (age $([math]::Round($age,1))m, ok=$($f.ok))" }
    } catch {
      $mAge = (New-TimeSpan -Start (Get-Item $flagPath).LastWriteTimeUtc -End ([datetime]::UtcNow)).TotalMinutes
      $stale = ($mAge -gt $StaleMin)
      Say "$($j.name): flag unreadable ($_) -> mtime age $([math]::Round($mAge,1))m, stale=$stale"
    }
  } else { Say "$($j.name): no flag yet" }

  if ($running.Count -gt 0 -and -not $stale) { continue }

  $recent = @($restarts[$j.name] | Where-Object { $_ -and ($nowEpoch - [double]$_) -lt ($RestartWindowMin * 60) })
  if ($recent.Count -ge $RestartCap) {
    Say "$($j.name): RESTART CAP ($($recent.Count) starts in ${RestartWindowMin}m) -> not restarting; needs a human"
    $restarts[$j.name] = $recent
    continue
  }

  if ($running.Count -gt 0 -and $stale) {
    Say "$($j.name): running but stale -> killing $($running.Count) pid(s)"
    $running | ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force } catch {} }
    Start-Sleep -Seconds 2
  }

  $out = Join-Path $Root $j.log
  Say "$($j.name): starting"
  Start-Process -FilePath $Py -ArgumentList $j.script -WorkingDirectory $Root `
                -RedirectStandardOutput $out -RedirectStandardError "$out.err" `
                -WindowStyle Hidden
  $restarts[$j.name] = @($recent) + @($nowEpoch)
}

try {
  ($restarts | ConvertTo-Json -Depth 3) | Set-Content -Path $RestartState -Encoding UTF8
} catch { Say "restart state not written: $_" }
