<#
.SYNOPSIS
    Prepare a platform project to run the toolkit: checkout, backends, venv.

.DESCRIPTION
    A platform repository holds configuration and results; every program it
    runs comes from this repository at the revision its telemetry-project.toml
    pins. One checkout supplies three things - the Python toolkit, the backend
    sources, and the Techstream scripts - so acquiring it once and installing
    the toolkit from it keeps them at the same revision by construction, and
    needs the network only when the checkout itself changes.

    Reads [toolkit] and [paths] from the project's schema-2 telemetry-project.toml,
    fetches this repository at the pinned revision, builds the backends into
    the project's ignored scratch tree, and installs the toolkit into a project
    virtual environment. Every step is skippable and each is safe to re-run.

    It will not touch a checkout that has uncommitted changes.

.EXAMPLE
    ..\tools\tools\setup-platform-project.ps1

.EXAMPLE
    .\tools\setup-platform-project.ps1 -ProjectRoot ..\xa50 -SkipBackends
#>
param(
    # The platform repository. Defaults to the current directory.
    [string]$ProjectRoot = (Get-Location).Path,

    # Where the toolkit is checked out. Defaults to paths.toolkit from
    # telemetry-project.toml, or ..\telemetry-tools beside the project.
    [string]$Toolkit,

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
    throw "No telemetry-project.toml in $ProjectRoot. Run this from a platform repository, or pass -ProjectRoot. Start one from the template: https://github.com/4piu/70Y074-telemetry"
}

$project = Read-ProjectToml -Path $projectFile
if ($project['schema_version'] -ne '2') {
    throw "$projectFile must use exact schema_version 2."
}
foreach ($required in 'toolkit.repository', 'toolkit.revision') {
    if (-not $project.ContainsKey($required) -or -not $project[$required]) {
        throw "$projectFile does not set $required."
    }
}
$repository = $project['toolkit.repository']
$revision = $project['toolkit.revision']
if ($revision -like 'replace-with-*') {
    throw "$projectFile still has the template's placeholder revision. Set toolkit.revision to the tools release you reviewed, then run this again."
}
$scratch = if ($project.ContainsKey('paths.scratch')) { $project['paths.scratch'] } else { 'scratch' }

if (-not $Toolkit) {
    $Toolkit = if ($project.ContainsKey('paths.toolkit')) {
        Join-Path $ProjectRoot $project['paths.toolkit']
    } else {
        Join-Path (Split-Path -Parent $ProjectRoot) 'telemetry-tools'
    }
}
# The path is printed and reported; resolve the ..\ out of it even when the
# directory does not exist yet, which Resolve-Path cannot do.
$Toolkit = [System.IO.Path]::GetFullPath($Toolkit)

Write-Step "project    $ProjectRoot"
Write-Step "toolkit    $repository @ $revision"

# ----------------------------------------------------------------- the toolkit

if (Test-Path -LiteralPath $Toolkit -PathType Container) {
    if (-not (Test-Path -LiteralPath (Join-Path $Toolkit '.git'))) {
        throw "$Toolkit exists but is not a git checkout. Move it aside or pass -Toolkit."
    }
    $Toolkit = (Resolve-Path -LiteralPath $Toolkit).Path
    $dirty = Invoke-Git -Directory $Toolkit -Arguments @('status', '--porcelain')
    if ($dirty) {
        # Someone may be working in that checkout. Changing what it points at
        # is not this script's call to make.
        throw "$Toolkit has uncommitted changes. Commit or stash them, or point -Toolkit somewhere else."
    }
    $origin = (Invoke-Git -Directory $Toolkit -Arguments @('remote', 'get-url', 'origin')) -join ''
    if ($origin.Trim() -ne $repository) {
        # Not fatal: a mirror, or ssh where the marker says https, is a fair
        # local choice. Silence would not be - the revision would be looked for
        # somewhere other than where the marker says it lives.
        Write-Warning "$Toolkit has origin $($origin.Trim()), and the marker names $repository. Fetching the former."
    }
    if (-not $Offline) {
        Write-Step "fetching $($origin.Trim())"
        [void](Invoke-Git -Directory $Toolkit -Arguments @('fetch', '--tags', 'origin'))
    }
    $commit = Resolve-Commit -Directory $Toolkit -Revision $revision
    if (-not $commit) {
        $hint = if ($Offline) { ' The checkout may need a fetch; drop -Offline.' } else { '' }
        throw "$revision is not a commit, tag, or branch in $Toolkit.$hint"
    }
    $head = (Invoke-Git -Directory $Toolkit -Arguments @('rev-parse', 'HEAD')) -join ''
    if ($head -eq $commit) {
        Write-Step "toolkit already at $($commit.Substring(0, 12))"
    }
    else {
        Write-Step "checking out $($commit.Substring(0, 12))"
        [void](Invoke-Git -Directory $Toolkit -Arguments @('checkout', '--detach', $commit))
    }
}
elseif ($Offline) {
    throw "$Toolkit does not exist and -Offline was given."
}
else {
    Write-Step "cloning $repository into $Toolkit"
    $parent = Split-Path -Parent $Toolkit
    if ($parent -and -not (Test-Path -LiteralPath $parent)) {
        [void](New-Item -ItemType Directory -Path $parent -Force)
    }
    & git clone $repository $Toolkit
    if ($LASTEXITCODE -ne 0) { throw "git clone failed." }
    $Toolkit = (Resolve-Path -LiteralPath $Toolkit).Path
    $commit = Resolve-Commit -Directory $Toolkit -Revision $revision
    if (-not $commit) {
        throw "$revision is not a commit, tag, or branch in $repository."
    }
    Write-Step "checking out $($commit.Substring(0, 12))"
    [void](Invoke-Git -Directory $Toolkit -Arguments @('checkout', '--detach', $commit))
}

# A tag is as immutable as a hash and is the documented way to pin a release,
# so only a branch name - which moves under you - is worth a warning. This can
# only be answered once a checkout exists to ask, which is why it runs here
# rather than when the marker was first read. Stderr is deliberately not
# redirected - see Invoke-Git above for why that matters under Stop.
$tagHash = & git -C $Toolkit rev-parse --verify --quiet "refs/tags/$revision"
$pinnedByTag = ($LASTEXITCODE -eq 0 -and $tagHash)
if (-not $pinnedByTag -and $revision -notmatch '^[0-9a-f]{7,40}$') {
    Write-Warning "toolkit.revision is '$revision', not a release tag or a commit hash. A branch moves; this is intentional only during coordinated pre-release development."
}

# ----------------------------------------------------------------- backends

if ($SkipBackends) {
    Write-Step 'skipping backends'
}
else {
    $binary = Join-Path (Join-Path $ProjectRoot $scratch) 'bin'
    Write-Step "building backends into $binary"
    & (Join-Path $Toolkit 'backends\build.ps1') -OutputDirectory $binary
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
    Write-Step 'installing the toolkit from that checkout'
    $install = @('pip', 'install', '--python', $python, '--reinstall', $Toolkit)
    if ($Offline) { $install += '--offline' }
    & uv @install
    if ($LASTEXITCODE -ne 0) { throw 'toolkit install failed.' }
}

Write-Host ''
Write-Step 'ready'
Write-Host "  toolkit      $Toolkit"
if (-not $SkipToolkit) {
    Write-Host '  activate     .\.venv\Scripts\Activate.ps1'
}
