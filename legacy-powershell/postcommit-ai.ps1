# git-ai post-commit repo mapping recorder
#
# The pre-commit adapter creates a mock_ai checkpoint. git-ai's global
# post_notes_updated hook then rewrites that note to tool=codex / model=unknown.
# That hook runs from the git-ai daemon directory and does not receive the repo
# path, so this post-commit hook records:
#
#   commit_sha -> repository path
#
# in %USERPROFILE%\.git-ai\notes-repo-map\<sha>.
#
# This fires for every commit, but only writes a small file and exits.

$ErrorActionPreference = 'Continue'

$head = (git rev-parse HEAD 2>$null | Select-Object -First 1)
if (-not $head) {
    exit 0
}

$repo = (git rev-parse --show-toplevel 2>$null | Select-Object -First 1)
if (-not $repo) {
    exit 0
}

$mapDir = Join-Path $env:USERPROFILE '.git-ai\notes-repo-map'
try {
    if (-not (Test-Path -LiteralPath $mapDir)) {
        New-Item -ItemType Directory -Path $mapDir -Force | Out-Null
    }
    $mapFile = Join-Path $mapDir $head
    $tmpFile = "$mapFile.tmp"
    [System.IO.File]::WriteAllText($tmpFile, $repo, [System.Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $tmpFile -Destination $mapFile -Force
} catch {
    # Never block the commit for a mapping file.
}

exit 0
