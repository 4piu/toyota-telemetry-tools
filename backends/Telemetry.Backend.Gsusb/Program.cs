using System;
using System.Globalization;
using Telemetry.Can.Core;

namespace Telemetry.Backend.Gsusb
{
    internal static class Program
    {
        private const string ToolName = "telemetry-gsusb";

        // gs_usb's assigned USB VID:PID (registered to the Geschwister
        // Schneider / candleLight project); every board target this backend
        // has been tested against uses it, since it comes from the shared
        // candleLight_fw source rather than being board-specific.
        private const string DefaultVendorId = "1d50";
        private const string DefaultProductId = "606f";

        private static int Main(string[] args)
        {
            return CandidateRunner.Run(OpenChannel, args, ToolName);
        }

        private static ICanChannel OpenChannel(BackendConfig config)
        {
            ushort vendorId = ParseHexUInt16(
                config.DeviceString("gsusb", "vendor_id", DefaultVendorId), "device.gsusb.vendor_id");
            ushort productId = ParseHexUInt16(
                config.DeviceString("gsusb", "product_id", DefaultProductId), "device.gsusb.product_id");
            string serial = config.DeviceString("gsusb", "serial", "");
            return new GsUsbChannel(vendorId, productId, serial);
        }

        private static ushort ParseHexUInt16(string text, string context)
        {
            ushort value;
            if (!UInt16.TryParse(
                    text, NumberStyles.AllowHexSpecifier, CultureInfo.InvariantCulture, out value))
            {
                throw new FormatException(context + " must be a four-digit hex value; got: " + text);
            }
            return value;
        }
    }
}
