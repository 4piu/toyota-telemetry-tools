// libusb-1.0 fallback: works regardless of whether the device is bound to
// WinUSB or libusbK, since libusb's own Windows backend auto-detects which
// one is present. Used when WinUsbTransport cannot open the device outright -
// see IGsUsbTransport for why that happens on some units.
//
// Bundles libusb-1.0.dll (verified 1.0.30) next to the executable rather than
// assuming it is already on the system, unlike PCANBasic.dll which the
// vendor's own installer places in System32.
using System;
using System.IO;
using System.Runtime.InteropServices;

namespace Telemetry.Backend.Gsusb
{
    internal sealed class LibUsbTransport : IGsUsbTransport
    {
        private const int LibUsbSuccess = 0;
        private const int LibUsbErrorTimeout = -7;
        private const byte RequestTypeDeviceToHost = 0xC1;
        private const byte RequestTypeHostToDevice = 0x41;
        private const uint DefaultControlTimeoutMs = 1000;

        private IntPtr context = IntPtr.Zero;
        private IntPtr deviceHandle = IntPtr.Zero;
        private bool interfaceClaimed;
        private bool disposed;

        public string DriverDescription
        {
            get { return "libusb"; }
        }

        public void Open(ushort vendorId, ushort productId, string serial)
        {
            RequireBundledLibrary();

            int initStatus = NativeMethods.libusb_init(out context);
            if (initStatus != LibUsbSuccess)
            {
                throw new InvalidOperationException("libusb_init failed: " + initStatus + ".");
            }

            deviceHandle = FindAndOpen(vendorId, productId, serial);
            if (deviceHandle == IntPtr.Zero)
            {
                throw new InvalidOperationException(
                    String.IsNullOrEmpty(serial)
                        ? "No gs_usb device found by libusb for VID/PID " +
                          vendorId.ToString("X4") + ":" + productId.ToString("X4") + "."
                        : "No gs_usb device found by libusb matching VID/PID " +
                          vendorId.ToString("X4") + ":" + productId.ToString("X4") +
                          " and serial " + serial + ".");
            }

            NativeMethods.libusb_set_auto_detach_kernel_driver(deviceHandle, 1);
            int claimStatus = NativeMethods.libusb_claim_interface(deviceHandle, 0);
            if (claimStatus != LibUsbSuccess)
            {
                throw new InvalidOperationException(
                    "libusb_claim_interface failed: " + DescribeError(claimStatus) +
                    ". Another process may already have this device open.");
            }
            interfaceClaimed = true;
        }

        public byte[] ControlTransferIn(GsUsbRequest request, int length)
        {
            byte[] buffer = new byte[length];
            int transferred = NativeMethods.libusb_control_transfer(
                deviceHandle, RequestTypeDeviceToHost, (byte)request, 0, 0,
                buffer, (ushort)length, DefaultControlTimeoutMs);
            if (transferred < 0)
            {
                throw new InvalidOperationException(
                    "gs_usb control IN for " + request + " failed: " + DescribeError(transferred) + ".");
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
            int transferred = NativeMethods.libusb_control_transfer(
                deviceHandle, RequestTypeHostToDevice, (byte)request, 0, 0,
                payload, (ushort)payload.Length, DefaultControlTimeoutMs);
            if (transferred < 0)
            {
                throw new InvalidOperationException(
                    "gs_usb control OUT for " + request + " failed: " + DescribeError(transferred) + ".");
            }
        }

        public void BulkWrite(byte[] data, int timeoutMs)
        {
            int transferred;
            int status = NativeMethods.libusb_bulk_transfer(
                deviceHandle, GsUsbEndpoint.Out, data, data.Length, out transferred, (uint)timeoutMs);
            if (status != LibUsbSuccess)
            {
                throw new InvalidOperationException(
                    "libusb_bulk_transfer (write) failed: " + DescribeError(status) + ".");
            }
            if (transferred != data.Length)
            {
                throw new InvalidOperationException(
                    "libusb bulk write sent " + transferred + " of " + data.Length + " bytes.");
            }
        }

        public bool TryBulkRead(byte[] buffer, out int bytesRead, int timeoutMs)
        {
            int transferred;
            int status = NativeMethods.libusb_bulk_transfer(
                deviceHandle, GsUsbEndpoint.In, buffer, buffer.Length, out transferred, (uint)timeoutMs);
            if (status == LibUsbErrorTimeout)
            {
                bytesRead = 0;
                return false;
            }
            if (status != LibUsbSuccess)
            {
                bytesRead = 0;
                throw new InvalidOperationException(
                    "libusb_bulk_transfer (read) failed: " + DescribeError(status) + ".");
            }
            bytesRead = transferred;
            return bytesRead > 0;
        }

        private static IntPtr FindAndOpen(ushort vendorId, ushort productId, string serial)
        {
            IntPtr deviceListHandle;
            int count = NativeMethods.libusb_get_device_list(IntPtr.Zero, out deviceListHandle);
            if (count < 0)
            {
                throw new InvalidOperationException(
                    "libusb_get_device_list failed: " + DescribeError(count) + ".");
            }
            try
            {
                for (int i = 0; i < count; i++)
                {
                    IntPtr device = Marshal.ReadIntPtr(deviceListHandle, i * IntPtr.Size);
                    NativeMethods.LibUsbDeviceDescriptor descriptor;
                    if (NativeMethods.libusb_get_device_descriptor(device, out descriptor) != LibUsbSuccess)
                    {
                        continue;
                    }
                    if (descriptor.idVendor != vendorId || descriptor.idProduct != productId)
                    {
                        continue;
                    }

                    IntPtr candidateHandle;
                    if (NativeMethods.libusb_open(device, out candidateHandle) != LibUsbSuccess)
                    {
                        continue;
                    }

                    if (!String.IsNullOrEmpty(serial) &&
                        !MatchesSerial(candidateHandle, descriptor.iSerialNumber, serial))
                    {
                        NativeMethods.libusb_close(candidateHandle);
                        continue;
                    }
                    return candidateHandle;
                }
                return IntPtr.Zero;
            }
            finally
            {
                NativeMethods.libusb_free_device_list(deviceListHandle, true);
            }
        }

        private static bool MatchesSerial(IntPtr deviceHandle, byte serialIndex, string expected)
        {
            if (serialIndex == 0)
            {
                return false;
            }
            byte[] buffer = new byte[256];
            int written = NativeMethods.libusb_get_string_descriptor_ascii(
                deviceHandle, serialIndex, buffer, buffer.Length);
            if (written <= 0)
            {
                return false;
            }
            string actual = System.Text.Encoding.ASCII.GetString(buffer, 0, written);
            return String.Equals(actual, expected, StringComparison.OrdinalIgnoreCase);
        }

        private static void RequireBundledLibrary()
        {
            string directory = Path.GetDirectoryName(
                System.Reflection.Assembly.GetExecutingAssembly().Location);
            string dllPath = Path.Combine(directory ?? ".", "libusb-1.0.dll");
            if (!File.Exists(dllPath))
            {
                throw new FileNotFoundException(
                    "libusb-1.0.dll was not found next to the executable. It ships with this " +
                    "backend; do not run the .exe from outside its build output directory.",
                    dllPath);
            }
        }

        private static string DescribeError(int code)
        {
            IntPtr namePtr = NativeMethods.libusb_error_name(code);
            string name = namePtr == IntPtr.Zero ? null : Marshal.PtrToStringAnsi(namePtr);
            return (name ?? "LIBUSB_ERROR") + " (" + code + ")";
        }

        public void Dispose()
        {
            if (disposed)
            {
                return;
            }
            disposed = true;
            if (interfaceClaimed)
            {
                NativeMethods.libusb_release_interface(deviceHandle, 0);
                interfaceClaimed = false;
            }
            if (deviceHandle != IntPtr.Zero)
            {
                NativeMethods.libusb_close(deviceHandle);
                deviceHandle = IntPtr.Zero;
            }
            if (context != IntPtr.Zero)
            {
                NativeMethods.libusb_exit(context);
                context = IntPtr.Zero;
            }
        }

        /// <summary>P/Invoke surface. Kept private to this file for the same
        /// reason as WinUsbTransport's.</summary>
        private static class NativeMethods
        {
            [StructLayout(LayoutKind.Sequential, Pack = 1)]
            internal struct LibUsbDeviceDescriptor
            {
                internal byte bLength;
                internal byte bDescriptorType;
                internal ushort bcdUSB;
                internal byte bDeviceClass;
                internal byte bDeviceSubClass;
                internal byte bDeviceProtocol;
                internal byte bMaxPacketSize0;
                internal ushort idVendor;
                internal ushort idProduct;
                internal ushort bcdDevice;
                internal byte iManufacturer;
                internal byte iProduct;
                internal byte iSerialNumber;
                internal byte bNumConfigurations;
            }

            [DllImport("libusb-1.0.dll")]
            internal static extern int libusb_init(out IntPtr context);

            [DllImport("libusb-1.0.dll")]
            internal static extern void libusb_exit(IntPtr context);

            [DllImport("libusb-1.0.dll")]
            internal static extern int libusb_get_device_list(IntPtr context, out IntPtr list);

            [DllImport("libusb-1.0.dll")]
            internal static extern void libusb_free_device_list(
                IntPtr list, [MarshalAs(UnmanagedType.Bool)] bool unrefDevices);

            [DllImport("libusb-1.0.dll")]
            internal static extern int libusb_get_device_descriptor(
                IntPtr device, out LibUsbDeviceDescriptor descriptor);

            [DllImport("libusb-1.0.dll")]
            internal static extern int libusb_open(IntPtr device, out IntPtr deviceHandle);

            [DllImport("libusb-1.0.dll")]
            internal static extern void libusb_close(IntPtr deviceHandle);

            [DllImport("libusb-1.0.dll")]
            internal static extern int libusb_claim_interface(IntPtr deviceHandle, int interfaceNumber);

            [DllImport("libusb-1.0.dll")]
            internal static extern int libusb_release_interface(IntPtr deviceHandle, int interfaceNumber);

            [DllImport("libusb-1.0.dll")]
            internal static extern int libusb_set_auto_detach_kernel_driver(
                IntPtr deviceHandle, int enable);

            [DllImport("libusb-1.0.dll")]
            internal static extern int libusb_control_transfer(
                IntPtr deviceHandle, byte requestType, byte request, ushort value, ushort index,
                byte[] data, ushort length, uint timeout);

            [DllImport("libusb-1.0.dll")]
            internal static extern int libusb_bulk_transfer(
                IntPtr deviceHandle, byte endpoint, byte[] data, int length,
                out int transferred, uint timeout);

            [DllImport("libusb-1.0.dll")]
            internal static extern int libusb_get_string_descriptor_ascii(
                IntPtr deviceHandle, byte descriptorIndex, byte[] data, int length);

            [DllImport("libusb-1.0.dll")]
            internal static extern IntPtr libusb_error_name(int errorCode);
        }
    }
}
