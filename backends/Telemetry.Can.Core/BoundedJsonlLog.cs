using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Text;

namespace Telemetry.Can.Core
{
    /// <summary>
    /// Writes candidate-capture schema 1 as JSONL, bounded by both a
    /// configured byte limit and a free-space reserve.
    ///
    /// The file is create-new: a capture never overwrites another. Space for
    /// the final <c>session_end</c> record is reserved up front, so a run that
    /// hits its size limit still terminates with a valid, parseable file rather
    /// than a truncated one.
    /// </summary>
    internal sealed class BoundedJsonlLog : IDisposable
    {
        private const long DiskReserveBytes = 256L * 1024L * 1024L;
        private const long EndRecordReserveBytes = 16L * 1024L;
        private const int SchemaVersion = 1;

        private readonly long maxBytes;
        private readonly FileStream stream;
        private readonly StreamWriter writer;
        private readonly UTF8Encoding encoding = new UTF8Encoding(false);
        private bool disposed;

        internal readonly string FullPath;
        internal bool Ended { get; private set; }

        internal long BytesWritten
        {
            get
            {
                writer.Flush();
                return stream.Position;
            }
        }

        internal BoundedJsonlLog(string path, int maxLogMiB)
        {
            if (String.IsNullOrWhiteSpace(path))
            {
                throw new ArgumentException("A bounded-capture output path is required.");
            }
            FullPath = Path.GetFullPath(path);
            string directory = Path.GetDirectoryName(FullPath);
            if (String.IsNullOrEmpty(directory))
            {
                throw new ArgumentException("Could not resolve the bounded-capture output directory.");
            }
            maxBytes = checked((long)maxLogMiB * 1024L * 1024L);

            DriveInfo drive = new DriveInfo(Path.GetPathRoot(FullPath));
            long requiredFree = checked(maxBytes + DiskReserveBytes);
            if (drive.AvailableFreeSpace < requiredFree)
            {
                throw new IOException(
                    "Insufficient free space for bounded capture. Required at least " +
                    requiredFree.ToString(CultureInfo.InvariantCulture) +
                    " bytes before start; available " +
                    drive.AvailableFreeSpace.ToString(CultureInfo.InvariantCulture) + ".");
            }

            Directory.CreateDirectory(directory);
            stream = new FileStream(
                FullPath, FileMode.CreateNew, FileAccess.Write, FileShare.Read,
                65536, FileOptions.SequentialScan);
            writer = new StreamWriter(stream, encoding, 65536);
            writer.AutoFlush = true;
        }

        internal void WriteSessionStart(
            BackendConfig config, Options options, string backendName, string deviceDescription)
        {
            StringBuilder dids = new StringBuilder();
            for (int i = 0; i < options.CandidateDids.Count; i++)
            {
                if (i != 0)
                {
                    dids.Append(',');
                }
                dids.Append('"');
                dids.Append(Hex.FormatDid(options.CandidateDids[i]));
                dids.Append('"');
            }
            string mode = options.CandidateQualify ? "candidate_qualification" : "candidate_batch";
            string json =
                "{\"record\":\"session_start\",\"schema_version\":" +
                SchemaVersion.ToString(CultureInfo.InvariantCulture) +
                ",\"profile_id\":\"" + Hex.JsonEscape(config.ProfileId) +
                "\",\"created_utc\":\"" +
                DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture) +
                "\",\"tool\":\"" + Hex.JsonEscape(options.ToolName) +
                "\",\"tool_log_format\":\"candidate-capture-v1\"" +
                ",\"mode\":\"" + mode +
                "\",\"batch_id\":\"" + Hex.JsonEscape(options.BatchId) +
                "\",\"transport\":{\"backend\":\"" + Hex.JsonEscape(backendName) +
                "\",\"channel\":\"" + Hex.JsonEscape(deviceDescription) +
                "\",\"network\":\"classical-can\",\"bitrate\":" +
                config.Bitrate.ToString(CultureInfo.InvariantCulture) +
                ",\"tester_id\":\"0x" +
                config.TesterCanId.ToString("X3", CultureInfo.InvariantCulture) +
                "\",\"response_id\":\"0x" +
                config.ResponseCanId.ToString("X3", CultureInfo.InvariantCulture) +
                "\"},\"dids\":[" + dids + "],\"period_ms\":" +
                options.PeriodMs.ToString(CultureInfo.InvariantCulture) +
                ",\"transport_retries\":" +
                (options.CandidateBatch
                    ? options.TransportRetries.ToString(CultureInfo.InvariantCulture)
                    : "0") +
                ",\"duration_limit_seconds\":" +
                (options.CandidateQualify || options.DurationSeconds == 0
                    ? "null"
                    : options.DurationSeconds.ToString(CultureInfo.InvariantCulture)) +
                ",\"log_limit_bytes\":" + maxBytes.ToString(CultureInfo.InvariantCulture) +
                ",\"timestamp_semantics\":\"host UTC after complete response plus monotonic request/response offsets\"," +
                "\"privacy\":\"VIN, location and adapter serial are not collected\"}";
            if (!TryWriteRecord(json, true))
            {
                throw new IOException("Capture header does not fit its configured size limit.");
            }
        }

        internal void WriteEvent(string name, string message)
        {
            string json = "{\"record\":\"event\",\"utc\":\"" +
                DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture) +
                "\",\"name\":\"" + Hex.JsonEscape(name) +
                "\",\"message\":\"" + Hex.JsonEscape(message) + "\"}";
            TryWriteRecord(json, true);
        }

        internal bool TryWriteCandidateSample(
            int sequence,
            int did,
            long requestElapsedMs,
            long responseElapsedMs,
            ExchangeResult result,
            int retryCount)
        {
            string json = "{\"record\":\"candidate_sample\",\"sequence\":" +
                sequence.ToString(CultureInfo.InvariantCulture) +
                ",\"did\":\"" + Hex.FormatDid(did) +
                "\",\"utc\":\"" + DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture) +
                "\",\"request_elapsed_ms\":" + requestElapsedMs.ToString(CultureInfo.InvariantCulture) +
                ",\"response_elapsed_ms\":" + responseElapsedMs.ToString(CultureInfo.InvariantCulture) +
                ",\"latency_ms\":" + result.LatencyMs.ToString(CultureInfo.InvariantCulture) +
                ",\"request_hex\":\"" + Hex.Format(IsoTpTransport.ReadDataByIdentifier(did)) +
                "\",\"response_hex\":\"" + Hex.JsonEscape(Hex.Format(result.Payload)) +
                "\",\"retry_count\":" + retryCount.ToString(CultureInfo.InvariantCulture) +
                ",\"supported\":" + (result.IsNegative ? "false" : "true") +
                ",\"qualification_status\":\"" +
                (result.IsNegative
                    ? ClassifyNegativeResponse(result.NegativeResponseCode)
                    : "supported") + "\"" +
                ",\"negative_response_code\":" +
                (result.IsNegative
                    ? "\"0x" + result.NegativeResponseCode.ToString("X2", CultureInfo.InvariantCulture) + "\""
                    : "null") + "}";
            return TryWriteRecord(json, true);
        }

        internal void TryWriteError(Exception exception)
        {
            string json = "{\"record\":\"error\",\"utc\":\"" +
                DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture) +
                "\",\"exception\":\"" + Hex.JsonEscape(exception.GetType().Name) +
                "\",\"message\":\"" + Hex.JsonEscape(exception.Message) + "\"}";
            TryWriteRecord(json, true);
        }

        internal void TryWriteEnd(string reason, int total, Dictionary<int, int> counts)
        {
            if (Ended)
            {
                return;
            }
            StringBuilder countJson = new StringBuilder();
            bool first = true;
            foreach (KeyValuePair<int, int> pair in counts)
            {
                if (!first)
                {
                    countJson.Append(',');
                }
                first = false;
                countJson.Append('"');
                countJson.Append(Hex.FormatDid(pair.Key));
                countJson.Append("\":");
                countJson.Append(pair.Value.ToString(CultureInfo.InvariantCulture));
            }
            string json = "{\"record\":\"session_end\",\"utc\":\"" +
                DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture) +
                "\",\"reason\":\"" + Hex.JsonEscape(reason) +
                "\",\"sample_count\":" + total.ToString(CultureInfo.InvariantCulture) +
                ",\"did_counts\":{" + countJson + "},\"bytes_before_end\":" +
                BytesWritten.ToString(CultureInfo.InvariantCulture) + "}";
            if (!TryWriteRecord(json, false))
            {
                throw new IOException("Could not write the reserved capture end record.");
            }
            Ended = true;
        }

        internal static string ClassifyNegativeResponse(byte code)
        {
            switch (code)
            {
                case 0x21: return "busy_retry";
                case 0x22: return "conditions_not_correct";
                case 0x31: return "request_out_of_range";
                case 0x33: return "security_denied";
                case 0x7E:
                case 0x7F: return "session_restricted";
                default: return "negative_response";
            }
        }

        private bool TryWriteRecord(string json, bool preserveEndReserve)
        {
            if (disposed)
            {
                throw new ObjectDisposedException("BoundedJsonlLog");
            }
            byte[] bytes = encoding.GetBytes(json + "\n");
            long allowed = preserveEndReserve ? maxBytes - EndRecordReserveBytes : maxBytes;
            writer.Flush();
            if (stream.Position + bytes.Length > allowed)
            {
                return false;
            }
            writer.Write(json);
            writer.Write('\n');
            writer.Flush();
            return true;
        }

        public void Dispose()
        {
            if (disposed)
            {
                return;
            }
            writer.Flush();
            writer.Dispose();
            stream.Dispose();
            disposed = true;
        }
    }
}
