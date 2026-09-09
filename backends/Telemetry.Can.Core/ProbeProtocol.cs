using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Text;

namespace Telemetry.Can.Core
{
    internal sealed class ProbeStep
    {
        internal readonly byte[] Request;
        internal readonly byte[] ExpectedPrefix;
        internal readonly string Label;

        internal ProbeStep(byte[] request, byte[] expectedPrefix, string label)
        {
            Request = request;
            ExpectedPrefix = expectedPrefix;
            Label = label;
        }
    }

    /// <summary>Exact private configuration for one bounded, single-shot
    /// candidate probe used by <c>telemetry discover endpoints</c> and
    /// <c>telemetry preflight dids</c>. Distinct from <see cref="WorkerConfig"/>,
    /// which configures the long-lived collect worker: a probe opens a
    /// channel, attempts a fixed session-setup sequence, optionally attempts
    /// one ReadDataByIdentifier, reports one result, and exits. It never
    /// accepts commands on stdin.</summary>
    internal sealed class ProbeConfig
    {
        internal string CandidateId;
        internal BackendConfig Backend;
        internal ProbeStep[] SessionSetup;
        internal ProbeStep ReadStep;

        internal static ProbeConfig Load(string path)
        {
            if (String.IsNullOrWhiteSpace(path))
            {
                throw new ArgumentException("--probe --config PATH is required.");
            }
            string full = Path.GetFullPath(path);
            if (!File.Exists(full))
            {
                throw new FileNotFoundException("Probe configuration not found.", full);
            }
            JsonValue root = JsonReader.Parse(File.ReadAllText(full));
            root.RejectUnknownMembers(
                "", "document_type", "schema_version", "candidate_id", "transport",
                "session_setup", "preflight_read", "device");
            if (!String.Equals(
                    root.Member("", "document_type").AsString("document_type"),
                    "telemetry.adapter-probe", StringComparison.Ordinal))
            {
                throw new FormatException("'document_type' must be \"telemetry.adapter-probe\".");
            }
            if (root.Member("", "schema_version").AsInteger("schema_version") != 1)
            {
                throw new FormatException("only adapter-probe schema 1 is supported");
            }

            ProbeConfig result = new ProbeConfig();
            result.CandidateId = RequiredText(root, "", "candidate_id");

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

            BackendConfig backend = new BackendConfig();
            backend.ProfileId = result.CandidateId;
            backend.EcuKey = 0;
            backend.EcuName = result.CandidateId;
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
                throw new FormatException("probe tester and response CAN identifiers must differ");
            }
            backend.ResponsePendingBudgetMs = transport.Member("transport", "response_pending_budget_ms")
                .AsInteger("transport.response_pending_budget_ms", 100, 60000);
            backend.InitialResponseTimeoutMs = transport.Member("transport", "initial_response_timeout_ms")
                .AsInteger("transport.initial_response_timeout_ms", 50, 10000);
            backend.SessionSetup = new SessionStep[0];

            List<JsonValue> steps = root.Member("", "session_setup")
                .AsArray("session_setup", 1, 16);
            result.SessionSetup = new ProbeStep[steps.Count];
            for (int index = 0; index < steps.Count; index++)
            {
                string where = "session_setup[" + index.ToString(CultureInfo.InvariantCulture) + "]";
                JsonValue step = steps[index];
                step.RejectUnknownMembers(where, "request_hex", "expected_prefix_hex", "label");
                byte[] request = ParseBytes(step, where, "request_hex");
                TelemetryGuard.Enforce(request, TelemetryGuardContext.SessionSetup);
                result.SessionSetup[index] = new ProbeStep(
                    request, ParseBytes(step, where, "expected_prefix_hex"),
                    RequiredText(step, where, "label"));
            }

            JsonValue read;
            if (root.TryMember("preflight_read", out read))
            {
                read.RejectUnknownMembers("preflight_read", "request_hex", "expected_prefix_hex", "label");
                byte[] request = ParseBytes(read, "preflight_read", "request_hex");
                TelemetryGuard.Enforce(request, TelemetryGuardContext.Transaction);
                result.ReadStep = new ProbeStep(
                    request, ParseBytes(read, "preflight_read", "expected_prefix_hex"),
                    RequiredText(read, "preflight_read", "label"));
            }

            backend.Device = root.Member("", "device");
            if (backend.Device.Kind != JsonKind.Object)
            {
                throw new FormatException("'device' must be an object.");
            }
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

    /// <summary>One bounded, single-shot candidate probe. Never reads stdin;
    /// prints exactly one result line and exits.</summary>
    internal static class ProbeRunner
    {
        internal static bool TryRun(
            Func<BackendConfig, ICanChannel> openChannel,
            string[] args,
            string toolName,
            out int exitCode)
        {
            exitCode = 0;
            if (args.Length == 0 || !String.Equals(args[0], "--probe", StringComparison.Ordinal))
            {
                return false;
            }
            try
            {
                if (args.Length != 3 || !String.Equals(args[1], "--config", StringComparison.Ordinal))
                {
                    throw new ArgumentException("probe usage: " + toolName + " --probe --config PATH");
                }
                RunState.Reset();
                ProbeConfig config = ProbeConfig.Load(args[2]);
                using (ICanChannel channel = openChannel(config.Backend))
                {
                    channel.RefuseConcurrentClients();
                    channel.Open(
                        config.Backend.Bitrate,
                        config.Backend.ResponseCanId,
                        config.Backend.ResponseCanId);
                    channel.CheckStatus("initial probe CAN status");
                    IsoTpTransport transport = new IsoTpTransport(
                        channel, config.Backend, config.Backend.InitialResponseTimeoutMs, false);

                    List<string> stepEvents = new List<string>();
                    bool sessionComplete = true;
                    bool responded = false;
                    for (int index = 0; index < config.SessionSetup.Length; index++)
                    {
                        ProbeStep step = config.SessionSetup[index];
                        StepOutcome outcome = RunStep(transport, step, TelemetryGuardContext.SessionSetup);
                        stepEvents.Add(StepJson(step.Label, outcome));
                        bool stepResponded = outcome.Outcome == "positive" || outcome.Outcome == "negative";
                        if (index == 0)
                        {
                            // The first step alone establishes candidate
                            // presence: a later step timing out (e.g. an
                            // extended-session refusal) does not retract that
                            // observation.
                            responded = stepResponded;
                        }
                        if (!stepResponded)
                        {
                            sessionComplete = false;
                            break;
                        }
                    }

                    string readJson;
                    if (config.ReadStep == null)
                    {
                        readJson = "null";
                    }
                    else if (!sessionComplete)
                    {
                        readJson = "{\"attempted\":false,\"outcome\":\"session_unavailable\"}";
                    }
                    else
                    {
                        StepOutcome readOutcome = RunStep(
                            transport, config.ReadStep, TelemetryGuardContext.Transaction);
                        readJson = "{\"attempted\":true," + StepFields(readOutcome) + "}";
                    }

                    StringBuilder json = new StringBuilder();
                    json.Append("{\"event\":\"probe_result\",\"schema_version\":1,\"candidate_id\":\"");
                    json.Append(Hex.JsonEscape(config.CandidateId));
                    json.Append("\",\"backend_kind\":\"");
                    json.Append(Hex.JsonEscape(channel.BackendName));
                    json.Append("\",\"responded\":");
                    json.Append(responded ? "true" : "false");
                    json.Append(",\"session_steps\":[");
                    json.Append(String.Join(",", stepEvents.ToArray()));
                    json.Append("],\"read\":");
                    json.Append(readJson);
                    json.Append("}");
                    Console.WriteLine(json.ToString());
                    Console.Out.Flush();
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

        private sealed class StepOutcome
        {
            internal readonly string Outcome;
            internal readonly long LatencyMs;
            internal readonly int? Nrc;
            internal readonly byte[] Response;

            internal StepOutcome(string outcome, long latencyMs, int? nrc, byte[] response)
            {
                Outcome = outcome;
                LatencyMs = latencyMs;
                Nrc = nrc;
                Response = response;
            }
        }

        private static StepOutcome RunStep(
            IsoTpTransport transport, ProbeStep step, TelemetryGuardContext context)
        {
            Stopwatch elapsed = Stopwatch.StartNew();
            try
            {
                ExchangeResult result = transport.ExchangeAllowNegative(
                    step.Request, step.ExpectedPrefix, step.Label, context);
                return new StepOutcome(
                    result.IsNegative ? "negative" : "positive",
                    result.LatencyMs,
                    result.IsNegative ? (int?)result.NegativeResponseCode : null,
                    result.Payload);
            }
            catch (TimeoutException)
            {
                return new StepOutcome("timeout", elapsed.ElapsedMilliseconds, null, null);
            }
            catch (OperationCanceledException)
            {
                return new StepOutcome("timeout", elapsed.ElapsedMilliseconds, null, null);
            }
            catch (Exception)
            {
                return new StepOutcome("transport_error", elapsed.ElapsedMilliseconds, null, null);
            }
        }

        private static string StepFields(StepOutcome outcome)
        {
            string nrcJson = outcome.Nrc.HasValue
                ? "\"0x" + outcome.Nrc.Value.ToString("X2", CultureInfo.InvariantCulture) + "\""
                : "null";
            string responseJson = outcome.Response == null
                ? "null"
                : "\"" + Hex.JsonEscape(Hex.Format(outcome.Response)) + "\"";
            return "\"outcome\":\"" + outcome.Outcome + "\",\"latency_ms\":" +
                outcome.LatencyMs.ToString(CultureInfo.InvariantCulture) +
                ",\"nrc\":" + nrcJson + ",\"response_hex\":" + responseJson;
        }

        private static string StepJson(string label, StepOutcome outcome)
        {
            return "{\"label\":\"" + Hex.JsonEscape(label) + "\"," + StepFields(outcome) + "}";
        }
    }
}
