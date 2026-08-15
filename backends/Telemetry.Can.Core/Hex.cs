using System;
using System.Collections.Generic;
using System.Globalization;
using System.Text;

namespace Telemetry.Can.Core
{
    /// <summary>Byte/hex/JSON-escaping helpers shared by every backend.</summary>
    internal static class Hex
    {
        /// <summary>Parse "22 F3 01" or "22F301" into bytes. Strict: rejects
        /// odd digit counts and any non-hex character.</summary>
        internal static byte[] Parse(string text, string context)
        {
            if (text == null)
            {
                throw new FormatException(context + " must be a hexadecimal byte string.");
            }
            StringBuilder digits = new StringBuilder(text.Length);
            foreach (char c in text)
            {
                if (c == ' ' || c == '-' || c == ':')
                {
                    continue;
                }
                bool isHex = (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F');
                if (!isHex)
                {
                    throw new FormatException(
                        context + " contains a non-hexadecimal character '" + c + "'.");
                }
                digits.Append(c);
            }
            if (digits.Length == 0 || digits.Length % 2 != 0)
            {
                throw new FormatException(
                    context + " must contain a whole number of hexadecimal bytes.");
            }
            byte[] result = new byte[digits.Length / 2];
            for (int i = 0; i < result.Length; i++)
            {
                result[i] = Byte.Parse(
                    digits.ToString(i * 2, 2), NumberStyles.HexNumber, CultureInfo.InvariantCulture);
            }
            return result;
        }

        /// <summary>Parse "0x7D2" or "7D2" into a CAN identifier or DID.</summary>
        internal static uint ParseId(string text, string context, uint minimum, uint maximum)
        {
            if (text == null)
            {
                throw new FormatException(context + " must be a hexadecimal identifier.");
            }
            string trimmed = text.Trim();
            if (trimmed.StartsWith("0x", StringComparison.OrdinalIgnoreCase))
            {
                trimmed = trimmed.Substring(2);
            }
            uint value;
            if (trimmed.Length == 0 ||
                !UInt32.TryParse(trimmed, NumberStyles.HexNumber, CultureInfo.InvariantCulture, out value))
            {
                throw new FormatException(context + " is not a hexadecimal identifier: " + text);
            }
            if (value < minimum || value > maximum)
            {
                throw new FormatException(
                    context + " must be 0x" + minimum.ToString("X", CultureInfo.InvariantCulture) +
                    "..0x" + maximum.ToString("X", CultureInfo.InvariantCulture) + ".");
            }
            return value;
        }

        internal static string Format(byte[] data, int length)
        {
            if (data == null)
            {
                return "<null>";
            }
            int count = Math.Min(length, data.Length);
            StringBuilder text = new StringBuilder(count * 3);
            for (int i = 0; i < count; i++)
            {
                if (i != 0)
                {
                    text.Append(' ');
                }
                text.Append(data[i].ToString("X2", CultureInfo.InvariantCulture));
            }
            return text.ToString();
        }

        internal static string Format(byte[] data)
        {
            return Format(data, data == null ? 0 : data.Length);
        }

        internal static bool Equal(byte[] left, byte[] right)
        {
            if (left == null || right == null || left.Length != right.Length)
            {
                return false;
            }
            for (int i = 0; i < left.Length; i++)
            {
                if (left[i] != right[i])
                {
                    return false;
                }
            }
            return true;
        }

        internal static bool StartsWith(byte[] data, byte[] prefix)
        {
            if (data == null || prefix == null || data.Length < prefix.Length)
            {
                return false;
            }
            for (int i = 0; i < prefix.Length; i++)
            {
                if (data[i] != prefix[i])
                {
                    return false;
                }
            }
            return true;
        }

        internal static byte[] Slice(byte[] source, int offset, int count)
        {
            byte[] result = new byte[count];
            Buffer.BlockCopy(source, offset, result, 0, count);
            return result;
        }

        internal static string JsonEscape(string value)
        {
            if (value == null)
            {
                return String.Empty;
            }
            StringBuilder result = new StringBuilder(value.Length + 8);
            foreach (char character in value)
            {
                switch (character)
                {
                    case '\\': result.Append("\\\\"); break;
                    case '"': result.Append("\\\""); break;
                    case '\b': result.Append("\\b"); break;
                    case '\f': result.Append("\\f"); break;
                    case '\n': result.Append("\\n"); break;
                    case '\r': result.Append("\\r"); break;
                    case '\t': result.Append("\\t"); break;
                    default:
                        if (character < 0x20)
                        {
                            result.Append("\\u");
                            result.Append(((int)character).ToString("X4", CultureInfo.InvariantCulture));
                        }
                        else
                        {
                            result.Append(character);
                        }
                        break;
                }
            }
            return result.ToString();
        }

        internal static string FormatDid(int did)
        {
            return "0x" + did.ToString("X4", CultureInfo.InvariantCulture);
        }

        /// <summary>Expand a UDS payload into the exact Classical-CAN frames it
        /// will occupy. Used both to transmit and to build the frame-level
        /// allowlist, so the two can never disagree.</summary>
        internal static IEnumerable<byte[]> IsoTpFrames(byte[] payload)
        {
            if (payload.Length <= 7)
            {
                byte[] single = new byte[8];
                single[0] = (byte)payload.Length;
                Buffer.BlockCopy(payload, 0, single, 1, payload.Length);
                yield return single;
                yield break;
            }
            if (payload.Length > 4095)
            {
                throw new InvalidOperationException(
                    "ISO-TP payload exceeds the Classical-CAN 12-bit length limit.");
            }

            byte[] first = new byte[8];
            first[0] = (byte)(0x10 | ((payload.Length >> 8) & 0x0F));
            first[1] = (byte)(payload.Length & 0xFF);
            Buffer.BlockCopy(payload, 0, first, 2, 6);
            yield return first;

            int offset = 6;
            int sequence = 1;
            while (offset < payload.Length)
            {
                byte[] consecutive = new byte[8];
                consecutive[0] = (byte)(0x20 | (sequence & 0x0F));
                int count = Math.Min(7, payload.Length - offset);
                Buffer.BlockCopy(payload, offset, consecutive, 1, count);
                yield return consecutive;
                offset += count;
                sequence = (sequence + 1) & 0x0F;
            }
        }
    }
}
