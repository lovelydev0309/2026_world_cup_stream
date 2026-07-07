<#
  channel-health-alert.ps1
  Desktop toast alerts on Windows when a re-stream channel goes unhealthy.

  It polls the live status dashboard JSON (published by the server's synthetic monitor at
  https://stream.tv247on.com/player/status.json) and pops a Windows notification whenever a
  channel breaks — or recovers. Nothing is installed on the server; this runs on YOUR PC.

  RUN IT:
     powershell -ExecutionPolicy Bypass -File .\channel-health-alert.ps1
  If Windows blocked the downloaded file first:
     Unblock-File .\channel-health-alert.ps1
  AUTO-START AT LOGIN: see the note at the very bottom of this file.
#>

param(
  [string]$StatusUrl      = "https://stream.tv247on.com/player/status.json",
  [int]   $IntervalSeconds = 120,   # how often to check
  [int]   $StaleMinutes    = 12,    # warn if the monitor itself stops updating
  [switch]$AlertAudioWarnings       # also toast SILENT / NO_AUDIO (off by default = less noise)
)

$ErrorActionPreference = "Stop"
$script:AppId = "ChannelHealth.Monitor"

# ---- notification helper: native Win10/11 toast, with graceful fallbacks --------------
function Show-Toast {
  param([string]$Title, [string]$Message)
  # 1) BurntToast module if the user installed it (nicest look)
  if (Get-Module -ListAvailable -Name BurntToast) {
    try { Import-Module BurntToast -ErrorAction Stop
          New-BurntToastNotification -Text $Title, $Message | Out-Null; return } catch {}
  }
  # 2) Native WinRT toast (Windows 10/11, nothing to install)
  try {
    $null = [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
    $null = [Windows.UI.Notifications.ToastNotification,        Windows.UI.Notifications, ContentType = WindowsRuntime]
    $null = [Windows.Data.Xml.Dom.XmlDocument,                 Windows.Data.Xml.Dom,     ContentType = WindowsRuntime]
    $tpl   = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(
               [Windows.UI.Notifications.ToastTemplateType]::ToastText02)
    $texts = $tpl.GetElementsByTagName("text")
    [void]$texts.Item(0).AppendChild($tpl.CreateTextNode($Title))
    [void]$texts.Item(1).AppendChild($tpl.CreateTextNode($Message))
    $toast = [Windows.UI.Notifications.ToastNotification]::new($tpl)
    [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($script:AppId).Show($toast)
    return
  } catch {}
  # 3) Fallback: system-tray balloon tip (works everywhere)
  try {
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    $ni = New-Object System.Windows.Forms.NotifyIcon
    $ni.Icon = [System.Drawing.SystemIcons]::Warning
    $ni.Visible = $true
    $ni.ShowBalloonTip(8000, $Title, $Message, [System.Windows.Forms.ToolTipIcon]::Warning)
    Start-Sleep -Seconds 9
    $ni.Dispose()
    return
  } catch {}
  # 4) Last resort: console line
  Write-Host "[$Title] $Message" -ForegroundColor Yellow
}

Write-Host "Channel health alerter started."
Write-Host "  watching : $StatusUrl"
Write-Host "  interval : $IntervalSeconds s   (Ctrl+C to stop)"
Show-Toast "Channel Health Monitor" "Started — watching all channels."

# statuses considered fine (don't alert). Audio warnings are opt-in.
$okStatuses = if ($AlertAudioWarnings) { @("OK") } else { @("OK", "SILENT", "NO_AUDIO") }

$prevBad     = @{}       # ch(string) -> "Name: STATUS"   (unhealthy last cycle)
$warnedStale = $false
$warnedFetch = $false

while ($true) {
  try {
    $bust = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    $data = Invoke-RestMethod -Uri "$StatusUrl`?_=$bust" -TimeoutSec 20
    $warnedFetch = $false

    # is the monitor itself alive?
    try {
      $ageMin = ([DateTime]::UtcNow - [DateTime]::Parse($data.updated).ToUniversalTime()).TotalMinutes
    } catch { $ageMin = 0 }
    if ($ageMin -gt $StaleMinutes) {
      if (-not $warnedStale) {
        Show-Toast "! Monitor may be down" ("Dashboard hasn't updated in {0:N0} min." -f $ageMin)
        $warnedStale = $true
      }
    } else { $warnedStale = $false }

    # current unhealthy set
    $nowBad = @{}
    foreach ($c in $data.channels) {
      if ($okStatuses -notcontains $c.status) {
        $nowBad[[string]$c.ch] = "$($c.name): $($c.status)"
      }
    }

    $newIssues = @(); foreach ($k in $nowBad.Keys)  { if (-not $prevBad.ContainsKey($k)) { $newIssues += $nowBad[$k] } }
    $recovered = @(); foreach ($k in $prevBad.Keys) { if (-not $nowBad.ContainsKey($k)) { $recovered += ($prevBad[$k] -split ':')[0] } }

    if ($newIssues.Count -gt 0) {
      $title = if ($newIssues.Count -eq 1) { "! Channel problem" } else { "! $($newIssues.Count) channels unhealthy" }
      Show-Toast $title (($newIssues | Select-Object -First 6) -join "`n")
    }
    if ($recovered.Count -gt 0) {
      Show-Toast "Recovered" (($recovered | Select-Object -First 8) -join ", ")
    }

    $prevBad = $nowBad
    $stamp = Get-Date -Format "HH:mm:ss"
    $extra = if ($nowBad.Count) { " | issues: " + (($nowBad.Values) -join '; ') } else { "" }
    Write-Host "[$stamp] $($data.ok)/$($data.total) healthy$extra"
  }
  catch {
    if (-not $warnedFetch) {
      Show-Toast "! Dashboard unreachable" "Can't fetch channel status. Check internet / server."
      $warnedFetch = $true
    }
    Write-Host "[fetch error] $($_.Exception.Message)" -ForegroundColor Red
  }
  Start-Sleep -Seconds $IntervalSeconds
}

# ---------------------------------------------------------------------------------------
# AUTO-START AT LOGIN (so it's always watching):
#   1) Press Win+R, type:  shell:startup   and press Enter.
#   2) In that Startup folder, create a shortcut with this target:
#        powershell.exe -WindowStyle Hidden -ExecutionPolicy Bypass -File "C:\path\to\channel-health-alert.ps1"
#   It will then launch quietly every time you log in and toast you on any channel problem.
# ---------------------------------------------------------------------------------------
