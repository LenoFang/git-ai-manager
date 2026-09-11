# Normalize existing git-ai notes: tool "mock_ai" -> "codex" (model stays "unknown").
#
# This fixes commits created before the post_notes_updated normalizer existed.
# It uses `git grep` on the notes tree to find only notes that actually contain
# mock_ai, so it is fast even in repositories with many notes.

param(
    [string]$ProjectsRoot = 'D:\Projects'
)

$ErrorActionPreference = 'Continue'

if (-not (Test-Path -LiteralPath $ProjectsRoot)) {
    throw "Projects root not found: $ProjectsRoot"
}

$repos = Get-ChildItem -LiteralPath $ProjectsRoot -Directory -Force |
    Where-Object {
        $gitPath = Join-Path $_.FullName '.git'
        Test-Path -LiteralPath $gitPath -PathType Container
    } |
    Sort-Object Name

$rewritten = @()
$failed    = @()

foreach ($repo in $repos) {
    # `git grep` returns paths like refs/notes/ai:e9/728fdf<...>.
    # The path (without slashes) is the annotated commit SHA.
    $grepLines = @(git -C $repo.FullName grep -l 'mock_ai' refs/notes/ai 2>$null)
    if ($LASTEXITCODE -ne 0 -or $grepLines.Count -eq 0) { continue }

    foreach ($grepPath in $grepLines) {
        $target = ($grepPath -replace '^refs/notes/ai:', '') -replace '/', ''

        $noteLines = @(git -C $repo.FullName notes --ref=ai show $target 2>$null)
        if ($LASTEXITCODE -ne 0 -or $noteLines.Count -eq 0) { continue }

        $noteText = $noteLines -join "`n"
        if ($noteText -notmatch '"tool"\s*:\s*"mock_ai"') { continue }

        $newNoteText = $noteText -replace '"tool"\s*:\s*"mock_ai"', '"tool": "codex"'
        if ($newNoteText -eq $noteText) { continue }

        $tmp = [System.IO.Path]::GetTempFileName()
        try {
            [System.IO.File]::WriteAllText($tmp, $newNoteText, [System.Text.UTF8Encoding]::new($false))
            git -C $repo.FullName notes --ref=ai add -f -F $tmp $target 2>$null | Out-Null
            if ($LASTEXITCODE -ne 0) {
                $failed += "$($repo.Name) $target"
            } else {
                $rewritten += "$($repo.Name) $target"
            }
        } finally {
            Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
        }
    }
}

Write-Host "=== git-ai note normalization summary ==="
Write-Host "Projects root : $ProjectsRoot"
Write-Host "Rewritten ($($rewritten.Count)):"
$rewritten | ForEach-Object { Write-Host "  + $_" }
Write-Host "Failed ($($failed.Count)):"
$failed | ForEach-Object { Write-Host "  ! $_" }
Write-Host "Done."
