<#
Deploy one reviewed commit of this repo to the local Discord MCP service.

The service never follows a branch. It runs a detached checkout of the exact commit
you pass here, with dependencies installed from the hash-locked requirements file.
A change on GitHub, including a compromised main branch or a moved tag, cannot
reach a machine until someone reviews it and runs this script with its full SHA.

  .\scripts\deploy-pinned.ps1 -Commit <40-char SHA> [-Tag v1.8.0-devino.1]
#>
param(
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$Commit,
    [string]$Tag
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$taskName = 'Codex Discord Personal MCP'
Set-Location $root

function Git { git @args; if ($LASTEXITCODE -ne 0) { throw "git $args failed" } }

$dirty = git status --porcelain --untracked-files=no
if ($dirty) { throw "Tracked files have local changes; commit them through a PR first:`n$dirty" }

Git fetch --quiet --tags origin main
git cat-file -e "$Commit^{commit}" 2>$null
if ($LASTEXITCODE -ne 0) { throw "Commit $Commit is not in origin." }
git merge-base --is-ancestor $Commit origin/main
if ($LASTEXITCODE -ne 0) { throw "Commit $Commit is not on origin/main, so it was never merged through review." }
if ($Tag) {
    $tagged = (git rev-parse "refs/tags/$Tag^{commit}").Trim()
    if ($tagged -ne $Commit) { throw "Tag $Tag points at $tagged, not $Commit. Refusing to deploy." }
}

Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
foreach ($p in (Get-NetTCPConnection -LocalPort 8085 -State Listen -ErrorAction SilentlyContinue).OwningProcess) { Stop-Process -Id $p -Force }

Git -c advice.detachedHead=false checkout --quiet --detach $Commit
if ((git rev-parse HEAD).Trim() -ne $Commit) { throw 'Checkout did not land on the requested commit.' }

& "$root\.venv\Scripts\python.exe" -m pip install --quiet --require-hashes --no-deps -r requirements-local.lock
if ($LASTEXITCODE -ne 0) { throw 'Locked dependency install failed.' }
Set-Content -LiteralPath (Join-Path $root '.deployed-commit') -Value $Commit -NoNewline

Start-ScheduledTask -TaskName $taskName
foreach ($i in 1..40) {
    Start-Sleep 3
    try {
        if ((Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8085/health -TimeoutSec 3).Content -match 'ready') {
            Write-Output "Deployed $Commit and the service is ready."
            return
        }
    } catch {}
}
throw "Deployed $Commit but the service did not report ready within 2 minutes; check server.log."
