using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Threading;

namespace Telemetry.Can.Core
{
    internal sealed class WorkerTransaction
    {
        internal readonly byte[] Request;
        internal readonly byte[] ExpectedPrefix;
        internal readonly string Label;

        internal WorkerTransaction(byte[] request, byte[] expectedPrefix, string label)
        {
            Request = request;
            ExpectedPrefix = expectedPrefix;
            Label = label;
        }
    }

    /// <summary>Exact private configuration emitted by the Python collector.</summary>
    internal sealed class WorkerConfig
    {
        internal string EndpointId;
        internal BackendConfig Backend;
        internal WorkerTransaction[] Transactions;

        internal static WorkerConfig Load(string path)
        {
            if (String.IsNullOrWhiteSpace(path))
            {
                throw new ArgumentException("--config PATH is required for --worker.");
            }
            string full = Path.GetFullPath(path);
            if (!File.Exists(full))
            {
                throw new FileNotFoundException("Worker configuration not found.", full);
            }
            JsonValue root = JsonReader.Parse(File.ReadAllText(full));
            root.RejectUnknownMembers(
                "", "document_type", "schema_version", "profile_id", "endpoint_id",
                "transport", "session_setup", "allowed_transactions", "device");
            if (!String.Equals(
                    root.Member("", "document_type").AsString("document_type"),
                    "telemetry.adapter-worker", StringComparison.Ordinal))
            {
                throw new FormatException("'document_type' must be \"telemetry.adapter-worker\".");
            }
            if (root.Member("", "schema_version").AsInteger("schema_version") != 1)
            {
                throw new FormatException("only adapter-worker schema 1 is supported");
            }

            WorkerConfig result = new WorkerConfig();
            result.EndpointId = RequiredText(root, "", "endpoint_id");
            BackendConfig backend = new BackendConfig();
            backend.ProfileId = RequiredText(root, "", "profile_id");
            backend.EcuKey = 0;
            backend.EcuName = result.EndpointId;

            JsonValue transport = root.Member("", "transport");
            transport.RejectUnknownMembers(
                "transport", "network", "bitrate", "addressing", "tester_can_id",
                "response_can_id", "initial_response_timeout_ms", "response_pending_budget_ms");
            if (!String.Equals(
                    transport.Member("transport", "network").AsString("transport.network"),
                    "classical-can", StringComparison.Ordinal))
            {
                throw new FormatException("'transport.network' must be \"classical-can\".");
            }
            backend.Bitrate = transport.Member("transport", "bitrate")
                .AsInteger("transport.bitrate", 10000, 1000000);
            backend.Addressing = transport.Member("transport", "addressing")
                .AsString("transport.addressing");
            if (!String.Equals(backend.Addressing, "normal", StringComparison.Ordinal))
            {
                throw new FormatException("'transport.addressing' must be \"normal\".");
            }
            backend.TesterCanId = Hex.ParseId(
                transport.Member("transport", "tester_can_id").AsString("transport.tester_can_id"),
                "transport.tester_can_id", 0, 0x7FF);
            backend.ResponseCanId = Hex.ParseId(
                transport.Member("transport", "response_can_id").AsString("transport.response_can_id"),
                "transport.response_can_id", 0, 0x7FF);
            if (backend.TesterCanId == backend.ResponseCanId)
            {
                throw new FormatException("worker tester and response CAN identifiers must differ");
            }
            backend.ResponsePendingBudgetMs = transport.Member("transport", "response_pending_budget_ms")
                .AsInteger("transport.response_pending_budget_ms", 100, 60000);
            backend.InitialResponseTimeoutMs = transport.Member("transport", "initial_response_timeout_ms")
                .AsInteger("transport.initial_response_timeout_ms", 100, 10000);

            List<JsonValue> steps = root.Member("", "session_setup")
                .AsArray("session_setup", 1, 16);
            backend.SessionSetup = new SessionStep[steps.Count];
            List<byte[]> allowlist = new List<byte[]>();
            for (int index = 0; index < steps.Count; index++)
            {
                string where = "session_setup[" + index.ToString(CultureInfo.InvariantCulture) + "]";
                JsonValue step = steps[index];
                step.RejectUnknownMembers(
                    where, "request_hex", "expected_prefix_hex", "label", "tolerate_negative");
                byte[] request = ParseBytes(step, where, "request_hex");
                if (request[0] != 0x10 && request[0] != 0x3E && request[0] != 0x2C)
                {
                    throw new FormatException(where + " uses a service outside the session allowlist.");
                }
                SessionStep parsed = new SessionStep(
                    request,
                    ParseBytes(step, where, "expected_prefix_hex"),
                    RequiredText(step, where, "label"),
                    step.Member(where, "tolerate_negative").AsBoolean(where + ".tolerate_negative"));
                backend.SessionSetup[index] = parsed;
                allowlist.Add(parsed.Request);
            }

            List<JsonValue> transactions = root.Member("", "allowed_transactions")
                .AsArray("allowed_transactions", 1, 4096);
            result.Transactions = new WorkerTransaction[transactions.Count];
            for (int index = 0; index < transactions.Count; index++)
            {
                string where = "allowed_transactions[" + index.ToString(CultureInfo.InvariantCulture) + "]";
                JsonValue item = transactions[index];
                item.RejectUnknownMembers(where, "request_hex", "expected_prefix_hex", "label");
                byte[] request = ParseBytes(item, where, "request_hex");
                if (request.Length != 3 || request[0] != 0x22)
                {
                    throw new FormatException(where + " must be one ReadDataByIdentifier request.");
                }
                foreach (WorkerTransaction prior in result.Transactions)
                {
                    if (prior != null && Hex.Equal(prior.Request, request))
                    {
                        throw new FormatException(where + " duplicates an allowlisted request.");
                    }
                }
                WorkerTransaction parsed = new WorkerTransaction(
                    request,
                    ParseBytes(item, where, "expected_prefix_hex"),
                    RequiredText(item, where, "label"));
                result.Transactions[index] = parsed;
                allowlist.Add(parsed.Request);
            }
            backend.Device = root.Member("", "device");
            if (backend.Device.Kind != JsonKind.Object)
            {
                throw new FormatException("'device' must be an object.");
            }
            backend.WorkerAllowlist = allowlist.ToArray();
            result.Backend = backend;
            return result;
        }

        private static byte[] ParseBytes(JsonValue value, string where, string name)
        {
            byte[] parsed = Hex.Parse(
                value.Member(where, name).AsString(where + "." + name), where + "." + name);
            if (parsed.Length == 0)
            {
                throw new FormatException(where + "." + name + " must not be empty.");
            }
            return parsed;
        }

        private static string RequiredText(JsonValue value, string where, string name)
        {
            string text = value.Member(where, name).AsString(
                String.IsNullOrEmpty(where) ? name : where + "." + name);
            if (String.IsNullOrWhiteSpace(text))
            {
                throw new FormatException("'" + name + "' must not be empty.");
            }
            return text;
        }
    }

    internal sealed class WorkerCommand
    {
        internal readonly string Name;
        internal readonly int Sequence;
        internal readonly byte[] Request;

        private WorkerCommand(string name, int sequence, byte[] request)
        {
            Name = name;
            Sequence = sequence;
            Request = request;
        }

        internal static WorkerCommand Parse(string line)
        {
            JsonValue root = JsonReader.Parse(line);
            string name = root.Member("", "command").AsString("command");
            if (String.Equals(name, "stop", StringComparison.Ordinal))
            {
                root.RejectUnknownMembers("", "command", "schema_version");
                RequireVersion(root);
                return new WorkerCommand(name, 0, null);
            }
            if (!String.Equals(name, "exchange", StringComparison.Ordinal))
            {
                throw new FormatException("unknown worker command '" + name + "'");
            }
            root.RejectUnknownMembers("", "command", "schema_version", "sequence", "request_hex");
            RequireVersion(root);
            int sequence = root.Member("", "sequence").AsInteger("sequence", 1, Int32.MaxValue);
            byte[] request = Hex.Parse(
                root.Member("", "request_hex").AsString("request_hex"), "request_hex");
            return new WorkerCommand(name, sequence, request);
        }

        private static void RequireVersion(JsonValue root)
        {
            if (root.Member("", "schema_version").AsInteger("schema_version") != 1)
            {
                throw new FormatException("only worker command schema 1 is supported");
            }
        }
    }

    /// <summary>Private, one-session transaction worker used only by Python collect.</summary>
    internal static class WorkerRunner
    {
        private sealed class Inbox
        {
            internal readonly object Gate = new object();
            internal readonly Queue<WorkerCommand> Commands = new Queue<WorkerCommand>();
            internal readonly AutoResetEvent Changed = new AutoResetEvent(false);
            internal Exception Failure;
            internal bool ExchangeOutstanding;
            internal bool InputEnded;
        }

        internal static bool TryRun(
            Func<BackendConfig, ICanChannel> openChannel,
            string[] args,
            string toolName,
            out int exitCode)
        {
            exitCode = 0;
            if (args.Length == 0 || !String.Equals(args[0], "--worker", StringComparison.Ordinal))
            {
                return false;
            }
            try
            {
                if (args.Length != 3 || !String.Equals(args[1], "--config", StringComparison.Ordinal))
                {
                    throw new ArgumentException("worker usage: " + toolName + " --worker --config PATH");
                }
                RunState.Reset();
                WorkerConfig config = WorkerConfig.Load(args[2]);
                SelfCheck.RunFramingChecks();
                using (ICanChannel channel = openChannel(config.Backend))
                {
                    channel.RefuseConcurrentClients();
                    channel.Open(
                        config.Backend.Bitrate,
                        config.Backend.ResponseCanId,
                        config.Backend.ResponseCanId);
                    channel.CheckStatus("initial worker CAN status");
                    IsoTpTransport transport = new IsoTpTransport(
                        channel,
                        config.Backend,
                        config.Backend.InitialResponseTimeoutMs,
                        false,
                        config.Backend.WorkerAllowlist);
                    foreach (SessionStep step in config.Backend.SessionSetup)
                    {
                        ExchangeResult setup = transport.ExchangeAllowNegative(
                            step.Request, step.ExpectedPrefix, step.Label);
                        if (setup.IsNegative && !step.TolerateNegative)
                        {
                            throw new InvalidOperationException(
                                "required session step was refused: " + step.Label);
                        }
                    }
                    WriteEvent(
                        "{\"event\":\"ready\",\"schema_version\":1,\"endpoint_id\":\"" +
                        Hex.JsonEscape(config.EndpointId) + "\",\"backend_kind\":\"" +
                        Hex.JsonEscape(channel.BackendName) + "\"}");
                    RunLoop(transport, config, channel);
                }
                exitCode = 0;
            }
            catch (Exception exc)
            {
                Console.Error.WriteLine("ERROR: " + exc.Message);
                exitCode = 1;
            }
            return true;
        }

        private static void RunLoop(
            IsoTpTransport transport, WorkerConfig config, ICanChannel channel)
        {
            Inbox inbox = new Inbox();
            Thread reader = new Thread(delegate() { ReadCommands(inbox); });
            reader.IsBackground = true;
            reader.Name = "telemetry-worker-stdin";
            reader.Start();
            HashSet<int> sequences = new HashSet<int>();
            while (true)
            {
                WorkerCommand command = TakeCommand(inbox);
                if (command == null || String.Equals(command.Name, "stop", StringComparison.Ordinal))
                {
                    WriteEvent("{\"event\":\"stopped\",\"schema_version\":1,\"reason\":\"stop\"}");
                    return;
                }
                if (!sequences.Add(command.Sequence))
                {
                    throw new FormatException(
                        "worker exchange sequence was already used: " +
                        command.Sequence.ToString(CultureInfo.InvariantCulture));
                }
                WorkerTransaction transaction = FindTransaction(config, command.Request);
                Stopwatch elapsed = Stopwatch.StartNew();
                int pendingCount = 0;
                long pendingDurationMs = 0;
                try
                {
                    transport.PendingObserved = delegate(int count, long durationMs)
                    {
                        pendingCount = count;
                        pendingDurationMs = durationMs;
                        WriteEvent(
                            "{\"event\":\"response_pending\",\"schema_version\":1,\"sequence\":" +
                            command.Sequence.ToString(CultureInfo.InvariantCulture) +
                            ",\"pending_count\":" + count.ToString(CultureInfo.InvariantCulture) +
                            ",\"pending_duration_ms\":" + durationMs.ToString(CultureInfo.InvariantCulture) + "}");
                    };
                    ExchangeResult result = transport.ExchangeAllowNegative(
                        transaction.Request, transaction.ExpectedPrefix, transaction.Label);
                    WriteResult(
                        command.Sequence,
                        result.IsNegative ? "negative" : "positive",
                        result.Payload,
                        result.IsNegative ? (int?)result.NegativeResponseCode : null,
                        result.PendingCount,
                        result.PendingDurationMs,
                        result.LatencyMs,
                        null);
                }
                catch (OperationCanceledException exc)
                {
                    WriteResult(command.Sequence, "abandoned", null, null, pendingCount,
                        pendingDurationMs, elapsed.ElapsedMilliseconds, exc.Message);
                }
                catch (TimeoutException exc)
                {
                    WriteResult(command.Sequence, "timeout", null, null, pendingCount,
                        pendingDurationMs, elapsed.ElapsedMilliseconds, exc.Message);
                }
                catch (Exception exc)
                {
                    WriteResult(command.Sequence, "transport_error", null, null, pendingCount,
                        pendingDurationMs, elapsed.ElapsedMilliseconds, exc.Message);
                    throw;
                }
                finally
                {
                    transport.PendingObserved = null;
                    lock (inbox.Gate)
                    {
                        inbox.ExchangeOutstanding = false;
                    }
                }
                lock (inbox.Gate)
                {
                    if (inbox.Failure != null)
                    {
                        throw new FormatException("worker input failed: " + inbox.Failure.Message);
                    }
                }
                if (RunState.StopRequested)
                {
                    WriteEvent("{\"event\":\"stopped\",\"schema_version\":1,\"reason\":\"stop\"}");
                    return;
                }
                channel.CheckStatus("after worker transaction");
            }
        }

        private static void ReadCommands(Inbox inbox)
        {
            try
            {
                string line;
                while ((line = Console.ReadLine()) != null)
                {
                    WorkerCommand command = WorkerCommand.Parse(line);
                    lock (inbox.Gate)
                    {
                        if (String.Equals(command.Name, "stop", StringComparison.Ordinal))
                        {
                            RunState.RequestStop();
                            inbox.Commands.Enqueue(command);
                        }
                        else
                        {
                            if (inbox.ExchangeOutstanding || inbox.Commands.Count != 0)
                            {
                                throw new FormatException(
                                    "a second exchange was received while one was outstanding");
                            }
                            inbox.ExchangeOutstanding = true;
                            inbox.Commands.Enqueue(command);
                        }
                    }
                    inbox.Changed.Set();
                    if (String.Equals(command.Name, "stop", StringComparison.Ordinal))
                    {
                        return;
                    }
                }
                lock (inbox.Gate)
                {
                    inbox.InputEnded = true;
                    RunState.RequestStop();
                }
            }
            catch (Exception exc)
            {
                lock (inbox.Gate)
                {
                    inbox.Failure = exc;
                    RunState.RequestStop();
                }
            }
            finally
            {
                inbox.Changed.Set();
            }
        }

        private static WorkerCommand TakeCommand(Inbox inbox)
        {
            while (true)
            {
                lock (inbox.Gate)
                {
                    if (inbox.Failure != null)
                    {
                        throw new FormatException("worker input failed: " + inbox.Failure.Message);
                    }
                    if (inbox.Commands.Count != 0)
                    {
                        return inbox.Commands.Dequeue();
                    }
                    if (inbox.InputEnded)
                    {
                        return null;
                    }
                }
                inbox.Changed.WaitOne();
            }
        }

        private static WorkerTransaction FindTransaction(WorkerConfig config, byte[] request)
        {
            foreach (WorkerTransaction transaction in config.Transactions)
            {
                if (Hex.Equal(transaction.Request, request))
                {
                    return transaction;
                }
            }
            throw new InvalidOperationException(
                "worker request failed the configured transaction allowlist: " + Hex.Format(request));
        }

        private static readonly object OutputGate = new object();

        private static void WriteEvent(string json)
        {
            lock (OutputGate)
            {
                Console.WriteLine(json);
                Console.Out.Flush();
            }
        }

        private static void WriteResult(
            int sequence,
            string outcome,
            byte[] response,
            int? nrc,
            int pendingCount,
            long pendingDurationMs,
            long latencyMs,
            string detail)
        {
            string responseJson = response == null ? "null" : "\"" + Hex.JsonEscape(Hex.Format(response)) + "\"";
            string nrcJson = nrc.HasValue
                ? "\"0x" + nrc.Value.ToString("X2", CultureInfo.InvariantCulture) + "\""
                : "null";
            string detailJson = detail == null ? "null" : "\"" + Hex.JsonEscape(detail) + "\"";
            WriteEvent(
                "{\"event\":\"result\",\"schema_version\":1,\"sequence\":" +
                sequence.ToString(CultureInfo.InvariantCulture) +
                ",\"outcome\":\"" + outcome + "\",\"response_hex\":" + responseJson +
                ",\"nrc\":" + nrcJson +
                ",\"pending_count\":" + pendingCount.ToString(CultureInfo.InvariantCulture) +
                ",\"pending_duration_ms\":" + pendingDurationMs.ToString(CultureInfo.InvariantCulture) +
                ",\"latency_ms\":" + latencyMs.ToString(CultureInfo.InvariantCulture) +
                ",\"detail\":" + detailJson + "}");
        }
    }
}
