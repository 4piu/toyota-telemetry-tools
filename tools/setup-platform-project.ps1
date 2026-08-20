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
    .\tools\setup-platform-project.ps1 -ProjectRoot ..\new-car -Initialize

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

    # Write the platform-project skeleton first, for a project that does not
    # exist yet. Refuses to overwrite anything already there.
    [switch]$Initialize,

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

# ------------------------------------------------------------- initialisation

function New-PlatformProject {
    <#
    .SYNOPSIS
    Write the platform-project skeleton into an empty or partial directory.

    .DESCRIPTION
    The skeleton is the shape every vehicle repository starts as: the marker,
    the data directories with their contracts, and the documentation headings
    that the method expects to be filled in. Blank configuration comes from the
    same templates the documentation already points at, so there is one copy of
    each rather than two that can disagree.

    Nothing existing is overwritten - re-running it fills in what is missing.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$Destination,
        [Parameter(Mandatory = $true)][string]$FieldGuideRoot
    )

    $skeleton = Join-Path $FieldGuideRoot 'templates\platform-project'
    if (-not (Test-Path -LiteralPath $skeleton -PathType Container)) {
        throw "The platform-project skeleton is missing from $FieldGuideRoot."
    }
    if (-not (Test-Path -LiteralPath $Destination -PathType Container)) {
        [void](New-Item -ItemType Directory -Path $Destination -Force)
    }

    $written = 0
    $kept = 0
    foreach ($source in Get-ChildItem -LiteralPath $skeleton -Recurse -File) {
        $relative = $source.FullName.Substring($skeleton.Length).TrimStart('\')
        # Carried as `gitignore` so it cannot filter the skeleton's own files
        # out of the field guide's history.
        if ($relative -eq 'gitignore' -or $relative -eq 'gitattributes') {
            $relative = '.' + $relative
        }
        $target = Join-Path $Destination $relative
        if (Test-Path -LiteralPath $target) { $kept++; continue }
        $parent = Split-Path -Parent $target
        if (-not (Test-Path -LiteralPath $parent)) {
            [void](New-Item -ItemType Directory -Path $parent -Force)
        }
        Copy-Item -LiteralPath $source.FullName -Destination $target
        $written++
    }

    # Blank configuration: one template, placed where the tools look for it.
    $configuration = @{
        'templates\telemetry-project.toml'     = 'telemetry-project.toml'
        'templates\validation-profile.json'    = 'data\config\validation-profile.json'
        'templates\backend-config.json'        = 'data\config\backend-config.json'
        'templates\catalog-profile.json'       = 'data\config\ecu-profile.json'
        'templates\validation-experiment.toml' = 'data\config\experiments\example-experiment.toml'
    }
    foreach ($entry in $configuration.GetEnumerator()) {
        $source = Join-Path $FieldGuideRoot $entry.Key
        $target = Join-Path $Destination $entry.Value
        if (-not (Test-Path -LiteralPath $source)) {
            throw "The field guide is missing $($entry.Key)."
        }
        if (Test-Path -LiteralPath $target) { $kept++; continue }
        $parent = Split-Path -Parent $target
        if (-not (Test-Path -LiteralPath $parent)) {
            [void](New-Item -ItemType Directory -Path $parent -Force)
        }
        Copy-Item -LiteralPath $source -Destination $target
        $written++
    }

    Write-Step "wrote $written file(s) into $Destination$(if ($kept) { "; kept $kept already there" })"
}

# ---------------------------------------------------------------- the project

if ($Initialize) {
    # The skeleton lives beside this script, in the checkout it was run from -
    # which is the field guide whether or not the project names one yet.
    $here = Split-Path -Parent $PSCommandPath
    New-PlatformProject -Destination $ProjectRoot -FieldGuideRoot (Split-Path -Parent $here)
}

$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$projectFile = Join-Path $ProjectRoot 'telemetry-project.toml'
if (-not (Test-Path -LiteralPath $projectFile -PathType Leaf)) {
    throw "No telemetry-project.toml in $ProjectRoot. Run this from a platform repository, pass -ProjectRoot, or add -Initialize to create one."
}

$project = Read-ProjectToml -Path $projectFile
foreach ($required in 'upstream.repository', 'upstream.revision') {
    if (-not $project.ContainsKey($required) -or -not $project[$required]) {
        throw "$projectFile does not set $required."
    }
}
$repository = $project['upstream.repository']
$revision = $project['upstream.revision']
if ($revision -like 'replace-with-*') {
    if ($Initialize) {
        # A fresh skeleton cannot know which revision was reviewed. Stopping
        # here is the expected end of the first run, not a failure.
        Write-Host ''
        Write-Step 'skeleton written'
        Write-Host "  next         set project_id and upstream.revision in $projectFile"
        Write-Host '  then         re-run this script to build the backends and install the toolkit'
        exit 0
    }
    throw "$projectFile still has the template's placeholder revision. Set upstream.revision to the field-guide commit you reviewed."
}
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
}
if ($Initialize) {
    Write-Host '  next         fill in data\config\, then follow docs\reproduce.md'
}
elseif (-not $SkipToolkit) {
    Write-Host '  then         telemetry-validate status --config data\config\validation-profile.json'
}
