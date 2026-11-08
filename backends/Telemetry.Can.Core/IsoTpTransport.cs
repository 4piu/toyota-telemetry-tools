using System;
using System.Diagnostics;
using System.Globalization;
using System.Threading;

namespace Telemetry.Can.Core
{
    /// <summary>Cooperative stop flag set by the worker control reader.</summary>
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
    /// Application-managed ISO-TP (ISO 15765-2) over raw Classical CAN. Every
    /// resolved request passes through the built-in <see cref="TelemetryGuard"/>
    /// immediately before transmission; the guard is independent of the
    /// transaction table and plan that produced the request.
    /// </summary>
    internal sealed class IsoTpTransport
    {
        private readonly ICanChannel channel;
        private readonly uint testerCanId;
        private readonly uint responseCanId;
        private readonly int timeoutMs;
        private readonly int responsePendingBudgetMs;
        private readonly int targetAddress;
        private readonly bool verbose;
        internal Action<int, long> PendingObserved;

        internal IsoTpTransport(
            ICanChannel channel,
            BackendConfig config,
            int timeoutMs,
            bool verbose)
        {
            this.channel = channel;
            this.testerCanId = config.TesterCanId;
            this.responseCanId = config.ResponseCanId;
            this.timeoutMs = timeoutMs;
            this.responsePendingBudgetMs = config.ResponsePendingBudgetMs;
            this.targetAddress = config.TargetAddress;
            this.verbose = verbose;
        }

        internal ExchangeResult Exchange(
            byte[] request, byte[] expectedPrefix, string label, TelemetryGuardContext context)
        {
            return ExchangeCore(request, expectedPrefix, label, false, context);
        }

        /// <summary>Qualification needs the NRC rather than an exception: a
        /// negative response is a result, not a failure.</summary>
        internal ExchangeResult ExchangeAllowNegative(
            byte[] request, byte[] expectedPrefix, string label, TelemetryGuardContext context)
        {
            return ExchangeCore(request, expectedPrefix, label, true, context);
        }

        private ExchangeResult ExchangeCore(
            byte[] request, byte[] expectedPrefix, string label, bool allowNegative,
            TelemetryGuardContext context)
        {
            if (RunState.StopRequested)
            {
                throw new OperationCanceledException("Stopped by user.");
            }
            TelemetryGuard.Enforce(request, context);
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
            // One segmenter (Hex.IsoTpFrames) builds every transmitted frame,
            // the same one the offline self-check pins byte for byte.
            var frames = new System.Collections.Generic.List<byte[]>(
                Hex.IsoTpFrames(payload, targetAddress));
            if (frames.Count == 1)
            {
                WriteCanFrame(frames[0], label + " SF");
                return;
            }
            WriteCanFrame(frames[0], label + " FF");

            FlowControl flowControl = ReadFlowControl(label);
            int sentInBlock = 0;
            for (int index = 1; index < frames.Count; index++)
            {
                DelaySeparationTime(flowControl.SeparationTime);
                WriteCanFrame(
                    frames[index], label + " CF" + index.ToString(CultureInfo.InvariantCulture));
                sentInBlock++;

                if (flowControl.BlockSize != 0 &&
                    sentInBlock >= flowControl.BlockSize && index + 1 < frames.Count)
                {
                    flowControl = ReadFlowControl(label);
                    sentInBlock = 0;
                }
            }
        }

        private byte[] ReceivePayload(string label, int frameTimeoutMs)
        {
            int pci = IsoTpAddress.Offset(targetAddress);
            byte[] first = ReadCanFrame(label + " response", frameTimeoutMs);
            int frameType = first[pci] >> 4;
            if (frameType == 0)
            {
                int length = first[pci] & 0x0F;
                if (length <= 0 || length > 7 - pci)
                {
                    throw new InvalidOperationException(
                        "Invalid ISO-TP single-frame length in " + Hex.Format(first));
                }
                return Hex.Slice(first, pci + 1, length);
            }
            if (frameType != 1)
            {
                throw new InvalidOperationException(
                    "Expected ISO-TP response SF/FF, received " + Hex.Format(first));
            }

            int totalLength = ((first[pci] & 0x0F) << 8) | first[pci + 1];
            if (totalLength <= 7 - pci || totalLength > 4095)
            {
                throw new InvalidOperationException(
                    "Invalid ISO-TP first-frame length in " + Hex.Format(first));
            }

            byte[] payload = new byte[totalLength];
            Buffer.BlockCopy(first, pci + 2, payload, 0, 6 - pci);
            int offset = 6 - pci;

            byte[] continueFlowControl = IsoTpAddress.NewFrame(targetAddress);
            continueFlowControl[pci] = 0x30;
            WriteCanFrame(continueFlowControl, label + " FC");

            int expectedSequence = 1;
            while (offset < totalLength)
            {
                byte[] consecutive = ReadCanFrame(label + " response CF", frameTimeoutMs);
                if ((consecutive[pci] >> 4) != 2 || (consecutive[pci] & 0x0F) != expectedSequence)
                {
                    throw new InvalidOperationException(
                        "ISO-TP consecutive-frame sequence mismatch; expected " +
                        expectedSequence.ToString(CultureInfo.InvariantCulture) +
                        ", received " + Hex.Format(consecutive));
                }
                int count = Math.Min(7 - pci, totalLength - offset);
                Buffer.BlockCopy(consecutive, pci + 1, payload, offset, count);
                offset += count;
                expectedSequence = (expectedSequence + 1) & 0x0F;
            }
            return payload;
        }

        private FlowControl ReadFlowControl(string label)
        {
            int pci = IsoTpAddress.Offset(targetAddress);
            byte[] frame = ReadCanFrame(label + " flow control", timeoutMs);
            if ((frame[pci] >> 4) != 3)
            {
                throw new InvalidOperationException(
                    "Expected ISO-TP flow control, received " + Hex.Format(frame));
            }
            int status = frame[pci] & 0x0F;
            if (status != 0)
            {
                throw new InvalidOperationException(
                    "ISO-TP flow-control status was " +
                    status.ToString(CultureInfo.InvariantCulture) +
                    " instead of ContinueToSend: " + Hex.Format(frame));
            }
            return new FlowControl(frame[pci + 1], frame[pci + 2]);
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
                if (targetAddress != IsoTpAddress.None && data[0] != targetAddress)
                {
                    // Behind a gateway several ECUs answer on one response ID;
                    // their frames carry their own address byte and are not ours.
                    if (verbose)
                    {
                        Console.WriteLine(
                            "CAN RX 0x" + responseCanId.ToString("X3", CultureInfo.InvariantCulture) +
                            " ignored (address 0x" + data[0].ToString("X2", CultureInfo.InvariantCulture) +
                            "): " + Hex.Format(data));
                    }
                    continue;
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
