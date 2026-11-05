using System;
using System.Globalization;
using System.IO;
using Telemetry.Can.Core;

namespace Telemetry.Backend.J2534
{
    /// <summary>
    /// J2534 (PassThru) binding operating the adapter in <em>raw CAN</em> mode.
    ///
    /// ISO15765 mode would let the adapter perform segmentation and flow
    /// control internally, but then transport behaviour varies by vendor and
    /// cannot be compared against another backend. Raw CAN plus the shared
    /// application ISO-TP layer keeps every backend byte-identical on the bus.
    /// </summary>
    internal sealed class J2534Channel : ICanChannel
    {
        private const uint ProtocolCan = 5;
        private const uint PassFilter = 1;
        private const uint ClearRxBuffer = 0x08;
        private const int StatusNoError = 0x00;
        private const int ErrorTimeout = 0x09;
        private const int ErrorBufferEmpty = 0x10;

        private readonly string dllPath;
        private J2534Api api;
        private uint deviceId;
        private uint channelId;
        private uint filterId;
        private bool deviceOpen;
        private bool channelOpen;
        private bool filterOpen;

        internal J2534Channel(string dllPath)
        {
            this.dllPath = dllPath;
        }

        public string Description
        {
            get { return Path.GetFileName(dllPath); }
        }

        public string BackendName
        {
            get { return "J2534"; }
        }

        public void RefuseConcurrentClients()
        {
            // A J2534 DLL rejects a second PassThruOpen itself; there is no
            // reliable cross-vendor way to detect the owner beforehand.
        }

        public void Open(int bitrate, uint filterFromId, uint filterToId)
        {
            VerifyArchitecture();
            api = new J2534Api(dllPath);
            Check(api.Open(out deviceId), "PassThruOpen");
            deviceOpen = true;

            string firmware, dll, apiVersion;
            if (api.ReadVersion(deviceId, out firmware, out dll, out apiVersion) == StatusNoError)
            {
                Console.Error.WriteLine(
                    "Adapter versions: firmware=" + firmware + ", DLL=" + dll + ", API=" + apiVersion);
            }

            Check(api.Connect(deviceId, ProtocolCan, 0, (uint)bitrate, out channelId),
                "PassThruConnect(CAN)");
            channelOpen = true;
            Check(api.Ioctl(channelId, ClearRxBuffer), "PassThruIoctl(CLEAR_RX_BUFFER)");

            // A pass filter matches one identifier, so a range needs the mask to
            // cover only the bits shared across it.
            uint mask = 0x7FF & ~(filterFromId ^ filterToId);
            PassThruMsg maskMessage = MakeMessage(CanIdBytes(mask));
            PassThruMsg patternMessage = MakeMessage(CanIdBytes(filterFromId));
            Check(
                api.StartPassFilter(
                    channelId, PassFilter, ref maskMessage, ref patternMessage, out filterId),
                "PassThruStartMsgFilter(PASS_FILTER)");
            filterOpen = true;
            DrainReceiveQueue();
        }

        public void Write(uint canId, byte[] data)
        {
            if (data == null || data.Length != 8)
            {
                throw new InvalidOperationException("A CAN frame must contain exactly eight bytes.");
            }
            byte[] frame = new byte[12];
            Buffer.BlockCopy(CanIdBytes(canId), 0, frame, 0, 4);
            Buffer.BlockCopy(data, 0, frame, 4, 8);
            PassThruMsg message = MakeMessage(frame);
            uint count = 1;
            Check(api.WriteMessages(channelId, ref message, ref count, 1000), "PassThruWriteMsgs");
            if (count != 1)
            {
                throw new InvalidOperationException(
                    "PassThruWriteMsgs sent " + count.ToString(CultureInfo.InvariantCulture) +
                    " messages instead of 1.");
            }
        }

        public bool TryRead(out uint canId, out byte[] data)
        {
            canId = 0;
            data = null;
            PassThruMsg received = EmptyMessage();
            uint count = 1;
            int status = api.ReadMessages(channelId, ref received, ref count, 1);
            if (status == ErrorTimeout || status == ErrorBufferEmpty)
            {
                return false;
            }
            Check(status, "PassThruReadMsgs");
            if (count == 0 || received.Data == null ||
                received.DataSize < 5 || received.DataSize > 4128)
            {
                return false;
            }
            // Some vendor DLLs report ProtocolId=0 on receive even on a CAN
            // channel; accept that quirk, and validate the identifier instead.
            if (received.ProtocolId != 0 && received.ProtocolId != ProtocolCan)
            {
                return false;
            }
            byte[] raw = Hex.Slice(received.Data, 0, (int)received.DataSize);
            canId = ReadCanId(raw);
            byte[] payload = Hex.Slice(raw, 4, raw.Length - 4);
            if (payload.Length < 8)
            {
                throw new InvalidOperationException(
                    "Raw CAN frame carried fewer than eight data bytes: " + Hex.Format(payload));
            }
            data = Hex.Slice(payload, 0, 8);
            return true;
        }

        public void CheckStatus(string context)
        {
            // J2534 exposes no cheap controller-state read equivalent to
            // PCAN's GetStatus; transport faults surface as read errors.
        }

        private void DrainReceiveQueue()
        {
            uint ignoredId;
            byte[] ignoredData;
            while (TryRead(out ignoredId, out ignoredData))
            {
            }
        }

        private void VerifyArchitecture()
        {
            if (!File.Exists(dllPath))
            {
                throw new FileNotFoundException("J2534 DLL not found.", dllPath);
            }
            ushort machine = ReadPeMachine(dllPath);
            bool dllIs64Bit = machine == 0x8664;
            if (dllIs64Bit != Environment.Is64BitProcess)
            {
                throw new InvalidOperationException(
                    "Architecture mismatch: the J2534 DLL is " +
                    (dllIs64Bit ? "x64" : "x86") + " but this process is " +
                    (Environment.Is64BitProcess ? "x64" : "x86") +
                    ". Build the backend to match the vendor DLL.");
            }
        }

        private static ushort ReadPeMachine(string path)
        {
            using (FileStream stream = File.OpenRead(path))
            using (BinaryReader reader = new BinaryReader(stream))
            {
                if (reader.ReadUInt16() != 0x5A4D)
                {
                    throw new InvalidOperationException("J2534 DLL has no MZ header.");
                }
                stream.Position = 0x3C;
                stream.Position = reader.ReadInt32();
                if (reader.ReadUInt32() != 0x00004550)
                {
                    throw new InvalidOperationException("J2534 DLL has no PE header.");
                }
                return reader.ReadUInt16();
            }
        }

        private static PassThruMsg EmptyMessage()
        {
            PassThruMsg message = new PassThruMsg();
            message.Data = new byte[4128];
            return message;
        }

        private static PassThruMsg MakeMessage(byte[] data)
        {
            PassThruMsg message = EmptyMessage();
            message.ProtocolId = ProtocolCan;
            message.TxFlags = 0;
            message.DataSize = (uint)data.Length;
            Buffer.BlockCopy(data, 0, message.Data, 0, data.Length);
            return message;
        }

        private static byte[] CanIdBytes(uint id)
        {
            return new byte[]
            {
                (byte)((id >> 24) & 0xFF),
                (byte)((id >> 16) & 0xFF),
                (byte)((id >> 8) & 0xFF),
                (byte)(id & 0xFF)
            };
        }

        private static uint ReadCanId(byte[] data)
        {
            return ((uint)data[0] << 24) | ((uint)data[1] << 16) | ((uint)data[2] << 8) | data[3];
        }

        private void Check(int status, string operation)
        {
            if (status != StatusNoError)
            {
                string description = api == null ? String.Empty : api.GetLastError();
                throw new InvalidOperationException(
                    operation + " failed: 0x" + status.ToString("X2", CultureInfo.InvariantCulture) +
                    (String.IsNullOrEmpty(description) ? String.Empty : " (" + description + ")"));
            }
        }

        public void Dispose()
        {
            if (api == null)
            {
                return;
            }
            if (filterOpen)
            {
                Report(api.StopMessageFilter(channelId, filterId), "PassThruStopMsgFilter");
                filterOpen = false;
            }
            if (channelOpen)
            {
                Report(api.Disconnect(channelId), "PassThruDisconnect");
                channelOpen = false;
            }
            if (deviceOpen)
            {
                Report(api.Close(deviceId), "PassThruClose");
                deviceOpen = false;
                Console.Error.WriteLine("J2534 session closed.");
            }
            api.Dispose();
            api = null;
        }

        private static void Report(int status, string operation)
        {
            if (status != StatusNoError)
            {
                Console.Error.WriteLine(
                    "Cleanup warning: " + operation + " failed: 0x" +
                    status.ToString("X2", CultureInfo.InvariantCulture));
            }
        }
    }
}
