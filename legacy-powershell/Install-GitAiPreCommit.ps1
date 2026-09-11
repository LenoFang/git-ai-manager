<#
.SYNOPSIS
    Install git-ai auto-AI hooks into main Git repos under a projects root.

.DESCRIPTION
    - Finds immediate subdirectories under -ProjectsRoot that contain a real .git directory
      (linked worktrees are skipped; their commits are covered by the main repo's hooks).
    - Creates .git\hooks\pre-commit and .git\hooks\post-commit if missing.
    - pre-commit calls precommit-ai.ps1, which marks staged changes as AI (mock_ai).
    - post-commit calls postcommit-ai.ps1, which records commit_sha -> repo path.
    - Configures the global git-ai hook git_ai_hooks.post_notes_updated to call
      notes-updated-ai.ps1, which rewrites mock_ai notes to codex / unknown.
    - This script is portable: keep it in the same folder as precommit-ai.ps1,
      postcommit-ai.ps1 and notes-updated-ai.ps1, then copy all files.
    - Run this on each computer; on a new machine install git-ai first.

.PARAMETER ProjectsRoot
    Root folder that contains your Git repositories. Defaults to D:\Projects.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\Install-GitAiPreCommit.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\Install-GitAiPreCommit.ps1 -ProjectsRoot C:\Users\me\repos
#>
param(
    [string]$ProjectsRoot = 'D:\Projects'
)

$ErrorActionPreference = 'Stop'

$preHelper   = Join-Path $PSScriptRoot 'precommit-ai.ps1'
$postHelper  = Join-Path $PSScriptRoot 'postcommit-ai.ps1'
$notesHelper = Join-Path $PSScriptRoot 'notes-updated-ai.ps1'

if (-not (Test-Path -LiteralPath $ProjectsRoot)) {
    throw "Projects root not found: $ProjectsRoot"
}
if (-not (Test-Path -LiteralPath $preHelper)) {
    throw "Helper script not found: $preHelper"
}
if (-not (Test-Path -LiteralPath $postHelper)) {
    throw "Helper script not found: $postHelper"
}
if (-not (Test-Path -LiteralPath $notesHelper)) {
    throw "Helper script not found: $notesHelper"
}

$preHookContent = @"
#!/bin/sh
# git-ai auto-attribution adapter: mark staged changes as AI before commit.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File '$($preHelper.Replace('\','/'))'
"@

$postHookContent = @"
#!/bin/sh
# git-ai commit -> repo mapping for the global post_notes_updated hook.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File '$($postHelper.Replace('\','/'))'
"@

$hookDefs = @(
    @{ Name = 'pre-commit';  Content = $preHookContent;  Marker = 'precommit-ai.ps1' },
    @{ Name = 'post-commit'; Content = $postHookContent; Marker = 'postcommit-ai.ps1' }
)

$repos = Get-ChildItem -LiteralPath $ProjectsRoot -Directory -Force |
    Where-Object {
        $gitPath = Join-Path $_.FullName '.git'
        Test-Path -LiteralPath $gitPath -PathType Container
    } |
    Sort-Object Name

$installed = @()
$already   = @()
$skipped   = @()

foreach ($repo in $repos) {
    foreach ($hookDef in $hookDefs) {
        $hookPath = Join-Path $repo.FullName ".git\hooks\$($hookDef.Name)"
        $label    = "$($repo.Name)/$($hookDef.Name)"

        if (Test-Path -LiteralPath $hookPath) {
            $existing = Get-Content -LiteralPath $hookPath -Raw -ErrorAction SilentlyContinue
            if ($existing -and $existing -match [regex]::Escape($hookDef.Marker)) {
                $already += $label
            } else {
                $skipped += $label
            }
            continue
        }

        $hooksDir = Split-Path -Parent $hookPath
        if (-not (Test-Path -LiteralPath $hooksDir)) {
            New-Item -ItemType Directory -Path $hooksDir -Force | Out-Null
        }

        Set-Content -LiteralPath $hookPath -Value $hookDef.Content -Encoding ascii
        $installed += $label
    }
}

# Configure the global git-ai hook that rewrites mock_ai notes to codex/unknown.
$gitAi = $env:GIT_AI_EXE
if (-not $gitAi -or -not (Test-Path -LiteralPath $gitAi)) {
    $cmd = Get-Command git-ai -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source) {
        $gitAi = $cmd.Source
    } else {
        $candidate = Join-Path $env:USERPROFILE '.git-ai\bin\git-ai.exe'
        if (Test-Path -LiteralPath $candidate) { $gitAi = $candidate }
    }
}

$gitAiHookConfigured = $false
if ($gitAi) {
    $hookCommand = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$notesHelper`" -ProjectRoots `"$ProjectsRoot`""
    try {
        & $gitAi config unset git_ai_hooks.post_notes_updated 2>$null | Out-Null
        & $gitAi config --add git_ai_hooks.post_notes_updated $hookCommand 2>$null | Out-Null
        & $gitAi bg restart --hard 2>$null | Out-Null
        $gitAiHookConfigured = $true
    } catch {
        $gitAiHookConfigured = $false
    }
}

Write-Host "=== git-ai hook install summary ==="
Write-Host "Projects root : $ProjectsRoot"
Write-Host "Pre helper    : $preHelper"
Write-Host "Post helper   : $postHelper"
Write-Host "Notes helper  : $notesHelper"
Write-Host "git-ai hook   : $(if ($gitAiHookConfigured) { 'configured' } else { 'NOT configured (install git-ai or set GIT_AI_EXE)' })"
Write-Host ""
Write-Host "Installed ($($installed.Count)):"
$installed | ForEach-Object { Write-Host "  + $_" }
Write-Host ""
Write-Host "Already installed ($($already.Count)):"
$already | ForEach-Object { Write-Host "  = $_" }
Write-Host ""
Write-Host "Skipped - has a different hook ($($skipped.Count)):"
$skipped | ForEach-Object { Write-Host "  ! $_" }
Write-Host ""
Write-Host "Done."
