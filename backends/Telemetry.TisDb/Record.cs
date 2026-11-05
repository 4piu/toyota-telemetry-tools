using System;
using System.Collections.Generic;
using System.Globalization;
using System.Text;

namespace Telemetry.TisDb
{
    /// <summary>One output record: ordered fields written as one JSON line.</summary>
    internal sealed class Record
    {
        private readonly List<KeyValuePair<string, object>> fields = new List<KeyValuePair<string, object>>();

        internal Record Add(string name, object value)
        {
            fields.Add(new KeyValuePair<string, object>(name, value));
            return this;
        }

        internal string ToJson()
        {
            var json = new StringBuilder("{");
            for (int index = 0; index < fields.Count; index++)
            {
                if (index > 0)
                {
                    json.Append(',');
                }
                Quote(json, fields[index].Key);
                json.Append(':');
                object value = fields[index].Value;
                if (value == null)
                {
                    json.Append("null");
                }
                else if (value is string)
                {
                    Quote(json, (string)value);
                }
                else if (value is int || value is long)
                {
                    json.Append(Convert.ToInt64(value, CultureInfo.InvariantCulture).ToString(CultureInfo.InvariantCulture));
                }
                else
                {
                    throw new ArgumentException("unsupported field type: " + value.GetType().Name);
                }
            }
            return json.Append('}').ToString();
        }

        private static void Quote(StringBuilder json, string text)
        {
            json.Append('"');
            foreach (char c in text)
            {
                switch (c)
                {
                    case '"': json.Append("\\\""); break;
                    case '\\': json.Append("\\\\"); break;
                    case '\n': json.Append("\\n"); break;
                    case '\r': json.Append("\\r"); break;
                    case '\t': json.Append("\\t"); break;
                    default:
                        if (c < 0x20)
                        {
                            json.Append("\\u").Append(((int)c).ToString("x4", CultureInfo.InvariantCulture));
                        }
                        else
                        {
                            json.Append(c);
                        }
                        break;
                }
            }
            json.Append('"');
        }
    }
}
