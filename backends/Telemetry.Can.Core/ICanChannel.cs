using System;

namespace Telemetry.Can.Core
{
    /// <summary>
    /// The whole device-specific surface. Everything above this interface —
    /// ISO-TP framing, allowlists, capture logging, scheduling — is shared by
    /// every backend, so adding a transport means implementing these seven
    /// members and nothing else.
    ///
    /// Implementations own bit timing, filtering, and cleanup. They must not
    /// interpret payloads.
    /// </summary>
    internal interface ICanChannel : IDisposable
    {
        /// <summary>Human-readable device description for the capture record
        /// and the dry-run plan, e.g. "PCAN-Basic PCAN_USBBUS1".</summary>
        string Description { get; }

        /// <summary>Value written to the capture's <c>transport.backend</c>
        /// field, e.g. "PCAN-Basic".</summary>
        string BackendName { get; }

        /// <summary>Open at the configured bitrate and install the narrowest
        /// available hardware filter for the inclusive identifier range.
        /// Must drain anything a previous session left queued.</summary>
        void Open(int bitrate, uint filterFromId, uint filterToId);

        /// <summary>Transmit one standard 11-bit frame with exactly eight data
        /// bytes. Callers have already enforced the allowlist.</summary>
        void Write(uint canId, byte[] data);

        /// <summary>Non-blocking read. Returns false when the queue is empty.
        /// <paramref name="data"/> is exactly eight bytes on success.</summary>
        bool TryRead(out uint canId, out byte[] data);

        /// <summary>Throw if the controller reports an error state. Called
        /// periodically while waiting so a bus fault surfaces as a fault
        /// rather than as a timeout.</summary>
        void CheckStatus(string context);

        /// <summary>Refuse to run live when another client owns the device.
        /// Two clients on one channel silently corrupt each other's view of
        /// the bus. No-op where the transport cannot detect this.</summary>
        void RefuseConcurrentClients();
    }
}
