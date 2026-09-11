# Convert historical git-ai "human" notes to AI codex notes.
#
# Only converts notes where:
#   - the note is a pure human note ("humans" section, no "sessions")
#   - the note author matches -AuthorFilter (default: fl13011@ly.com)
#   - the commit subject matches -SubjectPattern (default: ai-generate)
#
# These are typically Qoder/DSh AI-assisted commits that were committed before
# the pre-commit adapter existed. Genuinely human commits are left untouched.
#
# Default is dry-run. Pass -Apply to actually rewrite notes.

param(
    [string]$ProjectsRoot = 'D:\Projects',
    [string]$AuthorFilter = 'fl13011@ly.com',
    [string]$SubjectPattern = 'ai-generate',
    [switch]$Apply
)

$ErrorActionPreference = 'Continue'

if (-not (Test-Path -LiteralPath $ProjectsRoot)) {
    throw "Projects root not found: $ProjectsRoot"
}

function Convert-HumanNoteText {
    param([string]$NoteText, [string]$Author)

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

    $humanAuthor = $Author
    if (-not $humanAuthor) {
        $firstHuman = @($json.humans.PSObject.Properties | Select-Object -First 1)
        if ($firstHuman.Count -gt 0) {
            $humanAuthor = $firstHuman[0].Value.author
        }
    }

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

$repos = Get-ChildItem -LiteralPath $ProjectsRoot -Directory -Force |
    Where-Object {
        $gitPath = Join-Path $_.FullName '.git'
        Test-Path -LiteralPath $gitPath -PathType Container
    } |
    Sort-Object Name

$converted = @()
$skipped   = @()

foreach ($repo in $repos) {
    $pattern = if ($AuthorFilter) { $AuthorFilter } else { 'humans' }
    $paths = @(git -C $repo.FullName grep -l $pattern refs/notes/ai 2>$null)
    if ($LASTEXITCODE -ne 0 -or $paths.Count -eq 0) { continue }

    foreach ($path in $paths) {
        $target = ($path -replace '^refs/notes/ai:', '') -replace '/', ''

        $note = @(git -C $repo.FullName notes --ref=ai show $target 2>$null) -join "`n"
        if ($note -notmatch '"humans"') { continue }
        if ($AuthorFilter -and $note -notmatch [regex]::Escape($AuthorFilter)) { continue }

        $subject = (git -C $repo.FullName show -s --format=%s $target 2>$null)
        if ($SubjectPattern -and $subject -notmatch $SubjectPattern) { continue }

        $newNote = Convert-HumanNoteText -NoteText $note -Author ''
        if (-not $newNote) { $skipped += "$($repo.Name) $target (convert failed)"; continue }

        if ($Apply) {
            $tmp = [System.IO.Path]::GetTempFileName()
            try {
                [System.IO.File]::WriteAllText($tmp, $newNote, [System.Text.UTF8Encoding]::new($false))
                git -C $repo.FullName notes --ref=ai add -f -F $tmp $target 2>$null | Out-Null
                if ($LASTEXITCODE -ne 0) {
                    $skipped += "$($repo.Name) $target (write failed)"
                } else {
                    $converted += "$($repo.Name) $target :: $subject"
                }
            } finally {
                Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
            }
        } else {
            $converted += "$($repo.Name) $target :: $subject"
        }
    }
}

Write-Host "=== git-ai human note conversion ==="
Write-Host "Projects root   : $ProjectsRoot"
Write-Host "Author filter   : $AuthorFilter"
Write-Host "Subject pattern : $SubjectPattern"
Write-Host "Mode            : $(if ($Apply) { 'APPLY' } else { 'DRY-RUN' })"
Write-Host "Converted ($($converted.Count)):"
$converted | ForEach-Object { Write-Host "  + $_" }
if ($skipped.Count -gt 0) {
    Write-Host "Skipped ($($skipped.Count)):"
    $skipped | ForEach-Object { Write-Host "  ! $_" }
}
Write-Host "Done."
