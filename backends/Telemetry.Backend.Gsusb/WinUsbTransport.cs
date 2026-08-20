// Native WinUSB access: SetupAPI to locate the device by the interface GUID
// candleLight_fw's Microsoft OS descriptor declares, then winusb.dll directly.
// This is the path candleLight_fw is actually designed for — no driver
// install, no bundled DLL, just what Windows already ships — so it is tried
// first. See LibUsbTransport for the fallback and why one is needed at all.
using System;
using System.Globalization;
using System.Runtime.InteropServices;
using System.Text;

namespace Telemetry.Backend.Gsusb
{
    internal sealed class WinUsbTransport : IGsUsbTransport
    {
        // candleLight_fw registers every board under this fixed
        // DeviceInterfaceGUIDs value in its WCID Extended Properties
        // descriptor (src/usbd_gs_can.c) — the same GUID regardless of board
        // target, so it can be hardcoded rather than discovered per device.
        private static readonly Guid DeviceInterfaceGuid =
            new Guid("c15b4308-04d3-11e6-b3ea-6057189e6443");

        private const uint GenericRead = 0x80000000;
        private const uint GenericWrite = 0x40000000;
        private const uint FileShareRead = 0x00000001;
        private const uint FileShareWrite = 0x00000002;
        private const uint OpenExisting = 3;
        private const uint FileFlagOverlapped = 0x40000000;
        private const uint FileAttributeNormal = 0x80;

        private const byte PipePolicyPipeTransferTimeout = 0x03;
        private const int SemTimeoutErrorCode = 121; // ERROR_SEM_TIMEOUT

        // USB control transfer direction/type/recipient bytes for gs_usb's
        // vendor-class, interface-recipient requests.
        private const byte RequestTypeDeviceToHost = 0xC1;
        private const byte RequestTypeHostToDevice = 0x41;

        private IntPtr fileHandle = IntPtr.Zero;
        private IntPtr winUsbHandle = IntPtr.Zero;
        private bool disposed;

        public string DriverDescription
        {
            get { return "WinUSB"; }
        }

        public void Open(ushort vendorId, ushort productId, string serial)
        {
            string devicePath = FindDevicePath(vendorId, productId, serial);
            if (devicePath == null)
            {
                throw new InvalidOperationException(
                    "No WinUSB-bound gs_usb device found under GUID " +
                    DeviceInterfaceGuid.ToString("B") +
                    ". Either nothing is attached, or this unit's WinUSB binding " +
                    "was replaced (e.g. with libusbK via Zadig), in which case the " +
                    "libusb transport is the one that will find it.");
            }

            fileHandle = NativeMethods.CreateFile(
                devicePath, GenericRead | GenericWrite, FileShareRead | FileShareWrite,
                IntPtr.Zero, OpenExisting, FileFlagOverlapped, IntPtr.Zero);
            if (fileHandle == IntPtr.Zero || fileHandle.ToInt64() == -1)
            {
                int error = Marshal.GetLastWin32Error();
                throw new InvalidOperationException(
                    "CreateFile on " + devicePath + " failed with Win32 error " + error + ".");
            }

            if (!NativeMethods.WinUsb_Initialize(fileHandle, out winUsbHandle))
            {
                int error = Marshal.GetLastWin32Error();
                NativeMethods.CloseHandle(fileHandle);
                fileHandle = IntPtr.Zero;
                throw new InvalidOperationException(
                    "WinUsb_Initialize failed with Win32 error " + error +
                    ". A common cause is the device being bound to a different " +
                    "driver (e.g. libusbK) than WinUSB.");
            }

            // Read timeout in milliseconds; without this a stalled bus makes
            // TryBulkRead block forever instead of returning "no data yet".
            uint timeout = 50;
            GCHandle pinned = GCHandle.Alloc(timeout, GCHandleType.Pinned);
            try
            {
                NativeMethods.WinUsb_SetPipePolicy(
                    winUsbHandle, GsUsbEndpoint.In, PipePolicyPipeTransferTimeout,
                    sizeof(uint), pinned.AddrOfPinnedObject());
            }
            finally
            {
                pinned.Free();
            }
        }

        public byte[] ControlTransferIn(GsUsbRequest request, int length)
        {
            NativeMethods.WinUsbSetupPacket setup = new NativeMethods.WinUsbSetupPacket();
            setup.RequestType = RequestTypeDeviceToHost;
            setup.Request = (byte)request;
            setup.Value = 0;
            setup.Index = 0;
            setup.Length = (ushort)length;

            byte[] buffer = new byte[length];
            uint transferred;
            if (!NativeMethods.WinUsb_ControlTransfer(
                    winUsbHandle, setup, buffer, (uint)length, out transferred, IntPtr.Zero))
            {
                throw ControlTransferError(request, "IN", Marshal.GetLastWin32Error());
            }
            if (transferred != length)
            {
                throw new InvalidOperationException(
                    "gs_usb control IN for " + request + " returned " + transferred +
                    " of " + length + " expected bytes.");
            }
            return buffer;
        }

        public void ControlTransferOut(GsUsbRequest request, byte[] payload)
        {
            NativeMethods.WinUsbSetupPacket setup = new NativeMethods.WinUsbSetupPacket();
            setup.RequestType = RequestTypeHostToDevice;
            setup.Request = (byte)request;
            setup.Value = 0;
            setup.Index = 0;
            setup.Length = (ushort)payload.Length;

            uint transferred;
            if (!NativeMethods.WinUsb_ControlTransfer(
                    winUsbHandle, setup, payload, (uint)payload.Length, out transferred, IntPtr.Zero))
            {
                throw ControlTransferError(request, "OUT", Marshal.GetLastWin32Error());
            }
        }

        public void BulkWrite(byte[] data, int timeoutMs)
        {
            uint written;
            if (!NativeMethods.WinUsb_WritePipe(
                    winUsbHandle, GsUsbEndpoint.Out, data, (uint)data.Length, out written, IntPtr.Zero))
            {
                throw new InvalidOperationException(
                    "WinUsb_WritePipe failed with Win32 error " + Marshal.GetLastWin32Error() + ".");
            }
            if (written != data.Length)
            {
                throw new InvalidOperationException(
                    "WinUsb_WritePipe wrote " + written + " of " + data.Length + " bytes.");
            }
        }

        public bool TryBulkRead(byte[] buffer, out int bytesRead, int timeoutMs)
        {
            uint read;
            if (NativeMethods.WinUsb_ReadPipe(
                    winUsbHandle, GsUsbEndpoint.In, buffer, (uint)buffer.Length, out read, IntPtr.Zero))
            {
                bytesRead = (int)read;
                return bytesRead > 0;
            }
            int error = Marshal.GetLastWin32Error();
            bytesRead = 0;
            if (error == SemTimeoutErrorCode)
            {
                return false;
            }
            throw new InvalidOperationException(
                "WinUsb_ReadPipe failed with Win32 error " + error + ".");
        }

        private static InvalidOperationException ControlTransferError(
            GsUsbRequest request, string direction, int win32Error)
        {
            return new InvalidOperationException(
                "gs_usb control " + direction + " for " + request +
                " failed with Win32 error " + win32Error + ".");
        }

        /// <summary>Enumerate every WinUSB device interface under
        /// <see cref="DeviceInterfaceGuid"/> and return the path of the one
        /// matching VID/PID (and serial, if given). The interface path
        /// contains "vid_XXXX&amp;pid_XXXX" for a plain USB device, which is
        /// enough to filter without a second, registry-property round trip.</summary>
        private static string FindDevicePath(ushort vendorId, ushort productId, string serial)
        {
            const uint DigcfPresent = 0x00000002;
            const uint DigcfDeviceInterface = 0x00000010;

            Guid guid = DeviceInterfaceGuid;
            IntPtr deviceInfoSet = NativeMethods.SetupDiGetClassDevs(
                ref guid, null, IntPtr.Zero, DigcfPresent | DigcfDeviceInterface);
            if (deviceInfoSet == IntPtr.Zero || deviceInfoSet.ToInt64() == -1)
            {
                return null;
            }
            try
            {
                string vidPid = String.Format(
                    CultureInfo.InvariantCulture, "vid_{0:x4}&pid_{1:x4}", vendorId, productId);

                NativeMethods.SpDeviceInterfaceData interfaceData =
                    new NativeMethods.SpDeviceInterfaceData();
                interfaceData.Size = Marshal.SizeOf(interfaceData);

                for (uint index = 0; ; index++)
                {
                    if (!NativeMethods.SetupDiEnumDeviceInterfaces(
                            deviceInfoSet, IntPtr.Zero, ref guid, index, ref interfaceData))
                    {
                        return null; // ERROR_NO_MORE_ITEMS: exhausted the list
                    }

                    int requiredSize = 0;
                    NativeMethods.SetupDiGetDeviceInterfaceDetail(
                        deviceInfoSet, ref interfaceData, IntPtr.Zero, 0, out requiredSize, IntPtr.Zero);
                    if (requiredSize <= 0)
                    {
                        continue;
                    }

                    IntPtr detailBuffer = Marshal.AllocHGlobal(requiredSize);
                    try
                    {
                        // cbSize of SP_DEVICE_INTERFACE_DETAIL_DATA is fixed at
                        // 6 (4-byte size field + 2-byte pointer alignment pad
                        // on the common x86/x64 layouts this struct uses),
                        // not the variable total size.
                        Marshal.WriteInt32(detailBuffer, IntPtr.Size == 8 ? 8 : 6);
                        if (!NativeMethods.SetupDiGetDeviceInterfaceDetail(
                                deviceInfoSet, ref interfaceData, detailBuffer, requiredSize,
                                out requiredSize, IntPtr.Zero))
                        {
                            continue;
                        }
                        string path = Marshal.PtrToStringAuto(detailBuffer + 4);
                        if (path == null)
                        {
                            continue;
                        }
                        string lowerPath = path.ToLowerInvariant();
                        if (lowerPath.IndexOf(vidPid, StringComparison.Ordinal) < 0)
                        {
                            continue;
                        }
                        if (!String.IsNullOrEmpty(serial) &&
                            lowerPath.IndexOf(serial.ToLowerInvariant(), StringComparison.Ordinal) < 0)
                        {
                            continue;
                        }
                        return path;
                    }
                    finally
                    {
                        Marshal.FreeHGlobal(detailBuffer);
                    }
                }
            }
            finally
            {
                NativeMethods.SetupDiDestroyDeviceInfoList(deviceInfoSet);
            }
        }

        public void Dispose()
        {
            if (disposed)
            {
                return;
            }
            disposed = true;
            if (winUsbHandle != IntPtr.Zero)
            {
                NativeMethods.WinUsb_Free(winUsbHandle);
                winUsbHandle = IntPtr.Zero;
            }
            if (fileHandle != IntPtr.Zero)
            {
                NativeMethods.CloseHandle(fileHandle);
                fileHandle = IntPtr.Zero;
            }
        }

        /// <summary>P/Invoke surface. Kept private to this file: nothing
        /// outside WinUsbTransport should depend on raw handles.</summary>
        private static class NativeMethods
        {
            [StructLayout(LayoutKind.Sequential, Pack = 1)]
            internal struct WinUsbSetupPacket
            {
                internal byte RequestType;
                internal byte Request;
                internal ushort Value;
                internal ushort Index;
                internal ushort Length;
            }

            [StructLayout(LayoutKind.Sequential)]
            internal struct SpDeviceInterfaceData
            {
                internal int Size;
                internal Guid InterfaceClassGuid;
                internal int Flags;
                internal IntPtr Reserved;
            }

            [DllImport("setupapi.dll", CharSet = CharSet.Auto, SetLastError = true)]
            internal static extern IntPtr SetupDiGetClassDevs(
                ref Guid classGuid, string enumerator, IntPtr parent, uint flags);

            [DllImport("setupapi.dll", SetLastError = true)]
            [return: MarshalAs(UnmanagedType.Bool)]
            internal static extern bool SetupDiEnumDeviceInterfaces(
                IntPtr deviceInfoSet, IntPtr deviceInfoData, ref Guid interfaceClassGuid,
                uint memberIndex, ref SpDeviceInterfaceData deviceInterfaceData);

            [DllImport("setupapi.dll", CharSet = CharSet.Auto, SetLastError = true)]
            [return: MarshalAs(UnmanagedType.Bool)]
            internal static extern bool SetupDiGetDeviceInterfaceDetail(
                IntPtr deviceInfoSet, ref SpDeviceInterfaceData deviceInterfaceData,
                IntPtr deviceInterfaceDetailData, int deviceInterfaceDetailDataSize,
                out int requiredSize, IntPtr deviceInfoData);

            [DllImport("setupapi.dll", SetLastError = true)]
            [return: MarshalAs(UnmanagedType.Bool)]
            internal static extern bool SetupDiDestroyDeviceInfoList(IntPtr deviceInfoSet);

            [DllImport("kernel32.dll", CharSet = CharSet.Auto, SetLastError = true)]
            internal static extern IntPtr CreateFile(
                string fileName, uint desiredAccess, uint shareMode, IntPtr securityAttributes,
                uint creationDisposition, uint flagsAndAttributes, IntPtr templateFile);

            [DllImport("kernel32.dll", SetLastError = true)]
            [return: MarshalAs(UnmanagedType.Bool)]
            internal static extern bool CloseHandle(IntPtr handle);

            [DllImport("winusb.dll", SetLastError = true)]
            [return: MarshalAs(UnmanagedType.Bool)]
            internal static extern bool WinUsb_Initialize(
                IntPtr deviceHandle, out IntPtr interfaceHandle);

            [DllImport("winusb.dll", SetLastError = true)]
            [return: MarshalAs(UnmanagedType.Bool)]
            internal static extern bool WinUsb_Free(IntPtr interfaceHandle);

            [DllImport("winusb.dll", SetLastError = true)]
            [return: MarshalAs(UnmanagedType.Bool)]
            internal static extern bool WinUsb_SetPipePolicy(
                IntPtr interfaceHandle, byte pipeId, byte policyType,
                uint valueLength, IntPtr value);

            [DllImport("winusb.dll", SetLastError = true)]
            [return: MarshalAs(UnmanagedType.Bool)]
            internal static extern bool WinUsb_ControlTransfer(
                IntPtr interfaceHandle, WinUsbSetupPacket setupPacket,
                byte[] buffer, uint bufferLength, out uint lengthTransferred, IntPtr overlapped);

            [DllImport("winusb.dll", SetLastError = true)]
            [return: MarshalAs(UnmanagedType.Bool)]
            internal static extern bool WinUsb_ReadPipe(
                IntPtr interfaceHandle, byte pipeId, byte[] buffer, uint bufferLength,
                out uint lengthTransferred, IntPtr overlapped);

            [DllImport("winusb.dll", SetLastError = true)]
            [return: MarshalAs(UnmanagedType.Bool)]
            internal static extern bool WinUsb_WritePipe(
                IntPtr interfaceHandle, byte pipeId, byte[] buffer, uint bufferLength,
                out uint lengthTransferred, IntPtr overlapped);
        }
    }
}
