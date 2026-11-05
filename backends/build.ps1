<#
.SYNOPSIS
    Build the validation backend executables.

.DESCRIPTION
    Compiles Telemetry.Can.Core together with one device binding into a single
    self-contained executable. Uses the .NET Framework C# compiler that ships
    with Windows, so no SDK installation is required.

    PCAN builds x64 to match the 64-bit PCANBasic.dll. J2534 builds x86 by
    default because most Toyota-compatible vendor DLLs are 32-bit; pass
    -J2534Platform x64 for a 64-bit vendor DLL. gs_usb builds x64 and copies
    its bundled libusb-1.0.dll (the fallback transport's dependency; the
    primary, native-WinUSB transport needs nothing beyond what Windows ships)
    next to the executable, matching the bitness of the DLL under
    Telemetry.Backend.Gsusb\runtime\. slcan builds AnyCPU: it only talks to a
    CDC-ACM virtual COM port through System.IO.Ports, no native dependency.

    telemetry-tisdb is not a worker: it reads an installed Techstream (TIS)
    database for `telemetry source import tis` and never talks to a vehicle.
    It builds x86 without the CAN core because Techstream's KgpDataCtrl.dll
    is 32-bit.

    By default the workers go into the Python package
    (src\toyota_telemetry\workers), where the tool finds them and from which
    a wheel bundles them.

.EXAMPLE
    .\build.ps1
    .\build.ps1 -Backend pcan -OutputDirectory C:\temp\workers
#>
param(
    [ValidateSet('all', 'pcan', 'j2534', 'gsusb', 'slcan', 'tisdb')]
    [string]$Backend = 'all',

    [ValidateSet('Release', 'Debug')]
    [string]$Configuration = 'Release',

    [ValidateSet('x86', 'x64')]
    [string]$J2534Platform = 'x86',

    [string]$OutputDirectory
)

$ErrorActionPreference = 'Stop'

$coreDirectory = Join-Path $PSScriptRoot 'Telemetry.Can.Core'
if (-not (Test-Path -LiteralPath $coreDirectory)) {
    throw "Core sources not found at $coreDirectory."
}
$coreSources = @(Get-ChildItem -LiteralPath $coreDirectory -Filter '*.cs' | ForEach-Object { $_.FullName })
if ($coreSources.Count -eq 0) {
    throw "No core sources found in $coreDirectory."
}

if (-not $OutputDirectory) {
    $OutputDirectory = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\src\toyota_telemetry\workers'))
}
New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null

function Build-Backend {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$ProjectDirectory,
        [Parameter(Mandatory = $true)][string]$Platform,
        [switch]$WithoutCore
    )

    $frameworkDirectory = if ($Platform -eq 'x64') { 'Framework64' } else { 'Framework' }
    $compiler = Join-Path $env:WINDIR "Microsoft.NET\$frameworkDirectory\v4.0.30319\csc.exe"
    if (-not (Test-Path -LiteralPath $compiler)) {
        throw "The .NET Framework C# compiler was not found at $compiler."
    }

    $projectSources = @(
        Get-ChildItem -LiteralPath $ProjectDirectory -Filter '*.cs' | ForEach-Object { $_.FullName }
    )
    if ($projectSources.Count -eq 0) {
        throw "No sources found in $ProjectDirectory."
    }

    $executable = Join-Path $OutputDirectory "$Name.exe"
    $arguments = @(
        '/nologo',
        '/target:exe',
        "/platform:$Platform",
        '/warn:4',
        '/warnaserror+',
        "/out:$executable"
    )
    if ($Configuration -eq 'Release') {
        $arguments += '/optimize+'
        $arguments += '/debug:pdbonly'
    }
    else {
        $arguments += '/optimize-'
        $arguments += '/debug:full'
        $arguments += '/define:DEBUG;TRACE'
    }
    if (-not $WithoutCore) {
        $arguments += $coreSources
    }
    $arguments += $projectSources

    & $compiler @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Name compilation failed with exit code $LASTEXITCODE."
    }
    if (-not (Test-Path -LiteralPath $executable)) {
        throw "$Name build reported success but produced no executable."
    }
    Get-Item -LiteralPath $executable | Select-Object FullName, Length, LastWriteTime
}

if ($Backend -eq 'all' -or $Backend -eq 'pcan') {
    Build-Backend -Name 'telemetry-pcan' `
        -ProjectDirectory (Join-Path $PSScriptRoot 'Telemetry.Backend.Pcan') `
        -Platform 'x64'
}

if ($Backend -eq 'all' -or $Backend -eq 'j2534') {
    Build-Backend -Name 'telemetry-j2534' `
        -ProjectDirectory (Join-Path $PSScriptRoot 'Telemetry.Backend.J2534') `
        -Platform $J2534Platform
}

if ($Backend -eq 'all' -or $Backend -eq 'gsusb') {
    Build-Backend -Name 'telemetry-gsusb' `
        -ProjectDirectory (Join-Path $PSScriptRoot 'Telemetry.Backend.Gsusb') `
        -Platform 'x64'
    $gsusbRuntime = Join-Path $PSScriptRoot 'Telemetry.Backend.Gsusb\runtime\libusb-1.0.dll'
    Copy-Item -LiteralPath $gsusbRuntime -Destination $OutputDirectory -Force
}

if ($Backend -eq 'all' -or $Backend -eq 'slcan') {
    Build-Backend -Name 'telemetry-slcan' `
        -ProjectDirectory (Join-Path $PSScriptRoot 'Telemetry.Backend.Slcan') `
        -Platform 'anycpu'
}

if ($Backend -eq 'all' -or $Backend -eq 'tisdb') {
    Build-Backend -Name 'telemetry-tisdb' `
        -ProjectDirectory (Join-Path $PSScriptRoot 'Telemetry.TisDb') `
        -Platform 'x86' -WithoutCore
}

Write-Output ''
Write-Output "Verify the build before connecting to a vehicle:"
if ($Backend -eq 'all' -or $Backend -eq 'pcan') {
    Write-Output "  $OutputDirectory\telemetry-pcan.exe --self-test"
}
if ($Backend -eq 'all' -or $Backend -eq 'j2534') {
    Write-Output "  $OutputDirectory\telemetry-j2534.exe --self-test"
}
if ($Backend -eq 'all' -or $Backend -eq 'gsusb') {
    Write-Output "  $OutputDirectory\telemetry-gsusb.exe --self-test"
}
if ($Backend -eq 'all' -or $Backend -eq 'slcan') {
    Write-Output "  $OutputDirectory\telemetry-slcan.exe --self-test"
}
if ($Backend -eq 'all' -or $Backend -eq 'tisdb') {
    Write-Output "  $OutputDirectory\telemetry-tisdb.exe --self-test"
}
