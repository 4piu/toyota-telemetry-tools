// Minimal strict JSON reader.
//
// Deliberately hand-written rather than pulled from a package: this program
// transmits on a vehicle bus, ships without an SDK install (csc.exe only), and
// its configuration surface is small enough that an explicit, auditable parser
// is preferable to a general-purpose one. Supports objects, arrays, strings,
// integers, booleans, and null. Floating point is rejected because no
// configuration field needs it.
using System;
using System.Collections.Generic;
using System.Globalization;
using System.Text;

namespace Telemetry.Can.Core
{
    internal enum JsonKind
    {
        Object,
        Array,
        String,
        Integer,
        Boolean,
        Null
    }

    internal sealed class JsonValue
    {
        internal readonly JsonKind Kind;
        internal readonly Dictionary<string, JsonValue> Members;
        internal readonly List<JsonValue> Items;
        internal readonly string Text;
        internal readonly long Integer;
        internal readonly bool Boolean;

        private JsonValue(
            JsonKind kind,
            Dictionary<string, JsonValue> members,
            List<JsonValue> items,
            string text,
            long integer,
            bool boolean)
        {
            Kind = kind;
            Members = members;
            Items = items;
            Text = text;
            Integer = integer;
            Boolean = boolean;
        }

        internal static JsonValue NewObject(Dictionary<string, JsonValue> members)
        {
            return new JsonValue(JsonKind.Object, members, null, null, 0, false);
        }

        internal static JsonValue NewArray(List<JsonValue> items)
        {
            return new JsonValue(JsonKind.Array, null, items, null, 0, false);
        }

        internal static JsonValue NewString(string text)
        {
            return new JsonValue(JsonKind.String, null, null, text, 0, false);
        }

        internal static JsonValue NewInteger(long value)
        {
            return new JsonValue(JsonKind.Integer, null, null, null, value, false);
        }

        internal static JsonValue NewBoolean(bool value)
        {
            return new JsonValue(JsonKind.Boolean, null, null, null, 0, value);
        }

        internal static JsonValue NewNull()
        {
            return new JsonValue(JsonKind.Null, null, null, null, 0, false);
        }

        // ---- strict typed access; every failure names the path that failed ----

        internal JsonValue Member(string path, string name)
        {
            if (Kind != JsonKind.Object)
            {
                throw new FormatException(Where(path) + " must be an object.");
            }
            JsonValue value;
            if (!Members.TryGetValue(name, out value))
            {
                throw new FormatException(Where(path) + " is missing required field '" + name + "'.");
            }
            return value;
        }

        internal bool TryMember(string name, out JsonValue value)
        {
            value = null;
            return Kind == JsonKind.Object && Members.TryGetValue(name, out value);
        }

        internal string AsString(string path)
        {
            if (Kind != JsonKind.String)
            {
                throw new FormatException(Where(path) + " must be a string.");
            }
            return Text;
        }

        internal long AsInteger(string path)
        {
            if (Kind != JsonKind.Integer)
            {
                throw new FormatException(Where(path) + " must be an integer.");
            }
            return Integer;
        }

        internal int AsInteger(string path, int minimum, int maximum)
        {
            long value = AsInteger(path);
            if (value < minimum || value > maximum)
            {
                throw new FormatException(
                    Where(path) + " must be " +
                    minimum.ToString(CultureInfo.InvariantCulture) + ".." +
                    maximum.ToString(CultureInfo.InvariantCulture) + ".");
            }
            return (int)value;
        }

        internal bool AsBoolean(string path)
        {
            if (Kind != JsonKind.Boolean)
            {
                throw new FormatException(Where(path) + " must be true or false.");
            }
            return Boolean;
        }

        internal List<JsonValue> AsArray(string path, int minimumCount, int maximumCount)
        {
            if (Kind != JsonKind.Array)
            {
                throw new FormatException(Where(path) + " must be an array.");
            }
            if (Items.Count < minimumCount || Items.Count > maximumCount)
            {
                throw new FormatException(
                    Where(path) + " must contain " +
                    minimumCount.ToString(CultureInfo.InvariantCulture) + ".." +
                    maximumCount.ToString(CultureInfo.InvariantCulture) + " entries.");
            }
            return Items;
        }

        /// <summary>Reject any field the current schema does not define, so a
        /// typo in a safety-relevant config is an error rather than a default.</summary>
        internal void RejectUnknownMembers(string path, params string[] allowed)
        {
            if (Kind != JsonKind.Object)
            {
                throw new FormatException(Where(path) + " must be an object.");
            }
            foreach (KeyValuePair<string, JsonValue> member in Members)
            {
                bool known = false;
                foreach (string name in allowed)
                {
                    if (String.Equals(member.Key, name, StringComparison.Ordinal))
                    {
                        known = true;
                        break;
                    }
                }
                if (!known)
                {
                    throw new FormatException(
                        Where(path) + " contains unknown field '" + member.Key + "'.");
                }
            }
        }

        private static string Where(string path)
        {
            return String.IsNullOrEmpty(path) ? "the document root" : "'" + path + "'";
        }
    }

    internal static class JsonReader
    {
        internal static JsonValue Parse(string text)
        {
            if (text == null)
            {
                throw new FormatException("JSON document is empty.");
            }
            int index = 0;
            // A UTF-8 BOM survives File.ReadAllText on some encodings.
            if (text.Length != 0 && text[0] == '﻿')
            {
                index = 1;
            }
            JsonValue value = ParseValue(text, ref index, 0);
            SkipWhitespace(text, ref index);
            if (index != text.Length)
            {
                throw new FormatException(
                    "Unexpected trailing content at offset " +
                    index.ToString(CultureInfo.InvariantCulture) + ".");
            }
            return value;
        }

        private static JsonValue ParseValue(string text, ref int index, int depth)
        {
            if (depth > 32)
            {
                throw new FormatException("JSON nesting is deeper than 32 levels.");
            }
            SkipWhitespace(text, ref index);
            if (index >= text.Length)
            {
                throw new FormatException("Unexpected end of JSON document.");
            }
            char c = text[index];
            switch (c)
            {
                case '{': return ParseObject(text, ref index, depth);
                case '[': return ParseArray(text, ref index, depth);
                case '"': return JsonValue.NewString(ParseString(text, ref index));
                case 't':
                    Expect(text, ref index, "true");
                    return JsonValue.NewBoolean(true);
                case 'f':
                    Expect(text, ref index, "false");
                    return JsonValue.NewBoolean(false);
                case 'n':
                    Expect(text, ref index, "null");
                    return JsonValue.NewNull();
                default:
                    return JsonValue.NewInteger(ParseInteger(text, ref index));
            }
        }

        private static JsonValue ParseObject(string text, ref int index, int depth)
        {
            index++; // '{'
            Dictionary<string, JsonValue> members =
                new Dictionary<string, JsonValue>(StringComparer.Ordinal);
            SkipWhitespace(text, ref index);
            if (index < text.Length && text[index] == '}')
            {
                index++;
                return JsonValue.NewObject(members);
            }
            while (true)
            {
                SkipWhitespace(text, ref index);
                if (index >= text.Length || text[index] != '"')
                {
                    throw new FormatException(
                        "Expected an object member name at offset " +
                        index.ToString(CultureInfo.InvariantCulture) + ".");
                }
                string name = ParseString(text, ref index);
                if (members.ContainsKey(name))
                {
                    throw new FormatException("Duplicate object member '" + name + "'.");
                }
                SkipWhitespace(text, ref index);
                if (index >= text.Length || text[index] != ':')
                {
                    throw new FormatException("Expected ':' after member '" + name + "'.");
                }
                index++;
                members[name] = ParseValue(text, ref index, depth + 1);
                SkipWhitespace(text, ref index);
                if (index >= text.Length)
                {
                    throw new FormatException("Unterminated JSON object.");
                }
                if (text[index] == ',')
                {
                    index++;
                    continue;
                }
                if (text[index] == '}')
                {
                    index++;
                    return JsonValue.NewObject(members);
                }
                throw new FormatException(
                    "Expected ',' or '}' at offset " +
                    index.ToString(CultureInfo.InvariantCulture) + ".");
            }
        }

        private static JsonValue ParseArray(string text, ref int index, int depth)
        {
            index++; // '['
            List<JsonValue> items = new List<JsonValue>();
            SkipWhitespace(text, ref index);
            if (index < text.Length && text[index] == ']')
            {
                index++;
                return JsonValue.NewArray(items);
            }
            while (true)
            {
                items.Add(ParseValue(text, ref index, depth + 1));
                SkipWhitespace(text, ref index);
                if (index >= text.Length)
                {
                    throw new FormatException("Unterminated JSON array.");
                }
                if (text[index] == ',')
                {
                    index++;
                    continue;
                }
                if (text[index] == ']')
                {
                    index++;
                    return JsonValue.NewArray(items);
                }
                throw new FormatException(
                    "Expected ',' or ']' at offset " +
                    index.ToString(CultureInfo.InvariantCulture) + ".");
            }
        }

        private static string ParseString(string text, ref int index)
        {
            index++; // opening quote
            StringBuilder builder = new StringBuilder();
            while (true)
            {
                if (index >= text.Length)
                {
                    throw new FormatException("Unterminated JSON string.");
                }
                char c = text[index++];
                if (c == '"')
                {
                    return builder.ToString();
                }
                if (c != '\\')
                {
                    if (c < 0x20)
                    {
                        throw new FormatException("Unescaped control character in JSON string.");
                    }
                    builder.Append(c);
                    continue;
                }
                if (index >= text.Length)
                {
                    throw new FormatException("Unterminated JSON escape.");
                }
                char escape = text[index++];
                switch (escape)
                {
                    case '"': builder.Append('"'); break;
                    case '\\': builder.Append('\\'); break;
                    case '/': builder.Append('/'); break;
                    case 'b': builder.Append('\b'); break;
                    case 'f': builder.Append('\f'); break;
                    case 'n': builder.Append('\n'); break;
                    case 'r': builder.Append('\r'); break;
                    case 't': builder.Append('\t'); break;
                    case 'u':
                        if (index + 4 > text.Length)
                        {
                            throw new FormatException("Truncated \\u escape in JSON string.");
                        }
                        int code;
                        if (!Int32.TryParse(
                                text.Substring(index, 4),
                                NumberStyles.HexNumber,
                                CultureInfo.InvariantCulture,
                                out code))
                        {
                            throw new FormatException("Invalid \\u escape in JSON string.");
                        }
                        builder.Append((char)code);
                        index += 4;
                        break;
                    default:
                        throw new FormatException("Unsupported JSON escape '\\" + escape + "'.");
                }
            }
        }

        private static long ParseInteger(string text, ref int index)
        {
            int start = index;
            if (index < text.Length && text[index] == '-')
            {
                index++;
            }
            int digits = 0;
            while (index < text.Length && text[index] >= '0' && text[index] <= '9')
            {
                index++;
                digits++;
            }
            if (digits == 0)
            {
                throw new FormatException(
                    "Expected a JSON value at offset " +
                    start.ToString(CultureInfo.InvariantCulture) + ".");
            }
            if (index < text.Length && (text[index] == '.' || text[index] == 'e' || text[index] == 'E'))
            {
                throw new FormatException(
                    "Fractional and exponent numbers are not accepted; use an integer at offset " +
                    start.ToString(CultureInfo.InvariantCulture) + ".");
            }
            long value;
            if (!Int64.TryParse(
                    text.Substring(start, index - start),
                    NumberStyles.AllowLeadingSign,
                    CultureInfo.InvariantCulture,
                    out value))
            {
                throw new FormatException("Integer out of range in JSON document.");
            }
            return value;
        }

        private static void Expect(string text, ref int index, string literal)
        {
            if (index + literal.Length > text.Length ||
                String.CompareOrdinal(text, index, literal, 0, literal.Length) != 0)
            {
                throw new FormatException(
                    "Expected '" + literal + "' at offset " +
                    index.ToString(CultureInfo.InvariantCulture) + ".");
            }
            index += literal.Length;
        }

        private static void SkipWhitespace(string text, ref int index)
        {
            while (index < text.Length)
            {
                char c = text[index];
                if (c == ' ' || c == '\t' || c == '\r' || c == '\n')
                {
                    index++;
                    continue;
                }
                return;
            }
        }
    }
}
