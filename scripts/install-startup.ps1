$ErrorActionPreference = 'Stop'
$discordRoot = Split-Path -Parent $PSScriptRoot
$discordVault = Join-Path $discordRoot 'credentials.dpapi'
if (-not (Test-Path -LiteralPath $discordVault)) { throw 'Authenticate through the Stealth browser first.' }
$discordUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$discordAcl = Get-Acl -LiteralPath $discordVault
$discordAcl.SetAccessRuleProtection($true, $false)
$discordAcl.SetAccessRule([Security.AccessControl.FileSystemAccessRule]::new($discordUser, 'FullControl', 'Allow'))
Set-Acl -LiteralPath $discordVault -AclObject $discordAcl
$discordPython = Join-Path $discordRoot '.venv\Scripts\pythonw.exe'
$discordEntry = Join-Path $discordRoot 'local_server.py'
$discordTaskName = 'Codex Discord Personal MCP'
$discordExisting = Get-ScheduledTask -TaskName $discordTaskName -ErrorAction SilentlyContinue
if ($discordExisting -and $discordExisting.Actions.Execute -ne $discordPython) { throw 'A different task already uses this name.' }
$discordAction = New-ScheduledTaskAction -Execute $discordPython -Argument ('"' + $discordEntry + '"') -WorkingDirectory $discordRoot
$discordTrigger = New-ScheduledTaskTrigger -AtLogOn -User $discordUser
$discordPrincipal = New-ScheduledTaskPrincipal -UserId $discordUser -LogonType Interactive -RunLevel Limited
$discordSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $discordTaskName -Action $discordAction -Trigger $discordTrigger -Principal $discordPrincipal -Settings $discordSettings -Description 'Authenticated localhost Discord MCP for the current personal account, with sensitive deletions blocked.' -Force | Out-Null
Start-ScheduledTask -TaskName $discordTaskName
Write-Output 'Installed and started the persistent Discord MCP task.'
