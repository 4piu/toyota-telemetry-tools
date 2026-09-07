using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.Threading;

namespace Telemetry.Can.Core
{
    /// <summary>Cooperative stop flag set by the Ctrl+C handler.</summary>
    internal static class RunState
    {
        private static volatile bool stopRequested;

        internal static bool StopRequested
        {
            get { return stopRequested; }
        }

        internal static void RequestStop()
        {
            stopRequested = true;
        }

        internal static void Reset()
        {
            stopRequested = false;
        }
    }

    internal sealed class ExchangeResult
    {
        internal readonly byte[] Payload;
        internal readonly long LatencyMs;
        internal readonly bool IsNegative;
        internal readonly byte NegativeResponseCode;
        internal readonly int PendingCount;
        internal readonly long PendingDurationMs;

        internal ExchangeResult(byte[] payload, long latencyMs)
            : this(payload, latencyMs, false, 0, 0, 0)
        {
        }

        internal ExchangeResult(byte[] payload, long latencyMs, bool isNegative, byte negativeResponseCode)
            : this(payload, latencyMs, isNegative, negativeResponseCode, 0, 0)
        {
        }

        internal ExchangeResult(
            byte[] payload, long latencyMs, bool isNegative, byte negativeResponseCode,
            int pendingCount, long pendingDurationMs)
        {
            Payload = payload;
            LatencyMs = latencyMs;
            IsNegative = isNegative;
            NegativeResponseCode = negativeResponseCode;
            PendingCount = pendingCount;
            PendingDurationMs = pendingDurationMs;
        }
    }

    /// <summary>
    /// Application-managed ISO-TP (ISO 15765-2) over raw Classical CAN, with a
    /// byte-exact transmit allowlist enforced at both the UDS payload level and
    /// the individual CAN frame level.
    ///
    /// Both allowlists are derived from the same <see cref="Hex.IsoTpFrames"/>
    /// expansion used to transmit, so a payload that passes the first check can
    /// never produce a frame that fails the second for a different reason.
    /// </summary>
    internal sealed class IsoTpTransport
    {
        private readonly ICanChannel channel;
        private readonly uint testerCanId;
        private readonly uint responseCanId;
        private readonly int timeoutMs;
        private readonly int responsePendingBudgetMs;
        private readonly bool verbose;
        private readonly byte[][] allowedRequests;
        internal Action<int, long> PendingObserved;

        internal IsoTpTransport(
            ICanChannel channel,
            BackendConfig config,
            int timeoutMs,
            bool verbose,
            byte[][] allowedRequests)
        {
            this.channel = channel;
            this.testerCanId = config.TesterCanId;
            this.responseCanId = config.ResponseCanId;
            this.timeoutMs = timeoutMs;
            this.responsePendingBudgetMs = config.ResponsePendingBudgetMs;
            this.verbose = verbose;
            this.allowedRequests = allowedRequests;
        }

        internal ExchangeResult Exchange(byte[] request, byte[] expectedPrefix, string label)
        {
            return ExchangeCore(request, expectedPrefix, label, false);
        }

        /// <summary>Qualification needs the NRC rather than an exception: a
        /// negative response is a result, not a failure.</summary>
        internal ExchangeResult ExchangeAllowNegative(byte[] request, byte[] expectedPrefix, string label)
        {
            return ExchangeCore(request, expectedPrefix, label, true);
        }

        private ExchangeResult ExchangeCore(
            byte[] request, byte[] expectedPrefix, string label, bool allowNegative)
        {
            if (RunState.StopRequested)
            {
                throw new OperationCanceledException("Stopped by user.");
            }
            EnforceUdsAllowlist(request, allowedRequests);
            Stopwatch latency = Stopwatch.StartNew();
            if (verbose)
            {
                Console.WriteLine("UDS TX " + label + ": " + Hex.Format(request));
            }

            SendPayload(request, label);
            byte[] response = ReceivePayload(label, timeoutMs);
            int pendingCount = 0;
            long pendingStartedMs = 0;

            // NRC 0x78 (responsePending) may repeat; bound the total wait so a
            // stuck ECU cannot hold the session open indefinitely.
            while (response.Length >= 3 &&
                   response[0] == 0x7F && response[1] == request[0] && response[2] == 0x78)
            {
                pendingCount++;
                if (pendingStartedMs == 0)
                {
                    pendingStartedMs = latency.ElapsedMilliseconds;
                }
                if (PendingObserved != null)
                {
                    PendingObserved(pendingCount, latency.ElapsedMilliseconds - pendingStartedMs);
                }
                if (verbose)
                {
                    Console.WriteLine("UDS RX " + label + ": response pending (NRC 0x78)");
                }
                long remaining = responsePendingBudgetMs - latency.ElapsedMilliseconds;
                if (remaining <= 0)
                {
                    throw new TimeoutException(
                        "ECU remained response-pending for " + label + " beyond " +
                        responsePendingBudgetMs.ToString(CultureInfo.InvariantCulture) + " ms.");
                }
                response = ReceivePayload(
                    label + " after response pending", (int)Math.Min(remaining, 5000L));
            }

            if (verbose)
            {
                Console.WriteLine("UDS RX " + label + ": " + Hex.Format(response));
            }
            if (response.Length >= 3 && response[0] == 0x7F && response[1] == request[0])
            {
                if (allowNegative)
                {
                    return new ExchangeResult(
                        response, latency.ElapsedMilliseconds, true, response[2], pendingCount,
                        pendingCount == 0 ? 0 : latency.ElapsedMilliseconds - pendingStartedMs);
                }
                throw new InvalidOperationException(
                    "ECU negative response to " + label + ": service=0x" +
                    response[1].ToString("X2", CultureInfo.InvariantCulture) +
                    ", NRC=0x" + response[2].ToString("X2", CultureInfo.InvariantCulture) +
                    ", payload=" + Hex.Format(response));
            }
            if (!Hex.StartsWith(response, expectedPrefix))
            {
                throw new InvalidOperationException(
                    "Unexpected response to " + label + ": " + Hex.Format(response) +
                    " (expected prefix " + Hex.Format(expectedPrefix) + ")");
            }
            return new ExchangeResult(
                response, latency.ElapsedMilliseconds, false, 0, pendingCount,
                pendingCount == 0 ? 0 : latency.ElapsedMilliseconds - pendingStartedMs);
        }

        private void SendPayload(byte[] payload, string label)
        {
            if (payload.Length <= 7)
            {
                byte[] single = new byte[8];
                single[0] = (byte)payload.Length;
                Buffer.BlockCopy(payload, 0, single, 1, payload.Length);
                WriteCanFrame(single, label + " SF");
                return;
            }
            if (payload.Length > 4095)
            {
                throw new InvalidOperationException(
                    "ISO-TP payload exceeds the Classical-CAN 12-bit length limit.");
            }

            byte[] first = new byte[8];
            first[0] = (byte)(0x10 | ((payload.Length >> 8) & 0x0F));
            first[1] = (byte)(payload.Length & 0xFF);
            Buffer.BlockCopy(payload, 0, first, 2, 6);
            WriteCanFrame(first, label + " FF");

            FlowControl flowControl = ReadFlowControl(label);
            int offset = 6;
            int sequence = 1;
            int sentInBlock = 0;
            while (offset < payload.Length)
            {
                DelaySeparationTime(flowControl.SeparationTime);
                byte[] consecutive = new byte[8];
                consecutive[0] = (byte)(0x20 | (sequence & 0x0F));
                int count = Math.Min(7, payload.Length - offset);
                Buffer.BlockCopy(payload, offset, consecutive, 1, count);
                WriteCanFrame(
                    consecutive, label + " CF" + sequence.ToString(CultureInfo.InvariantCulture));
                offset += count;
                sequence = (sequence + 1) & 0x0F;
                sentInBlock++;

                if (flowControl.BlockSize != 0 &&
                    sentInBlock >= flowControl.BlockSize && offset < payload.Length)
                {
                    flowControl = ReadFlowControl(label);
                    sentInBlock = 0;
                }
            }
        }

        private byte[] ReceivePayload(string label, int frameTimeoutMs)
        {
            byte[] first = ReadCanFrame(label + " response", frameTimeoutMs);
            int frameType = first[0] >> 4;
            if (frameType == 0)
            {
                int length = first[0] & 0x0F;
                if (length <= 0 || length > 7)
                {
                    throw new InvalidOperationException(
                        "Invalid ISO-TP single-frame length in " + Hex.Format(first));
                }
                return Hex.Slice(first, 1, length);
            }
            if (frameType != 1)
            {
                throw new InvalidOperationException(
                    "Expected ISO-TP response SF/FF, received " + Hex.Format(first));
            }

            int totalLength = ((first[0] & 0x0F) << 8) | first[1];
            if (totalLength <= 6 || totalLength > 4095)
            {
                throw new InvalidOperationException(
                    "Invalid ISO-TP first-frame length in " + Hex.Format(first));
            }

            byte[] payload = new byte[totalLength];
            Buffer.BlockCopy(first, 2, payload, 0, 6);
            int offset = 6;

            byte[] continueFlowControl = new byte[8];
            continueFlowControl[0] = 0x30;
            WriteCanFrame(continueFlowControl, label + " FC");

            int expectedSequence = 1;
            while (offset < totalLength)
            {
                byte[] consecutive = ReadCanFrame(label + " response CF", frameTimeoutMs);
                if ((consecutive[0] >> 4) != 2 || (consecutive[0] & 0x0F) != expectedSequence)
                {
                    throw new InvalidOperationException(
                        "ISO-TP consecutive-frame sequence mismatch; expected " +
                        expectedSequence.ToString(CultureInfo.InvariantCulture) +
                        ", received " + Hex.Format(consecutive));
                }
                int count = Math.Min(7, totalLength - offset);
                Buffer.BlockCopy(consecutive, 1, payload, offset, count);
                offset += count;
                expectedSequence = (expectedSequence + 1) & 0x0F;
            }
            return payload;
        }

        private FlowControl ReadFlowControl(string label)
        {
            byte[] frame = ReadCanFrame(label + " flow control", timeoutMs);
            if ((frame[0] >> 4) != 3)
            {
                throw new InvalidOperationException(
                    "Expected ISO-TP flow control, received " + Hex.Format(frame));
            }
            int status = frame[0] & 0x0F;
            if (status != 0)
            {
                throw new InvalidOperationException(
                    "ISO-TP flow-control status was " +
                    status.ToString(CultureInfo.InvariantCulture) +
                    " instead of ContinueToSend: " + Hex.Format(frame));
            }
            return new FlowControl(frame[1], frame[2]);
        }

        private byte[] ReadCanFrame(string label, int frameTimeoutMs)
        {
            Stopwatch wait = Stopwatch.StartNew();
            long nextStatusCheckMs = 50;
            while (wait.ElapsedMilliseconds < frameTimeoutMs && !RunState.StopRequested)
            {
                uint canId;
                byte[] data;
                if (!channel.TryRead(out canId, out data))
                {
                    if (wait.ElapsedMilliseconds >= nextStatusCheckMs)
                    {
                        channel.CheckStatus("while awaiting " + label);
                        nextStatusCheckMs += 50;
                    }
                    Thread.Sleep(1);
                    continue;
                }
                if (canId != responseCanId)
                {
                    throw new InvalidOperationException(
                        "Unexpected CAN frame while awaiting " + label + ": ID=0x" +
                        canId.ToString("X", CultureInfo.InvariantCulture) + ".");
                }
                if (data == null || data.Length != 8)
                {
                    throw new InvalidOperationException(
                        "CAN frame from 0x" +
                        responseCanId.ToString("X", CultureInfo.InvariantCulture) +
                        " was not eight bytes while awaiting " + label + ".");
                }
                if (verbose)
                {
                    Console.WriteLine(
                        "CAN RX 0x" + responseCanId.ToString("X3", CultureInfo.InvariantCulture) +
                        " " + label + ": " + Hex.Format(data));
                }
                return data;
            }

            if (RunState.StopRequested)
            {
                throw new OperationCanceledException("Stopped by user.");
            }
            throw new TimeoutException(
                "No CAN frame from 0x" +
                responseCanId.ToString("X", CultureInfo.InvariantCulture) + " for " + label +
                " within " + frameTimeoutMs.ToString(CultureInfo.InvariantCulture) + " ms.");
        }

        private void WriteCanFrame(byte[] data, string label)
        {
            EnforceFrameAllowlist(data, allowedRequests);
            channel.Write(testerCanId, data);
            if (verbose)
            {
                Console.WriteLine(
                    "CAN TX 0x" + testerCanId.ToString("X3", CultureInfo.InvariantCulture) +
                    " " + label + ": " + Hex.Format(data));
            }
        }

        private static void DelaySeparationTime(byte separationTime)
        {
            if (separationTime <= 0x7F)
            {
                if (separationTime > 0)
                {
                    Thread.Sleep(separationTime);
                }
                return;
            }
            if (separationTime >= 0xF1 && separationTime <= 0xF9)
            {
                double targetMicroseconds = (separationTime - 0xF0) * 100.0;
                long targetTicks = (long)Math.Ceiling(
                    targetMicroseconds * Stopwatch.Frequency / 1000000.0);
                Stopwatch delay = Stopwatch.StartNew();
                while (delay.ElapsedTicks < targetTicks)
                {
                    Thread.SpinWait(20);
                }
            }
        }

        // ---- allowlists ----

        internal static void EnforceUdsAllowlist(byte[] request, byte[][] allowedRequests)
        {
            foreach (byte[] allowed in allowedRequests)
            {
                if (Hex.Equal(request, allowed))
                {
                    return;
                }
            }
            throw new InvalidOperationException(
                "UDS request failed the transmit allowlist: " + Hex.Format(request));
        }

        internal static void EnforceFrameAllowlist(byte[] frame, byte[][] allowedRequests)
        {
            if (frame == null || frame.Length != 8)
            {
                throw new InvalidOperationException(
                    "Raw ISO-TP CAN frames must contain exactly eight bytes.");
            }

            // Tester flow control carries no request payload and is always
            // permitted; every other transmitted frame must be a fragment of an
            // allowlisted request.
            byte[] flowControl = new byte[8];
            flowControl[0] = 0x30;
            if (Hex.Equal(frame, flowControl))
            {
                return;
            }

            foreach (byte[] request in allowedRequests)
            {
                foreach (byte[] allowedFrame in Hex.IsoTpFrames(request))
                {
                    if (Hex.Equal(frame, allowedFrame))
                    {
                        return;
                    }
                }
            }
            throw new InvalidOperationException(
                "Raw CAN frame failed the transmit allowlist: " + Hex.Format(frame));
        }

        /// <summary>Session-setup requests plus one ReadDataByIdentifier per
        /// selected DID. Nothing else can be transmitted in candidate mode.</summary>
        internal static byte[][] BuildCandidateAllowlist(BackendConfig config, List<int> dids)
        {
            List<byte[]> allowed = new List<byte[]>();
            foreach (SessionStep step in config.SessionSetup)
            {
                allowed.Add(step.Request);
            }
            foreach (int did in dids)
            {
                allowed.Add(ReadDataByIdentifier(did));
            }
            return allowed.ToArray();
        }

        internal static byte[] ReadDataByIdentifier(int did)
        {
            return new byte[] { 0x22, (byte)(did >> 8), (byte)did };
        }

        internal static byte[] PositiveResponsePrefix(int did)
        {
            return new byte[] { 0x62, (byte)(did >> 8), (byte)did };
        }

        private sealed class FlowControl
        {
            internal readonly byte BlockSize;
            internal readonly byte SeparationTime;

            internal FlowControl(byte blockSize, byte separationTime)
            {
                BlockSize = blockSize;
                SeparationTime = separationTime;
            }
        }
    }
}
