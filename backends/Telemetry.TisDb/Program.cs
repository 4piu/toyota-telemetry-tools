using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Text;
using System.Text.RegularExpressions;

namespace Telemetry.TisDb
{
    /// <summary>
    /// telemetry-tisdb: read-only queries of an installed Techstream (TIS)
    /// database, one JSON record per line on stdout. It is 32-bit because
    /// Techstream's KgpDataCtrl.dll is. It never touches a vehicle.
    /// </summary>
    internal static class Program
    {
        private const string Usage =
            "telemetry-tisdb - read-only Techstream database queries (JSON Lines)\n" +
            "\n" +
            "Usage:\n" +
            "  telemetry-tisdb --self-test\n" +
            "  telemetry-tisdb --database DIR [options] --catalog\n" +
            "  telemetry-tisdb --database DIR [options] --keys K[,K] --monitors [--table N]\n" +
            "  telemetry-tisdb --database DIR [options] --keys K[,K] --protocol\n" +
            "  telemetry-tisdb --database DIR [options] --keys K[,K] --p4-monitors\n" +
            "  telemetry-tisdb --database DIR [options] --keys K[,K] --physical G[,G]\n" +
            "  telemetry-tisdb --database DIR [options] --keys K[,K] --units U[,U]\n" +
            "  telemetry-tisdb --database DIR [options] --keys K[,K] --states S[,S]\n" +
            "\n" +
            "Options:\n" +
            "  --bin DIR          Techstream bin folder (default: DIR\\..\\bin)\n" +
            "  --ecu-file NAME    ECU text database (default: M_English.ddb)\n" +
            "  --code-page N      text code page (default 932; 0 = system ANSI)\n" +
            "  --name-pattern RE  only names (or catalog files) matching RE\n" +
            "  --max-items N      stop after N items per key\n" +
            "  --raw              include each item's raw bytes\n";

        private static int Main(string[] args)
        {
            Console.OutputEncoding = new UTF8Encoding(false);
            try
            {
                return Run(args);
            }
            catch (ArgumentException exc)
            {
                Console.Error.WriteLine("usage error: " + exc.Message);
                Console.Error.Write(Usage);
                return 2;
            }
            catch (Exception exc)
            {
                Console.Error.WriteLine("ERROR: " + exc.Message);
                return 1;
            }
        }

        private static int Run(string[] args)
        {
            if (args.Length == 1 && args[0] == "--self-test")
            {
                SelfTest();
                Console.WriteLine("Argument, record, and text self-checks passed; no database was opened.");
                return 0;
            }
            if (args.Length == 1 && (args[0] == "--help" || args[0] == "-h"))
            {
                Console.Write(Usage);
                return 0;
            }
            Options options = Options.Parse(args);
            Encoding text = TextEncoding(options.CodePage);
            using (var database = new TisDatabase(options.Database, options.Bin, options.EcuFile, text))
            {
                foreach (Record record in Query(database, options))
                {
                    Console.WriteLine(record.ToJson());
                }
            }
            return 0;
        }

        private static IEnumerable<Record> Query(TisDatabase database, Options options)
        {
            if (options.Mode == "catalog")
            {
                return Limit(database.Catalog(options.NamePattern, options.Raw), options.MaxItems);
            }
            var records = new List<Record>();
            foreach (uint key in options.Keys)
            {
                switch (options.Mode)
                {
                    case "monitors":
                        records.AddRange(Limit(database.Monitors(key, options.Table, options.NamePattern, options.Raw), options.MaxItems));
                        break;
                    case "protocol":
                        records.AddRange(Limit(database.Protocol(key, options.Raw), options.MaxItems));
                        break;
                    case "p4-monitors":
                        records.AddRange(Limit(database.Phase4Monitors(key, options.NamePattern, options.Raw), options.MaxItems));
                        break;
                    case "physical":
                        foreach (uint group in options.Values)
                        {
                            records.AddRange(Limit(database.Physical(key, group, options.Raw), options.MaxItems));
                        }
                        break;
                    case "units":
                        foreach (uint unit in options.Values)
                        {
                            records.AddRange(database.Unit(key, unit));
                        }
                        break;
                    case "states":
                        foreach (uint stateSet in options.Values)
                        {
                            records.AddRange(Limit(database.States(key, stateSet, options.Raw), options.MaxItems));
                        }
                        break;
                }
            }
            return records;
        }

        private static IEnumerable<Record> Limit(IEnumerable<Record> records, int maxItems)
        {
            int emitted = 0;
            foreach (Record record in records)
            {
                if (maxItems > 0 && emitted >= maxItems)
                {
                    yield break;
                }
                emitted++;
                yield return record;
            }
        }

        internal static Encoding TextEncoding(int codePage)
        {
            int effective = codePage <= 0 ? (int)Native.GetACP() : codePage;
            try
            {
                return Encoding.GetEncoding(effective);
            }
            catch (Exception exc)
            {
                throw new IOException("Code page " + effective + " is not available here: " + exc.Message);
            }
        }

        private static void SelfTest()
        {
            Check(new Record().Add("A", 1).Add("B", "x\"\\\n\u0001").Add("C", null).ToJson()
                  == "{\"A\":1,\"B\":\"x\\\"\\\\\\n\\u0001\",\"C\":null}", "record JSON");
            // The databases store text in code page 932: the degree sign is 0x81 0x8B.
            Check(TextEncoding(932).GetString(new byte[] { 0x81, 0x8B }) == "\u00B0", "code page 932 text");
            Options parsed = Options.Parse(new[] {
                "--database", @"C:\TS\NA", "--keys", "372, 0x18F", "--physical", "0x20,7", "--max-items", "3" });
            Check(parsed.Mode == "physical" && parsed.Keys.Count == 2 && parsed.Keys[1] == 0x18F
                  && parsed.Values[0] == 0x20 && parsed.Values[1] == 7 && parsed.MaxItems == 3
                  && parsed.Bin == @"C:\TS\bin" && parsed.Table == TisDatabase.MonitorTable, "argument parsing");
            Options states = Options.Parse(new[] { "--database", "x", "--keys", "397", "--states", "7,0xC0" });
            Check(states.Mode == "states" && states.Values.Count == 2 && states.Values[1] == 0xC0, "states arguments");
            Expect(new[] { "--database", "x", "--monitors" }, "monitors without keys");
            Expect(new[] { "--database", "x", "--catalog", "--protocol" }, "two query modes");
            Expect(new[] { "--database", "x", "--catalog", "--bogus" }, "unknown option");
        }

        private static void Expect(string[] args, string what)
        {
            try
            {
                Options.Parse(args);
            }
            catch (ArgumentException)
            {
                return;
            }
            throw new InvalidOperationException("self-test failed: accepted " + what);
        }

        private static void Check(bool ok, string what)
        {
            if (!ok)
            {
                throw new InvalidOperationException("self-test failed: " + what);
            }
        }
    }

    internal sealed class Options
    {
        internal string Database;
        internal string Bin;
        internal string EcuFile = "M_English.ddb";
        internal int CodePage = 932;
        internal string Mode;
        internal List<uint> Keys = new List<uint>();
        internal List<uint> Values = new List<uint>();
        internal short Table = TisDatabase.MonitorTable;
        internal Regex NamePattern;
        internal int MaxItems;
        internal bool Raw;

        internal static Options Parse(string[] args)
        {
            var options = new Options();
            for (int index = 0; index < args.Length; index++)
            {
                string name = args[index];
                switch (name)
                {
                    case "--database": options.Database = Value(args, ref index); break;
                    case "--bin": options.Bin = Value(args, ref index); break;
                    case "--ecu-file": options.EcuFile = Value(args, ref index); break;
                    case "--code-page": options.CodePage = (int)Number(Value(args, ref index)); break;
                    case "--keys": options.Keys = Numbers(Value(args, ref index)); break;
                    case "--table": options.Table = (short)Number(Value(args, ref index)); break;
                    case "--name-pattern": options.NamePattern = new Regex(Value(args, ref index)); break;
                    case "--max-items": options.MaxItems = (int)Number(Value(args, ref index)); break;
                    case "--raw": options.Raw = true; break;
                    case "--catalog": options.SetMode("catalog"); break;
                    case "--monitors": options.SetMode("monitors"); break;
                    case "--protocol": options.SetMode("protocol"); break;
                    case "--p4-monitors": options.SetMode("p4-monitors"); break;
                    case "--physical": options.SetMode("physical"); options.Values = Numbers(Value(args, ref index)); break;
                    case "--units": options.SetMode("units"); options.Values = Numbers(Value(args, ref index)); break;
                    case "--states": options.SetMode("states"); options.Values = Numbers(Value(args, ref index)); break;
                    default: throw new ArgumentException("unknown argument: " + name);
                }
            }
            if (options.Database == null)
            {
                throw new ArgumentException("--database is required");
            }
            if (options.Mode == null)
            {
                throw new ArgumentException(
                    "choose one query: --catalog, --monitors, --protocol, --physical, --units, --states");
            }
            if (options.Mode != "catalog" && options.Keys.Count == 0)
            {
                throw new ArgumentException("--" + options.Mode + " needs --keys");
            }
            if (options.Bin == null)
            {
                options.Bin = Path.Combine(Path.GetDirectoryName(options.Database.TrimEnd('\\', '/')) ?? "", "bin");
            }
            return options;
        }

        private void SetMode(string mode)
        {
            if (Mode != null)
            {
                throw new ArgumentException("only one query per run (--" + Mode + " and --" + mode + ")");
            }
            Mode = mode;
        }

        private static string Value(string[] args, ref int index)
        {
            if (index + 1 >= args.Length)
            {
                throw new ArgumentException(args[index] + " needs a value");
            }
            return args[++index];
        }

        private static List<uint> Numbers(string text)
        {
            var values = new List<uint>();
            foreach (string part in text.Split(','))
            {
                if (part.Trim().Length > 0)
                {
                    values.Add(Number(part));
                }
            }
            if (values.Count == 0)
            {
                throw new ArgumentException("expected a comma-separated list of numbers: " + text);
            }
            return values;
        }

        private static uint Number(string text)
        {
            string value = text.Trim();
            uint number;
            bool ok = value.StartsWith("0x", StringComparison.OrdinalIgnoreCase)
                ? uint.TryParse(value.Substring(2), NumberStyles.HexNumber, CultureInfo.InvariantCulture, out number)
                : uint.TryParse(value, NumberStyles.None, CultureInfo.InvariantCulture, out number);
            if (!ok)
            {
                throw new ArgumentException("not a number: " + text);
            }
            return number;
        }
    }
}
