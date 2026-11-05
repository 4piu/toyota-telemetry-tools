using System;
using System.Globalization;
using Telemetry.Can.Core;

namespace Telemetry.Backend.Gsusb
{
    /// <summary>
    /// gs_usb binding: candleLight_fw and any other firmware implementing the
    /// same protocol (the Linux kernel's gs_usb class, cantact, USB2CAN, and
    /// their many board-specific derivatives).
    ///
    /// Opens over native WinUSB first, falling back to libusb if that fails -
    /// see IGsUsbTransport for why both exist. Which one actually opened is
    /// reported in <see cref="Description"/> so a capture record shows it.
    /// </summary>
    internal sealed class GsUsbChannel : ICanChannel
    {
        private const int BulkReadTimeoutMs = 50;
        private const int BulkWriteTimeoutMs = 1000;

        private readonly ushort vendorId;
        private readonly ushort productId;
        private readonly string serial;

        private IGsUsbTransport transport;
        private uint filterFromId;
        private uint filterToId;
        private bool started;

        internal GsUsbChannel(ushort vendorId, ushort productId, string serial)
        {
            this.vendorId = vendorId;
            this.productId = productId;
            this.serial = serial;
        }

        public string Description
        {
            get
            {
                string id = String.IsNullOrEmpty(serial)
                    ? vendorId.ToString("X4", CultureInfo.InvariantCulture) + ":" +
                      productId.ToString("X4", CultureInfo.InvariantCulture)
                    : serial;
                return "gs_usb " + id +
                    (transport == null ? "" : " (" + transport.DriverDescription + ")");
            }
        }

        public string BackendName
        {
            get { return "gs_usb"; }
        }

        public string Kind
        {
            get { return "gsusb"; }
        }

        public void RefuseConcurrentClients()
        {
            // Both transports fail Open() outright when another process
            // already holds the device (WinUsb_Initialize / libusb_open both
            // surface this as an access error), so there is nothing useful to
            // pre-check before attempting to open.
        }

        public void Open(int bitrate, uint filterFromId, uint filterToId)
        {
            this.filterFromId = filterFromId;
            this.filterToId = filterToId;

            transport = OpenWithFailover();
            Console.Error.WriteLine("gs_usb device opened via " + transport.DriverDescription + ".");

            // Handshake: declare host byte order. candleLight ignores the
            // value and always exchanges little-endian, but real gs_usb
            // hardware and the Linux driver both send this first.
            GsHostConfig hostConfig = new GsHostConfig();
            hostConfig.ByteOrder = GsHostConfig.MagicNumber;
            transport.ControlTransferOut(GsUsbRequest.HostFormat, StructToBytes(hostConfig));

            byte[] constBytes = transport.ControlTransferIn(GsUsbRequest.BtConst, GsDeviceBtConst.WireSize);
            GsDeviceBtConst constants = BytesToBtConst(constBytes);
            GsDeviceBittiming timing = GsBitTiming.Compute(constants, bitrate);
            transport.ControlTransferOut(GsUsbRequest.BitTiming, StructToBytes(timing));

            GsDeviceMode mode = new GsDeviceMode();
            mode.Mode = (uint)GsCanMode.Start;
            mode.Feature = (uint)GsCanFeature.None;
            transport.ControlTransferOut(GsUsbRequest.Mode, StructToBytes(mode));
            started = true;

            DrainReceiveQueue();
        }

        public void Write(uint canId, byte[] data)
        {
            if (data == null || data.Length != 8)
            {
                throw new InvalidOperationException("A CAN frame must contain exactly eight bytes.");
            }
            GsHostFrame frame = new GsHostFrame();
            frame.EchoId = GsHostFrame.EchoIdTransmitPlaceholder;
            frame.CanId = canId & GsCanId.StandardIdMask;
            frame.CanDlc = 8;
            frame.Channel = 0;
            frame.Flags = 0;
            frame.Reserved = 0;
            frame.Data = (byte[])data.Clone();
            transport.BulkWrite(frame.ToBytes(), BulkWriteTimeoutMs);
        }

        public bool TryRead(out uint canId, out byte[] data)
        {
            byte[] buffer = new byte[GsHostFrame.WireSize];
            while (true)
            {
                int bytesRead;
                if (!transport.TryBulkRead(buffer, out bytesRead, BulkReadTimeoutMs))
                {
                    canId = 0;
                    data = null;
                    return false;
                }
                GsHostFrame frame = GsHostFrame.FromBytes(Hex.Slice(buffer, 0, bytesRead));
                if (frame.EchoId != GsHostFrame.EchoIdReceived)
                {
                    // The device's own echo of a frame this backend
                    // transmitted, not new data from the bus. Keep polling
                    // within this same call rather than making the caller's
                    // loop absorb an extra no-op iteration per TX.
                    continue;
                }
                uint id = frame.CanId & GsCanId.StandardIdMask;
                if ((frame.CanId & (GsCanId.ExtendedFlag | GsCanId.RemoteFlag | GsCanId.ErrorFlag)) != 0)
                {
                    // Extended, remote, and error frames are outside this
                    // channel's scope (see ICanChannel: "one standard 11-bit
                    // frame"); drop them rather than misreport their id.
                    continue;
                }
                if (id < filterFromId || id > filterToId)
                {
                    // No hardware filter is applied for gs_usb (bxcan filter
                    // banks are a device-specific extension not every gs_usb
                    // firmware implements); software filtering here keeps the
                    // contract - "narrowest available" - honest about what is
                    // actually available on this transport.
                    continue;
                }
                canId = id;
                data = frame.Data;
                return true;
            }
        }

        public void CheckStatus(string context)
        {
            // No cheap state query exists over this protocol without a
            // dedicated request/response round trip on every call; a stalled
            // or bus-off controller instead surfaces as TryRead never
            // returning data, which the caller already treats as a fault
            // after its own timeout.
        }

        private IGsUsbTransport OpenWithFailover()
        {
            WinUsbTransport winUsb = new WinUsbTransport();
            try
            {
                winUsb.Open(vendorId, productId, serial);
                return winUsb;
            }
            catch (Exception winUsbError)
            {
                winUsb.Dispose();
                Console.Error.WriteLine(
                    "WARNING: WinUSB could not open the gs_usb device (" + winUsbError.Message +
                    "); trying libusb instead.");
            }

            LibUsbTransport libUsb = new LibUsbTransport();
            try
            {
                libUsb.Open(vendorId, productId, serial);
                return libUsb;
            }
            catch (Exception libUsbError)
            {
                libUsb.Dispose();
                throw new InvalidOperationException(
                    "Could not open the gs_usb device through WinUSB or libusb. libusb reported: " +
                    libUsbError.Message +
                    " If the device is bound to WinUSB, try rebinding it to libusbK with Zadig.",
                    libUsbError);
            }
        }

        private void DrainReceiveQueue()
        {
            // A timeout of 0 means "wait forever" to both transports (it is
            // libusb's convention, which WinUsbTransport's pipe policy also
            // follows for consistency) - the opposite of what draining needs
            // on an idle bus, where nothing will ever arrive to stop the
            // wait. BulkReadTimeoutMs bounds each poll instead.
            byte[] buffer = new byte[GsHostFrame.WireSize];
            int bytesRead;
            while (transport.TryBulkRead(buffer, out bytesRead, BulkReadTimeoutMs))
            {
                // Discard; a previous session's frames must not surface as
                // belonging to this one.
            }
        }

        private static byte[] StructToBytes<T>(T value) where T : struct
        {
            int size = System.Runtime.InteropServices.Marshal.SizeOf(typeof(T));
            byte[] buffer = new byte[size];
            IntPtr pointer = System.Runtime.InteropServices.Marshal.AllocHGlobal(size);
            try
            {
                System.Runtime.InteropServices.Marshal.StructureToPtr(value, pointer, false);
                System.Runtime.InteropServices.Marshal.Copy(pointer, buffer, 0, size);
                return buffer;
            }
            finally
            {
                System.Runtime.InteropServices.Marshal.FreeHGlobal(pointer);
            }
        }

        private static GsDeviceBtConst BytesToBtConst(byte[] buffer)
        {
            GsDeviceBtConst result = new GsDeviceBtConst();
            result.Feature = BitConverter.ToUInt32(buffer, 0);
            result.FclkCan = BitConverter.ToUInt32(buffer, 4);
            CanBittimingConst btc = new CanBittimingConst();
            btc.Tseg1Min = BitConverter.ToUInt32(buffer, 8);
            btc.Tseg1Max = BitConverter.ToUInt32(buffer, 12);
            btc.Tseg2Min = BitConverter.ToUInt32(buffer, 16);
            btc.Tseg2Max = BitConverter.ToUInt32(buffer, 20);
            btc.SjwMax = BitConverter.ToUInt32(buffer, 24);
            btc.BrpMin = BitConverter.ToUInt32(buffer, 28);
            btc.BrpMax = BitConverter.ToUInt32(buffer, 32);
            btc.BrpInc = BitConverter.ToUInt32(buffer, 36);
            result.Btc = btc;
            return result;
        }

        public void Dispose()
        {
            if (transport == null)
            {
                return;
            }
            if (started)
            {
                try
                {
                    GsDeviceMode mode = new GsDeviceMode();
                    mode.Mode = (uint)GsCanMode.Reset;
                    mode.Feature = (uint)GsCanFeature.None;
                    transport.ControlTransferOut(GsUsbRequest.Mode, StructToBytes(mode));
                }
                catch (Exception exc)
                {
                    Console.Error.WriteLine("WARNING: gs_usb reset-mode request failed: " + exc.Message);
                }
            }
            transport.Dispose();
            transport = null;
            Console.Error.WriteLine("gs_usb channel closed.");
        }
    }
}
