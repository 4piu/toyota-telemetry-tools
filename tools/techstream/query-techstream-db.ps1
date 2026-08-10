param(
    [string]$DatabaseDirectory = (Join-Path ${env:ProgramFiles(x86)} 'Toyota Diagnostics\Techstream\NA'),
    [string]$TechstreamBinDirectory = (Join-Path ${env:ProgramFiles(x86)} 'Toyota Diagnostics\Techstream\bin'),
    [string]$EcuFile = 'M_English.ddb',
    [string]$Keys,
    [uint16]$Table = 0x023e,
    [switch]$Catalog,
    [switch]$ProtocolInfo,
    [string]$NamePattern,
    [ValidateRange(0, 100000)]
    [int]$MaxItems = 0,
    [switch]$NoRaw,
    [string]$PhysicalGroups,
    [string]$UnitIds,
    [ValidateSet('Object', 'JsonLines')]
    [string]$OutputFormat = 'Object'
)

$ErrorActionPreference = 'Stop'
if ([Environment]::Is64BitProcess) {
    $x86PowerShell = Join-Path $env:WINDIR 'SysWOW64\WindowsPowerShell\v1.0\powershell.exe'
    $relayArguments = @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $PSCommandPath,
        '-DatabaseDirectory', $DatabaseDirectory,
        '-TechstreamBinDirectory', $TechstreamBinDirectory,
        '-EcuFile', $EcuFile, '-Table', [string]$Table,
        '-OutputFormat', $OutputFormat
    )
    if ($Keys) {
        $relayArguments += @('-Keys', $Keys)
    }
    if ($Catalog) {
        $relayArguments += '-Catalog'
    }
    if ($ProtocolInfo) {
        $relayArguments += '-ProtocolInfo'
    }
    if ($NamePattern) {
        $relayArguments += @('-NamePattern', $NamePattern)
    }
    if ($MaxItems -gt 0) {
        $relayArguments += @('-MaxItems', [string]$MaxItems)
    }
    if ($NoRaw) {
        $relayArguments += '-NoRaw'
    }
    if ($PhysicalGroups) {
        $relayArguments += @('-PhysicalGroups', $PhysicalGroups)
    }
    if ($UnitIds) {
        $relayArguments += @('-UnitIds', $UnitIds)
    }
    & $x86PowerShell $relayArguments
    exit $LASTEXITCODE
}

if (-not (Test-Path -LiteralPath $DatabaseDirectory -PathType Container)) {
    throw "Techstream database root not found: $DatabaseDirectory. Pass -DatabaseDirectory or set the installed location explicitly."
}
if (-not (Test-Path -LiteralPath $TechstreamBinDirectory -PathType Container)) {
    throw "Techstream binary directory not found: $TechstreamBinDirectory. Pass -TechstreamBinDirectory explicitly."
}
if (-not $Catalog -and [string]::IsNullOrWhiteSpace($Keys)) {
    throw '-Keys is required for monitor, protocol, physical, and unit queries. Use -Catalog to discover ECU keys.'
}
$keyValues = if ($Keys) {
    @($Keys -split ',' | ForEach-Object { [uint32]$_.Trim() })
} else {
    @()
}

function Write-QueryResult {
    param([Parameter(Mandatory = $true)][object]$Value)

    if ($OutputFormat -eq 'JsonLines') {
        $Value | ConvertTo-Json -Compress -Depth 4
    } else {
        Write-Output $Value
    }
}

if (-not ('TechstreamDbNative' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;

public static class TechstreamDbNative
{
    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Ansi)]
    public static extern IntPtr LoadLibrary(string path);

    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Ansi)]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool SetDllDirectory(string path);

    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Ansi)]
    public static extern IntPtr GetProcAddress(IntPtr module, string name);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool FreeLibrary(IntPtr module);

    [UnmanagedFunctionPointer(CallingConvention.ThisCall)]
    public delegate IntPtr DataCtrlConstructor(IntPtr self, IntPtr dbDirectory, IntPtr ecuFile);

    [UnmanagedFunctionPointer(CallingConvention.ThisCall)]
    public delegate void Destructor(IntPtr self);

    [UnmanagedFunctionPointer(CallingConvention.ThisCall)]
    public delegate IntPtr RecordConstructor(IntPtr self);

    [UnmanagedFunctionPointer(CallingConvention.ThisCall)]
    public delegate UInt32 GetDbRecord0(IntPtr self, Int16 table, IntPtr record);

    [UnmanagedFunctionPointer(CallingConvention.ThisCall)]
    public delegate UInt32 GetDbRecord1(IntPtr self, Int16 table, IntPtr record, UInt32 key1);

    [UnmanagedFunctionPointer(CallingConvention.ThisCall)]
    public delegate UInt32 GetDbRecord2(
        IntPtr self, Int16 table, IntPtr record, UInt32 key1, UInt32 key2);

    [UnmanagedFunctionPointer(CallingConvention.ThisCall)]
    public delegate IntPtr GetMonitorName(IntPtr self, Int16 index);

    public static Delegate Export(IntPtr module, string name, Type delegateType)
    {
        IntPtr address = GetProcAddress(module, name);
        if (address == IntPtr.Zero) {
            throw new Win32Exception(Marshal.GetLastWin32Error(), "GetProcAddress(" + name + ")");
        }
        return Marshal.GetDelegateForFunctionPointer(address, delegateType);
    }
}
'@
}

$dllPath = Join-Path $TechstreamBinDirectory 'KgpDataCtrl.dll'
[void][TechstreamDbNative]::SetDllDirectory($TechstreamBinDirectory)
$module = [TechstreamDbNative]::LoadLibrary($dllPath)
if ($module -eq [IntPtr]::Zero) {
    throw "Could not load $dllPath (Win32 error $([Runtime.InteropServices.Marshal]::GetLastWin32Error()))."
}

$dataCtrl = [IntPtr]::Zero
$record = [IntPtr]::Zero
$dbDirectoryString = [IntPtr]::Zero
$ecuFileString = [IntPtr]::Zero

try {
    $dataCtrlCtor = [TechstreamDbNative]::Export(
        $module, '??0CDataCtrl@@QAE@QBD0@Z', [TechstreamDbNative+DataCtrlConstructor])
    $dataCtrlDtor = [TechstreamDbNative]::Export(
        $module, '??1CDataCtrl@@QAE@XZ', [TechstreamDbNative+Destructor])
    if ($Catalog) {
        $recordCtor = [TechstreamDbNative]::Export(
            $module, '??0CDbEcuCategoryResRecords@@QAE@XZ', [TechstreamDbNative+RecordConstructor])
        $recordDtor = [TechstreamDbNative]::Export(
            $module, '??1CDbEcuCategoryResRecords@@UAE@XZ', [TechstreamDbNative+Destructor])
        $getRecord0 = [TechstreamDbNative]::Export(
            $module, '?GetDbRecord@CDiagToolDb@@QAEKFPAVCDbResRecordsBase@@@Z',
            [TechstreamDbNative+GetDbRecord0])
        $getName = [TechstreamDbNative]::Export(
            $module, '?GetEcuName@CDbEcuCategoryResRecords@@QAEPADF@Z',
            [TechstreamDbNative+GetMonitorName])
        $getFileName = [TechstreamDbNative]::Export(
            $module, '?GetEcuFileName@CDbEcuCategoryResRecords@@QAEPADF@Z',
            [TechstreamDbNative+GetMonitorName])
    } elseif ($ProtocolInfo) {
        $recordCtor = [TechstreamDbNative]::Export(
            $module, '??0CDbProtInfoResRecords@@QAE@XZ', [TechstreamDbNative+RecordConstructor])
        $recordDtor = [TechstreamDbNative]::Export(
            $module, '??1CDbProtInfoResRecords@@UAE@XZ', [TechstreamDbNative+Destructor])
        $getRecord = [TechstreamDbNative]::Export(
            $module, '?GetDbRecord@CDiagToolDb@@QAEKFPAVCDbResRecordsBase@@K@Z',
            [TechstreamDbNative+GetDbRecord1])
    } elseif ($PhysicalGroups) {
        $recordCtor = [TechstreamDbNative]::Export(
            $module, '??0CDbPhyDataResRecords@@QAE@XZ', [TechstreamDbNative+RecordConstructor])
        $recordDtor = [TechstreamDbNative]::Export(
            $module, '??1CDbPhyDataResRecords@@UAE@XZ', [TechstreamDbNative+Destructor])
        $getRecord2 = [TechstreamDbNative]::Export(
            $module, '?GetDbRecord@CDiagToolDb@@QAEKFPAVCDbResRecordsBase@@KK@Z',
            [TechstreamDbNative+GetDbRecord2])
    } elseif ($UnitIds) {
        $recordCtor = [TechstreamDbNative]::Export(
            $module, '??0CDbUnitResRecords@@QAE@XZ', [TechstreamDbNative+RecordConstructor])
        $recordDtor = [TechstreamDbNative]::Export(
            $module, '??1CDbUnitResRecords@@UAE@XZ', [TechstreamDbNative+Destructor])
        $getRecord2 = [TechstreamDbNative]::Export(
            $module, '?GetDbRecord@CDiagToolDb@@QAEKFPAVCDbResRecordsBase@@KK@Z',
            [TechstreamDbNative+GetDbRecord2])
        $getUnitString = [TechstreamDbNative]::Export(
            $module, '?GetDefaultUnitStr@CDbUnitResRecords@@QAEPADF@Z',
            [TechstreamDbNative+GetMonitorName])
    } else {
        $recordCtor = [TechstreamDbNative]::Export(
            $module, '??0CDbDatamonitorP5ResRecords@@QAE@XZ', [TechstreamDbNative+RecordConstructor])
        $recordDtor = [TechstreamDbNative]::Export(
            $module, '??1CDbDatamonitorP5ResRecords@@UAE@XZ', [TechstreamDbNative+Destructor])
        $getRecord = [TechstreamDbNative]::Export(
            $module, '?GetDbRecord@CDiagToolDb@@QAEKFPAVCDbResRecordsBase@@K@Z',
            [TechstreamDbNative+GetDbRecord1])
        $getName = [TechstreamDbNative]::Export(
            $module, '?GetDataMonitorName@CDbDatamonitorP5ResRecords@@QAEPADF@Z',
            [TechstreamDbNative+GetMonitorName])
    }

    # Generous buffers isolate this experimental caller from incomplete class layouts.
    $dataCtrl = [Runtime.InteropServices.Marshal]::AllocHGlobal(4096)
    $record = [Runtime.InteropServices.Marshal]::AllocHGlobal(256)
    for ($offset = 0; $offset -lt 4096; $offset += 4) {
        [Runtime.InteropServices.Marshal]::WriteInt32($dataCtrl, $offset, 0)
    }
    $databaseRoot = $DatabaseDirectory.TrimEnd('\') + '\'
    $dbDirectoryString = [Runtime.InteropServices.Marshal]::StringToHGlobalAnsi($databaseRoot)
    $ecuPath = if ([IO.Path]::IsPathRooted($EcuFile)) {
        $EcuFile
    } else {
        Join-Path (Join-Path $DatabaseDirectory 'DB') $EcuFile
    }
    $ecuFileString = [Runtime.InteropServices.Marshal]::StringToHGlobalAnsi($ecuPath)
    [void]$dataCtrlCtor.Invoke($dataCtrl, $dbDirectoryString, $ecuFileString)

    $databaseStatus = [Runtime.InteropServices.Marshal]::ReadInt32($dataCtrl, 0xec)
    if ($databaseStatus -ne 0) {
        $ecuDbObject = [Runtime.InteropServices.Marshal]::ReadInt32($dataCtrl, 0xe8)
        throw ('Techstream database constructor failed with status 0x{0:X8}; ECU DB object 0x{1:X8}; root {2}; file {3}' -f $databaseStatus, $ecuDbObject, $databaseRoot, $ecuPath)
    }

    $diagToolDb = [IntPtr]::Add($dataCtrl, 0xbc)
    if ($Catalog) {
        [void]$recordCtor.Invoke($record)
        try {
            # 0x110 is master-file table index 0x10 (ECU category/file map).
            $status = $getRecord0.Invoke($diagToolDb, [int16]0x0110, $record)
            $count = [Runtime.InteropServices.Marshal]::ReadInt16($record, 4)
            if ($status -ne 0) {
                throw ('ECU catalog query failed with status 0x{0:X8}' -f $status)
            }
            $recordPointers = [IntPtr][Runtime.InteropServices.Marshal]::ReadInt32($record, 12)
            $itemsEmitted = 0
            for ($index = 0; $index -lt $count; $index++) {
                $namePointer = $getName.Invoke($record, [int16]$index)
                $filePointer = $getFileName.Invoke($record, [int16]$index)
                $name = if ($namePointer -eq [IntPtr]::Zero) { $null } else {
                    [Runtime.InteropServices.Marshal]::PtrToStringAnsi($namePointer)
                }
                $fileName = if ($filePointer -eq [IntPtr]::Zero) { $null } else {
                    [Runtime.InteropServices.Marshal]::PtrToStringAnsi($filePointer)
                }
                if ($NamePattern -and
                    $name -notmatch $NamePattern -and $fileName -notmatch $NamePattern) {
                    continue
                }
                $itemPointer = [IntPtr][Runtime.InteropServices.Marshal]::ReadInt32(
                    $recordPointers, $index * 4)
                $raw = [byte[]]::new(72)
                [Runtime.InteropServices.Marshal]::Copy($itemPointer, $raw, 0, $raw.Length)
                $result = [ordered]@{
                    Index = $index
                    Ecu = $name
                    File = $fileName
                    Key = [BitConverter]::ToUInt32($raw, 68)
                }
                if (-not $NoRaw) {
                    $result.Raw = ($raw | ForEach-Object { $_.ToString('X2') }) -join ' '
                }
                Write-QueryResult ([pscustomobject]$result)
                $itemsEmitted++
                if ($MaxItems -gt 0 -and $itemsEmitted -ge $MaxItems) {
                    break
                }
            }
        }
        finally {
            $recordDtor.Invoke($record)
        }
        return
    }

    if ($ProtocolInfo) {
        foreach ($key in $keyValues) {
            for ($offset = 0; $offset -lt 256; $offset += 4) {
                [Runtime.InteropServices.Marshal]::WriteInt32($record, $offset, 0)
            }
            [void]$recordCtor.Invoke($record)
            try {
                # DataMonitorPhase5_DT.dll queries protocol records from table 0x010d.
                $status = $getRecord.Invoke($diagToolDb, [int16]0x010d, $record, $key)
                $count = [Runtime.InteropServices.Marshal]::ReadInt16($record, 4)
                if ($status -ne 0 -or $count -le 0) {
                    Write-QueryResult ([pscustomobject]@{
                        Key = $key
                        Status = '0x{0:X8}' -f $status
                        Count = $count
                        Index = $null
                        Raw = $null
                    })
                    continue
                }
                $recordPointers = [IntPtr][Runtime.InteropServices.Marshal]::ReadInt32($record, 12)
                $limit = if ($MaxItems -gt 0) { [Math]::Min($count, $MaxItems) } else { $count }
                for ($index = 0; $index -lt $limit; $index++) {
                    $itemPointer = [IntPtr][Runtime.InteropServices.Marshal]::ReadInt32(
                        $recordPointers, $index * 4)
                    $raw = [byte[]]::new(28)
                    [Runtime.InteropServices.Marshal]::Copy($itemPointer, $raw, 0, $raw.Length)
                    $physicalRequest = [BitConverter]::ToUInt16($raw, 0x08)
                    Write-QueryResult ([pscustomobject]@{
                        Key = $key
                        Status = '0x{0:X8}' -f $status
                        Count = $count
                        Index = $index
                        ProtocolId = '0x{0:X4}' -f [BitConverter]::ToUInt16($raw, 0x02)
                        FunctionalRequestCanId = '0x{0:X3}' -f [BitConverter]::ToUInt16($raw, 0x06)
                        PhysicalRequestCanId = '0x{0:X3}' -f $physicalRequest
                        ResponseCanIdCandidate = '0x{0:X3}' -f ($physicalRequest + 8)
                        ResponseMask = '0x{0:X3}' -f [BitConverter]::ToUInt16($raw, 0x0e)
                        RequestMask = '0x{0:X3}' -f [BitConverter]::ToUInt16($raw, 0x10)
                        AlternateRequestCanId = '0x{0:X3}' -f [BitConverter]::ToUInt16($raw, 0x14)
                        Raw = if ($NoRaw) { $null } else {
                            ($raw | ForEach-Object { $_.ToString('X2') }) -join ' '
                        }
                    })
                }
            }
            finally {
                $recordDtor.Invoke($record)
            }
        }
        return
    }

    if ($PhysicalGroups) {
        $groupValues = @($PhysicalGroups -split ',' | ForEach-Object {
            $value = $_.Trim()
            if ($value -match '^0x') { [Convert]::ToUInt32($value.Substring(2), 16) }
            else { [uint32]$value }
        })
        foreach ($key in $keyValues) {
            foreach ($group in $groupValues) {
                for ($offset = 0; $offset -lt 256; $offset += 4) {
                    [Runtime.InteropServices.Marshal]::WriteInt32($record, $offset, 0)
                }
                [void]$recordCtor.Invoke($record)
                try {
                    $status = $getRecord2.Invoke(
                        $diagToolDb, [int16]0x020d, $record, $key, $group)
                    $count = [Runtime.InteropServices.Marshal]::ReadInt16($record, 4)
                    if ($status -ne 0 -or $count -le 0) {
                        Write-QueryResult ([pscustomobject]@{
                            Key = $key
                            PhysicalGroup = '0x{0:X4}' -f $group
                            Status = '0x{0:X8}' -f $status
                            Count = $count
                        })
                        continue
                    }
                    $recordPointers = [IntPtr][Runtime.InteropServices.Marshal]::ReadInt32($record, 12)
                    $limit = if ($MaxItems -gt 0) { [Math]::Min($count, $MaxItems) } else { $count }
                    for ($index = 0; $index -lt $limit; $index++) {
                        $itemPointer = [IntPtr][Runtime.InteropServices.Marshal]::ReadInt32(
                            $recordPointers, $index * 4)
                        $raw = [byte[]]::new(24)
                        [Runtime.InteropServices.Marshal]::Copy($itemPointer, $raw, 0, $raw.Length)
                        $result = [ordered]@{
                            Key = $key
                            PhysicalGroup = '0x{0:X4}' -f $group
                            Status = '0x{0:X8}' -f $status
                            Count = $count
                            Index = $index
                            CoefficientA = [BitConverter]::ToInt32($raw, 0)
                            CoefficientB = [BitConverter]::ToInt32($raw, 4)
                            CoefficientC = [BitConverter]::ToInt32($raw, 8)
                            UnitId = '0x{0:X4}' -f [BitConverter]::ToUInt16($raw, 0x0e)
                            ValueType = '0x{0:X2}' -f $raw[0x14]
                            SignalLsb = $raw[0x15]
                        }
                        if (-not $NoRaw) {
                            $result.Raw = ($raw | ForEach-Object { $_.ToString('X2') }) -join ' '
                        }
                        Write-QueryResult ([pscustomobject]$result)
                    }
                }
                finally {
                    $recordDtor.Invoke($record)
                }
            }
        }
        return
    }

    if ($UnitIds) {
        $unitValues = @($UnitIds -split ',' | ForEach-Object {
            $value = $_.Trim()
            if ($value -match '^0x') { [Convert]::ToUInt32($value.Substring(2), 16) }
            else { [uint32]$value }
        })
        foreach ($key in $keyValues) {
            foreach ($unitId in $unitValues) {
                for ($offset = 0; $offset -lt 256; $offset += 4) {
                    [Runtime.InteropServices.Marshal]::WriteInt32($record, $offset, 0)
                }
                [void]$recordCtor.Invoke($record)
                try {
                    $status = $getRecord2.Invoke(
                        $diagToolDb, [int16]0x020f, $record, $key, $unitId)
                    $count = [Runtime.InteropServices.Marshal]::ReadInt16($record, 4)
                    $unitPointer = if ($status -eq 0 -and $count -gt 0) {
                        $getUnitString.Invoke($record, [int16]0)
                    } else { [IntPtr]::Zero }
                    Write-QueryResult ([pscustomobject]@{
                        Key = $key
                        UnitId = '0x{0:X4}' -f $unitId
                        Status = '0x{0:X8}' -f $status
                        Count = $count
                        Unit = if ($unitPointer -eq [IntPtr]::Zero) { $null } else {
                            [Runtime.InteropServices.Marshal]::PtrToStringAnsi($unitPointer)
                        }
                    })
                }
                finally {
                    $recordDtor.Invoke($record)
                }
            }
        }
        return
    }

    foreach ($key in $keyValues) {
        $itemsEmitted = 0
        for ($offset = 0; $offset -lt 256; $offset += 4) {
            [Runtime.InteropServices.Marshal]::WriteInt32($record, $offset, 0)
        }
        [void]$recordCtor.Invoke($record)
        try {
            $status = $getRecord.Invoke($diagToolDb, [int16]$Table, $record, $key)
            $count = [Runtime.InteropServices.Marshal]::ReadInt16($record, 4)
            if ($status -ne 0 -or $count -le 0) {
                Write-QueryResult ([pscustomobject]@{
                    Key = $key
                    Status = '0x{0:X8}' -f $status
                    Count = $count
                    Index = $null
                    Name = $null
                    Raw = $null
                })
                continue
            }

            $recordPointers = [IntPtr][Runtime.InteropServices.Marshal]::ReadInt32($record, 12)
            for ($index = 0; $index -lt $count; $index++) {
                $namePointer = $getName.Invoke($record, [int16]$index)
                $name = if ($namePointer -eq [IntPtr]::Zero) {
                    $null
                } else {
                    [Runtime.InteropServices.Marshal]::PtrToStringAnsi($namePointer)
                }
                $itemPointer = [IntPtr][Runtime.InteropServices.Marshal]::ReadInt32(
                    $recordPointers, $index * 4)
                $raw = [byte[]]::new(60)
                [Runtime.InteropServices.Marshal]::Copy($itemPointer, $raw, 0, $raw.Length)
                if ($NamePattern -and $name -notmatch $NamePattern) {
                    continue
                }
                $flags = [BitConverter]::ToUInt32($raw, 0x20)
                $internalId = [BitConverter]::ToUInt16($raw, 0x24)
                $dataId = [BitConverter]::ToUInt16($raw, 0x36)
                $result = [ordered]@{
                    Key = $key
                    Status = '0x{0:X8}' -f $status
                    Count = $count
                    Index = $index
                    Name = $name
                    Flags = '0x{0:X8}' -f $flags
                    InternalId = '0x{0:X4}' -f $internalId
                    Acquisition = if (($flags -band 0x10) -eq 0) { 'direct DID' } else { 'mapped/dynamic' }
                    ReadRequest = if (($flags -band 0x10) -eq 0) {
                        '22 {0:X2} {1:X2}' -f (($dataId -shr 8) -band 0xff), ($dataId -band 0xff)
                    } else { $null }
                    FirstBit = [BitConverter]::ToUInt16($raw, 0x2c)
                    LastBit = [BitConverter]::ToUInt16($raw, 0x2e)
                    PhysicalGroup = '0x{0:X4}' -f [BitConverter]::ToUInt16($raw, 0x2a)
                    DataId = '0x{0:X4}' -f $dataId
                }
                if (-not $NoRaw) {
                    $result.Raw = ($raw | ForEach-Object { $_.ToString('X2') }) -join ' '
                }
                Write-QueryResult ([pscustomobject]$result)
                $itemsEmitted++
                if ($MaxItems -gt 0 -and $itemsEmitted -ge $MaxItems) {
                    break
                }
            }
        }
        finally {
            $recordDtor.Invoke($record)
        }
    }
}
finally {
    if ($dataCtrl -ne [IntPtr]::Zero) {
        if ($null -ne $dataCtrlDtor) {
            $dataCtrlDtor.Invoke($dataCtrl)
        }
        [Runtime.InteropServices.Marshal]::FreeHGlobal($dataCtrl)
    }
    if ($record -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::FreeHGlobal($record)
    }
    if ($dbDirectoryString -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::FreeHGlobal($dbDirectoryString)
    }
    if ($ecuFileString -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::FreeHGlobal($ecuFileString)
    }
    [void][TechstreamDbNative]::FreeLibrary($module)
}
