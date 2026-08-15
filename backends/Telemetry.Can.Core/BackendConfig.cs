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

        internal SessionStep(byte[] request, byte[] expectedPrefix, string label)
        {
            Request = request;
            ExpectedPrefix = expectedPrefix;
            Label = label;
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

        internal static BackendConfig Load(string path)
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
                "transport", "session_setup", "limits", "device");

            if (root.Member("", "schema_version").AsInteger("schema_version") != 1)
            {
                throw new FormatException("only backend-config schema 1 is supported");
            }

            BackendConfig config = new BackendConfig();
            config.ProfileId = root.Member("", "profile_id").AsString("profile_id");
            if (config.ProfileId.Length == 0)
            {
                throw new FormatException("'profile_id' must not be empty.");
            }

            JsonValue transport = root.Member("", "transport");
            transport.RejectUnknownMembers(
                "transport", "network", "bitrate", "tester_can_id", "response_can_id", "addressing");

            string network = transport.Member("transport", "network").AsString("transport.network");
            if (!String.Equals(network, "classical-can", StringComparison.Ordinal))
            {
                throw new FormatException(
                    "'transport.network' must be \"classical-can\"; CAN FD is not implemented.");
            }
            config.Bitrate = transport.Member("transport", "bitrate")
                .AsInteger("transport.bitrate", 10000, 1000000);
            config.TesterCanId = Hex.ParseId(
                transport.Member("transport", "tester_can_id").AsString("transport.tester_can_id"),
                "transport.tester_can_id", 0x000, 0x7FF);
            config.ResponseCanId = Hex.ParseId(
                transport.Member("transport", "response_can_id").AsString("transport.response_can_id"),
                "transport.response_can_id", 0x000, 0x7FF);
            if (config.TesterCanId == config.ResponseCanId)
            {
                throw new FormatException(
                    "'transport.tester_can_id' and 'transport.response_can_id' must differ.");
            }
            config.Addressing = transport.Member("transport", "addressing").AsString("transport.addressing");
            if (!String.Equals(config.Addressing, "normal", StringComparison.Ordinal))
            {
                throw new FormatException(
                    "'transport.addressing' must be \"normal\"; extended addressing is not implemented.");
            }

            List<JsonValue> steps = root.Member("", "session_setup").AsArray("session_setup", 1, 16);
            config.SessionSetup = new SessionStep[steps.Count];
            for (int i = 0; i < steps.Count; i++)
            {
                string where = "session_setup[" + i.ToString(CultureInfo.InvariantCulture) + "]";
                JsonValue step = steps[i];
                step.RejectUnknownMembers(where, "request", "expect", "label");
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
                config.SessionSetup[i] = new SessionStep(request, expect, label);
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
