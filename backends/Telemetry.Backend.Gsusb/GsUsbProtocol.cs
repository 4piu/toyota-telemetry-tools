// gs_usb protocol constants and wire structs, transport-agnostic.
// Sourced directly from candle-usb/candleLight_fw's include/gs_usb.h rather
// than from memory: request numbers, struct layout, and endpoint addresses
// must match the firmware exactly or every transfer is silently misread.
using System;
using System.Runtime.InteropServices;

namespace Telemetry.Backend.Gsusb
{
    /// <summary>Endpoint addresses fixed by the gs_usb protocol.</summary>
    internal static class GsUsbEndpoint
    {
        internal const byte In = 0x81;
        internal const byte Out = 0x02;
    }

    /// <summary>USB control-request numbers (bRequest values), in the order
    /// gs_usb.h declares them. Only the subset a classical-CAN channel needs
    /// is used; the rest exist so the enum matches the firmware's numbering.</summary>
    internal enum GsUsbRequest : byte
    {
        HostFormat = 0,
        BitTiming = 1,
        Mode = 2,
        Berr = 3,
        BtConst = 4,
        DeviceConfig = 5,
        Timestamp = 6,
        Identify = 7
    }

    internal enum GsCanMode : uint
    {
        Reset = 0,
        Start = 1
    }

    [Flags]
    internal enum GsCanFeature : uint
    {
        None = 0,
        ListenOnly = 1 << 0,
        LoopBack = 1 << 1,
        TripleSample = 1 << 2,
        OneShot = 1 << 3,
        HwTimestamp = 1 << 4
    }

    /// <summary>Sent once after opening, to declare host byte order. candleLight
    /// always exchanges little-endian regardless of this value, but the Linux
    /// gs_usb driver sends it as a handshake and real hardware expects it.</summary>
    [StructLayout(LayoutKind.Sequential, Pack = 1)]
    internal struct GsHostConfig
    {
        internal uint ByteOrder;

        internal const uint MagicNumber = 0x0000beef;
    }

    [StructLayout(LayoutKind.Sequential, Pack = 1)]
    internal struct GsDeviceMode
    {
        internal uint Mode;
        internal uint Feature;
    }

    [StructLayout(LayoutKind.Sequential, Pack = 1)]
    internal struct GsDeviceBittiming
    {
        internal uint PropSeg;
        internal uint PhaseSeg1;
        internal uint PhaseSeg2;
        internal uint Sjw;
        internal uint Brp;
    }

    [StructLayout(LayoutKind.Sequential, Pack = 1)]
    internal struct CanBittimingConst
    {
        internal uint Tseg1Min;
        internal uint Tseg1Max;
        internal uint Tseg2Min;
        internal uint Tseg2Max;
        internal uint SjwMax;
        internal uint BrpMin;
        internal uint BrpMax;
        internal uint BrpInc;
    }

    [StructLayout(LayoutKind.Sequential, Pack = 1)]
    internal struct GsDeviceBtConst
    {
        internal uint Feature;
        internal uint FclkCan;
        internal CanBittimingConst Btc;

        internal const int WireSize = 4 + 4 + 8 * 4; // = 40 bytes
    }

    /// <summary>One classical CAN frame as exchanged with the device: 12-byte
    /// header plus 8 data bytes, always 20 bytes total. The firmware also
    /// supports a 24-byte variant carrying a hardware timestamp when the
    /// HwTimestamp feature is requested at Start; this backend never requests
    /// it; <see cref="ICanChannel"/> has no timestamp to report.</summary>
    [StructLayout(LayoutKind.Sequential, Pack = 1)]
    internal struct GsHostFrame
    {
        internal uint EchoId;
        internal uint CanId;
        internal byte CanDlc;
        internal byte Channel;
        internal byte Flags;
        internal byte Reserved;
        [MarshalAs(UnmanagedType.ByValArray, SizeConst = 8)]
        internal byte[] Data;

        internal const int WireSize = 4 + 4 + 1 + 1 + 1 + 1 + 8; // = 20 bytes

        /// <summary>0xffffffff marks a frame as received from the bus (see
        /// GS_HOST_FRAME_ECHO_ID_RX in gs_usb.h). A frame this backend
        /// transmits therefore must use any other value; it is otherwise
        /// unused, since Write does not wait for or match the device's TX
        /// echo. The device places both on the same IN endpoint, so a reader
        /// must discard any frame whose echo_id is not this constant - those
        /// are TX echoes of something already sent, not new received data.</summary>
        internal const uint EchoIdReceived = 0xffffffff;
        internal const uint EchoIdTransmitPlaceholder = 0;

        internal byte[] ToBytes()
        {
            byte[] buffer = new byte[WireSize];
            BitConverter.GetBytes(EchoId).CopyTo(buffer, 0);
            BitConverter.GetBytes(CanId).CopyTo(buffer, 4);
            buffer[8] = CanDlc;
            buffer[9] = Channel;
            buffer[10] = Flags;
            buffer[11] = Reserved;
            Array.Copy(Data ?? new byte[8], 0, buffer, 12, 8);
            return buffer;
        }

        internal static GsHostFrame FromBytes(byte[] buffer)
        {
            if (buffer == null || buffer.Length < WireSize)
            {
                throw new FormatException(
                    "gs_usb IN transfer was shorter than one host frame (" +
                    (buffer == null ? 0 : buffer.Length) + " of " + WireSize + " bytes).");
            }
            GsHostFrame frame = new GsHostFrame();
            frame.EchoId = BitConverter.ToUInt32(buffer, 0);
            frame.CanId = BitConverter.ToUInt32(buffer, 4);
            frame.CanDlc = buffer[8];
            frame.Channel = buffer[9];
            frame.Flags = buffer[10];
            frame.Reserved = buffer[11];
            frame.Data = new byte[8];
            Array.Copy(buffer, 12, frame.Data, 0, 8);
            return frame;
        }
    }

    /// <summary>CAN_EFF_FLAG / CAN_RTR_FLAG / CAN_ERR_FLAG live in the top bits
    /// of can_id on the wire; only the 11-bit standard identifier below them
    /// is meaningful to a classical-CAN-only channel.</summary>
    internal static class GsCanId
    {
        internal const uint StandardIdMask = 0x000007ff;
        internal const uint ExtendedFlag = 0x80000000;
        internal const uint RemoteFlag = 0x40000000;
        internal const uint ErrorFlag = 0x20000000;
    }

    /// <summary>Turns a target bitrate into prop_seg/phase_seg1/phase_seg2/sjw/brp
    /// against whatever CAN clock and timing constraints the connected device
    /// reports. Not hardcoded per board, unlike PcanChannel's fixed presets:
    /// PEAK's driver always runs the same PEAK-chosen clock, but a gs_usb
    /// device's fclk_can is a firmware/board property this backend cannot
    /// assume in advance.</summary>
    internal static class GsBitTiming
    {
        private const double SamplePoint = 0.875;

        internal static GsDeviceBittiming Compute(GsDeviceBtConst constants, int bitrate)
        {
            CanBittimingConst btc = constants.Btc;
            uint bestBrp = 0;
            uint bestTseg1 = 0;
            uint bestTseg2 = 0;
            double bestError = double.MaxValue;

            for (uint brp = Math.Max(btc.BrpMin, 1u); brp <= btc.BrpMax; brp += Math.Max(btc.BrpInc, 1u))
            {
                double timeQuantaPerBit = (double)constants.FclkCan / (brp * bitrate);
                int totalTq = (int)Math.Round(timeQuantaPerBit);
                if (totalTq < 8 || totalTq > 25)
                {
                    // Outside the range a sane single-bit CAN timing ever
                    // occupies; skip without spending a sqrt-free division on
                    // constraint checks that would reject it anyway.
                    continue;
                }
                double error = Math.Abs(timeQuantaPerBit - totalTq) / timeQuantaPerBit;

                // 1 tq is always Sync_Seg; the rest splits between the two
                // phase segments at the target sample point.
                int tseg1 = (int)Math.Round((totalTq - 1) * SamplePoint) - 1;
                int tseg2 = totalTq - 1 - tseg1;
                if (tseg1 < (int)btc.Tseg1Min || tseg1 > (int)btc.Tseg1Max ||
                    tseg2 < (int)btc.Tseg2Min || tseg2 > (int)btc.Tseg2Max)
                {
                    continue;
                }
                if (error < bestError)
                {
                    bestError = error;
                    bestBrp = brp;
                    bestTseg1 = (uint)tseg1;
                    bestTseg2 = (uint)tseg2;
                }
            }

            if (bestBrp == 0)
            {
                throw new FormatException(
                    "No bit-timing solution for " + bitrate + " bit/s at a " +
                    constants.FclkCan + " Hz CAN clock within this device's constraints.");
            }

            GsDeviceBittiming result = new GsDeviceBittiming();
            result.Brp = bestBrp;
            result.PropSeg = 0; // folded into phase_seg1, as the Linux gs_usb driver does
            result.PhaseSeg1 = bestTseg1;
            result.PhaseSeg2 = bestTseg2;
            result.Sjw = Math.Min(bestTseg2, btc.SjwMax);
            if (result.Sjw == 0)
            {
                result.Sjw = 1;
            }
            return result;
        }
    }
}
