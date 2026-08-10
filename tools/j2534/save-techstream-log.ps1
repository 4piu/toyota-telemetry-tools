param(
    [ValidateRange(100, 60000)]
    [int]$TimeoutMs = 10000,

    [ValidateRange(0, 3600)]
    [int]$WaitForLoggerSeconds = 0,

    [string]$SaveEventName = 'SAVE_J2534_LOG_FILE_EVENT',

    [string]$FinishEventName = 'FINISH_J2534_LOG_FILE_EVENT',

    [string]$LogDirectory = (Join-Path ([Environment]::GetFolderPath('CommonDocuments')) 'Techstream\ErrorReport'),

    [string]$LogPattern = 'j2534_*.log'
)

$ErrorActionPreference = 'Stop'

if (-not ('J2534LogEventNative' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;

public static class J2534LogEventNative
{
    public const uint EVENT_MODIFY_STATE = 0x0002;
    public const uint SYNCHRONIZE = 0x00100000;
    public const uint WAIT_OBJECT_0 = 0x00000000;
    public const uint WAIT_TIMEOUT = 0x00000102;

    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Ansi)]
    public static extern IntPtr OpenEvent(
        uint desiredAccess,
        bool inheritHandle,
        string name);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool SetEvent(IntPtr handle);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool ResetEvent(IntPtr handle);

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern uint WaitForSingleObject(IntPtr handle, uint milliseconds);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool CloseHandle(IntPtr handle);

    public static Exception LastError(string operation)
    {
        return new Win32Exception(Marshal.GetLastWin32Error(), operation);
    }
}
'@
}

$eventAccess = [J2534LogEventNative]::EVENT_MODIFY_STATE -bor [J2534LogEventNative]::SYNCHRONIZE

$loggerDeadline = (Get-Date).AddSeconds($WaitForLoggerSeconds)
do {
    $saveEvent = [J2534LogEventNative]::OpenEvent($eventAccess, $false, $SaveEventName)
    if ($saveEvent -ne [IntPtr]::Zero) {
        break
    }
    if ((Get-Date) -ge $loggerDeadline) {
        throw "No compatible J2534 logger opened event '$SaveEventName'. Start the OEM tool/logger, verify the event names for that version, and try again."
    }
    Start-Sleep -Milliseconds 250
} while ($true)

$finishEvent = [J2534LogEventNative]::OpenEvent($eventAccess, $false, $FinishEventName)
if ($finishEvent -eq [IntPtr]::Zero) {
    [void][J2534LogEventNative]::CloseHandle($saveEvent)
    throw [J2534LogEventNative]::LastError("OpenEvent($FinishEventName)")
}

try {
    if (-not [J2534LogEventNative]::ResetEvent($finishEvent)) {
        throw [J2534LogEventNative]::LastError("ResetEvent($FinishEventName)")
    }

    $requestedAt = Get-Date
    if (-not [J2534LogEventNative]::SetEvent($saveEvent)) {
        throw [J2534LogEventNative]::LastError("SetEvent($SaveEventName)")
    }

    $waitResult = [J2534LogEventNative]::WaitForSingleObject($finishEvent, [uint32]$TimeoutMs)
    if ($waitResult -eq [J2534LogEventNative]::WAIT_TIMEOUT) {
        throw "Timed out after $TimeoutMs ms while waiting for Techstream to save the J2534 log."
    }
    if ($waitResult -ne [J2534LogEventNative]::WAIT_OBJECT_0) {
        throw [J2534LogEventNative]::LastError("WaitForSingleObject($FinishEventName)")
    }

    $logFile = Get-ChildItem -LiteralPath $LogDirectory -Filter $LogPattern -File |
        Where-Object { $_.LastWriteTime -ge $requestedAt.AddSeconds(-2) } |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1

    if ($null -eq $logFile) {
        throw "The logger signaled completion, but no new '$LogPattern' file appeared in $LogDirectory."
    }

    $logFile | Select-Object FullName, Length, LastWriteTime
}
finally {
    [void][J2534LogEventNative]::CloseHandle($finishEvent)
    [void][J2534LogEventNative]::CloseHandle($saveEvent)
}
