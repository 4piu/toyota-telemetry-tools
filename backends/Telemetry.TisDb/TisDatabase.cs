using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.RegularExpressions;

namespace Telemetry.TisDb
{
    /// <summary>
    /// Read-only queries against an installed Techstream database through its
    /// own KgpDataCtrl.dll. Table numbers, record sizes, and field offsets are
    /// what the examined Techstream version uses; each query emits one record
    /// per item, or one record carrying the status when the table has none.
    /// </summary>
    internal sealed class TisDatabase : IDisposable
    {
        internal const short CatalogTable = 0x0110;
        internal const short MonitorTable = 0x023e;
        internal const short ProtocolTable = 0x010d;
        internal const short PhysicalTable = 0x020d;
        internal const short UnitTable = 0x020f;

        // Generous buffers isolate this caller from incomplete class layouts.
        private const int DataCtrlSize = 4096;
        private const int RecordSize = 256;

        private readonly Encoding text;
        private readonly IntPtr module;
        private readonly Native.Destructor dataCtrlDestructor;
        private IntPtr dataCtrl;
        private IntPtr record;

        internal TisDatabase(string databaseDirectory, string binDirectory, string ecuFile, Encoding text)
        {
            if (!Directory.Exists(databaseDirectory))
            {
                throw new DirectoryNotFoundException("Techstream database folder not found: " + databaseDirectory);
            }
            if (!Directory.Exists(binDirectory))
            {
                throw new DirectoryNotFoundException("Techstream bin folder not found: " + binDirectory);
            }
            this.text = text;
            string dll = Path.Combine(binDirectory, "KgpDataCtrl.dll");
            Native.SetDllDirectory(binDirectory);
            module = Native.LoadLibrary(dll);
            if (module == IntPtr.Zero)
            {
                throw new IOException(
                    "Could not load " + dll + " (Win32 error " + Marshal.GetLastWin32Error() + ").");
            }

            var constructor = Native.Export<Native.DataCtrlConstructor>(module, "??0CDataCtrl@@QAE@QBD0@Z");
            dataCtrlDestructor = Native.Export<Native.Destructor>(module, "??1CDataCtrl@@QAE@XZ");
            dataCtrl = Zeroed(DataCtrlSize);
            record = Zeroed(RecordSize);
            string root = databaseDirectory.TrimEnd('\\') + "\\";
            string ecuPath = Path.IsPathRooted(ecuFile) ? ecuFile : Path.Combine(Path.Combine(databaseDirectory, "DB"), ecuFile);
            IntPtr rootString = Marshal.StringToHGlobalAnsi(root);
            IntPtr ecuString = Marshal.StringToHGlobalAnsi(ecuPath);
            try
            {
                constructor(dataCtrl, rootString, ecuString);
            }
            finally
            {
                Marshal.FreeHGlobal(rootString);
                Marshal.FreeHGlobal(ecuString);
            }
            int status = Marshal.ReadInt32(dataCtrl, 0xec);
            if (status != 0)
            {
                throw new IOException(string.Format(CultureInfo.InvariantCulture,
                    "Techstream database constructor failed with status 0x{0:X8}; root {1}; file {2}",
                    status, root, ecuPath));
            }
        }

        private IntPtr DiagToolDb
        {
            get { return IntPtr.Add(dataCtrl, 0xbc); }
        }

        /// <summary>The ECU category/file map (master-file table 0x10).</summary>
        internal IEnumerable<Record> Catalog(Regex namePattern, bool raw)
        {
            var records = new List<Record>();
            WithRecord("CDbEcuCategoryResRecords", delegate
            {
                var get = Native.Export<Native.GetDbRecord0>(
                    module, "?GetDbRecord@CDiagToolDb@@QAEKFPAVCDbResRecordsBase@@@Z");
                var getName = Native.Export<Native.GetString>(
                    module, "?GetEcuName@CDbEcuCategoryResRecords@@QAEPADF@Z");
                var getFile = Native.Export<Native.GetString>(
                    module, "?GetEcuFileName@CDbEcuCategoryResRecords@@QAEPADF@Z");
                uint status = get(DiagToolDb, CatalogTable, record);
                if (status != 0)
                {
                    throw new IOException(string.Format(CultureInfo.InvariantCulture,
                        "ECU catalog query failed with status 0x{0:X8}", status));
                }
                int count = Count();
                for (int index = 0; index < count; index++)
                {
                    string name = Text(getName(record, (short)index));
                    string file = Text(getFile(record, (short)index));
                    if (namePattern != null && !namePattern.IsMatch(name ?? "") && !namePattern.IsMatch(file ?? ""))
                    {
                        continue;
                    }
                    byte[] item = Item(index, 72);
                    var result = new Record()
                        .Add("Index", index).Add("Ecu", name).Add("File", file)
                        .Add("Key", (long)BitConverter.ToUInt32(item, 68));
                    records.Add(raw ? result.Add("Raw", Hex(item)) : result);
                }
            });
            return records;
        }

        /// <summary>Data-monitor records of one ECU key: name, DID, bit range, physical group.</summary>
        internal IEnumerable<Record> Monitors(uint key, short table, Regex namePattern, bool raw)
        {
            var records = new List<Record>();
            WithRecord("CDbDatamonitorP5ResRecords", delegate
            {
                var get = Native.Export<Native.GetDbRecord1>(
                    module, "?GetDbRecord@CDiagToolDb@@QAEKFPAVCDbResRecordsBase@@K@Z");
                var getName = Native.Export<Native.GetString>(
                    module, "?GetDataMonitorName@CDbDatamonitorP5ResRecords@@QAEPADF@Z");
                uint status = get(DiagToolDb, table, record, key);
                int count = Count();
                if (status != 0 || count <= 0)
                {
                    records.Add(Missing(key, status, count).Add("Index", null).Add("Name", null));
                    return;
                }
                for (int index = 0; index < count; index++)
                {
                    string name = Text(getName(record, (short)index));
                    if (namePattern != null && !namePattern.IsMatch(name ?? ""))
                    {
                        continue;
                    }
                    byte[] item = Item(index, 60);
                    uint flags = BitConverter.ToUInt32(item, 0x20);
                    ushort dataId = BitConverter.ToUInt16(item, 0x36);
                    bool direct = (flags & 0x10) == 0;
                    var result = new Record()
                        .Add("Key", (long)key).Add("Status", Status(status)).Add("Count", count)
                        .Add("Index", index).Add("Name", name)
                        .Add("Flags", string.Format(CultureInfo.InvariantCulture, "0x{0:X8}", flags))
                        .Add("InternalId", Hex16(BitConverter.ToUInt16(item, 0x24)))
                        .Add("Acquisition", direct ? "direct DID" : "mapped/dynamic")
                        .Add("ReadRequest", direct
                            ? string.Format(CultureInfo.InvariantCulture, "22 {0:X2} {1:X2}", dataId >> 8, dataId & 0xff)
                            : null)
                        .Add("FirstBit", (long)BitConverter.ToUInt16(item, 0x2c))
                        .Add("LastBit", (long)BitConverter.ToUInt16(item, 0x2e))
                        .Add("PhysicalGroup", Hex16(BitConverter.ToUInt16(item, 0x2a)))
                        .Add("DataId", Hex16(dataId));
                    records.Add(raw ? result.Add("Raw", Hex(item)) : result);
                }
            });
            return records;
        }

        /// <summary>Protocol records of one ECU key: request CAN IDs.</summary>
        internal IEnumerable<Record> Protocol(uint key, bool raw)
        {
            var records = new List<Record>();
            WithRecord("CDbProtInfoResRecords", delegate
            {
                var get = Native.Export<Native.GetDbRecord1>(
                    module, "?GetDbRecord@CDiagToolDb@@QAEKFPAVCDbResRecordsBase@@K@Z");
                uint status = get(DiagToolDb, ProtocolTable, record, key);
                int count = Count();
                if (status != 0 || count <= 0)
                {
                    records.Add(Missing(key, status, count).Add("Index", null));
                    return;
                }
                for (int index = 0; index < count; index++)
                {
                    byte[] item = Item(index, 28);
                    ushort physical = BitConverter.ToUInt16(item, 0x08);
                    var result = new Record()
                        .Add("Key", (long)key).Add("Status", Status(status)).Add("Count", count).Add("Index", index)
                        .Add("ProtocolId", Hex16(BitConverter.ToUInt16(item, 0x02)))
                        .Add("FunctionalRequestCanId", Can(BitConverter.ToUInt16(item, 0x06)))
                        .Add("PhysicalRequestCanId", Can(physical))
                        // Candidate only: tester + 8 is the usual convention, not a database field.
                        .Add("ResponseCanIdCandidate", Can(physical + 8))
                        .Add("ResponseMask", Can(BitConverter.ToUInt16(item, 0x0e)))
                        .Add("RequestMask", Can(BitConverter.ToUInt16(item, 0x10)))
                        .Add("AlternateRequestCanId", Can(BitConverter.ToUInt16(item, 0x14)));
                    records.Add(raw ? result.Add("Raw", Hex(item)) : result);
                }
            });
            return records;
        }

        /// <summary>Physical-conversion groups: coefficients A, B, C, decimal position, unit.</summary>
        internal IEnumerable<Record> Physical(uint key, uint group, bool raw)
        {
            var records = new List<Record>();
            WithRecord("CDbPhyDataResRecords", delegate
            {
                var get = Native.Export<Native.GetDbRecord2>(
                    module, "?GetDbRecord@CDiagToolDb@@QAEKFPAVCDbResRecordsBase@@KK@Z");
                uint status = get(DiagToolDb, PhysicalTable, record, key, group);
                int count = Count();
                var head = new Record().Add("Key", (long)key).Add("PhysicalGroup", Hex16(group))
                    .Add("Status", Status(status)).Add("Count", count);
                if (status != 0 || count <= 0)
                {
                    records.Add(head);
                    return;
                }
                for (int index = 0; index < count; index++)
                {
                    byte[] item = Item(index, 24);
                    var result = new Record().Add("Key", (long)key).Add("PhysicalGroup", Hex16(group))
                        .Add("Status", Status(status)).Add("Count", count).Add("Index", index)
                        .Add("CoefficientA", (long)BitConverter.ToInt32(item, 0))
                        .Add("CoefficientB", (long)BitConverter.ToInt32(item, 4))
                        .Add("CoefficientC", (long)BitConverter.ToInt32(item, 8))
                        .Add("UnitId", Hex16(BitConverter.ToUInt16(item, 0x0e)))
                        .Add("ValueType", string.Format(CultureInfo.InvariantCulture, "0x{0:X2}", item[0x14]))
                        .Add("SignalLsb", (long)item[0x15]);
                    records.Add(raw ? result.Add("Raw", Hex(item)) : result);
                }
            });
            return records;
        }

        /// <summary>The default display string of a unit ID.</summary>
        internal IEnumerable<Record> Unit(uint key, uint unitId)
        {
            var records = new List<Record>();
            WithRecord("CDbUnitResRecords", delegate
            {
                var get = Native.Export<Native.GetDbRecord2>(
                    module, "?GetDbRecord@CDiagToolDb@@QAEKFPAVCDbResRecordsBase@@KK@Z");
                var getUnit = Native.Export<Native.GetString>(
                    module, "?GetDefaultUnitStr@CDbUnitResRecords@@QAEPADF@Z");
                uint status = get(DiagToolDb, UnitTable, record, key, unitId);
                int count = Count();
                string unit = status == 0 && count > 0 ? Text(getUnit(record, 0)) : null;
                records.Add(new Record().Add("Key", (long)key).Add("UnitId", Hex16(unitId))
                    .Add("Status", Status(status)).Add("Count", count).Add("Unit", unit));
            });
            return records;
        }

        public void Dispose()
        {
            if (dataCtrl != IntPtr.Zero)
            {
                dataCtrlDestructor(dataCtrl);
                Marshal.FreeHGlobal(dataCtrl);
                dataCtrl = IntPtr.Zero;
            }
            if (record != IntPtr.Zero)
            {
                Marshal.FreeHGlobal(record);
                record = IntPtr.Zero;
            }
            Native.FreeLibrary(module);
        }

        private void WithRecord(string recordClass, Action query)
        {
            for (int offset = 0; offset < RecordSize; offset += 4)
            {
                Marshal.WriteInt32(record, offset, 0);
            }
            var constructor = Native.Export<Native.RecordConstructor>(module, "??0" + recordClass + "@@QAE@XZ");
            var destructor = Native.Export<Native.Destructor>(module, "??1" + recordClass + "@@UAE@XZ");
            constructor(record);
            try
            {
                query();
            }
            finally
            {
                destructor(record);
            }
        }

        private int Count()
        {
            return Marshal.ReadInt16(record, 4);
        }

        private byte[] Item(int index, int size)
        {
            IntPtr items = new IntPtr(Marshal.ReadInt32(record, 12));
            IntPtr item = new IntPtr(Marshal.ReadInt32(items, index * 4));
            var bytes = new byte[size];
            Marshal.Copy(item, bytes, 0, size);
            return bytes;
        }

        /// <summary>
        /// Decode a NUL-terminated native string with the chosen code page, so
        /// the text is a property of the database, not of the reading machine.
        /// </summary>
        private string Text(IntPtr pointer)
        {
            if (pointer == IntPtr.Zero)
            {
                return null;
            }
            int length = 0;
            while (Marshal.ReadByte(pointer, length) != 0)
            {
                length++;
            }
            var bytes = new byte[length];
            Marshal.Copy(pointer, bytes, 0, length);
            return text.GetString(bytes);
        }

        private static Record Missing(uint key, uint status, int count)
        {
            return new Record().Add("Key", (long)key).Add("Status", Status(status)).Add("Count", count);
        }

        private static IntPtr Zeroed(int size)
        {
            IntPtr memory = Marshal.AllocHGlobal(size);
            for (int offset = 0; offset < size; offset += 4)
            {
                Marshal.WriteInt32(memory, offset, 0);
            }
            return memory;
        }

        internal static string Status(uint status)
        {
            return string.Format(CultureInfo.InvariantCulture, "0x{0:X8}", status);
        }

        internal static string Hex16(uint value)
        {
            return string.Format(CultureInfo.InvariantCulture, "0x{0:X4}", value);
        }

        internal static string Can(int value)
        {
            return string.Format(CultureInfo.InvariantCulture, "0x{0:X3}", value);
        }

        internal static string Hex(byte[] bytes)
        {
            return BitConverter.ToString(bytes).Replace('-', ' ');
        }
    }
}
