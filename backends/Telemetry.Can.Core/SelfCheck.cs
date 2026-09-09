using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;

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

            // Telemetry guard behaviour: recognized read-only shapes pass,
            // every control/coding/security/programming service is rejected
            // outright regardless of what any transaction table might contain.
            TelemetryGuard.Enforce(singleRequest, TelemetryGuardContext.Transaction);
            AssertGuardRejected(
                new byte[] { 0x22, 0x10, 0x75, 0x00 }, TelemetryGuardContext.Transaction,
                "an oversized ReadDataByIdentifier request");
            AssertGuardRejected(
                new byte[] { 0x27, 0x01 }, TelemetryGuardContext.Transaction,
                "SecurityAccess as a runtime transaction");
            AssertGuardRejected(
                new byte[] { 0x2E, 0x10, 0x75, 0x00 }, TelemetryGuardContext.Transaction,
                "WriteDataByIdentifier as a runtime transaction");
            AssertGuardRejected(
                new byte[] { 0x10, 0x01 }, TelemetryGuardContext.Transaction,
                "DiagnosticSessionControl as a runtime transaction");

            TelemetryGuard.Enforce(new byte[] { 0x3E, 0x00 }, TelemetryGuardContext.SessionSetup);
            TelemetryGuard.Enforce(new byte[] { 0x10, 0x01 }, TelemetryGuardContext.SessionSetup);
            TelemetryGuard.Enforce(new byte[] { 0x10, 0x03 }, TelemetryGuardContext.SessionSetup);
            AssertGuardRejected(
                new byte[] { 0x10, 0x02 }, TelemetryGuardContext.SessionSetup,
                "programmingSession");
            AssertGuardRejected(
                new byte[] { 0x10, 0x04 }, TelemetryGuardContext.SessionSetup,
                "safetySystemDiagnosticSession");
            AssertGuardRejected(
                new byte[] { 0x22, 0x10, 0x75 }, TelemetryGuardContext.SessionSetup,
                "a runtime read used as a session-setup step");
            AssertGuardRejected(
                new byte[] { 0x11, 0x01 }, TelemetryGuardContext.SessionSetup,
                "ECUReset");

            TelemetryGuard.Enforce(
                new byte[] { 0x2C, 0x01, 0xF3, 0x01, 0x10, 0xA0, 0x01, 0x02 },
                TelemetryGuardContext.SessionSetup);
            AssertGuardRejected(
                new byte[] { 0x2C, 0x01, 0x01, 0x02, 0x10, 0xA0, 0x01, 0x02 },
                TelemetryGuardContext.SessionSetup,
                "a DefineByIdentifier target outside the dynamic-identifier range");
            AssertGuardRejected(
                new byte[] { 0x2C, 0x02, 0xF3, 0x01, 0x10, 0xA0, 0x01, 0x02 },
                TelemetryGuardContext.SessionSetup,
                "DefineByMemoryAddress instead of DefineByIdentifier");
            AssertGuardRejected(
                new byte[] { 0x2C, 0x01, 0xF3, 0x01, 0x10, 0xA0 },
                TelemetryGuardContext.SessionSetup,
                "a truncated DefineByIdentifier source tuple");
            AssertGuardRejected(
                new byte[] { 0x2C, 0x01, 0xF3, 0x01, 0x10, 0xA0, 0x00, 0x02 },
                TelemetryGuardContext.SessionSetup,
                "a zero-position DefineByIdentifier source tuple");

            RunWorkerProtocolChecks();
            RunProbeProtocolChecks();
        }

        private static void RunProbeProtocolChecks()
        {
            string validConfig =
                "{\"document_type\":\"telemetry.adapter-probe\",\"schema_version\":1," +
                "\"candidate_id\":\"700-708\",\"transport\":{\"network\":\"classical-can\"," +
                "\"bitrate\":500000,\"addressing\":\"normal\",\"tester_can_id\":\"0x700\"," +
                "\"response_can_id\":\"0x708\",\"initial_response_timeout_ms\":300," +
                "\"response_pending_budget_ms\":2000},\"session_setup\":[{\"request_hex\":\"3E 00\"," +
                "\"expected_prefix_hex\":\"7E 00\",\"label\":\"tester present\"," +
                "\"tolerate_negative\":true}]," +
                "\"preflight_read\":{\"request_hex\":\"22 10 75\",\"expected_prefix_hex\":\"62 10 75\"," +
                "\"label\":\"read 0x1075\"},\"device\":{\"pcan\":{\"channel\":\"PCAN_USBBUS1\"}}}";
            ProbeConfig loaded = LoadProbeConfig(validConfig);
            if (!String.Equals(loaded.CandidateId, "700-708", StringComparison.Ordinal) ||
                loaded.SessionSetup.Length != 1 || loaded.ReadStep == null ||
                loaded.ReadStep.Request.Length != 3 ||
                !loaded.SessionSetup[0].TolerateNegative)
            {
                throw new InvalidOperationException("Probe config self-check failed.");
            }
            if (!ProbeRunner.SessionStepAllowsContinuation("negative", true) ||
                ProbeRunner.SessionStepAllowsContinuation("negative", false) ||
                ProbeRunner.SessionStepAllowsContinuation("timeout", true))
            {
                throw new InvalidOperationException(
                    "Probe session tolerate-negative self-check failed.");
            }

            string discoveryOnly =
                "{\"document_type\":\"telemetry.adapter-probe\",\"schema_version\":1," +
                "\"candidate_id\":\"700-708\",\"transport\":{\"network\":\"classical-can\"," +
                "\"bitrate\":500000,\"addressing\":\"normal\",\"tester_can_id\":\"0x700\"," +
                "\"response_can_id\":\"0x708\",\"initial_response_timeout_ms\":300," +
                "\"response_pending_budget_ms\":2000},\"session_setup\":[{\"request_hex\":\"3E 00\"," +
                "\"expected_prefix_hex\":\"7E 00\",\"label\":\"tester present\"," +
                "\"tolerate_negative\":true}]," +
                "\"device\":{\"pcan\":{\"channel\":\"PCAN_USBBUS1\"}}}";
            if (LoadProbeConfig(discoveryOnly).ReadStep != null)
            {
                throw new InvalidOperationException(
                    "Probe config self-check failed: discovery-only config carried a read step.");
            }

            AssertProbeConfigRejected(
                validConfig.Replace("\"tester_can_id\":\"0x700\"", "\"tester_can_id\":\"0x708\""),
                "identical tester and response CAN identifiers");
            AssertProbeConfigRejected(
                validConfig.Replace(
                    "\"request_hex\":\"3E 00\",\"expected_prefix_hex\":\"7E 00\"",
                    "\"request_hex\":\"27 01\",\"expected_prefix_hex\":\"67 01\""),
                "SecurityAccess in a probe session-setup step");
        }

        private static ProbeConfig LoadProbeConfig(string json)
        {
            string path = Path.GetTempFileName();
            try
            {
                File.WriteAllText(path, json);
                return ProbeConfig.Load(path);
            }
            finally
            {
                File.Delete(path);
            }
        }

        private static void AssertProbeConfigRejected(string json, string label)
        {
            try
            {
                LoadProbeConfig(json);
            }
            catch (FormatException)
            {
                return;
            }
            catch (InvalidOperationException)
            {
                return;
            }
            throw new InvalidOperationException(
                "Probe config self-check failed to reject " + label + ".");
        }

        private static void RunWorkerProtocolChecks()
        {
            WorkerCommand exchange = WorkerCommand.Parse(
                "{\"command\":\"exchange\",\"schema_version\":1," +
                "\"sequence\":7,\"transaction_id\":\"700-708-session/0x1075\"}");
            if (!String.Equals(exchange.Name, "exchange", StringComparison.Ordinal) ||
                exchange.Sequence != 7 ||
                !String.Equals(
                    exchange.TransactionId, "700-708-session/0x1075", StringComparison.Ordinal))
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
                "\"sequence\":1,\"transaction_id\":\"700-708-session/0x1075\"}",
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

        private static void AssertGuardRejected(byte[] request, TelemetryGuardContext context, string label)
        {
            try
            {
                TelemetryGuard.Enforce(request, context);
            }
            catch (InvalidOperationException)
            {
                return;
            }
            throw new InvalidOperationException(
                "Telemetry guard self-check failed to reject " + label + ".");
        }
    }
}
