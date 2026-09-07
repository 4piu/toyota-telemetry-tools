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
        internal SessionStep[] SessionSetup;
        internal int ResponsePendingBudgetMs;
        internal int InitialResponseTimeoutMs;
        internal byte[][] WorkerAllowlist;
        internal JsonValue Device;

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
