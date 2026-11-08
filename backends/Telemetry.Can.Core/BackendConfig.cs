using System;

namespace Telemetry.Can.Core
{
    internal sealed class SessionStep
    {
        internal readonly byte[] Request;
        internal readonly byte[] ExpectedPrefix;
        internal readonly string Label;
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
    /// Resolved adapter/session state built only by the exact private worker
    /// configuration reader. It is not a second public project schema.
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
        /// <summary>Extended-addressing target byte, or IsoTpAddress.None.</summary>
        internal int TargetAddress = IsoTpAddress.None;
        internal SessionStep[] SessionSetup;
        internal int ResponsePendingBudgetMs;
        internal int InitialResponseTimeoutMs;
        internal JsonValue Device;

        /// <summary>
        /// Read "addressing" and, for extended addressing only, the exact
        /// "target_address" byte. Normal addressing must not carry one.
        /// </summary>
        internal void ReadAddressing(JsonValue transport)
        {
            Addressing = transport.Member("transport", "addressing").AsString("transport.addressing");
            JsonValue address;
            bool hasAddress = transport.TryMember("target_address", out address);
            if (String.Equals(Addressing, "normal", StringComparison.Ordinal))
            {
                if (hasAddress)
                {
                    throw new FormatException(
                        "'transport.target_address' is only valid with extended addressing.");
                }
                TargetAddress = IsoTpAddress.None;
                return;
            }
            if (!String.Equals(Addressing, "extended", StringComparison.Ordinal))
            {
                throw new FormatException("'transport.addressing' must be \"normal\" or \"extended\".");
            }
            if (!hasAddress)
            {
                throw new FormatException("extended addressing needs 'transport.target_address'.");
            }
            TargetAddress = (int)Hex.ParseId(
                address.AsString("transport.target_address"), "transport.target_address", 0, 0xFF);
        }

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
