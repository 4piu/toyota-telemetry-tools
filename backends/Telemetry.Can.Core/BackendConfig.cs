using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;

namespace Telemetry.Can.Core
{
    /// <summary>One request/response pair replayed to open a diagnostic
    /// session, recovered from a captured tester conversation.</summary>
    internal sealed class SessionStep
    {
        internal readonly byte[] Request;
        internal readonly byte[] ExpectedPrefix;
        internal readonly string Label;

        /// <summary>Continue when this step draws a negative response.
        ///
        /// Steps differ in what a refusal means. Tester present is a keepalive:
        /// an ECU that rejects it can still answer reads, and several do. A
        /// session change is not: proceeding after one is refused would read
        /// identifiers in a session the ECU never entered. Tolerance is
        /// therefore per step, and off unless a configuration asks for it.
        /// </summary>
        internal readonly bool TolerateNegative;

        internal SessionStep(
            byte[] request, byte[] expectedPrefix, string label, bool tolerateNegative)
        {
            Request = request;
            ExpectedPrefix = expectedPrefix;
            Label = label;
            TolerateNegative = tolerateNegative;
        }
    }

    /// <summary>
    /// Everything vehicle-specific about a backend run. Nothing here is
    /// compiled in; a platform project supplies this file and the executable
    /// stays generic.
    /// </summary>
    internal sealed class BackendConfig
    {
        internal string ProfileId;
        internal int EcuKey;
        internal string EcuName;
        internal int Bitrate;
        internal uint TesterCanId;
        internal uint ResponseCanId;
        internal string Addressing;
        internal SessionStep[] SessionSetup;
        internal int MaximumCandidateDids;
        internal uint DidMinimum;
        internal uint DidMaximum;
        internal int ResponsePendingBudgetMs;
        internal JsonValue Device;

        /// <summary>
        /// Load the configuration and resolve it to one ECU.
        ///
        /// ECUs behind a common gateway share bitrate, addressing, device, and
        /// usually their session sequence, so those live once at the top level
        /// and each entry in <c>ecus</c> contributes only its endpoint. A run
        /// still targets exactly one ECU: a single CAN channel carries one
        /// diagnostic session at a time.
        /// </summary>
        internal static BackendConfig Load(string path, int requestedEcuKey)
        {
            if (String.IsNullOrWhiteSpace(path))
            {
                throw new ArgumentException("--config PATH is required.");
            }
            string full = Path.GetFullPath(path);
            if (!File.Exists(full))
            {
                throw new FileNotFoundException("Backend configuration not found.", full);
            }
            JsonValue root;
            try
            {
                root = JsonReader.Parse(File.ReadAllText(full));
            }
            catch (FormatException exc)
            {
                throw new FormatException(full + ": " + exc.Message, exc);
            }

            root.RejectUnknownMembers(
                "",
                "schema_version", "profile_id", "description",
                "transport", "session_setup", "limits", "device", "ecus");

            if (root.Member("", "schema_version").AsInteger("schema_version") != 2)
            {
                throw new FormatException("only backend-config schema 2 is supported");
            }

            BackendConfig config = new BackendConfig();
            config.ProfileId = root.Member("", "profile_id").AsString("profile_id");
            if (config.ProfileId.Length == 0)
            {
                throw new FormatException("'profile_id' must not be empty.");
            }

            JsonValue transport = root.Member("", "transport");
            transport.RejectUnknownMembers("transport", "network", "bitrate", "addressing");

            string network = transport.Member("transport", "network").AsString("transport.network");
            if (!String.Equals(network, "classical-can", StringComparison.Ordinal))
            {
                throw new FormatException(
                    "'transport.network' must be \"classical-can\"; CAN FD is not implemented.");
            }
            config.Bitrate = transport.Member("transport", "bitrate")
                .AsInteger("transport.bitrate", 10000, 1000000);
            config.Addressing = transport.Member("transport", "addressing").AsString("transport.addressing");
            if (!String.Equals(config.Addressing, "normal", StringComparison.Ordinal))
            {
                throw new FormatException(
                    "'transport.addressing' must be \"normal\"; extended addressing is not implemented.");
            }

            string ecuWhere;
            JsonValue selected = SelectEcu(root, requestedEcuKey, out ecuWhere);
            config.EcuKey = selected.Member(ecuWhere, "key").AsInteger(ecuWhere + ".key", 0, 65535);
            config.EcuName = selected.Member(ecuWhere, "name").AsString(ecuWhere + ".name");
            config.TesterCanId = Hex.ParseId(
                selected.Member(ecuWhere, "tester_can_id").AsString(ecuWhere + ".tester_can_id"),
                ecuWhere + ".tester_can_id", 0x000, 0x7FF);
            config.ResponseCanId = Hex.ParseId(
                selected.Member(ecuWhere, "response_can_id").AsString(ecuWhere + ".response_can_id"),
                ecuWhere + ".response_can_id", 0x000, 0x7FF);
            if (config.TesterCanId == config.ResponseCanId)
            {
                throw new FormatException(
                    ecuWhere + ": tester and response identifiers must differ.");
            }

            // An ECU may open its session differently from its neighbours, so an
            // entry can override the shared sequence.
            JsonValue stepsValue;
            string stepsWhere = ecuWhere + ".session_setup";
            if (!selected.TryMember("session_setup", out stepsValue))
            {
                stepsValue = root.Member("", "session_setup");
                stepsWhere = "session_setup";
            }
            List<JsonValue> steps = stepsValue.AsArray(stepsWhere, 1, 16);
            config.SessionSetup = new SessionStep[steps.Count];
            for (int i = 0; i < steps.Count; i++)
            {
                string where = stepsWhere + "[" + i.ToString(CultureInfo.InvariantCulture) + "]";
                JsonValue step = steps[i];
                step.RejectUnknownMembers(where, "request", "expect", "label", "tolerate_negative");
                byte[] request = Hex.Parse(
                    step.Member(where, "request").AsString(where + ".request"), where + ".request");
                byte[] expect = Hex.Parse(
                    step.Member(where, "expect").AsString(where + ".expect"), where + ".expect");
                string label = step.Member(where, "label").AsString(where + ".label");
                if (request.Length == 0 || expect.Length == 0 || label.Length == 0)
                {
                    throw new FormatException(where + " must have a non-empty request, expect, and label.");
                }
                // A session step that reads data would make the allowlist a lie:
                // the capture schema only accounts for 0x22 responses it issued.
                if (request[0] == 0x22)
                {
                    throw new FormatException(
                        where + " must not be a ReadDataByIdentifier request; " +
                        "DIDs are supplied with --did so they appear in the capture record.");
                }
                JsonValue tolerate;
                bool tolerateNegative =
                    step.TryMember("tolerate_negative", out tolerate)
                    && tolerate.AsBoolean(where + ".tolerate_negative");
                config.SessionSetup[i] =
                    new SessionStep(request, expect, label, tolerateNegative);
            }

            JsonValue limits = root.Member("", "limits");
            limits.RejectUnknownMembers(
                "limits", "maximum_candidate_dids", "did_min", "did_max", "response_pending_budget_ms");
            config.MaximumCandidateDids = limits.Member("limits", "maximum_candidate_dids")
                .AsInteger("limits.maximum_candidate_dids", 1, 64);
            config.DidMinimum = Hex.ParseId(
                limits.Member("limits", "did_min").AsString("limits.did_min"),
                "limits.did_min", 0x0000, 0xFFFF);
            config.DidMaximum = Hex.ParseId(
                limits.Member("limits", "did_max").AsString("limits.did_max"),
                "limits.did_max", 0x0000, 0xFFFF);
            if (config.DidMinimum > config.DidMaximum)
            {
                throw new FormatException("'limits.did_min' must not exceed 'limits.did_max'.");
            }
            config.ResponsePendingBudgetMs = limits.Member("limits", "response_pending_budget_ms")
                .AsInteger("limits.response_pending_budget_ms", 100, 60000);

            JsonValue device;
            config.Device = root.TryMember("device", out device) ? device : JsonValue.NewObject(
                new Dictionary<string, JsonValue>(StringComparer.Ordinal));

            return config;
        }

        /// <summary>Pick the requested ECU, or the only one if a run did not
        /// name a key. Listing the available keys on failure matters: the
        /// operator is usually at a vehicle when this goes wrong.</summary>
        private static JsonValue SelectEcu(JsonValue root, int requestedEcuKey, out string where)
        {
            List<JsonValue> ecus = root.Member("", "ecus").AsArray("ecus", 1, 64);
            List<string> available = new List<string>();
            JsonValue match = null;
            string matchWhere = null;
            for (int i = 0; i < ecus.Count; i++)
            {
                string current = "ecus[" + i.ToString(CultureInfo.InvariantCulture) + "]";
                ecus[i].RejectUnknownMembers(
                    current, "key", "name", "tester_can_id", "response_can_id",
                    "session_setup", "description");
                int key = ecus[i].Member(current, "key").AsInteger(current + ".key", 0, 65535);
                available.Add(key.ToString(CultureInfo.InvariantCulture));
                if (key == requestedEcuKey)
                {
                    if (match != null)
                    {
                        throw new FormatException(
                            "'ecus' contains more than one entry with key " +
                            key.ToString(CultureInfo.InvariantCulture) + ".");
                    }
                    match = ecus[i];
                    matchWhere = current;
                }
            }
            if (requestedEcuKey < 0)
            {
                if (ecus.Count != 1)
                {
                    throw new ArgumentException(
                        "--ecu KEY is required because 'ecus' lists " +
                        ecus.Count.ToString(CultureInfo.InvariantCulture) +
                        " entries: " + String.Join(", ", available.ToArray()));
                }
                where = "ecus[0]";
                return ecus[0];
            }
            if (match == null)
            {
                throw new ArgumentException(
                    "--ecu " + requestedEcuKey.ToString(CultureInfo.InvariantCulture) +
                    " is not in this configuration. Available: " +
                    String.Join(", ", available.ToArray()));
            }
            where = matchWhere;
            return match;
        }

        /// <summary>Read a backend-specific string from the optional
        /// <c>device</c> object, e.g. the PCAN channel or the J2534 DLL path.</summary>
        internal string DeviceString(string section, string name, string fallback)
        {
            JsonValue group;
            if (!Device.TryMember(section, out group))
            {
                return fallback;
            }
            JsonValue value;
            if (!group.TryMember(name, out value))
            {
                return fallback;
            }
            return value.AsString("device." + section + "." + name);
        }
    }
}
