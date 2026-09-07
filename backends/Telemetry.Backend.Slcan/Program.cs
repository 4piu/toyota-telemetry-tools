using System;
using Telemetry.Can.Core;

namespace Telemetry.Backend.Slcan
{
    internal static class Program
    {
        private const string ToolName = "telemetry-slcan";

        private static int Main(string[] args)
        {
            return BackendEntry.Run(OpenChannel, args, ToolName);
        }

        private static ICanChannel OpenChannel(BackendConfig config)
        {
            string port = config.DeviceString("slcan", "port", null);
            if (String.IsNullOrWhiteSpace(port))
            {
                throw new ArgumentException(
                    "Set device.slcan.port in the backend configuration to the adapter's COM port, " +
                    "e.g. \"COM8\".");
            }
            return new SlcanChannel(port);
        }
    }
}
