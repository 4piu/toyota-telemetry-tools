using System;

namespace Telemetry.Can.Core
{
    /// <summary>
    /// Process entry shared by every adapter. Vehicle access is available only
    /// through the private worker protocol; the only public backend action is
    /// the offline self-check.
    /// </summary>
    internal static class BackendEntry
    {
        internal static int Run(
            Func<BackendConfig, ICanChannel> openChannel,
            string[] args,
            string toolName)
        {
            int workerExitCode;
            if (WorkerRunner.TryRun(openChannel, args, toolName, out workerExitCode))
            {
                return workerExitCode;
            }
            try
            {
                if (args.Length == 1 && String.Equals(args[0], "--self-test", StringComparison.Ordinal))
                {
                    SelfCheck.RunFramingChecks();
                    Console.WriteLine(
                        "Framing, byte-exact allowlist, and worker-protocol self-checks passed; " +
                        "no device was opened.");
                    return 0;
                }
                if (args.Length == 1 &&
                    (String.Equals(args[0], "--help", StringComparison.Ordinal) ||
                     String.Equals(args[0], "-h", StringComparison.Ordinal) ||
                     String.Equals(args[0], "/?", StringComparison.Ordinal)))
                {
                    PrintHelp(toolName);
                    return 0;
                }
                throw new ArgumentException(
                    "Use --self-test or the private --worker --config PATH protocol.");
            }
            catch (Exception exc)
            {
                Console.Error.WriteLine("ERROR: " + exc.Message);
                return 1;
            }
        }

        private static void PrintHelp(string toolName)
        {
            Console.WriteLine(toolName + " - private telemetry adapter worker");
            Console.WriteLine();
            Console.WriteLine("Usage:");
            Console.WriteLine("  " + toolName + " --self-test");
            Console.WriteLine("  " + toolName + " --worker --config PATH");
            Console.WriteLine();
            Console.WriteLine("The worker form is launched and controlled only by telemetry collect.");
        }
    }
}
