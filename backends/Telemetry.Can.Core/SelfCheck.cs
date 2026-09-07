using System;
using System.Collections.Generic;
using System.Globalization;

namespace Telemetry.Can.Core
{
    /// <summary>
    /// Offline assertions that run before the device is ever opened.
    ///
    /// These exist because ISO-TP framing errors are silent: a wrong
    /// consecutive-frame sequence or a mis-sized first frame produces a
    /// plausible-looking transaction that means something else on the bus.
    /// Byte-exact expectations catch a refactor that changes framing before
    /// anything is transmitted at a vehicle.
    /// </summary>
    internal static class SelfCheck
    {
        // A synthetic 20-byte payload chosen to exercise first-frame plus two
        // consecutive frames, including the 6/7/7 byte split.
        private static readonly byte[] MultiFramePayload =
        {
            0x2C, 0x01, 0xF3, 0x01, 0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0xFF,
            0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88, 0x99, 0x00
        };

        internal static void RunFramingChecks()
        {
            // Single frame: length nibble then payload, zero padded to eight.
            byte[] singleRequest = IsoTpTransport.ReadDataByIdentifier(0x1075);
            AssertFrames(
                singleRequest,
                new byte[][]
                {
                    new byte[] { 0x03, 0x22, 0x10, 0x75, 0x00, 0x00, 0x00, 0x00 }
                },
                "single-frame ReadDataByIdentifier");

            // First frame carries six bytes; each consecutive frame carries seven.
            AssertFrames(
                MultiFramePayload,
                new byte[][]
                {
                    new byte[] { 0x10, 0x14, 0x2C, 0x01, 0xF3, 0x01, 0xAA, 0xBB },
                    new byte[] { 0x21, 0xCC, 0xDD, 0xEE, 0xFF, 0x11, 0x22, 0x33 },
                    new byte[] { 0x22, 0x44, 0x55, 0x66, 0x77, 0x88, 0x99, 0x00 }
                },
                "multi-frame segmentation");

            // A seven-byte payload is the largest single frame.
            byte[] sevenBytes = { 0x2E, 0x11, 0x22, 0x33, 0x44, 0x55, 0x66 };
            AssertFrames(
                sevenBytes,
                new byte[][]
                {
                    new byte[] { 0x07, 0x2E, 0x11, 0x22, 0x33, 0x44, 0x55, 0x66 }
                },
                "maximum single-frame payload");

            // Eight bytes must segment rather than overflow the single frame.
            byte[] eightBytes = { 0x2E, 0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77 };
            AssertFrames(
                eightBytes,
                new byte[][]
                {
                    new byte[] { 0x10, 0x08, 0x2E, 0x11, 0x22, 0x33, 0x44, 0x55 },
                    new byte[] { 0x21, 0x66, 0x77, 0x00, 0x00, 0x00, 0x00, 0x00 }
                },
                "minimum multi-frame payload");

            // Allowlist behaviour, both directions.
            byte[][] allowlist = { singleRequest };
            IsoTpTransport.EnforceUdsAllowlist(singleRequest, allowlist);
            AssertRejected(
                IsoTpTransport.ReadDataByIdentifier(0x1090), allowlist, "unrelated DID");

            byte[] flowControl = new byte[8];
            flowControl[0] = 0x30;
            IsoTpTransport.EnforceFrameAllowlist(flowControl, allowlist);
            AssertFrameRejected(
                new byte[] { 0x03, 0x22, 0x10, 0x90, 0x00, 0x00, 0x00, 0x00 },
                allowlist,
                "frame for an unrelated DID");
            AssertFrameRejected(
                new byte[] { 0x02, 0x10, 0x02, 0x00, 0x00, 0x00, 0x00, 0x00 },
                allowlist,
                "unallowlisted session control");

            RunWorkerProtocolChecks();
        }

        private static void RunWorkerProtocolChecks()
        {
            WorkerCommand exchange = WorkerCommand.Parse(
                "{\"command\":\"exchange\",\"schema_version\":1," +
                "\"sequence\":7,\"request_hex\":\"22 10 75\"}");
            if (!String.Equals(exchange.Name, "exchange", StringComparison.Ordinal) ||
                exchange.Sequence != 7 ||
                !Hex.Equal(exchange.Request, new byte[] { 0x22, 0x10, 0x75 }))
            {
                throw new InvalidOperationException("Worker command self-check failed.");
            }
            WorkerCommand stop = WorkerCommand.Parse(
                "{\"command\":\"stop\",\"schema_version\":1}");
            if (!String.Equals(stop.Name, "stop", StringComparison.Ordinal))
            {
                throw new InvalidOperationException("Worker stop self-check failed.");
            }
            AssertWorkerCommandRejected(
                "{\"command\":\"exchange\",\"schema_version\":2," +
                "\"sequence\":1,\"request_hex\":\"22 10 75\"}",
                "wrong schema version");
            AssertWorkerCommandRejected(
                "{\"command\":\"stop\",\"schema_version\":1,\"extra\":true}",
                "unknown field");
        }

        private static void AssertWorkerCommandRejected(string line, string label)
        {
            try
            {
                WorkerCommand.Parse(line);
            }
            catch (FormatException)
            {
                return;
            }
            throw new InvalidOperationException(
                "Worker protocol self-check failed to reject " + label + ".");
        }

        /// <summary>Prove every request this run could transmit is covered by
        /// its own allowlist, at both the payload and frame level.</summary>
        internal static void RunConfigChecks(BackendConfig config, Options options)
        {
            if (!options.IsCandidateMode)
            {
                return;
            }
            byte[][] allowlist = IsoTpTransport.BuildCandidateAllowlist(config, options.CandidateDids);
            foreach (byte[] request in allowlist)
            {
                IsoTpTransport.EnforceUdsAllowlist(request, allowlist);
                foreach (byte[] frame in Hex.IsoTpFrames(request))
                {
                    IsoTpTransport.EnforceFrameAllowlist(frame, allowlist);
                }
            }

            // A DID not selected for this run must be rejected, or the
            // per-batch allowlist would not actually narrow anything.
            int unselected = -1;
            for (int candidate = (int)config.DidMinimum; candidate <= (int)config.DidMaximum; candidate++)
            {
                if (!options.CandidateDids.Contains(candidate))
                {
                    unselected = candidate;
                    break;
                }
            }
            if (unselected >= 0)
            {
                AssertRejected(
                    IsoTpTransport.ReadDataByIdentifier(unselected), allowlist, "unselected DID");
            }
        }

        internal static int RunWriterSelfTest(Options options, string toolName)
        {
            BackendConfig config = SyntheticConfig();
            Options synthetic = new Options();
            synthetic.ToolName = toolName;
            synthetic.CandidateQualify = true;
            synthetic.BatchId = "self-test";
            synthetic.PeriodMs = options.PeriodMs;
            synthetic.MaxLogMiB = options.MaxLogMiB;
            synthetic.CandidateDids.Add(0x1063);
            synthetic.CandidateDids.Add(0x1064);

            Dictionary<int, int> counts = new Dictionary<int, int>();
            using (BoundedJsonlLog log = new BoundedJsonlLog(options.OutputPath, options.MaxLogMiB))
            {
                log.WriteSessionStart(config, synthetic, "self-test", "no device");
                log.WriteEvent("setup_complete", "Synthetic writer self-test; no device was opened.");

                ExchangeResult positive = new ExchangeResult(
                    new byte[] { 0x62, 0x10, 0x63, 0x12, 0x34 }, 14);
                log.TryWriteCandidateSample(1, 0x1063, 100, 114, positive, 0);
                counts[0x1063] = 1;

                ExchangeResult negative = new ExchangeResult(
                    new byte[] { 0x7F, 0x22, 0x31 }, 9, true, 0x31);
                log.TryWriteCandidateSample(2, 0x1064, 200, 209, negative, 0);
                counts[0x1064] = 1;

                log.TryWriteEnd("completed", 2, counts);
                Console.WriteLine(
                    "Writer self-test created {0} bytes at {1}; no device was opened.",
                    log.BytesWritten.ToString(CultureInfo.InvariantCulture),
                    log.FullPath);
            }
            Console.WriteLine("Framing, allowlist, and writer self-checks all passed.");
            return 0;
        }

        private static BackendConfig SyntheticConfig()
        {
            BackendConfig config = new BackendConfig();
            config.ProfileId = "self-test-profile";
            config.Bitrate = 500000;
            config.TesterCanId = 0x700;
            config.ResponseCanId = 0x708;
            config.Addressing = "normal";
            config.SessionSetup = new SessionStep[]
            {
                new SessionStep(
                    new byte[] { 0x3E, 0x00 }, new byte[] { 0x7E, 0x00 }, "tester present", false)
            };
            config.MaximumCandidateDids = 6;
            config.DidMinimum = 0x0100;
            config.DidMaximum = 0xEFFF;
            config.ResponsePendingBudgetMs = 10000;
            config.Device = JsonValue.NewObject(
                new Dictionary<string, JsonValue>(StringComparer.Ordinal));
            return config;
        }

        private static void AssertFrames(byte[] payload, byte[][] expected, string label)
        {
            List<byte[]> actual = new List<byte[]>(Hex.IsoTpFrames(payload));
            if (actual.Count != expected.Length)
            {
                throw new InvalidOperationException(
                    "Self-check failed for " + label + ": expected " +
                    expected.Length.ToString(CultureInfo.InvariantCulture) + " frames, produced " +
                    actual.Count.ToString(CultureInfo.InvariantCulture) + ".");
            }
            for (int i = 0; i < expected.Length; i++)
            {
                if (!Hex.Equal(actual[i], expected[i]))
                {
                    throw new InvalidOperationException(
                        "Self-check failed for " + label + " frame " +
                        i.ToString(CultureInfo.InvariantCulture) + ": expected " +
                        Hex.Format(expected[i]) + ", produced " + Hex.Format(actual[i]) + ".");
                }
            }
        }

        private static void AssertRejected(byte[] request, byte[][] allowlist, string label)
        {
            try
            {
                IsoTpTransport.EnforceUdsAllowlist(request, allowlist);
            }
            catch (InvalidOperationException)
            {
                return;
            }
            throw new InvalidOperationException(
                "Allowlist self-check failed: " + label + " was accepted.");
        }

        private static void AssertFrameRejected(byte[] frame, byte[][] allowlist, string label)
        {
            try
            {
                IsoTpTransport.EnforceFrameAllowlist(frame, allowlist);
            }
            catch (InvalidOperationException)
            {
                return;
            }
            throw new InvalidOperationException(
                "Frame allowlist self-check failed: " + label + " was accepted.");
        }
    }
}
