using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO.Ports;
using System.Text;
using Telemetry.Can.Core;

namespace Telemetry.Backend.Slcan
{
    /// <summary>
    /// LAWICEL/slcan binding: any adapter that answers ASCII commands over a
    /// USB CDC-ACM virtual COM port (O/C/S/t/T/r/R/V, CR-terminated). Windows'
    /// inbox usbser.sys handles the port, so this needs no vendor driver and
    /// no P/Invoke, unlike gs_usb's WinUSB/libusb transports.
    ///
    /// Verified against a CANable v1.0 nano (STM32F072) reflashed with a
    /// board-adapted slcan firmware: command handshake, ACK/NACK, and a
    /// byte-exact frame round trip through the CAN peripheral's internal
    /// loopback mode, not just that the port opens.
    /// </summary>
    internal sealed class SlcanChannel : ICanChannel
    {
        private const byte Ack = (byte)'\r';
        private const byte Nack = 0x07; // BEL

        private readonly string portName;
        private readonly List<byte> receiveBuffer = new List<byte>();

        private SerialPort port;
        private string firmwareId = "";
        private uint filterFromId;
        private uint filterToId;
        private bool opened;

        internal SlcanChannel(string portName)
        {
            this.portName = portName;
        }

        public string Description
        {
            get
            {
                return "slcan " + portName +
                    (firmwareId.Length == 0 ? "" : " (" + firmwareId + ")");
            }
        }

        public string BackendName
        {
            get { return "slcan"; }
        }

        public void RefuseConcurrentClients()
        {
            // Windows serial ports are exclusive by default: a second Open()
            // on a port another process holds fails outright, so there is
            // nothing useful to pre-check before attempting to open.
        }

        public void Open(int bitrate, uint filterFromId, uint filterToId)
        {
            this.filterFromId = filterFromId;
            this.filterToId = filterToId;
            int bitrateIndex = BitrateToSlcanIndex(bitrate);

            port = new SerialPort(portName);
            // The CDC-ACM virtual port ignores the actual baud rate - the USB
            // link runs at full speed regardless - but SerialPort requires a
            // value to open.
            port.BaudRate = 115200;
            port.Parity = Parity.None;
            port.DataBits = 8;
            port.StopBits = StopBits.One;
            port.ReadTimeout = 500;
            port.WriteTimeout = 1000;
            try
            {
                port.Open();
            }
            catch (Exception exc)
            {
                throw new InvalidOperationException(
                    "Could not open slcan serial port " + portName + ": " + exc.Message, exc);
            }
            port.DiscardInBuffer();
            port.DiscardOutBuffer();
            receiveBuffer.Clear();

            firmwareId = QueryVersion();
            // Close first: a previous session left open without a clean
            // shutdown (a crash, an unplug) leaves the firmware's bus state
            // ON_BUS, and S/O commands while on-bus are silently ignored.
            SendAndAwaitAck("C", "close (idempotent, clears a previous session's state)");
            SendAndAwaitAck("S" + bitrateIndex.ToString(CultureInfo.InvariantCulture), "set bitrate");
            SendAndAwaitAck("O", "open channel");
            opened = true;

            Console.WriteLine(
                "slcan channel opened on " + portName + " at " + bitrate.ToString(CultureInfo.InvariantCulture) +
                " bit/s" + (firmwareId.Length == 0 ? "." : " (firmware: " + firmwareId + ")."));
        }

        public void Write(uint canId, byte[] data)
        {
            if (data == null || data.Length != 8)
            {
                throw new InvalidOperationException("A CAN frame must contain exactly eight bytes.");
            }
            if (canId > 0x7FF)
            {
                throw new InvalidOperationException(
                    "slcan standard frames require an 11-bit CAN ID (0x000-0x7FF); got 0x" +
                    canId.ToString("X", CultureInfo.InvariantCulture) + ".");
            }
            StringBuilder command = new StringBuilder(22);
            command.Append('t');
            command.Append(canId.ToString("X3", CultureInfo.InvariantCulture));
            command.Append('8');
            for (int i = 0; i < 8; i++)
            {
                command.Append(data[i].ToString("X2", CultureInfo.InvariantCulture));
            }
            command.Append('\r');
            byte[] bytes = Encoding.ASCII.GetBytes(command.ToString());
            port.Write(bytes, 0, bytes.Length);
            // No synchronous ack wait here, unlike Open()'s one-time setup
            // commands: this runs per frame, and the ack byte is harmless
            // noise that TryRead's parser already discards as it scans the
            // stream for real received frames.
        }

        public bool TryRead(out uint canId, out byte[] data)
        {
            int available = port.BytesToRead;
            if (available > 0)
            {
                byte[] chunk = new byte[available];
                int actuallyRead = port.Read(chunk, 0, available);
                for (int i = 0; i < actuallyRead; i++)
                {
                    receiveBuffer.Add(chunk[i]);
                }
            }

            while (receiveBuffer.Count > 0)
            {
                byte first = receiveBuffer[0];
                if (first == Ack || first == Nack)
                {
                    // The device's ack/nack for a previously written command,
                    // interleaved in the same byte stream as received frames
                    // - there is no separate channel to keep them apart.
                    receiveBuffer.RemoveAt(0);
                    continue;
                }

                int crIndex = receiveBuffer.IndexOf(Ack);
                if (crIndex < 0)
                {
                    // The rest of this line has not arrived yet.
                    break;
                }

                byte[] line = receiveBuffer.GetRange(0, crIndex).ToArray();
                receiveBuffer.RemoveRange(0, crIndex + 1);

                if (line.Length == 0 || line[0] != (byte)'t')
                {
                    // 'T' (extended), 'r'/'R' (remote), or an unrecognized
                    // line: outside this channel's scope (ICanChannel is
                    // standard data frames only), or noise. Keep scanning.
                    continue;
                }

                uint parsedId;
                byte[] parsedData;
                if (!TryParseStandardDataFrame(line, out parsedId, out parsedData))
                {
                    continue;
                }
                if (parsedId < filterFromId || parsedId > filterToId)
                {
                    // No hardware filter narrower than accept-all is
                    // configured on this firmware; software filtering here
                    // keeps the contract - "narrowest available" - honest.
                    continue;
                }
                canId = parsedId;
                data = parsedData;
                return true;
            }

            canId = 0;
            data = null;
            return false;
        }

        public void CheckStatus(string context)
        {
            // This firmware's error register (nonstandard 'E' command) is
            // sticky since boot with no clear command - it cannot distinguish
            // "currently faulted" from "one historical error, long since
            // recovered from". A bus-off or stalled controller instead
            // surfaces as TryRead never returning data, which the caller
            // already treats as a fault after its own timeout.
        }

        private static bool TryParseStandardDataFrame(byte[] line, out uint canId, out byte[] data)
        {
            canId = 0;
            data = null;
            // "t" + 3 hex ID digits + 1 hex DLC digit, then 2 hex digits per
            // data byte.
            if (line.Length < 4)
            {
                return false;
            }
            int id;
            if (!TryParseHex(line, 1, 3, out id))
            {
                return false;
            }
            int dlc;
            if (!TryParseHex(line, 4, 1, out dlc) || dlc > 8)
            {
                return false;
            }
            if (line.Length != 5 + dlc * 2)
            {
                return false;
            }
            byte[] frameData = new byte[8];
            for (int i = 0; i < dlc; i++)
            {
                int b;
                if (!TryParseHex(line, 5 + i * 2, 2, out b))
                {
                    return false;
                }
                frameData[i] = (byte)b;
            }
            canId = (uint)id;
            data = frameData;
            return true;
        }

        private static bool TryParseHex(byte[] line, int offset, int length, out int value)
        {
            value = 0;
            for (int i = 0; i < length; i++)
            {
                int digit = HexDigit(line[offset + i]);
                if (digit < 0)
                {
                    return false;
                }
                value = (value << 4) | digit;
            }
            return true;
        }

        private static int HexDigit(byte c)
        {
            if (c >= (byte)'0' && c <= (byte)'9')
            {
                return c - (byte)'0';
            }
            if (c >= (byte)'A' && c <= (byte)'F')
            {
                return c - (byte)'A' + 10;
            }
            if (c >= (byte)'a' && c <= (byte)'f')
            {
                return c - (byte)'a' + 10;
            }
            return -1;
        }

        private static int BitrateToSlcanIndex(int bitrate)
        {
            switch (bitrate)
            {
                case 10000: return 0;
                case 20000: return 1;
                case 50000: return 2;
                case 100000: return 3;
                case 125000: return 4;
                case 250000: return 5;
                case 500000: return 6;
                case 750000: return 7;
                case 1000000: return 8;
                default:
                    throw new FormatException(
                        "slcan only supports fixed bitrate presets (10000, 20000, 50000, 100000, " +
                        "125000, 250000, 500000, 750000, 1000000 bit/s); got " +
                        bitrate.ToString(CultureInfo.InvariantCulture) + ".");
            }
        }

        private void SendAndAwaitAck(string command, string label)
        {
            byte[] bytes = Encoding.ASCII.GetBytes(command + "\r");
            port.Write(bytes, 0, bytes.Length);
            int response;
            try
            {
                response = port.ReadByte();
            }
            catch (TimeoutException)
            {
                throw new InvalidOperationException(
                    "slcan " + label + " (\"" + command + "\") got no response from " + portName + ".");
            }
            if (response == Nack)
            {
                throw new InvalidOperationException(
                    "slcan " + label + " (\"" + command + "\") was rejected by the device (NACK).");
            }
            if (response != Ack)
            {
                throw new InvalidOperationException(
                    "slcan " + label + " (\"" + command + "\") got an unexpected response byte 0x" +
                    response.ToString("X2", CultureInfo.InvariantCulture) + ".");
            }
        }

        // Reads the 'V' response: a version/remote string terminated by CR,
        // immediately followed by the command's own CR ack - two CR bytes
        // total, confirmed against real hardware. Best-effort: used only to
        // decorate Description, so a timeout leaves it blank rather than
        // failing Open().
        private string QueryVersion()
        {
            byte[] bytes = Encoding.ASCII.GetBytes("V\r");
            port.Write(bytes, 0, bytes.Length);
            StringBuilder text = new StringBuilder();
            int crCount = 0;
            while (crCount < 2)
            {
                int b;
                try
                {
                    b = port.ReadByte();
                }
                catch (TimeoutException)
                {
                    break;
                }
                if (b == Ack)
                {
                    crCount++;
                    continue;
                }
                text.Append((char)b);
            }
            return text.ToString();
        }

        public void Dispose()
        {
            if (port == null)
            {
                return;
            }
            if (opened)
            {
                try
                {
                    byte[] bytes = Encoding.ASCII.GetBytes("C\r");
                    port.Write(bytes, 0, bytes.Length);
                }
                catch (Exception exc)
                {
                    Console.Error.WriteLine("WARNING: slcan close command failed: " + exc.Message);
                }
            }
            try
            {
                port.Close();
            }
            finally
            {
                port.Dispose();
                port = null;
            }
            Console.WriteLine("slcan channel closed.");
        }
    }
}
