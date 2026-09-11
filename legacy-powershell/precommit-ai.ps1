# git-ai pre-commit adapter
# Marks staged changes as AI-authored before any commit (manual or agent-driven).
# Used by a git pre-commit hook so DSh/manual commits are not left as untracked/human.
#
# Notes:
# - Uses `git-ai checkpoint mock_ai`, which records the staged changes as AI code.
# - This runs even when some real AI checkpoints already exist, so that remaining
#   untracked/human lines do not stay un-attributed. Tool metadata may become
#   mock_ai for those staged files; this is intentional for DSh/manual commits.
# - A failed git-ai checkpoint never blocks the commit; it only logs a warning.

$ErrorActionPreference = 'Continue'

# Locate git-ai on this machine. Order:
#   1. $env:GIT_AI_EXE
#   2. git-ai on PATH
#   3. $env:USERPROFILE\.git-ai\bin\git-ai.exe
#   4. $env:HOME\.git-ai\bin\git-ai.exe
$gitAi = $null
if ($env:GIT_AI_EXE -and (Test-Path -LiteralPath $env:GIT_AI_EXE)) {
    $gitAi = $env:GIT_AI_EXE
} else {
    $cmd = Get-Command git-ai -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source -and (Test-Path -LiteralPath $cmd.Source)) {
        $gitAi = $cmd.Source
    }
}
if (-not $gitAi) {
    $candidates = @(
        (Join-Path $env:USERPROFILE '.git-ai\bin\git-ai.exe'),
        (Join-Path $env:HOME '.git-ai\bin\git-ai.exe')
    ) | Where-Object { $_ } | Select-Object -Unique
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) {
            $gitAi = $candidate
            break
        }
    }
}
if (-not $gitAi) {
    Write-Warning '[git-ai-precommit] git-ai not found; skipping AI attribution.'
    exit 0
}

# Allow one-off opt-out, e.g. GIT_AI_AUTO_AI=0
if ($env:GIT_AI_AUTO_AI -eq '0') {
    exit 0
}

# Collect staged files that can contain added/modified lines.
$raw = git diff --cached --name-only --diff-filter=ACMR -z 2>$null
if ($LASTEXITCODE -ne 0) {
    exit 0
}

$files = @($raw -split "`0" | Where-Object { $_ })
if ($files.Count -eq 0) {
    exit 0
}

Write-Host '[git-ai-precommit] Marking staged changes as AI code...'
& $gitAi checkpoint mock_ai @files 2>&1 | ForEach-Object { Write-Host $_ }
if ($LASTEXITCODE -ne 0) {
    Write-Warning "[git-ai-precommit] git-ai checkpoint failed (exit=$LASTEXITCODE); commit will continue without AI attribution."
    exit 0
}

# Wait briefly for the daemon to persist the checkpoint for the current HEAD.
# Without this wait the commit can race ahead and be recorded as human/unknown.
$head = (git rev-parse HEAD 2>$null | Select-Object -First 1)
$gitDir = (git rev-parse --absolute-git-dir 2>$null | Select-Object -First 1)
if ($head -and $gitDir) {
    $checkpointFile = Join-Path (Join-Path $gitDir 'ai\working_logs') (Join-Path $head 'checkpoints.jsonl')
    $deadline = (Get-Date).AddSeconds(10)
    while ((Get-Date) -lt $deadline -and -not (Test-Path -LiteralPath $checkpointFile)) {
        Start-Sleep -Milliseconds 150
    }
    if (Test-Path -LiteralPath $checkpointFile) {
        # Give the daemon a moment to finish writing the file.
        Start-Sleep -Milliseconds 300
    }
}

exit 0
