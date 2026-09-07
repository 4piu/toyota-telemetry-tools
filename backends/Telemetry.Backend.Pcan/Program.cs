using System;
using Telemetry.Can.Core;

namespace Telemetry.Backend.Pcan
{
    internal static class Program
    {
        private const string ToolName = "telemetry-pcan";

        private static int Main(string[] args)
        {
            return BackendEntry.Run(OpenChannel, args, ToolName);
        }

        private static ICanChannel OpenChannel(BackendConfig config)
        {
            if (!Environment.Is64BitProcess)
            {
                throw new PlatformNotSupportedException(
                    "Build this backend for x64 so it loads the 64-bit PCANBasic.dll.");
            }
            string channel = config.DeviceString("pcan", "channel", "PCAN_USBBUS1");
            return new PcanChannel(channel);
        }
    }
}
