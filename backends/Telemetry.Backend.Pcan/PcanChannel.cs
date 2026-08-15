using System;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Text;
using Telemetry.Can.Core;

namespace Telemetry.Backend.Pcan
{
    /// <summary>
    /// PEAK PCAN-Basic binding. Also drives any adapter whose firmware the PEAK
    /// Windows driver enumerates as a PCAN channel, such as a CANable flashed
    /// with PCAN-compatible firmware.
    /// </summary>
    internal sealed class PcanChannel : ICanChannel
    {
        private const byte StandardMessage = 0x00;
        private const byte ListenOnlyParameter = 0x08;
        private const byte MessageFilterParameter = 0x04;
        private const uint FilterClosed = 0;
        private const uint ErrorOk = 0x00000;
        private const uint ErrorQueueReceiveEmpty = 0x00020;

        private readonly ushort channel;
        private readonly string channelName;
        private bool initialized;

        internal PcanChannel(string channelName)
        {
            this.channelName = channelName;
            this.channel = ResolveChannel(channelName);
        }

        public string Description
        {
            get { return channelName; }
        }

        public string BackendName
        {
            get { return "PCAN-Basic"; }
        }

        public void RefuseConcurrentClients()
        {
            Process[] processes = Process.GetProcessesByName("PcanView");
            try
            {
                if (processes.Length != 0)
                {
                    throw new InvalidOperationException(
                        "PCAN-View is running. Close it completely before --live so there is " +
                        "only one CAN client on the channel.");
                }
            }
            finally
            {
                foreach (Process process in processes)
                {
                    process.Dispose();
                }
            }
        }

        public void Open(int bitrate, uint filterFromId, uint filterToId)
        {
            string systemDll = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.System), "PCANBasic.dll");
            if (!File.Exists(systemDll))
            {
                throw new FileNotFoundException(
                    "PCAN-Basic is not installed; PCANBasic.dll was not found.", systemDll);
            }

            // Clear any listen-only preset before initializing: a leftover
            // listen-only channel accepts writes and silently drops them.
            uint off = 0;
            Check(PcanBasic.SetValue(channel, ListenOnlyParameter, ref off, sizeof(uint)),
                "disable listen-only preset");
            Check(PcanBasic.Initialize(channel, ResolveBaudRate(bitrate), 0, 0, 0),
                "initialize " + channelName + " at " +
                bitrate.ToString(CultureInfo.InvariantCulture) + " bit/s");
            initialized = true;
            Check(PcanBasic.GetStatus(channel), "initial CAN status");

            uint closed = FilterClosed;
            Check(PcanBasic.SetValue(channel, MessageFilterParameter, ref closed, sizeof(uint)),
                "close receive filter");
            Check(PcanBasic.FilterMessages(channel, filterFromId, filterToId, StandardMessage),
                "allow standard IDs 0x" + filterFromId.ToString("X", CultureInfo.InvariantCulture) +
                "..0x" + filterToId.ToString("X", CultureInfo.InvariantCulture));
            DrainReceiveQueue();
        }

        public void Write(uint canId, byte[] data)
        {
            if (data == null || data.Length != 8)
            {
                throw new InvalidOperationException("A CAN frame must contain exactly eight bytes.");
            }
            PcanMessage message = new PcanMessage
            {
                Id = canId,
                MessageType = StandardMessage,
                Length = 8,
                Data = (byte[])data.Clone()
            };
            Check(PcanBasic.Write(channel, ref message), "write CAN frame");
        }

        public bool TryRead(out uint canId, out byte[] data)
        {
            PcanMessage message;
            PcanTimestamp timestamp;
            uint status = PcanBasic.Read(channel, out message, out timestamp);
            if (status == ErrorQueueReceiveEmpty)
            {
                canId = 0;
                data = null;
                return false;
            }
            Check(status, "read CAN frame");
            if (message.MessageType != StandardMessage)
            {
                throw new InvalidOperationException(
                    "Received a non-standard CAN frame; message type=0x" +
                    message.MessageType.ToString("X2", CultureInfo.InvariantCulture) + ".");
            }
            canId = message.Id;
            data = message.Data == null ? null : Hex.Slice(message.Data, 0, 8);
            return true;
        }

        public void CheckStatus(string context)
        {
            Check(PcanBasic.GetStatus(channel), "CAN status " + context);
        }

        private void DrainReceiveQueue()
        {
            while (true)
            {
                PcanMessage ignored;
                PcanTimestamp ignoredTimestamp;
                uint status = PcanBasic.Read(channel, out ignored, out ignoredTimestamp);
                if (status == ErrorQueueReceiveEmpty)
                {
                    return;
                }
                Check(status, "drain receive queue");
            }
        }

        private static ushort ResolveChannel(string name)
        {
            // PCAN_USBBUS1..8 are 0x51..0x58.
            if (name != null && name.StartsWith("PCAN_USBBUS", StringComparison.OrdinalIgnoreCase))
            {
                int index;
                if (Int32.TryParse(
                        name.Substring("PCAN_USBBUS".Length),
                        NumberStyles.None,
                        CultureInfo.InvariantCulture,
                        out index) &&
                    index >= 1 && index <= 8)
                {
                    return (ushort)(0x50 + index);
                }
            }
            throw new FormatException(
                "device.pcan.channel must be PCAN_USBBUS1 through PCAN_USBBUS8; got: " + name);
        }

        private static ushort ResolveBaudRate(int bitrate)
        {
            switch (bitrate)
            {
                case 1000000: return 0x0014;
                case 800000: return 0x0016;
                case 500000: return 0x001C;
                case 250000: return 0x011C;
                case 125000: return 0x031C;
                case 100000: return 0x432F;
                case 50000: return 0x472F;
                case 20000: return 0x532F;
                case 10000: return 0x672F;
                default:
                    throw new FormatException(
                        "PCAN-Basic has no preset for " +
                        bitrate.ToString(CultureInfo.InvariantCulture) +
                        " bit/s; use a standard CAN bitrate.");
            }
        }

        private static void Check(uint status, string operation)
        {
            if (status != ErrorOk)
            {
                throw new InvalidOperationException(operation + " failed: " + Describe(status));
            }
        }

        private static string Describe(uint status)
        {
            StringBuilder text = new StringBuilder(256);
            uint result = PcanBasic.GetErrorText(status, 0x09, text);
            if (result == ErrorOk && text.Length != 0)
            {
                return text + " (0x" + status.ToString("X", CultureInfo.InvariantCulture) + ")";
            }
            return "PCAN status 0x" + status.ToString("X", CultureInfo.InvariantCulture);
        }

        public void Dispose()
        {
            if (!initialized)
            {
                return;
            }
            initialized = false;
            uint status = PcanBasic.Uninitialize(channel);
            if (status != ErrorOk)
            {
                Console.Error.WriteLine("WARNING: PCAN uninitialize failed: " + Describe(status));
            }
            else
            {
                Console.WriteLine("PCAN channel closed.");
            }
        }
    }
}
