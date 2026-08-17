using System;
using System.Collections.Generic;
using System.Globalization;

namespace Telemetry.Can.Core
{
    /// <summary>
    /// The command-line contract the generic <c>telemetry-validate</c> runner
    /// invokes. Documented in the field guide under
    /// <c>docs/08-adapters/adding-an-adapter.md</c>; changing an option name
    /// here breaks every platform project's validation profile.
    ///
    /// Unknown arguments are rejected rather than ignored: a mistyped flag must
    /// not silently downgrade into a different, live transmission.
    /// </summary>
    internal sealed class Options
    {
        internal string ToolName = "telemetry-backend";

        internal string ConfigPath;
        internal int EcuKey = -1;
        internal bool Live;
        internal bool ShowHelp;
        internal bool SelfTest;
        internal bool Probe;
        internal bool ObdProbe;
        internal bool CandidateQualify;
        internal bool CandidateBatch;
        internal bool Verbose;
        internal bool Quiet;
        internal int TimeoutMs = 1500;
        internal int PeriodMs = 100;
        internal int DurationSeconds = 900;
        internal int MaxLogMiB = 64;
        internal int TransportRetries = 2;
        internal bool TransportRetriesSpecified;
        internal bool DurationSpecified;
        internal string OutputPath;
        internal string BatchId = "adhoc";
        internal readonly List<int> CandidateDids = new List<int>();

        internal bool IsCandidateMode
        {
            get { return CandidateQualify || CandidateBatch; }
        }

        internal string ModeName
        {
            get
            {
                if (CandidateQualify) { return "candidate qualification"; }
                if (CandidateBatch) { return "candidate batch"; }
                if (ObdProbe) { return "standardized OBD probe"; }
                if (Probe) { return "endpoint tester-present probe"; }
                if (SelfTest) { return "offline self-test"; }
                return "none";
            }
        }

        internal static Options Parse(string[] args, string toolName)
        {
            Options options = new Options();
            options.ToolName = toolName;
            for (int i = 0; i < args.Length; i++)
            {
                string argument = args[i];
                switch (argument)
                {
                    case "--live": options.Live = true; break;
                    case "--dry-run": options.Live = false; break;
                    case "--self-test": options.SelfTest = true; break;
                    case "--probe": options.Probe = true; break;
                    case "--obd-probe": options.ObdProbe = true; break;
                    case "--candidate-qualify": options.CandidateQualify = true; break;
                    case "--candidate-batch": options.CandidateBatch = true; break;
                    case "--verbose": options.Verbose = true; break;
                    case "--quiet": options.Quiet = true; break;
                    case "--help":
                    case "-h":
                    case "/?":
                        options.ShowHelp = true;
                        break;
                    case "--config":
                        options.ConfigPath = NextValue(args, ref i, argument);
                        break;
                    case "--ecu":
                        options.EcuKey = ParseInt(
                            NextValue(args, ref i, argument), argument, 0, 65535);
                        break;
                    case "--batch-id":
                        options.BatchId = NextValue(args, ref i, argument);
                        break;
                    case "--output":
                        options.OutputPath = NextValue(args, ref i, argument);
                        break;
                    case "--did":
                        options.CandidateDids.Add(options.ParseDid(NextValue(args, ref i, argument)));
                        break;
                    case "--timeout-ms":
                        options.TimeoutMs = ParseInt(NextValue(args, ref i, argument), argument, 100, 10000);
                        break;
                    case "--period-ms":
                        options.PeriodMs = ParseInt(NextValue(args, ref i, argument), argument, 70, 5000);
                        break;
                    case "--duration-seconds":
                        options.DurationSeconds = ParseInt(NextValue(args, ref i, argument), argument, 0, 7200);
                        options.DurationSpecified = true;
                        break;
                    case "--max-log-mib":
                        options.MaxLogMiB = ParseInt(NextValue(args, ref i, argument), argument, 1, 1024);
                        break;
                    case "--transport-retries":
                        options.TransportRetries = ParseInt(NextValue(args, ref i, argument), argument, 0, 5);
                        options.TransportRetriesSpecified = true;
                        break;
                    default:
                        throw new ArgumentException("Unknown argument: " + argument);
                }
            }

            if (options.ShowHelp)
            {
                return options;
            }

            int selectedModes =
                (options.Probe ? 1 : 0) +
                (options.ObdProbe ? 1 : 0) +
                (options.CandidateQualify ? 1 : 0) +
                (options.CandidateBatch ? 1 : 0) +
                (options.SelfTest ? 1 : 0);
            if (selectedModes > 1)
            {
                throw new ArgumentException(
                    "Probe, candidate, and self-test modes are mutually exclusive.");
            }
            if (selectedModes == 0)
            {
                throw new ArgumentException(
                    "Select exactly one of --self-test, --probe, --obd-probe, " +
                    "--candidate-qualify, or --candidate-batch.");
            }
            if (options.SelfTest && options.Live)
            {
                throw new ArgumentException("--self-test is offline and cannot be combined with --live.");
            }
            if (!options.SelfTest && String.IsNullOrWhiteSpace(options.ConfigPath))
            {
                throw new ArgumentException("--config PATH is required.");
            }
            if (options.IsCandidateMode && String.IsNullOrWhiteSpace(options.BatchId))
            {
                throw new ArgumentException("--batch-id cannot be empty.");
            }
            if (!options.IsCandidateMode && options.CandidateDids.Count != 0)
            {
                throw new ArgumentException("--did is supported only with a candidate mode.");
            }
            if (options.IsCandidateMode && options.Live &&
                String.IsNullOrWhiteSpace(options.OutputPath))
            {
                throw new ArgumentException("--output PATH is required with a live candidate mode.");
            }
            if (!options.IsCandidateMode && !options.SelfTest &&
                !String.IsNullOrWhiteSpace(options.OutputPath))
            {
                throw new ArgumentException(
                    "--output is supported only with candidate capture and --self-test.");
            }
            if (options.SelfTest && String.IsNullOrWhiteSpace(options.OutputPath))
            {
                throw new ArgumentException("--output PATH is required with --self-test.");
            }
            if (options.Verbose && options.Quiet)
            {
                throw new ArgumentException("--verbose and --quiet cannot be used together.");
            }
            if (options.Quiet && !options.CandidateBatch)
            {
                throw new ArgumentException("--quiet is supported only with --candidate-batch.");
            }
            if (options.TransportRetriesSpecified && !options.CandidateBatch)
            {
                throw new ArgumentException(
                    "--transport-retries is supported only with --candidate-batch.");
            }
            if (options.DurationSeconds == 0 && !options.CandidateBatch)
            {
                throw new ArgumentException(
                    "--duration-seconds 0 is supported only with --candidate-batch.");
            }
            if (options.CandidateQualify && options.DurationSpecified)
            {
                throw new ArgumentException(
                    "--duration-seconds does not apply to --candidate-qualify; " +
                    "qualification reads each DID exactly once.");
            }
            return options;
        }

        /// <summary>Range and count checks that need the loaded configuration.
        /// Kept separate so --help and --self-test never require a config.</summary>
        internal void ValidateAgainst(BackendConfig config)
        {
            if (!IsCandidateMode)
            {
                return;
            }
            if (CandidateDids.Count == 0 || CandidateDids.Count > config.MaximumCandidateDids)
            {
                throw new ArgumentException(
                    "Candidate modes require 1 through " +
                    config.MaximumCandidateDids.ToString(CultureInfo.InvariantCulture) +
                    " unique --did values.");
            }
            foreach (int did in CandidateDids)
            {
                if (did < config.DidMinimum || did > config.DidMaximum)
                {
                    throw new ArgumentException(
                        "--did " + Hex.FormatDid(did) + " is outside the configured range 0x" +
                        config.DidMinimum.ToString("X4", CultureInfo.InvariantCulture) + "..0x" +
                        config.DidMaximum.ToString("X4", CultureInfo.InvariantCulture) + ".");
                }
            }
        }

        private int ParseDid(string value)
        {
            string text = value;
            if (text.StartsWith("0x", StringComparison.OrdinalIgnoreCase))
            {
                text = text.Substring(2);
            }
            int did;
            if (!Int32.TryParse(text, NumberStyles.AllowHexSpecifier, CultureInfo.InvariantCulture, out did) ||
                did < 0 || did > 0xFFFF)
            {
                throw new ArgumentException("--did must be a hexadecimal identifier 0x0000..0xFFFF.");
            }
            if (CandidateDids.Contains(did))
            {
                throw new ArgumentException("Duplicate --did " + Hex.FormatDid(did) + ".");
            }
            return did;
        }

        private static string NextValue(string[] args, ref int index, string option)
        {
            index++;
            if (index >= args.Length)
            {
                throw new ArgumentException("Missing value after " + option + ".");
            }
            return args[index];
        }

        private static int ParseInt(string text, string option, int minimum, int maximum)
        {
            int value;
            if (!Int32.TryParse(text, NumberStyles.Integer, CultureInfo.InvariantCulture, out value) ||
                value < minimum || value > maximum)
            {
                throw new ArgumentException(
                    option + " must be between " +
                    minimum.ToString(CultureInfo.InvariantCulture) + " and " +
                    maximum.ToString(CultureInfo.InvariantCulture) + ".");
            }
            return value;
        }

        internal static void PrintHelp(string toolName, string deviceHelp)
        {
            Console.WriteLine(toolName + " - read-only UDS candidate capture over Classical CAN");
            Console.WriteLine();
            Console.WriteLine("Usage:");
            Console.WriteLine("  " + toolName + " --self-test --output PATH");
            Console.WriteLine("  " + toolName + " --config PATH --probe [--live]");
            Console.WriteLine("  " + toolName + " --config PATH --obd-probe [--live]");
            Console.WriteLine("  " + toolName + " --config PATH --candidate-qualify --live \\");
            Console.WriteLine("      --batch-id ID --output PATH --did 0xNNNN [--did ...]");
            Console.WriteLine("  " + toolName + " --config PATH --candidate-batch --live \\");
            Console.WriteLine("      --batch-id ID --output PATH --duration-seconds N --did 0xNNNN [...]");
            Console.WriteLine();
            Console.WriteLine("Modes:");
            Console.WriteLine("  --self-test           Offline framing and writer checks; never opens the device.");
            Console.WriteLine("  --probe               Send one tester-present frame to the configured endpoint.");
            Console.WriteLine("  --obd-probe           Send one standardized Mode-01/PID-00 query at 0x7DF.");
            Console.WriteLine("  --candidate-qualify   Read each --did exactly once and record support or NRC.");
            Console.WriteLine("  --candidate-batch     Poll 1..N prequalified --did values round-robin.");
            Console.WriteLine();
            Console.WriteLine("Options:");
            Console.WriteLine("  --config PATH         Backend configuration JSON. Required except with --self-test.");
            Console.WriteLine("  --ecu KEY             Which ECU in the configuration to talk to. Required");
            Console.WriteLine("                        unless the configuration lists exactly one.");
            Console.WriteLine("  --live                Open the device and transmit. Omitted means dry run.");
            Console.WriteLine("  --did 0xNNNN          Candidate DID; repeat for several.");
            Console.WriteLine("  --batch-id ID         Label recorded in the capture (default: adhoc).");
            Console.WriteLine("  --output PATH         Create-new capture file. Required for live capture.");
            Console.WriteLine("  --period-ms N         Minimum request start interval, 70-5000 (default 100).");
            Console.WriteLine("  --duration-seconds N  Batch time limit, 0-7200; 0 means operator-stopped.");
            Console.WriteLine("  --transport-retries N Timeout retries per DID, 0-5 (default 2). Batch only.");
            Console.WriteLine("  --timeout-ms N        Per-exchange timeout, 100-10000 (default 1500).");
            Console.WriteLine("  --max-log-mib N       Capture size cap, 1-1024 (default 64).");
            Console.WriteLine("  --quiet               Suppress per-sample output. Batch only.");
            Console.WriteLine("  --verbose             Print every UDS payload and raw CAN frame.");
            Console.WriteLine("  --help                Show this help.");
            if (!String.IsNullOrEmpty(deviceHelp))
            {
                Console.WriteLine();
                Console.WriteLine("Device:");
                Console.WriteLine(deviceHelp);
            }
        }
    }
}
