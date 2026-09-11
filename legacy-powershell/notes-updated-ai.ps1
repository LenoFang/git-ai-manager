# git-ai post_notes_updated hook
#
# git-ai calls this after writing/updating an authorship note. The payload is a
# JSON array on stdin, e.g.:
#   [{"commit_sha":"...","note_content":"...","branch":"...","repo_name":"...","repo_url":"..."}]
#
# This hook normalizes authorship notes:
#   1. mock_ai notes (created by the pre-commit adapter) -> tool codex / model unknown
#   2. pure human notes authored by the local git user -> tool codex / model unknown
#
# The git-ai daemon invokes hooks from its own directory and does not pass the
# repository path, so the post-commit hook records:
#   %USERPROFILE%\.git-ai\notes-repo-map\<commit_sha> -> repository path
# and this script reads that mapping.

param(
    [string[]]$ProjectRoots = @('D:\Projects')
)

$ErrorActionPreference = 'Continue'

$mapRoot = Join-Path $env:USERPROFILE '.git-ai\notes-repo-map'

function Get-MappedRepo {
    param([string]$Sha)
    $mapFile = Join-Path $mapRoot $Sha
    if (-not (Test-Path -LiteralPath $mapFile)) { return $null }
    $repo = Get-Content -LiteralPath $mapFile -Raw -ErrorAction SilentlyContinue
    if (-not $repo) { return $null }
    $repo = $repo.Trim()
    if (-not (Test-Path -LiteralPath $repo)) { return $null }
    return $repo
}

function Remove-MappedRepo {
    param([string]$Sha)
    $mapFile = Join-Path $mapRoot $Sha
    Remove-Item -LiteralPath $mapFile -Force -ErrorAction SilentlyContinue
}

function Write-AiNote {
    param([string]$Repo, [string]$Sha, [string]$NoteText)
    $tmp = [System.IO.Path]::GetTempFileName()
    try {
        [System.IO.File]::WriteAllText($tmp, $NoteText, [System.Text.UTF8Encoding]::new($false))
        git -C $Repo notes --ref=ai add -f -F $tmp $Sha 2>$null | Out-Null
        return ($LASTEXITCODE -eq 0)
    } finally {
        Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
    }
}

function Convert-HumanNoteToCodex {
    param([string]$NoteText)

    $parts = [regex]::Split($NoteText, '(?m)^---\s*$', 2)
    if ($parts.Count -lt 2) { return $null }

    $attr = $parts[0]
    $jsonText = $parts[1]

    $json = $null
    try { $json = $jsonText | ConvertFrom-Json } catch { return $null }
    if (-not $json -or -not $json.humans) { return $null }

    $humanIds = @([regex]::Matches($attr, 'h_[0-9a-f]+') | ForEach-Object { $_.Value } | Select-Object -Unique)
    if ($humanIds.Count -eq 0) { return $null }

    $sessionId = 's_' + ([guid]::NewGuid().ToString('N').Substring(0, 14))
    $traceId   = 't_' + ([guid]::NewGuid().ToString('N').Substring(0, 14))
    $authorId  = "$sessionId::$traceId"

    $newAttr = $attr
    foreach ($hid in $humanIds) {
        $newAttr = $newAttr.Replace($hid, $authorId)
    }

    $humanAuthor = ''
    $firstHuman = @($json.humans.PSObject.Properties | Select-Object -First 1)
    if ($firstHuman.Count -gt 0) {
        $humanAuthor = $firstHuman[0].Value.author
    }

    # Preserve existing real AI sessions and add a codex session for human lines.
    $newSessions = [ordered]@{}
    if ($json.sessions) {
        foreach ($sp in $json.sessions.PSObject.Properties) {
            $newSessions[$sp.Name] = $sp.Value
        }
    }
    $newSessions[$sessionId] = [ordered]@{
        agent_id = [ordered]@{
            tool  = 'codex'
            id    = 'ai-thread-' + [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
            model = 'unknown'
        }
        human_author = $humanAuthor
    }

    $newObj = [ordered]@{}
    foreach ($prop in $json.PSObject.Properties) {
        if ($prop.Name -eq 'humans' -or $prop.Name -eq 'sessions') { continue }
        $newObj[$prop.Name] = $prop.Value
    }
    $newObj['sessions'] = $newSessions

    $newJson = $newObj | ConvertTo-Json -Depth 10 -Compress
    return ($newAttr.TrimEnd() + "`n---`n" + $newJson + "`n")
}

$raw = [Console]::In.ReadToEnd()
if (-not $raw) {
    exit 0
}

$events = $null
try {
    $events = $raw | ConvertFrom-Json
} catch {
    exit 0
}
if (-not $events) {
    exit 0
}

foreach ($ev in @($events)) {
    $sha = $ev.commit_sha
    if (-not $sha) { continue }

    $repo = Get-MappedRepo -Sha $sha
    if (-not $repo) { continue }

    $noteLines = @(git -C $repo notes --ref=ai show $sha 2>$null)
    if ($LASTEXITCODE -ne 0 -or $noteLines.Count -eq 0) { continue }

    $noteText = $noteLines -join "`n"

    # Case 1: mock_ai checkpoint note -> codex.
    if ($noteText -match '"tool"\s*:\s*"mock_ai"') {
        $newNoteText = $noteText -replace '"tool"\s*:\s*"mock_ai"', '"tool": "codex"'
        if ($newNoteText -ne $noteText -and (Write-AiNote -Repo $repo -Sha $sha -NoteText $newNoteText)) {
            Remove-MappedRepo -Sha $sha
        }
        continue
    }

    # Case 2: pure human note -> codex, but only for the local git user.
    if ($noteText -match '"humans"') {
        $candidates = @()
        $configEmail = (git -C $repo config user.email 2>$null | Select-Object -First 1)
        $authorEmail = (git -C $repo show -s --format=%ae $sha 2>$null | Select-Object -First 1)
        $commitEmail = (git -C $repo show -s --format=%ce $sha 2>$null | Select-Object -First 1)
        foreach ($email in @($configEmail, $authorEmail, $commitEmail)) {
            if ($email) { $candidates += $email.Trim() }
        }
        $candidates = @($candidates | Select-Object -Unique)

        $isLocalUser = $false
        foreach ($email in $candidates) {
            if ($noteText -match [regex]::Escape($email)) {
                $isLocalUser = $true
                break
            }
        }
        if (-not $isLocalUser) { continue }

        $newNoteText = Convert-HumanNoteToCodex -NoteText $noteText
        if ($newNoteText -and (Write-AiNote -Repo $repo -Sha $sha -NoteText $newNoteText)) {
            Remove-MappedRepo -Sha $sha
        }
    }
}

exit 0
