// Minimal PEAK-System PCAN-Basic P/Invoke wrapper for classical CAN.
// Vehicle-agnostic hardware binding only: no vehicle, ECU, or DID content.
// A platform project vendors (copies) this file into its own compiled tool
// and layers ISO-TP framing, allowlists, and diagnostic sessions on top.
using System.Runtime.InteropServices;
using System.Text;

namespace Telemetry.Backend.Pcan
{
    [StructLayout(LayoutKind.Sequential)]
    internal struct PcanMessage
    {
        internal uint Id;
        [MarshalAs(UnmanagedType.U1)]
        internal byte MessageType;
        internal byte Length;
        [MarshalAs(UnmanagedType.ByValArray, SizeConst = 8)]
        internal byte[] Data;
    }

    [StructLayout(LayoutKind.Sequential)]
    internal struct PcanTimestamp
    {
        internal uint Milliseconds;
        internal ushort MillisecondsOverflow;
        internal ushort Microseconds;
    }

    /// <summary>
    /// Thin P/Invoke surface over PCANBasic.dll. Covers exactly the calls needed
    /// to initialize a channel, apply a hardware filter, and read/write classical
    /// CAN frames. Callers own bitrate, channel selection, filters, and cleanup.
    /// </summary>
    internal static class PcanBasic
    {
        [DllImport("PCANBasic.dll", EntryPoint = "CAN_Initialize")]
        internal static extern uint Initialize(
            [MarshalAs(UnmanagedType.U2)] ushort channel,
            [MarshalAs(UnmanagedType.U2)] ushort bitRate,
            byte deprecated1,
            uint deprecated2,
            ushort deprecated3);

        [DllImport("PCANBasic.dll", EntryPoint = "CAN_Uninitialize")]
        internal static extern uint Uninitialize([MarshalAs(UnmanagedType.U2)] ushort channel);

        [DllImport("PCANBasic.dll", EntryPoint = "CAN_GetStatus")]
        internal static extern uint GetStatus([MarshalAs(UnmanagedType.U2)] ushort channel);

        [DllImport("PCANBasic.dll", EntryPoint = "CAN_Read")]
        internal static extern uint Read(
            [MarshalAs(UnmanagedType.U2)] ushort channel,
            out PcanMessage message,
            out PcanTimestamp timestamp);

        [DllImport("PCANBasic.dll", EntryPoint = "CAN_Write")]
        internal static extern uint Write(
            [MarshalAs(UnmanagedType.U2)] ushort channel,
            ref PcanMessage message);

        [DllImport("PCANBasic.dll", EntryPoint = "CAN_FilterMessages")]
        internal static extern uint FilterMessages(
            [MarshalAs(UnmanagedType.U2)] ushort channel,
            uint fromId,
            uint toId,
            [MarshalAs(UnmanagedType.U1)] byte mode);

        [DllImport("PCANBasic.dll", EntryPoint = "CAN_SetValue")]
        internal static extern uint SetValue(
            [MarshalAs(UnmanagedType.U2)] ushort channel,
            [MarshalAs(UnmanagedType.U1)] byte parameter,
            ref uint value,
            uint valueLength);

        [DllImport("PCANBasic.dll", EntryPoint = "CAN_GetErrorText")]
        internal static extern uint GetErrorText(
            [MarshalAs(UnmanagedType.U4)] uint error,
            ushort language,
            StringBuilder text);
    }
}
