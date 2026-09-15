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
  # HMM 候選群（2026-09-14，TODO §1.41b）。全部 --shadow：完整策略跑、
  # 一張單都不送。G2 讀它們各自的 shadow.csv。設定由
  # arblib/make_hmm_config.py **從量測生成**，不是複製的。
  # HL **core** 上的候選（2026-09-14）：不用動任何資金 —— HIP-3 的
  # io/para/xyz 是獨立保證金池而且都是 $0.00，core 有 $98.30。
  # ***** 2026-09-14 18:2x：以下七支 shadow 全部停用。*****
  # 理由不是「先省資源」,是**它們產不出還需要的東西**：
  #   OPENAI/ANSEM/MINIMAX  ok=False「ACCOUNT OUT OF MARGIN」——
  #     HIP-3 的 io/para/xyz 池都是 $0.00,它們永遠不可能成交,決策數 0。
  #   GRAM   決策 0（價差太窄,一次都沒報過價）
  #   MNT    決策 94、CHIP 3018、GMX 18216 —— 三個都已被新的五關判掉
  #     （CHIP 切片 $0.03、吃單流 97.4% 單向；GMX 市場一天成交 45 筆）
  # 而它們同時是 WAF 預算的主要消耗者：`_http_keepalive_loop` 只在
  # `not record_only` 時起,所以 shadow 每 10 秒打一次兩腿,
  # 七支就是 84 req/分,而九支 record-only 加起來才 18。
  # **九支 record-only 留著** —— 它們餵 §0.75 的 minutes.csv,而且很省。
  # 要重開哪一支,先讓它過 arblib/hmm_universe.py 的關。
  # 'GRAM'    = @('--symbol GRAM ',    'run_recorder_GRAM.bat')
  # 'CHIP'    = @('--symbol CHIP ',    'run_recorder_CHIP.bat')
  # 'MNT'     = @('--symbol MNT ',     'run_recorder_MNT.bat')
  # OPENAI 的 CLI 旗標是 **OAI**（HL 側 io:OAI）,不是 OPENAI ——
  # config.py 對 HL 腿用 CLI 的 symbol,而兩所對同一資產叫不同名字。
  # 'OPENAI'  = @('--symbol OAI ',     'run_recorder_OPENAI.bat')
  # 'ANSEM'   = @('--symbol ANSEM ',   'run_recorder_ANSEM.bat')
  # 'MINIMAX' = @('--symbol MINIMAX ', 'run_recorder_MINIMAX.bat')
  # ***** MET（Meteora）—— 這一個在送真單。2026-09-14 起。*****
  # 使用者當天第二次明確授權（第 9 次 override 寫著「翻成 live 要使用者再
  # 說一次」）。從 record-only 換成 live 的理由不是它過了 G2（少數側 18.8%
  # < 20%），是 **G2 以外的三個判準在 shadow 下量不到**：
  # shadow_decisions 與 quotes_cancelled 逐筆相等而 quotes_rested 恆為 0，
  # 所以 M2／M3／M4 的分母永遠是零，再跑幾天也不會出現。
  #
  # **看門狗對這一列的責任因此不一樣**：它重啟的是一個會下單的行程。
  # 重啟本身是安全的（HALT 是單向的，而重啟會用 strict=True 重讀真實部位），
  # 但要停掉它**不可以只靠殺行程** —— 這張表會把它拉回來。
  # 停止的順序寫在 run_hmm_MET.bat 的檔頭：先 `echo flat > logs\MET\control.cmd`
  # 等它平完，再把這一列註解掉，最後才殺行程。
  # 2026-09-14 12:45-12:52 曾短暫停用：Lighter 的 CloudFront WAF 對我們回
  # `x-amzn-waf-action: captcha`（HTTP 405），而引擎啟動要讀
  # /api/v1/orderBooks 拿市場表，讀不到就 5 次重試後崩潰 -> .bat 迴圈 30 秒
  # 再來一輪 = 一個產不出東西卻在燒**共用 IP 預算**的迴圈，而那個預算是另外
  # 16 支錄製器在用的（下面 scanner 那段搬去 Railway 就是為了同一件事）。
  # 12:52 連探三次都 200 -> 解除。**這段留著當下次的處置**：
  # WAF 擋的時候不要重開，因為重連正是它最會擋的動作，而那時引擎手上有部位。
  # ***** FIL（Filecoin）—— 現在是這一個在送真單。2026-09-14 15:2x 起。*****
  # 取代 MET。MET 不是參數沒調好：它在 Lighter 的**中位成交切片是 $0.27**,
  # 而兩腿的最小單都是 $10 -> 93% 的成交在收到的當下對沖不掉。實跑 20 分鐘、
  # 17 筆成交,HL 那一腿**一次都沒被碰到**,整輪是單腿方向性交易。
  # FIL 的中位切片 $100（是最小單的十倍）、半價差 8.4 bps、兩所同名且價格
  # 差 0.19%。我們整張單 $15 而對手中位一口 $100 —— 通常一口吃光整張,
  # 立刻對沖就會觸發。
  #
  # **看門狗對這一列的責任跟別人不一樣**：它重啟的是一個會下單的行程。
  # 重啟本身安全（HALT 單向,重啟用 strict=True 重讀真實部位),但要停掉它
  # 不可以只靠殺行程 —— 這張表會把它拉回來。順序寫在 run_hmm_FIL.bat 檔頭：
  # 先 `echo flat > logs\FIL\control.cmd` 等它平完,再註解掉這一行,最後殺行程。
  # FIL 2026-09-14 16:30 停用。一小時 0 成交、只報價 2 次 —— 不是壞掉,
  # 是它的淨邊際 +0.46 bps 貼在損益平衡線上（Lighter 半價差 7.86 減 HL
  # 半價差 2.5 減費用 4.90）,所以引擎大部分時間不報價**是對的**。
  # 留著跑只會消耗共用的 WAF 預算,而那是 17 支錄製器在用的。
  # 判準的修正登記在 TODO；在那之前不再換標的。
  # 'FIL'     = @('--symbol FIL ',  'run_hmm_FIL.bat')
  # MET 2026-09-14 退場（不是暫停,是這個標的做不了 HMM,理由見上）。
  # 啟動器留在磁碟上當紀錄,但**不註冊** —— 註冊等於讓看門狗把一個已知
  # 跑不起來的策略重新拉起來送真單。豁免寫在 tests/test_watchdog_registry.py。
  # 'MET'     = @('--symbol MET ',  'run_hmm_MET.bat')
  # ***** AERO（Aerodrome）—— 現在是這一個在送真單。2026-09-14 起。*****
  # 今天第四個標的,但**第一個用「能複現已付費答案的尺」挑出來的**。
  # GMX/MET/FIL 都是被一把偏向放行的篩選挑出來的（G1 門檻 5.0 低於損益
  # 平衡 7.4、G3 數的分鐘灰塵流全滿足、M3 的零點是半價差不是零）。
  # AERO 過了 arblib/hmm_universe.py 的五關（128 個市場取 6 個）,
  # 而那支的自曝檢查現在能複現實盤：MET 過不了 G4/G5、FIL 過不了 G1。
  # 選它的理由是**殺死 MET 的那兩項它都最好**：
  #   淨邊際 4.51 bps（六個裡最高）  吃單流少數側 47.4%（MET 是 6.9%）
  #   中位切片 $39.93（對沖最小單的四倍,而我們整張單才 $15）
  #
  # **停它的順序**（2026-09-14 在 MET 上弄錯過一次,看門狗把一個會送真單的
  # 引擎拉回來跑了 2 小時 21 分無人看管）：
  #   1) echo flat > logs\AERO\control.cmd  2) 註解掉這一行
  #   3) 才殺行程  4) **跨過一個 5 分鐘週期再確認一次** —— 當下回空不是證據
  # AERO 2026-09-14 18:20 停用。**不是壞掉,是它的邊際在我們上線後蒸發了**：
  # 上線時 Lighter 半價差中位 10.57 bps,一小時後塌到 5.31 ->
  # 淨 5.31 - HL 1.31 - 費用 4.90 = **-0.90 bps**,引擎正確地停止報價。
  # 一小時 122 張報價、2 筆成交（都在最初 26 秒）、帳戶淨 -$0.016。
  # 留著只會消耗共用的 WAF 預算。判準已改（G6 淨值/sigma@延遲）,重篩再說。
  # 'AERO'    = @('--symbol AERO ', 'run_hmm_AERO.bat')
  # ***** XPL —— 現在是這一個在送真單。2026-09-14 18:4x 起。*****
  # 選它的理由是**它的 Lighter 日成交 $602k 是候選裡最高的**（AERO 只有
  # $227k）,而 AERO 的病是「報價 122 次只成交 2 次」—— 我們最缺的是樣本。
  # 其餘：淨 4.03 bps、G6 1.60、中位切片 $366（是整張單的 24 倍,一口吃單
  # 就吃光,立刻對沖直接觸發）、HL 對沖腿日成交 $9.3M。
  #
  # **誠實註記**：新加的 G6/G7 兩關都**沒通過 AERO 的反向證明** ——
  # 它們解釋不了「報了 122 次沒人吃」。那指向佇列位置,離線量不到。
  # 所以這一輪 live 同時檢定兩件事：(1) 十分鐘一次的斷線是不是我們自己
  # 造成的（停掉七支 shadow 後,現在只有這一支報得出來 —— record-only 的
  # `_reconcile_loop` 不起,結構上報不了,所以先前那排 0 不是證據）,
  # (2) 低成交率是 AERO 特有還是通病。
  #
  # 停它的兩步（2026-09-14 改）：flat -> `type nul > logs\stop\<啟動器>.stop`。
  # 舊的四步裡有一步是「註解這一行」,而那一步沒有任何當下的回饋,我漏掉過
  # 兩次。現在兩個重拉者都讀同一個 STOP 檔,所以停止是一個看得見的狀態。
  #
  # **XPL 已於 2026-09-14 20:16 平倉停機（STOP 檔）。** 它過了舊 G1（單側
  # +3.99）而且過了 G5（吃單流少數側 46.0%,流量非常兩側）,但兩側檢驗
  # 量到賣側 +14.40 / 買側 **-6.42**（基差 +10.41）—— 單側市場。實盤照著
  # 這個算術走完了全程：報價側別 332:0、庫存堆到 $58.7/$60、十分鐘 95 次
  # blocked by position caps,今日 -$0.07。註冊留著、靠 STOP 檔停,
  # 因為「它為什麼停」寫在這裡比寫在別的檔案裡有用。
  'XPL'     = @('--symbol XPL ', 'run_hmm_XPL.bat')

  # MON（2026-09-14 20:2x 上線,接替 XPL；市場數維持 1）。
  #
  # **它是全宇宙唯一一個兩側都為正的市場**（132 個裡）：
  #     賣側 +8.69 bps   買側 +2.29 bps   基差 +3.20
  # 差 0.71 bps 沒到 G1 門檻 3.0,所以嚴格說它**沒有通過**新關卡 ——
  # 這是知情的,理由是其餘 131 個要嘛買側是負的（XPL -6.42、PENDLE -4.19、
  # POL -7.29）,要嘛差 G5/G7 更多。0 個通過是這條線的真實狀態,而我們需要
  # 的是實盤樣本,不是再等一週。
  #
  # 其餘：切片 $100.5（整張單 $15 的 6.7 倍）、吃單流少數側 35.4%、
  # 在線 90.1%（除 SKR 外最高）、Lighter 日成交 $139k、HL 對沖腿 $1.55M。
  #
  # **這一輪同時檢定兩件事**：
  #   (1) 兩側檢驗有沒有預測力 —— MON 的報價側別應該**明顯不是 332:0**；
  #   (2) 折讓（relief_frac 0.1,本檔第一個開的）會不會讓庫存自穩在約
  #       七成上限（$42/腿）而不是貼在 $60。兩個都可否證。
  'MON'     = @('--symbol MON ', 'run_hmm_MON.bat')
  # HMM（對沖做市）Stage 1。現在是 record-only；翻 live 只改 .bat。
  # 'HMM_GMX' = @('--symbol GMX ', 'run_hmm_GMX.bat')
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
$stopped = @()
foreach ($name in $Members.Keys) {
  $sig, $bat = $Members[$name]
  $alive = @($procs | Where-Object { $_ -like "*main.py*$sig*" -or ($name -in 'universe' -and $_ -like "*$sig*") }).Count
  if ($alive -ge 1) { continue }

  # STOP 閘門（2026-09-14）。這個系統有**兩套**重拉機制 —— .bat 自己的
  # `:loop` + `timeout 30` + `goto loop`，以及這裡。兩套做同一件事，於是
  # 「停止」變成六個動作，而其中兩個沒有任何當下的回饋：
  #   flat -> 註解這張表 -> 殺 python -> 殺 cmd 包裝層 -> 等 5 分鐘 -> 複查
  # 2026-09-14 我漏掉第四步兩次，後果是一支已搬到 Railway 的掃描器多跑了
  # 21 小時（每輪 139 次 Lighter REST = 那天「每十分鐘斷線」的來源），
  # 以及一支已退場的 shadow 引擎多跑了十小時。
  #
  # 所以把「這支該不該跑」變成一個檔案，兩個重拉者都讀它：
  #   logs\stop\<啟動器檔名>.stop
  # .bat 那側的閘門是同一條路徑（`if exist logs\stop\%~n0.stop goto end`）。
  # 停止 = 建一個檔案；恢復 = 刪掉它。**一個看得見的狀態，不是一串要記得
  # 的動作** —— 這是 mistake.md 反覆講的那條：判準寫成產物，不要寫成程序。
  #
  # 註冊表這張表**仍然是權威**（不在表上的東西沒人拉）；STOP 檔是「在表上，
  # 但現在刻意不要跑」，它的好處是不必改程式碼、而且 .bat 那側也看得到。
  $stopFile = Join-Path $Root ('logs\stop\' +
              [IO.Path]::GetFileNameWithoutExtension($bat) + '.stop')
  if (Test-Path $stopFile) { $stopped += $name; continue }

  $dead += $name
  if (-not $DryRun) {
    Start-Process -FilePath (Join-Path $Root $bat) -WorkingDirectory $Root -WindowStyle Minimized
  }
}
$nRun = $Members.Count - $stopped.Count
$line = if ($dead.Count -eq 0 -and $stopped.Count -eq 0) {
          "$stamp UTC  all $($Members.Count) alive"
        } elseif ($dead.Count -eq 0) {
          # 被 STOP 檔停掉的成員**沒有**活著。沿用 "all N alive" 會讓一個
          # 刻意停掉的東西跟一個健康的東西印出同一行字,而那正是這支腳本
          # 存在要擋的事（mistake.md：未知/停止狀態不可以長得像已知的好狀態）。
          "$stamp UTC  $nRun/$($Members.Count) alive"
        } elseif ($DryRun) {
          "$stamp UTC  DRY-RUN would relaunch: $($dead -join ',')"
        } else {
          "$stamp UTC  RELAUNCHED: $($dead -join ',')"
        }
if ($stopped.Count -gt 0) {
  # 刻意印出來：一個被 STOP 檔停掉的成員，跟一個「活著」的成員在
  # "all N alive" 那一行裡長得一模一樣,而它們是完全不同的狀態。
  $line = $line + "  | STOPPED by flag: " + ($stopped -join ',')
}
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

# HALT 自動恢復（2026-09-15，使用者選的「只做 C」）。
#
# **只有一種 HALT 會被自動恢復**：net imbalance 超過 max_net_base，而且
# 裸曝險**現在已經平回容忍內**（= 引擎的 reduce-only 平倉成功了）。
# 其他六類（每日虧損 kill switch、曝險上限、連續錯誤、maker 迴圈崩潰、
# 簿口過期、波動熔斷）一律要人 —— 判準與理由寫在 halt_recover.py 檔頭，
# 每一道關卡都有一個「證明它擋得住」的測試（tests/test_halt_recover.py）。
#
# 為什麼放看門狗而不是看護裡：**動手與回報分成兩個行程**。它不送 Discord，只寫
# logs/<pair>/halt_recover.json，由 ops/hmm_watch.py 讀去報
# （2026-09-15 起看護也在 arb，排程 Arb_HmmWatch）。
#
# 只對 run_hmm_* 且沒有 STOP 旗標的跑 —— record-only 不會 HALT。
foreach ($m in $Members.GetEnumerator()) {
  $bat = $m.Value[1]
  if (-not $bat.StartsWith('run_hmm_')) { continue }
  $stopFile = Join-Path $Root ('logs\stop\' +
              [IO.Path]::GetFileNameWithoutExtension($bat) + '.stop')
  if (Test-Path $stopFile) { continue }
  try {
    $hr = & python (Join-Path $Eng 'tools\halt_recover.py') --pair $m.Key 2>&1
    $hrTxt = ($hr | Where-Object { "$_".Trim() }) -join ' | '
    if ($hrTxt) {
      $line = "$stamp UTC  halt_recover[$($m.Key)]: $hrTxt"
      Add-Content -Path $Log -Value $line -Encoding UTF8
      Write-Output $line
    }
  } catch {
    $line = "$stamp UTC  halt_recover[$($m.Key)] FAILED: $($_.Exception.Message)"
    Add-Content -Path $Log -Value $line -Encoding UTF8
    Write-Output $line
  }
}

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

# ---------------------------------------------------------------------------
# 名單外的陌生人（2026-09-14）
#
# 上面每一段問的都是「**該跑的有沒有在跑**」。這一段問相反的問題：
# 「**不該跑的有沒有還在跑**」—— 而這個專案的守衛從來沒有人問過後者。
# freshness 盯的是心跳停了，watchdog 盯的是成員死了；一個「早就該停、
# 但還活著」的東西在這兩面鏡子裡都是隱形的。
#
# 今天一次抓到兩個，而且是同一個形狀：**殺了 python，沒殺那個會把它重拉
# 的 cmd 包裝層**。
#   * run_scanner.bat          9/10 07:40 起。掃描器 9/13 搬到 Railway，我改
#     的是開機啟動檔 —— 那只擋「下次開機不要起」，而這台機器沒重開過，
#     所以那個 /K 包裝層在 9/13 22:08 又把它拉回來，跑了 21 小時。它一輪
#     序列打 139 次 Lighter REST，是「每十分鐘斷線一次」的來源。
#   * run_recorder_OPENAI.bat  同日 08:52 起，一支已經退場的 shadow 引擎。
#
# **只報告，絕不動手殺。** 會殺行程的自動化，它的例外分支預設必須是不動手
# （mistake.md 2026-09-11：看門狗把「我讀不懂旗標」當成「它該死」，殺掉 70
# 次健康的行程、丟掉六小時不可回填的資料）。這裡連「讀不懂」都談不上 ——
# 一個陌生人可能是人正在手動跑的東西，而那不是看門狗該替人決定的。
# ---------------------------------------------------------------------------
$knownSigs = @($Members.Values | ForEach-Object { ([string]$_[0]).Trim() })
$knownBats = @($Members.Values | ForEach-Object { ([string]$_[1]).ToLower() })
$strangers = @()

# (a) 引擎：長得像 main.py --symbol X，但 X 不在註冊表裡。
foreach ($c in $procs) {
  if ($c -notlike '*main.py*') { continue }
  $hit = $false
  foreach ($s in $knownSigs) { if ($c -like "*$s*") { $hit = $true; break } }
  if (-not $hit) {
    $sym = if ($c -match '--symbol\s+(\S+)') { $Matches[1] } else { '?' }
    $mode = if ($c -like '*--record-only*') { 'record' }
            elseif ($c -like '*--shadow*')  { 'shadow' }
            else                            { 'LIVE' }
    $strangers += "engine --symbol $sym ($mode)"
  }
}

# (b) 本機掃描器：9/13 起一律不該在本機跑（Railway 那支才是現役，而本檔
#     上面的 scan_pull 就是去拉它的產物 —— 兩支同時跑就是重複第二份）。
foreach ($c in $procs) {
  if ($c -like '*scanner.py*') { $strangers += 'scanner.py (moved to Railway)' }
}

# (c) 重拉迴圈本身。**這一項才是真正的元凶** —— 殺 python 完全不會動到它，
#     所以「行程清單現在是空的」從來不是它停了的證據。
Get-CimInstance Win32_Process -Filter "Name='cmd.exe'" | ForEach-Object {
  $cl = [string]$_.CommandLine
  if ($cl -match 'run_[A-Za-z0-9_]+\.bat') {
    $b = $Matches[0].ToLower()
    if ($knownBats -notcontains $b) {
      $strangers += ("launcher {0} (pid {1}, relaunches every 30s)" -f $b, $_.ProcessId)
    }
  }
}

# 這幾行刻意全是 ASCII。本檔沒有 BOM，PowerShell 5.1 於是用主控台碼頁
# （cp950）讀它 —— 中文待在**註解**裡無害（就像本檔其他地方），但中文一旦
# 進到**字串字面值**，全形括號會被解成別的位元組、括號配對斷掉，
# **整支腳本解析失敗**。2026-09-14 實際發生過一次：這一段第一版把
# 「已搬 Railway」寫進字串，看門狗當場變成 ParserError。
# 判斷法很便宜：字串裡有沒有非 ASCII。有就改掉，或替整個檔加 BOM
# （這裡選前者，因為本檔既有慣例就是「中文只在註解」）。
$sline = if ($strangers.Count -eq 0) {
           "$stamp UTC  strangers: none"
         } else {
           "$stamp UTC  STRANGERS (not killed, decide by hand): " +
             (($strangers | Sort-Object -Unique) -join ' | ')
         }
Add-Content -Path $Log -Value $sline -Encoding UTF8
Write-Output $sline
