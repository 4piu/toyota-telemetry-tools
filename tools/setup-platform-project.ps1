<#
.SYNOPSIS
    Prepare a platform project to run the toolkit: field guide, backends, venv.

.DESCRIPTION
    A platform repository holds configuration and results; every program it
    runs comes from this field guide at the revision its telemetry-project.toml
    pins. That means one checkout supplies three things - the Python toolkit,
    the backend sources, and the Techstream scripts - so acquiring it once and
    installing the toolkit from it keeps them at the same revision by
    construction, and needs the network only when the checkout itself changes.

    Reads [upstream] and [paths] from the project's telemetry-project.toml,
    fetches the field guide at the pinned revision, builds the backends into
    the project's ignored scratch tree, and installs the toolkit into a project
    virtual environment. Every step is skippable and each is safe to re-run.

    It will not touch a field-guide checkout that has uncommitted changes.

.EXAMPLE
    ..\field-guide\tools\setup-platform-project.ps1

.EXAMPLE
    .\tools\setup-platform-project.ps1 -ProjectRoot ..\xa50 -SkipBackends
#>
param(
    # The platform repository. Defaults to the current directory.
    [string]$ProjectRoot = (Get-Location).Path,

    # Where the field guide is checked out. Defaults to paths.field_guide from
    # telemetry-project.toml, or ..\field-guide beside the project.
    [string]$FieldGuide,

    # Skip the backend build; useful when only the Python tools are wanted.
    [switch]$SkipBackends,

    # Skip the virtual environment and toolkit install.
    [switch]$SkipToolkit,

    # Fail rather than reach the network. The install itself needs no network
    # once uv has the build backend cached; a checkout that is already at the
    # pinned revision needs none either.
    [switch]$Offline
)

$ErrorActionPreference = 'Stop'

function Read-ProjectToml {
    <#
    .SYNOPSIS
    Read the flat scalar keys this script needs from a project file.

    .DESCRIPTION
    Not a TOML implementation: it understands `[section]` headers and
    `key = "value"` lines and ignores everything else, which is all
    telemetry-project.toml contains. Anything richer belongs in a real parser
    rather than in a regex here.
    #>
    param([Parameter(Mandatory = $true)][string]$Path)

    $values = @{}
    $section = ''
    foreach ($line in Get-Content -LiteralPath $Path) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith('#')) { continue }
        if ($trimmed -match '^\[([A-Za-z0-9_.]+)\]$') {
            $section = $Matches[1]
            continue
        }
        if ($trimmed -match '^([A-Za-z0-9_]+)\s*=\s*(.+)$') {
            $key = $Matches[1]
            $raw = $Matches[2].Trim()
            if ($raw -match '^"([^"]*)"') { $value = $Matches[1] }
            elseif ($raw -match "^'([^']*)'") { $value = $Matches[1] }
            else { $value = ($raw -split '#')[0].Trim() }
            $name = if ($section) { "$section.$key" } else { $key }
            $values[$name] = $value
        }
    }
    return $values
}

function Invoke-Git {
    param(
        [Parameter(Mandatory = $true)][string]$Directory,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )

    # Deliberately not redirecting stderr. git reports ordinary progress there
    # - "HEAD is now at ...", clone counters - and in Windows PowerShell
    # redirecting a native command's stderr wraps each line in an ErrorRecord,
    # which under ErrorActionPreference Stop turns success into a failure. Let
    # it reach the console and judge the command by its exit code.
    $output = & git -C $Directory @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "git $($Arguments -join ' ') failed in $Directory (exit $LASTEXITCODE)."
    }
    return $output
}

function Resolve-Commit {
    <#
    .SYNOPSIS
    Turn a pin - a commit, a tag, or a branch name - into one commit hash.

    .DESCRIPTION
    A branch in a fresh clone often exists only as origin/<name>, and
    `checkout --detach` does not apply git's remote-branch DWIM: it reads a
    name it cannot resolve as a path and fails confusingly. Resolving here
    means the checkout below always receives a hash.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$Directory,
        [Parameter(Mandatory = $true)][string]$Revision
    )

    foreach ($candidate in @($Revision, "origin/$Revision")) {
        $hash = & git -C $Directory rev-parse --verify --quiet "$candidate^{commit}"
        if ($LASTEXITCODE -eq 0 -and $hash) { return ($hash -join '').Trim() }
    }
    return $null
}

function Write-Step {
    param([string]$Message)
    Write-Host "==> $Message"
}

# ---------------------------------------------------------------- the project

$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$projectFile = Join-Path $ProjectRoot 'telemetry-project.toml'
if (-not (Test-Path -LiteralPath $projectFile -PathType Leaf)) {
    throw "No telemetry-project.toml in $ProjectRoot. Run this from a platform repository, or pass -ProjectRoot."
}

$project = Read-ProjectToml -Path $projectFile
foreach ($required in 'upstream.repository', 'upstream.revision') {
    if (-not $project.ContainsKey($required) -or -not $project[$required]) {
        throw "$projectFile does not set $required."
    }
}
$repository = $project['upstream.repository']
$revision = $project['upstream.revision']
$scratch = if ($project.ContainsKey('paths.scratch')) { $project['paths.scratch'] } else { 'scratch' }

if (-not $FieldGuide) {
    $FieldGuide = if ($project.ContainsKey('paths.field_guide')) {
        Join-Path $ProjectRoot $project['paths.field_guide']
    } else {
        Join-Path (Split-Path -Parent $ProjectRoot) 'field-guide'
    }
}
# The path is printed and reported; resolve the ..\ out of it even when the
# directory does not exist yet, which Resolve-Path cannot do.
$FieldGuide = [System.IO.Path]::GetFullPath($FieldGuide)

Write-Step "project    $ProjectRoot"
Write-Step "upstream   $repository @ $revision"
if ($revision -notmatch '^[0-9a-f]{40}$') {
    Write-Warning "upstream.revision is '$revision', not a commit hash. A branch moves, so this setup is not reproducible; pin the reviewed commit before publishing."
}

# ------------------------------------------------------------- the field guide

if (Test-Path -LiteralPath $FieldGuide -PathType Container) {
    if (-not (Test-Path -LiteralPath (Join-Path $FieldGuide '.git'))) {
        throw "$FieldGuide exists but is not a git checkout. Move it aside or pass -FieldGuide."
    }
    $FieldGuide = (Resolve-Path -LiteralPath $FieldGuide).Path
    $dirty = Invoke-Git -Directory $FieldGuide -Arguments @('status', '--porcelain')
    if ($dirty) {
        # Someone may be working in that checkout. Changing what it points at
        # is not this script's call to make.
        throw "$FieldGuide has uncommitted changes. Commit or stash them, or point -FieldGuide somewhere else."
    }
    $origin = (Invoke-Git -Directory $FieldGuide -Arguments @('remote', 'get-url', 'origin')) -join ''
    if ($origin.Trim() -ne $repository) {
        # Not fatal: a mirror, or ssh where the marker says https, is a fair
        # local choice. Silence would not be - the revision would be looked for
        # somewhere other than where the marker says it lives.
        Write-Warning "$FieldGuide has origin $($origin.Trim()), and the marker names $repository. Fetching the former."
    }
    if (-not $Offline) {
        Write-Step "fetching $($origin.Trim())"
        [void](Invoke-Git -Directory $FieldGuide -Arguments @('fetch', '--tags', 'origin'))
    }
    $commit = Resolve-Commit -Directory $FieldGuide -Revision $revision
    if (-not $commit) {
        $hint = if ($Offline) { ' The checkout may need a fetch; drop -Offline.' } else { '' }
        throw "$revision is not a commit, tag, or branch in $FieldGuide.$hint"
    }
    $head = (Invoke-Git -Directory $FieldGuide -Arguments @('rev-parse', 'HEAD')) -join ''
    if ($head -eq $commit) {
        Write-Step "field guide already at $($commit.Substring(0, 12))"
    }
    else {
        Write-Step "checking out $($commit.Substring(0, 12))"
        [void](Invoke-Git -Directory $FieldGuide -Arguments @('checkout', '--detach', $commit))
    }
}
elseif ($Offline) {
    throw "$FieldGuide does not exist and -Offline was given."
}
else {
    Write-Step "cloning $repository into $FieldGuide"
    $parent = Split-Path -Parent $FieldGuide
    if ($parent -and -not (Test-Path -LiteralPath $parent)) {
        [void](New-Item -ItemType Directory -Path $parent -Force)
    }
    & git clone $repository $FieldGuide
    if ($LASTEXITCODE -ne 0) { throw "git clone failed." }
    $FieldGuide = (Resolve-Path -LiteralPath $FieldGuide).Path
    $commit = Resolve-Commit -Directory $FieldGuide -Revision $revision
    if (-not $commit) {
        throw "$revision is not a commit, tag, or branch in $repository."
    }
    Write-Step "checking out $($commit.Substring(0, 12))"
    [void](Invoke-Git -Directory $FieldGuide -Arguments @('checkout', '--detach', $commit))
}

# ----------------------------------------------------------------- backends

if ($SkipBackends) {
    Write-Step 'skipping backends'
}
else {
    $binary = Join-Path (Join-Path $ProjectRoot $scratch) 'bin'
    Write-Step "building backends into $binary"
    & (Join-Path $FieldGuide 'backends\build.ps1') -OutputDirectory $binary
    if ($LASTEXITCODE -ne 0) { throw 'backend build failed.' }
}

# ------------------------------------------------------------------ toolkit

if ($SkipToolkit) {
    Write-Step 'skipping toolkit'
}
else {
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        throw 'uv was not found on PATH. See https://docs.astral.sh/uv/ to install it.'
    }
    $venv = Join-Path $ProjectRoot '.venv'
    $python = Join-Path $venv 'Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        Write-Step "creating $venv"
        & uv venv $venv
        if ($LASTEXITCODE -ne 0) { throw 'uv venv failed.' }
    }
    Write-Step 'installing the toolkit from the field-guide checkout'
    $install = @('pip', 'install', '--python', $python, '--reinstall', $FieldGuide)
    if ($Offline) { $install += '--offline' }
    & uv @install
    if ($LASTEXITCODE -ne 0) { throw 'toolkit install failed.' }
}

Write-Host ''
Write-Step 'ready'
Write-Host "  field guide  $FieldGuide"
if (-not $SkipToolkit) {
    Write-Host '  activate     .\.venv\Scripts\Activate.ps1'
    Write-Host '  then         telemetry-validate status --config data\config\validation-profile.json'
}
