using System;

namespace Telemetry.Backend.Gsusb
{
    /// <summary>
    /// The USB access an <c>ICanChannel</c> needs, independent of which
    /// driver actually carries it. Two implementations exist because no one
    /// Windows driver reliably opens every gs_usb device: native WinUSB is
    /// what candleLight_fw's WCID descriptors are designed for and needs no
    /// installed driver at all, but some units' automatic WinUSB binding
    /// refuses <c>libusb_open</c>-style access with no diagnosable cause; on
    /// those, rebinding the device to libusbK through Zadig and going through
    /// libusb instead has been the reliable fallback.
    /// </summary>
    internal interface IGsUsbTransport : IDisposable
    {
        /// <summary>What actually opened it, for the capture record and error
        /// messages — e.g. "WinUSB" or "libusb (libusbK)".</summary>
        string DriverDescription { get; }

        /// <summary>Locate and open the device by VID/PID, and by serial when
        /// more than one gs_usb device is attached. Throws with a message
        /// naming this transport specifically; the caller decides whether
        /// that is fatal or a reason to try the other transport.</summary>
        void Open(ushort vendorId, ushort productId, string serial);

        /// <summary>Device-to-host control transfer (bRequest, wValue=0,
        /// wIndex=0), returning exactly <paramref name="length"/> bytes.</summary>
        byte[] ControlTransferIn(GsUsbRequest request, int length);

        /// <summary>Host-to-device control transfer (bRequest, wValue=0,
        /// wIndex=0) carrying <paramref name="payload"/>.</summary>
        void ControlTransferOut(GsUsbRequest request, byte[] payload);

        /// <summary>Blocking bulk write to the OUT endpoint.</summary>
        void BulkWrite(byte[] data, int timeoutMs);

        /// <summary>Bulk read from the IN endpoint. Returns false on a timeout
        /// (queue empty), which is not an error for a non-blocking poll.</summary>
        bool TryBulkRead(byte[] buffer, out int bytesRead, int timeoutMs);
    }
}
