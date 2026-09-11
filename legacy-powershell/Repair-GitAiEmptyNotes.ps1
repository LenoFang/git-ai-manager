# Repair git-ai notes that have no line attributions (empty notes).
#
# Builds line attributions for all lines added by the commit and writes a
# sessions note with tool=codex, model=unknown. Intended for historical
# AI-generated commits whose original note only contained an empty humans block.
#
# Usage:
#   powershell -File Repair-GitAiEmptyNotes.ps1 -Repo D:\Projects\TCOA.SCM -Commit <sha1>,<sha2> [-Apply]

param(
    [Parameter(Mandatory = $true)]
    [string]$Repo,

    [Parameter(Mandatory = $true)]
    [string[]]$Commit,

    [switch]$Apply
)

$ErrorActionPreference = 'Continue'

function Get-CommitAddedLines {
    param([string]$RepoPath, [string]$CommitSha)

    $diff = @(git -C $RepoPath diff --unified=0 "$CommitSha^" $CommitSha -- 2>$null)
    if ($LASTEXITCODE -ne 0) { return @{} }

    $result = @{}
    $currentFile = $null
    $newLine = 0

    foreach ($line in $diff) {
        if ($line -like '+++ b/*') {
            $currentFile = $line.Substring(6)
            if (-not $result.ContainsKey($currentFile)) {
                $result[$currentFile] = New-Object 'System.Collections.Generic.HashSet[int]'
            }
            continue
        }
        if ($line -like '+++ /dev/null') {
            $currentFile = $null
            continue
        }
        if ($line -match '^@@ -\d+(?:,\d+)? \+(?<start>\d+)(?:,(?<count>\d+))? @@') {
            $newLine = [int]$Matches.start
            continue
        }
        if (-not $currentFile -or $newLine -le 0) { continue }

        if ($line.StartsWith('+') -and -not $line.StartsWith('+++')) {
            [void]$result[$currentFile].Add($newLine)
            $newLine++
            continue
        }
        if ($line.StartsWith('-') -and -not $line.StartsWith('---')) {
            continue
        }
        if ($line.StartsWith('\')) {
            continue
        }
        $newLine++
    }

    return $result
}

function Convert-LinesToRanges {
    param([int[]]$Lines)

    $sorted = @($Lines | Sort-Object -Unique)
    if ($sorted.Count -eq 0) { return @() }

    $ranges = @()
    $start = $sorted[0]
    $prev = $sorted[0]
    for ($i = 1; $i -lt $sorted.Count; $i++) {
        $line = $sorted[$i]
        if ($line -eq ($prev + 1)) {
            $prev = $line
            continue
        }
        $ranges += if ($start -eq $prev) { "$start" } else { "$start-$prev" }
        $start = $line
        $prev = $line
    }
    $ranges += if ($start -eq $prev) { "$start" } else { "$start-$prev" }
    return $ranges
}

function Get-GitAiVersion {
    $gitAi = $env:GIT_AI_EXE
    if (-not $gitAi -or -not (Test-Path -LiteralPath $gitAi)) {
        $cmd = Get-Command git-ai -ErrorAction SilentlyContinue
        if ($cmd -and $cmd.Source) { $gitAi = $cmd.Source }
    }
    if (-not $gitAi) {
        $candidate = Join-Path $env:USERPROFILE '.git-ai\bin\git-ai.exe'
        if (Test-Path -LiteralPath $candidate) { $gitAi = $candidate }
    }
    if (-not $gitAi) { return '1.7.4' }
    $v = (& $gitAi version 2>$null | Select-Object -First 1)
    if ($v) { return $v.Trim() }
    return '1.7.4'
}

$gitAiVersion = Get-GitAiVersion

# Accept both -Commit a,b and -Commit a -Commit b.
$commitList = @()
foreach ($c in $Commit) {
    $commitList += @($c -split ',' | Where-Object { $_ })
}
$commitList = @($commitList | Select-Object -Unique)

foreach ($sha in $commitList) {
    $fullSha = (git -C $Repo rev-parse --verify --quiet "$sha^{commit}" 2>$null | Select-Object -First 1)
    if (-not $fullSha) {
        Write-Warning "Commit not found in ${Repo}: $sha"
        continue
    }

    $parentLine = (git -C $Repo rev-list --parents -n 1 $fullSha 2>$null | Select-Object -First 1)
    $parentCount = @($parentLine -split '\s+').Count - 1
    if ($parentCount -ne 1) {
        Write-Warning "Skip ${fullSha}: not a single-parent commit."
        continue
    }

    $existingNote = @(git -C $Repo notes --ref=ai show $fullSha 2>$null) -join "`n"

    $addedByFile = Get-CommitAddedLines -RepoPath $Repo -CommitSha $fullSha
    if ($addedByFile.Count -eq 0) {
        Write-Warning "Skip ${fullSha}: no added lines found."
        continue
    }

    $sessionId = 's_' + ([guid]::NewGuid().ToString('N').Substring(0, 14))
    $traceId   = 't_' + ([guid]::NewGuid().ToString('N').Substring(0, 14))
    $authorId  = "$sessionId::$traceId"
    $humanAuthor = (git -C $Repo show -s --format='%an <%ae>' $fullSha 2>$null | Select-Object -First 1)

    $attrLines = @()
    foreach ($file in ($addedByFile.Keys | Sort-Object)) {
        $lines = @($addedByFile[$file])
        $ranges = @(Convert-LinesToRanges -Lines $lines)
        if ($ranges.Count -eq 0) { continue }
        $attrLines += $file
        $attrLines += ('  ' + $authorId + ' ' + ($ranges -join ','))
    }

    if ($attrLines.Count -eq 0) {
        Write-Warning "Skip ${fullSha}: no attribution ranges built."
        continue
    }

    $sessions = [ordered]@{
        $sessionId = [ordered]@{
            agent_id = [ordered]@{
                tool  = 'codex'
                id    = 'ai-thread-' + [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
                model = 'unknown'
            }
            human_author = $humanAuthor
        }
    }

    $meta = [ordered]@{
        schema_version  = 'authorship/3.0.0'
        git_ai_version  = $gitAiVersion
        base_commit_sha = $fullSha
        prompts         = [ordered]@{}
        sessions        = $sessions
    }

    $noteText = (($attrLines -join "`n") + "`n---`n" + ($meta | ConvertTo-Json -Depth 10 -Compress) + "`n")

    Write-Host "=== $fullSha ==="
    Write-Host $noteText

    if (-not $Apply) { continue }

    $tmp = [System.IO.Path]::GetTempFileName()
    try {
        [System.IO.File]::WriteAllText($tmp, $noteText, [System.Text.UTF8Encoding]::new($false))
        git -C $Repo notes --ref=ai add -f -F $tmp $fullSha 2>$null | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "Failed to write note for $fullSha"
        } else {
            Write-Host "Wrote AI note for $fullSha"
        }
    } finally {
        Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
    }
}
