using System;
using System.ComponentModel;
using System.Runtime.InteropServices;

namespace Telemetry.TisDb
{
    /// <summary>
    /// Entry points of Techstream's 32-bit KgpDataCtrl.dll. The exports are
    /// MSVC C++ methods (thiscall); their names and the object layouts used
    /// by <see cref="TisDatabase"/> are observed implementation details of
    /// the examined Techstream version, not a published interface.
    /// </summary>
    internal static class Native
    {
        [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Ansi)]
        internal static extern IntPtr LoadLibrary(string path);

        [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Ansi)]
        [return: MarshalAs(UnmanagedType.Bool)]
        internal static extern bool SetDllDirectory(string path);

        [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Ansi)]
        internal static extern IntPtr GetProcAddress(IntPtr module, string name);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        internal static extern bool FreeLibrary(IntPtr module);

        // The system code page for non-Unicode programs.
        [DllImport("kernel32.dll")]
        internal static extern uint GetACP();

        [UnmanagedFunctionPointer(CallingConvention.ThisCall)]
        internal delegate IntPtr DataCtrlConstructor(IntPtr self, IntPtr dbDirectory, IntPtr ecuFile);

        [UnmanagedFunctionPointer(CallingConvention.ThisCall)]
        internal delegate void Destructor(IntPtr self);

        [UnmanagedFunctionPointer(CallingConvention.ThisCall)]
        internal delegate IntPtr RecordConstructor(IntPtr self);

        [UnmanagedFunctionPointer(CallingConvention.ThisCall)]
        internal delegate uint GetDbRecord0(IntPtr self, short table, IntPtr record);

        [UnmanagedFunctionPointer(CallingConvention.ThisCall)]
        internal delegate uint GetDbRecord1(IntPtr self, short table, IntPtr record, uint key1);

        [UnmanagedFunctionPointer(CallingConvention.ThisCall)]
        internal delegate uint GetDbRecord2(IntPtr self, short table, IntPtr record, uint key1, uint key2);

        [UnmanagedFunctionPointer(CallingConvention.ThisCall)]
        internal delegate IntPtr GetString(IntPtr self, short index);

        internal static T Export<T>(IntPtr module, string name) where T : class
        {
            IntPtr address = GetProcAddress(module, name);
            if (address == IntPtr.Zero)
            {
                throw new Win32Exception(Marshal.GetLastWin32Error(), "GetProcAddress(" + name + ")");
            }
            return (T)(object)Marshal.GetDelegateForFunctionPointer(address, typeof(T));
        }
    }
}
