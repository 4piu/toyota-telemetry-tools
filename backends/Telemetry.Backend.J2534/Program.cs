using System;
using Telemetry.Can.Core;

namespace Telemetry.Backend.J2534
{
    internal static class Program
    {
        private const string ToolName = "telemetry-j2534";

        private static int Main(string[] args)
        {
            return CandidateRunner.Run(OpenChannel, args, ToolName);
        }

        private static ICanChannel OpenChannel(BackendConfig config)
        {
            string dll = config.DeviceString("j2534", "dll", null);
            if (String.IsNullOrWhiteSpace(dll))
            {
                throw new ArgumentException(
                    "Set device.j2534.dll in the backend configuration to the vendor PassThru DLL path.");
            }
            return new J2534Channel(dll);
        }
    }
}
