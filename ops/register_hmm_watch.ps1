# register_hmm_watch.ps1 — 把 HMM live 看護註冊成 Windows 排程（arb 版，2026-09-15）
#
# 2026-09-14 原本在 flow_system/research/ops，註冊的工作叫 FlowBot_HmmWatch。
# 2026-09-15 HMM 整條搬進 arb，工作改名 Arb_HmmWatch，**本腳本會順手移除舊的那個**：
# 兩個同時在跑 = 同一個狀態轉換在 Discord 響兩次（兩份狀態檔各記各的）。
#
# 為什麼要有這個檔而不是「跑一次指令就算了」：
# **排程的 action 是 grep 不到的**。2026-09-04 套利線搬 repo 時枚舉出 10 個
# 持有舊路徑的地方，第 11 個是看門狗的排程 action——它不在 repo 裡。
# 把註冊動作留成檔案，至少讓「這台機器上有這個排程」在 repo 裡看得見。
#
# 為什麼是排程而不是背景迴圈：一個會送真單的引擎，它的安全網不可以依賴
# 某個對話 session 活著（2026-09-14 背景迴圈被 harness 殺了兩次）。
#
# 為什麼要 run_hidden.vbs：Register-ScheduledTask 預設 LogonType=Interactive，
# 每 5 分鐘彈一個主控台視窗；改 S4U 要管理員。wscript.exe 沒有主控台。
#
# 驗收判準是**產物**：results\hmm_watch.log 長出新行。不是 LastTaskResult。

$ErrorActionPreference = 'Stop'
$ops  = 'C:\Users\rfo\Desktop\flowbot\arb\ops'
$name = 'Arb_HmmWatch'
$old  = 'FlowBot_HmmWatch'

foreach ($f in @('run_hidden.vbs', 'hmm_watch.bat', 'hmm_watch.py', 'live_hmm.py', 'alert.py')) {
    if (-not (Test-Path (Join-Path $ops $f))) {
        throw "$f 不在 $ops —— 先別註冊，會註冊出一條壞路徑"
    }
}

$arg = '//B //Nologo "{0}\run_hidden.vbs" "{0}\hmm_watch.bat"' -f $ops
$act = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument $arg
$trg = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
# IgnoreNew：一輪還沒跑完不要再疊一個。ExecutionTimeLimit 10 分鐘：卡住就自己收掉。
$set = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName $name -Action $act -Trigger $trg -Settings $set -Force | Out-Null

if (Get-ScheduledTask -TaskName $old -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $old -Confirm:$false
    Write-Output ('已移除舊排程 ' + $old)
}

$t = Get-ScheduledTask -TaskName $name
Write-Output ('已註冊 ' + $name)
Write-Output ('  Execute  : ' + $t.Actions[0].Execute)
Write-Output ('  Arguments: ' + $t.Actions[0].Arguments)
Write-Output ('  Repeat   : ' + $t.Triggers[0].Repetition.Interval)
Write-Output ('  State    : ' + $t.State)
Write-Output ''
Write-Output '驗收看產物，不看退出碼：results\hmm_watch.log 要長出新行'
