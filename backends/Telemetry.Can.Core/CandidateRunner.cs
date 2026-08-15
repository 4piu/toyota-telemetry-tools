using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.Threading;

namespace Telemetry.Can.Core
{
    /// <summary>
    /// Mode dispatch and the capture loops. Everything here is vehicle-neutral:
    /// endpoints, session setup, and DID selection arrive through
    /// <see cref="BackendConfig"/> and <see cref="Options"/>.
    /// </summary>
    internal static class CandidateRunner
    {
        private const uint ObdFunctionalRequestId = 0x7DF;
        private const uint ObdResponseFirstId = 0x7E8;
        private const uint ObdResponseLastId = 0x7EF;
        private const int RetryDelayMs = 100;

        private static readonly byte[] ObdSupportedPidsFrame =
            { 0x02, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00 };

        /// <summary>Entry point shared by every backend's Main.</summary>
        internal static int Run(Func<BackendConfig, ICanChannel> openChannel, string[] args, string toolName)
        {
            try
            {
                Options options = Options.Parse(args, toolName);
                if (options.ShowHelp)
                {
                    Options.PrintHelp(toolName, null);
                    return 0;
                }

                SelfCheck.RunFramingChecks();

                if (options.SelfTest)
                {
                    return SelfCheck.RunWriterSelfTest(options, toolName);
                }

                BackendConfig config = BackendConfig.Load(options.ConfigPath);
                options.ValidateAgainst(config);
                SelfCheck.RunConfigChecks(config, options);
                PrintPlan(config, options, toolName);

                if (!options.Live)
                {
                    Console.WriteLine();
                    Console.WriteLine(
                        "Dry run only: the device was not opened and nothing was transmitted.");
                    Console.WriteLine(
                        "Re-run with --live once the vehicle is stationary and no other CAN client is open.");
                    return 0;
                }

                Console.CancelKeyPress += delegate(object sender, ConsoleCancelEventArgs e)
                {
                    e.Cancel = true;
                    RunState.RequestStop();
                    Console.Error.WriteLine("Stop requested; closing the CAN session...");
                };

                using (ICanChannel channel = openChannel(config))
                {
                    channel.RefuseConcurrentClients();
                    if (options.ObdProbe)
                    {
                        return RunObdProbe(channel, config, options);
                    }
                    if (options.Probe)
                    {
                        return RunEndpointProbe(channel, config, options);
                    }
                    return RunCandidate(channel, config, options);
                }
            }
            catch (OperationCanceledException)
            {
                Console.Error.WriteLine("Stopped by user.");
                return 2;
            }
            catch (Exception exc)
            {
                Console.Error.WriteLine("ERROR: " + exc.Message);
                return 1;
            }
        }

        // ---- probes: the adapter acceptance test, before any capture ----

        private static int RunObdProbe(ICanChannel channel, BackendConfig config, Options options)
        {
            channel.Open(config.Bitrate, ObdResponseFirstId, ObdResponseLastId);
            channel.CheckStatus("initial CAN status");

            Stopwatch latency = Stopwatch.StartNew();
            channel.Write(ObdFunctionalRequestId, (byte[])ObdSupportedPidsFrame.Clone());
            Console.WriteLine("TX 0x7DF  " + Hex.Format(ObdSupportedPidsFrame));

            byte[] data;
            uint canId;
            if (!AwaitFrame(channel, options.TimeoutMs, latency, out canId, out data))
            {
                throw new TimeoutException(
                    "No standardized OBD response within " +
                    options.TimeoutMs.ToString(CultureInfo.InvariantCulture) + " ms.");
            }
            if (data.Length < 7 || data[0] != 0x06 || data[1] != 0x41 || data[2] != 0x00)
            {
                throw new InvalidOperationException("Unexpected OBD response: " + Hex.Format(data));
            }
            uint supported = ((uint)data[3] << 24) | ((uint)data[4] << 16) |
                             ((uint)data[5] << 8) | data[6];
            Console.WriteLine(
                "RX 0x" + canId.ToString("X3", CultureInfo.InvariantCulture) + "  " + Hex.Format(data) +
                "  latency=" + latency.ElapsedMilliseconds.ToString(CultureInfo.InvariantCulture) + " ms");
            Console.WriteLine(
                "Mode-01 PID support bitmap: 0x" + supported.ToString("X8", CultureInfo.InvariantCulture));
            Console.WriteLine(
                "SUCCESS: standardized OBD-over-CAN transmit and receive both work through this adapter.");
            channel.CheckStatus("final CAN status");
            return 0;
        }

        private static int RunEndpointProbe(ICanChannel channel, BackendConfig config, Options options)
        {
            SessionStep first = config.SessionSetup[0];
            byte[][] allowlist = { first.Request };
            channel.Open(config.Bitrate, config.ResponseCanId, config.ResponseCanId);
            channel.CheckStatus("initial CAN status");

            IsoTpTransport transport =
                new IsoTpTransport(channel, config, options.TimeoutMs, options.Verbose, allowlist);
            ExchangeResult result = transport.Exchange(first.Request, first.ExpectedPrefix, first.Label);
            Console.WriteLine(
                "RX 0x" + config.ResponseCanId.ToString("X3", CultureInfo.InvariantCulture) +
                "  " + Hex.Format(result.Payload) +
                "  latency=" + result.LatencyMs.ToString(CultureInfo.InvariantCulture) + " ms");
            Console.WriteLine(
                "SUCCESS: the gateway routed '" + first.Label + "' to the configured endpoint.");
            channel.CheckStatus("final CAN status");
            return 0;
        }

        private static bool AwaitFrame(
            ICanChannel channel, int timeoutMs, Stopwatch latency, out uint canId, out byte[] data)
        {
            long nextStatusCheckMs = 50;
            while (latency.ElapsedMilliseconds < timeoutMs && !RunState.StopRequested)
            {
                if (channel.TryRead(out canId, out data))
                {
                    return true;
                }
                if (latency.ElapsedMilliseconds >= nextStatusCheckMs)
                {
                    channel.CheckStatus("while awaiting a response");
                    nextStatusCheckMs += 50;
                }
                Thread.Sleep(1);
            }
            canId = 0;
            data = null;
            return false;
        }

        // ---- candidate qualification and batch capture ----

        private static int RunCandidate(ICanChannel channel, BackendConfig config, Options options)
        {
            BoundedJsonlLog log = null;
            int sequence = 0;
            Dictionary<int, int> counts = new Dictionary<int, int>();
            try
            {
                log = new BoundedJsonlLog(options.OutputPath, options.MaxLogMiB);
                log.WriteSessionStart(config, options, channel.BackendName, channel.Description);

                channel.Open(config.Bitrate, config.ResponseCanId, config.ResponseCanId);
                channel.CheckStatus("initial CAN status");

                byte[][] allowlist = IsoTpTransport.BuildCandidateAllowlist(config, options.CandidateDids);
                IsoTpTransport transport =
                    new IsoTpTransport(channel, config, options.TimeoutMs, options.Verbose, allowlist);

                foreach (SessionStep step in config.SessionSetup)
                {
                    transport.Exchange(step.Request, step.ExpectedPrefix, step.Label);
                }
                log.WriteEvent("setup_complete", "Configured session setup accepted.");

                Stopwatch schedule = Stopwatch.StartNew();
                long nextRequestNotBefore = 0;

                if (options.CandidateQualify)
                {
                    foreach (int did in options.CandidateDids)
                    {
                        if (RunState.StopRequested) { break; }
                        WaitUntil(schedule, nextRequestNotBefore);
                        if (RunState.StopRequested) { break; }

                        long requestStartedAt = schedule.ElapsedMilliseconds;
                        ExchangeResult result = transport.ExchangeAllowNegative(
                            IsoTpTransport.ReadDataByIdentifier(did),
                            IsoTpTransport.PositiveResponsePrefix(did),
                            "qualify DID " + Hex.FormatDid(did));
                        sequence++;
                        Increment(counts, did);
                        if (!log.TryWriteCandidateSample(
                                sequence, did, requestStartedAt, schedule.ElapsedMilliseconds, result, 0))
                        {
                            log.TryWriteEnd("log_limit", sequence, counts);
                            return 2;
                        }
                        Console.WriteLine(
                            "{0} {1} latency={2} ms payload={3}",
                            Hex.FormatDid(did),
                            result.IsNegative
                                ? "NEGATIVE " +
                                  BoundedJsonlLog.ClassifyNegativeResponse(result.NegativeResponseCode) +
                                  " NRC=0x" +
                                  result.NegativeResponseCode.ToString("X2", CultureInfo.InvariantCulture)
                                : "SUPPORTED",
                            result.LatencyMs.ToString(CultureInfo.InvariantCulture),
                            Hex.Format(result.Payload));
                        nextRequestNotBefore = requestStartedAt + options.PeriodMs;
                    }
                }
                else
                {
                    Console.WriteLine(
                        "Qualified candidate batch started; no decoding is performed in the vehicle.");
                    int index = 0;
                    bool hasDeadline = options.DurationSeconds > 0;
                    while (!RunState.StopRequested &&
                           (!hasDeadline || schedule.Elapsed.TotalSeconds < options.DurationSeconds))
                    {
                        WaitUntil(schedule, nextRequestNotBefore);
                        if (RunState.StopRequested ||
                            (hasDeadline && schedule.Elapsed.TotalSeconds >= options.DurationSeconds))
                        {
                            break;
                        }
                        long requestStartedAt = schedule.ElapsedMilliseconds;
                        int did = options.CandidateDids[index];
                        int retryCount;
                        ExchangeResult result = ExchangeWithRetry(
                            channel, transport, did, options.TransportRetries, log, out retryCount);
                        sequence++;
                        Increment(counts, did);
                        if (!log.TryWriteCandidateSample(
                                sequence, did, requestStartedAt, schedule.ElapsedMilliseconds,
                                result, retryCount))
                        {
                            log.TryWriteEnd("log_limit", sequence, counts);
                            return 2;
                        }
                        if (!options.Quiet)
                        {
                            Console.WriteLine(
                                "{0:HH:mm:ss.fff} sample={1:D5} DID={2} latency={3} ms payload={4}",
                                DateTime.Now, sequence, Hex.FormatDid(did),
                                result.LatencyMs, Hex.Format(result.Payload));
                        }
                        index = (index + 1) % options.CandidateDids.Count;
                        // After a retry the slot has already slipped; measure the
                        // next interval from now so the scheduler never "catches up".
                        nextRequestNotBefore = retryCount == 0
                            ? requestStartedAt + options.PeriodMs
                            : schedule.ElapsedMilliseconds + options.PeriodMs;
                    }
                }

                string reason = RunState.StopRequested ? "user_stop" : "completed";
                log.TryWriteEnd(reason, sequence, counts);
                channel.CheckStatus("final CAN status");
                Console.WriteLine(
                    "Capture ended: reason={0}, samples={1}, file={2}",
                    reason, sequence.ToString(CultureInfo.InvariantCulture), log.FullPath);
                return RunState.StopRequested ? 2 : 0;
            }
            catch (Exception exc)
            {
                if (log != null && !log.Ended)
                {
                    log.TryWriteError(exc);
                    log.TryWriteEnd(
                        exc is OperationCanceledException ? "user_stop" : "error", sequence, counts);
                }
                throw;
            }
            finally
            {
                if (log != null)
                {
                    log.Dispose();
                }
            }
        }

        /// <summary>Retry a timed-out transaction only. A negative response is
        /// an answer and is never retried; a timeout is not evidence that a DID
        /// is unsupported.</summary>
        private static ExchangeResult ExchangeWithRetry(
            ICanChannel channel,
            IsoTpTransport transport,
            int did,
            int maximumRetries,
            BoundedJsonlLog log,
            out int retryCount)
        {
            retryCount = 0;
            byte[] request = IsoTpTransport.ReadDataByIdentifier(did);
            byte[] expected = IsoTpTransport.PositiveResponsePrefix(did);
            string label = "poll DID " + Hex.FormatDid(did);
            while (true)
            {
                try
                {
                    return transport.Exchange(request, expected, label);
                }
                catch (TimeoutException exc)
                {
                    if (retryCount >= maximumRetries || RunState.StopRequested)
                    {
                        throw;
                    }
                    retryCount++;
                    string message = "DID " + Hex.FormatDid(did) + " retry " +
                        retryCount.ToString(CultureInfo.InvariantCulture) + "/" +
                        maximumRetries.ToString(CultureInfo.InvariantCulture) +
                        " after TimeoutException: " + exc.Message;
                    log.WriteEvent("transport_retry", message);
                    Console.Error.WriteLine("WARNING: " + message);
                    Thread.Sleep(RetryDelayMs);
                    channel.CheckStatus("before candidate retry");
                }
            }
        }

        private static void Increment(Dictionary<int, int> counts, int did)
        {
            int count;
            counts.TryGetValue(did, out count);
            counts[did] = count + 1;
        }

        private static void WaitUntil(Stopwatch schedule, long targetMilliseconds)
        {
            while (!RunState.StopRequested)
            {
                long remaining = targetMilliseconds - schedule.ElapsedMilliseconds;
                if (remaining <= 0)
                {
                    return;
                }
                Thread.Sleep((int)Math.Min(remaining, 10));
            }
        }

        private static void PrintPlan(BackendConfig config, Options options, string toolName)
        {
            Console.WriteLine(toolName + " - " + options.ModeName);
            Console.WriteLine("Profile:   " + config.ProfileId);
            Console.WriteLine(
                "Transport: classical CAN, " +
                config.Bitrate.ToString(CultureInfo.InvariantCulture) + " bit/s, application ISO-TP");
            if (options.ObdProbe)
            {
                Console.WriteLine("Endpoint:  0x7DF -> 0x7E8..0x7EF (standardized OBD)");
                Console.WriteLine("TX once:   " + Hex.Format(ObdSupportedPidsFrame));
            }
            else
            {
                Console.WriteLine(
                    "Endpoint:  0x" + config.TesterCanId.ToString("X3", CultureInfo.InvariantCulture) +
                    " -> 0x" + config.ResponseCanId.ToString("X3", CultureInfo.InvariantCulture));
            }
            if (options.IsCandidateMode)
            {
                Console.WriteLine("Setup:     " + DescribeSessionSetup(config));
                Console.WriteLine("Batch:     " + options.BatchId);
                List<string> dids = new List<string>();
                foreach (int did in options.CandidateDids)
                {
                    dids.Add(Hex.FormatDid(did));
                }
                Console.WriteLine("DIDs:      " + String.Join(", ", dids.ToArray()));
                Console.WriteLine(options.CandidateQualify
                    ? "Poll:      exactly one read per DID; negative responses are recorded"
                    : "Poll:      round-robin over the selected DIDs");
                Console.WriteLine(
                    "Schedule:  one outstanding request, period >= " +
                    options.PeriodMs.ToString(CultureInfo.InvariantCulture) + " ms");
                if (options.CandidateBatch)
                {
                    Console.WriteLine(options.DurationSeconds == 0
                        ? "Duration:  operator stop; no time limit"
                        : "Duration:  <= " +
                          options.DurationSeconds.ToString(CultureInfo.InvariantCulture) + " seconds");
                    Console.WriteLine(
                        "Retries:   <= " +
                        options.TransportRetries.ToString(CultureInfo.InvariantCulture) +
                        " timeout retries per transaction");
                }
                Console.WriteLine(
                    "Log:       JSONL, <= " +
                    options.MaxLogMiB.ToString(CultureInfo.InvariantCulture) + " MiB, create-new");
                Console.WriteLine("Output:    " + (options.OutputPath ?? "<required for --live>"));
            }
            Console.WriteLine(
                "Timeout:   " + options.TimeoutMs.ToString(CultureInfo.InvariantCulture) + " ms");
            Console.WriteLine("Mode:      " + (options.Live ? "LIVE - allowlisted requests only" : "DRY RUN"));
        }

        private static string DescribeSessionSetup(BackendConfig config)
        {
            List<string> labels = new List<string>();
            foreach (SessionStep step in config.SessionSetup)
            {
                labels.Add(step.Label);
            }
            return String.Join(", ", labels.ToArray());
        }
    }
}
