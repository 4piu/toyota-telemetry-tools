// Minimal J2534 (04.04) PassThru API binding, loaded dynamically by path so the
// caller controls which vendor DLL and process architecture (x86/x64) is used.
// Vehicle-agnostic hardware binding only: no vehicle, ECU, or DID content.
// A platform project vendors (copies) this file into its own compiled tool and
// layers protocol selection, filters, allowlists, and sessions on top.
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;

namespace Telemetry.Adapters.J2534
{
    [StructLayout(LayoutKind.Sequential, Pack = 4)]
    internal struct PassThruMsg
    {
        internal uint ProtocolId;
        internal uint RxStatus;
        internal uint TxFlags;
        internal uint Timestamp;
        internal uint DataSize;
        internal uint ExtraDataIndex;

        [MarshalAs(UnmanagedType.ByValArray, SizeConst = 4128)]
        internal byte[] Data;
    }

    /// <summary>
    /// Loads a J2534 vendor DLL by path and exposes the PassThru functions
    /// needed for an open/connect/filter/read/write/disconnect/close session.
    /// Falls back to the stdcall name-decoration form some vendor DLLs export
    /// instead of an undecorated symbol.
    /// </summary>
    internal sealed class J2534Api : IDisposable
    {
        [UnmanagedFunctionPointer(CallingConvention.StdCall)]
        private delegate int PassThruOpenDelegate(IntPtr name, out uint deviceId);

        [UnmanagedFunctionPointer(CallingConvention.StdCall)]
        private delegate int PassThruCloseDelegate(uint deviceId);

        [UnmanagedFunctionPointer(CallingConvention.StdCall)]
        private delegate int PassThruConnectDelegate(
            uint deviceId, uint protocolId, uint flags, uint baudRate, out uint channelId);

        [UnmanagedFunctionPointer(CallingConvention.StdCall)]
        private delegate int PassThruDisconnectDelegate(uint channelId);

        [UnmanagedFunctionPointer(CallingConvention.StdCall)]
        private delegate int PassThruReadMsgsDelegate(
            uint channelId, ref PassThruMsg message, ref uint messageCount, uint timeout);

        [UnmanagedFunctionPointer(CallingConvention.StdCall)]
        private delegate int PassThruWriteMsgsDelegate(
            uint channelId, ref PassThruMsg message, ref uint messageCount, uint timeout);

        [UnmanagedFunctionPointer(CallingConvention.StdCall)]
        private delegate int PassThruStartMsgFilterDelegate(
            uint channelId,
            uint filterType,
            ref PassThruMsg mask,
            ref PassThruMsg pattern,
            ref PassThruMsg flowControl,
            out uint filterId);

        [UnmanagedFunctionPointer(CallingConvention.StdCall)]
        private delegate int PassThruStartMsgFilterNoFlowDelegate(
            uint channelId,
            uint filterType,
            ref PassThruMsg mask,
            ref PassThruMsg pattern,
            IntPtr flowControl,
            out uint filterId);

        [UnmanagedFunctionPointer(CallingConvention.StdCall)]
        private delegate int PassThruStopMsgFilterDelegate(uint channelId, uint filterId);

        [UnmanagedFunctionPointer(CallingConvention.StdCall)]
        private delegate int PassThruReadVersionDelegate(
            uint deviceId,
            [Out, MarshalAs(UnmanagedType.LPStr)] StringBuilder firmwareVersion,
            [Out, MarshalAs(UnmanagedType.LPStr)] StringBuilder dllVersion,
            [Out, MarshalAs(UnmanagedType.LPStr)] StringBuilder apiVersion);

        [UnmanagedFunctionPointer(CallingConvention.StdCall)]
        private delegate int PassThruGetLastErrorDelegate(
            [Out, MarshalAs(UnmanagedType.LPStr)] StringBuilder errorDescription);

        [UnmanagedFunctionPointer(CallingConvention.StdCall)]
        private delegate int PassThruIoctlDelegate(
            uint channelId, uint ioctlId, IntPtr input, IntPtr output);

        private IntPtr module;
        private readonly PassThruOpenDelegate open;
        private readonly PassThruCloseDelegate close;
        private readonly PassThruConnectDelegate connect;
        private readonly PassThruDisconnectDelegate disconnect;
        private readonly PassThruReadMsgsDelegate readMessages;
        private readonly PassThruWriteMsgsDelegate writeMessages;
        private readonly PassThruStartMsgFilterDelegate startMessageFilter;
        private readonly PassThruStartMsgFilterNoFlowDelegate startMessageFilterNoFlow;
        private readonly PassThruStopMsgFilterDelegate stopMessageFilter;
        private readonly PassThruReadVersionDelegate readVersion;
        private readonly PassThruGetLastErrorDelegate getLastError;
        private readonly PassThruIoctlDelegate ioctl;

        internal J2534Api(string path)
        {
            module = NativeMethods.LoadLibrary(path);
            if (module == IntPtr.Zero)
            {
                throw new Win32Exception(Marshal.GetLastWin32Error(), "LoadLibrary failed for " + path);
            }

            open = Load<PassThruOpenDelegate>("PassThruOpen", 8);
            close = Load<PassThruCloseDelegate>("PassThruClose", 4);
            connect = Load<PassThruConnectDelegate>("PassThruConnect", 20);
            disconnect = Load<PassThruDisconnectDelegate>("PassThruDisconnect", 4);
            readMessages = Load<PassThruReadMsgsDelegate>("PassThruReadMsgs", 16);
            writeMessages = Load<PassThruWriteMsgsDelegate>("PassThruWriteMsgs", 16);
            startMessageFilter = Load<PassThruStartMsgFilterDelegate>("PassThruStartMsgFilter", 28);
            startMessageFilterNoFlow = Load<PassThruStartMsgFilterNoFlowDelegate>("PassThruStartMsgFilter", 28);
            stopMessageFilter = Load<PassThruStopMsgFilterDelegate>("PassThruStopMsgFilter", 8);
            readVersion = Load<PassThruReadVersionDelegate>("PassThruReadVersion", 16);
            getLastError = Load<PassThruGetLastErrorDelegate>("PassThruGetLastError", 4);
            ioctl = Load<PassThruIoctlDelegate>("PassThruIoctl", 16);
        }

        internal int Open(out uint deviceId)
        {
            return open(IntPtr.Zero, out deviceId);
        }

        internal int Close(uint deviceId)
        {
            return close(deviceId);
        }

        internal int Connect(uint deviceId, uint protocolId, uint flags, uint baudRate, out uint channelId)
        {
            return connect(deviceId, protocolId, flags, baudRate, out channelId);
        }

        internal int Disconnect(uint channelId)
        {
            return disconnect(channelId);
        }

        internal int ReadMessages(uint channelId, ref PassThruMsg message, ref uint messageCount, uint timeout)
        {
            return readMessages(channelId, ref message, ref messageCount, timeout);
        }

        internal int WriteMessages(uint channelId, ref PassThruMsg message, ref uint messageCount, uint timeout)
        {
            return writeMessages(channelId, ref message, ref messageCount, timeout);
        }

        internal int StartMessageFilter(
            uint channelId,
            uint filterType,
            ref PassThruMsg mask,
            ref PassThruMsg pattern,
            ref PassThruMsg flowControl,
            out uint filterId)
        {
            return startMessageFilter(channelId, filterType, ref mask, ref pattern, ref flowControl, out filterId);
        }

        internal int StopMessageFilter(uint channelId, uint filterId)
        {
            return stopMessageFilter(channelId, filterId);
        }

        internal int StartPassFilter(
            uint channelId,
            uint filterType,
            ref PassThruMsg mask,
            ref PassThruMsg pattern,
            out uint filterId)
        {
            return startMessageFilterNoFlow(channelId, filterType, ref mask, ref pattern, IntPtr.Zero, out filterId);
        }

        internal int Ioctl(uint channelId, uint ioctlId)
        {
            return ioctl(channelId, ioctlId, IntPtr.Zero, IntPtr.Zero);
        }

        internal int ReadVersion(uint deviceId, out string firmware, out string dll, out string api)
        {
            StringBuilder firmwareBuffer = new StringBuilder(80);
            StringBuilder dllBuffer = new StringBuilder(80);
            StringBuilder apiBuffer = new StringBuilder(80);
            int status = readVersion(deviceId, firmwareBuffer, dllBuffer, apiBuffer);
            firmware = firmwareBuffer.ToString();
            dll = dllBuffer.ToString();
            api = apiBuffer.ToString();
            return status;
        }

        internal string GetLastError()
        {
            try
            {
                StringBuilder buffer = new StringBuilder(256);
                int status = getLastError(buffer);
                return status == 0 ? buffer.ToString().Trim() : String.Empty;
            }
            catch
            {
                return String.Empty;
            }
        }

        public void Dispose()
        {
            if (module != IntPtr.Zero)
            {
                NativeMethods.FreeLibrary(module);
                module = IntPtr.Zero;
            }
        }

        private T Load<T>(string name, int decoratedArgumentBytes) where T : class
        {
            IntPtr address = NativeMethods.GetProcAddress(module, name);
            if (address == IntPtr.Zero)
            {
                address = NativeMethods.GetProcAddress(module, "_" + name + "@" + decoratedArgumentBytes);
            }
            if (address == IntPtr.Zero)
            {
                throw new MissingMethodException("J2534 DLL does not export " + name + ".");
            }
            return (T)(object)Marshal.GetDelegateForFunctionPointer(address, typeof(T));
        }
    }

    internal static class NativeMethods
    {
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        internal static extern IntPtr LoadLibrary(string path);

        [DllImport("kernel32.dll", CharSet = CharSet.Ansi, ExactSpelling = true, SetLastError = true)]
        internal static extern IntPtr GetProcAddress(IntPtr module, string name);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        internal static extern bool FreeLibrary(IntPtr module);
    }
}
