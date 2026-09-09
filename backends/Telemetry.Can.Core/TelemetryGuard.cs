using System;

namespace Telemetry.Can.Core
{
    /// <summary>Distinguishes a replayed session-setup step, which may change
    /// diagnostic-session state, from a runtime-selected transaction-table
    /// read.</summary>
    internal enum TelemetryGuardContext
    {
        SessionSetup,
        Transaction,
    }

    /// <summary>
    /// The built-in, vehicle-agnostic guard every resolved UDS request passes
    /// through immediately before transmission. It knows nothing about a
    /// specific vehicle, endpoint, or plan: it recognizes only the small set of
    /// read-only diagnostic service shapes the telemetry toolchain implements,
    /// and rejects everything else outright, including every vehicle control,
    /// coding, clearing, security-access, memory-access, and programming
    /// service.
    ///
    /// The immutable transaction table that supplies request bytes is itself a
    /// settings/provenance snapshot derived from the reviewed admitted plan,
    /// not an independent safety authority. This guard is that independent
    /// authority: its logic is fixed by the toolkit, not by the plan, config,
    /// or any separately authored allowlist, so a defect further up the
    /// pipeline cannot itself authorize a service this guard does not
    /// recognize.
    /// </summary>
    internal static class TelemetryGuard
    {
        // Dynamic-identifier assignment range reserved by ISO 14229-1 for
        // DefineByIdentifier / dynamically-defined data identifiers.
        private const int DynamicDidRangeMin = 0xF200;
        private const int DynamicDidRangeMax = 0xF3FF;

        internal static void Enforce(byte[] request, TelemetryGuardContext context)
        {
            if (request == null || request.Length == 0)
            {
                throw Reject(request, "an empty request");
            }
            byte service = request[0];
            if (context == TelemetryGuardContext.SessionSetup)
            {
                EnforceSessionSetup(request, service);
                return;
            }
            EnforceTransaction(request, service);
        }

        /// <summary>Session setup replays once per worker launch and may
        /// change diagnostic-session state: TesterPresent, default/extended
        /// DiagnosticSessionControl, and a reviewed dynamic-DID definition are
        /// the only recognized shapes. ProgrammingSession, SafetySystem
        /// session, and every other service are rejected outright.</summary>
        private static void EnforceSessionSetup(byte[] request, byte service)
        {
            switch (service)
            {
                case 0x3E: // TesterPresent
                    if (request.Length != 2 || request[1] != 0x00)
                    {
                        throw Reject(request, "an unsupported TesterPresent shape");
                    }
                    return;
                case 0x10: // DiagnosticSessionControl: default or extended only.
                    if (request.Length != 2 || (request[1] != 0x01 && request[1] != 0x03))
                    {
                        throw Reject(request, "an unsupported diagnostic session type");
                    }
                    return;
                case 0x2C: // DefineByIdentifier: dynamic-DID definition only.
                    if (request.Length < 8 || request[1] != 0x01 ||
                        (request.Length - 4) % 4 != 0)
                    {
                        throw Reject(request, "an unsupported DefineByIdentifier shape");
                    }
                    int targetDid = (request[2] << 8) | request[3];
                    if (targetDid < DynamicDidRangeMin || targetDid > DynamicDidRangeMax)
                    {
                        throw Reject(
                            request, "a DefineByIdentifier target outside the dynamic-identifier range");
                    }
                    for (int offset = 4; offset < request.Length; offset += 4)
                    {
                        int position = request[offset + 2];
                        int size = request[offset + 3];
                        if (position < 1 || size < 1 || position + size - 1 > 0xFF)
                        {
                            throw Reject(
                                request, "an invalid DefineByIdentifier source tuple");
                        }
                    }
                    return;
                default:
                    throw Reject(request, "a service outside the session-setup capability");
            }
        }

        /// <summary>Runtime transactions are selected by identifier from the
        /// worker's own immutable table and may only be reads: session control
        /// and dynamic-DID definition are session-setup-only and are never
        /// reachable through the runtime transaction table, regardless of what
        /// the table itself contains.</summary>
        private static void EnforceTransaction(byte[] request, byte service)
        {
            if (service != 0x22 || request.Length != 3)
            {
                throw Reject(request, "a service outside the runtime read capability");
            }
        }

        private static InvalidOperationException Reject(byte[] request, string what)
        {
            return new InvalidOperationException(
                "Telemetry guard rejected " + what + ": " + Hex.Format(request));
        }
    }
}
