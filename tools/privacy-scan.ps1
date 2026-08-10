param(
    [string]$Root = (Get-Location).Path
)

$ErrorActionPreference = 'Stop'

$patterns = [ordered]@{
    PossibleVin = '(?<![A-HJ-NPR-Z0-9])[A-HJ-NPR-Z0-9]{17}(?![A-HJ-NPR-Z0-9])'
    WindowsUserPath = 'C:\\Users\\(?!Public\\)'
    MacUserPath = '/Users/'
    LinuxHomePath = '/home/'
}

$files = if (Test-Path -LiteralPath (Join-Path $Root '.git')) {
    git -C $Root ls-files --cached --others --exclude-standard
} else {
    Get-ChildItem -LiteralPath $Root -Recurse -File |
        Where-Object { $_.FullName -notmatch '[\\/](\.git|bin|obj|__pycache__)[\\/]' } |
        ForEach-Object { $_.FullName }
}

$matchCount = 0
foreach ($file in $files) {
    $normalizedFile = $file -replace '\\', '/'
    # These two tracked files contain the scan expressions as documentation/code.
    if ($normalizedFile -in @('PRIVATE_DATA.md', 'tools/privacy-scan.ps1')) {
        continue
    }
    $path = if ([IO.Path]::IsPathRooted($file)) { $file } else { Join-Path $Root $file }
    foreach ($entry in $patterns.GetEnumerator()) {
        $found = Select-String -LiteralPath $path -Pattern $entry.Value -AllMatches -CaseSensitive -ErrorAction SilentlyContinue
        foreach ($match in $found) {
            # A decorated Techstream C++ class name happens to match the broad
            # 17-character VIN heuristic. It is stable source code, not data.
            if ($entry.Key -eq 'PossibleVin' -and $match.Line -match 'CDbPhyDataResRecords') {
                continue
            }
            Write-Output ([pscustomobject]@{
                Rule = $entry.Key
                File = $file
                Line = $match.LineNumber
                Text = $match.Line.Trim()
            })
            $matchCount++
        }
    }
}

if ($matchCount -gt 0) {
    Write-Error "Privacy scan found $matchCount match(es); review or explicitly whitelist each one."
}

Write-Output 'Privacy scan completed with no matches.'
