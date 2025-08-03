#!/usr/bin/env python3
import argparse
import fnmatch
import logging
import os
import selectors
import signal
import sys
import termios
import threading
import time
import tty
from pathlib import Path
from sqlite3 import connect
from typing import Optional, Callable, Dict, Tuple

import serial
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

import pyboard


class RawConsole:
    """Handles raw terminal mode for direct keystroke capture."""

    def __init__(self):
        self.orig_attrs = termios.tcgetattr(sys.stdin.fileno())
        self.in_raw = False

    def enable_raw(self):
        if not self.in_raw:
            tty.setraw(sys.stdin.fileno())
            self.in_raw = True

    def disable_raw(self):
        if self.in_raw:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self.orig_attrs)
            self.in_raw = False

    def restore(self):
        self.disable_raw()


class DebouncedHandler(FileSystemEventHandler):
    """File system event handler with debouncing to prevent rapid successive events."""

    def __init__(self, callback: Callable[[str, str], None],
                 include_pattern: str = "*",
                 debounce_secs: float = 0.5):
        super().__init__()
        self.callback = callback
        self.include_pattern = include_pattern
        self.debounce_secs = debounce_secs
        self._timers: Dict[str, Tuple[str, threading.Timer]] = {}
        self._lock = threading.Lock()

    def _schedule(self, path: str, event_type: str):
        if not fnmatch.fnmatch(os.path.basename(path), self.include_pattern):
            return

        with self._lock:
            existing = self._timers.get(path)
            if existing:
                _, timer = existing
                timer.cancel()

            timer = threading.Timer(self.debounce_secs, self._fire, args=(path,))
            self._timers[path] = (event_type, timer)
            timer.daemon = True
            timer.start()

    def _fire(self, path: str):
        with self._lock:
            record = self._timers.pop(path, None)
        if not record:
            return
        event_type, _ = record
        try:
            self.callback(event_type, path)
        except Exception:
            logging.exception("Callback raised exception for %s %s", event_type, path)

    def on_created(self, event):
        if not event.is_directory:
            self._schedule(event.src_path, "created")

    def on_modified(self, event):
        if not event.is_directory:
            self._schedule(event.src_path, "modified")

    def on_deleted(self, event):
        if not event.is_directory:
            with self._lock:
                existing = self._timers.pop(event.src_path, None)
                if existing:
                    _, timer = existing
                    timer.cancel()
            try:
                self.callback("deleted", event.src_path)
            except Exception:
                logging.exception("Callback raised exception on delete for %s", event.src_path)


class MicroPythonESP32REPL:
    """Main class for MicroPython ESP32 REPL with file watching capabilities."""

    def __init__(self, port: str, baud: int = 460800, folder: str = ".",
                 watch_pattern: str = "*.py", debounce: float = 0.5):
        self.port = port
        self.baud = baud
        self.folder = folder
        self.watch_pattern = watch_pattern
        self.debounce = debounce

        self.pb: Optional[pyboard.Pyboard] = None
        self.ser: Optional[serial.Serial] = None
        self.console: Optional[RawConsole] = None
        self.observer: Optional[Observer] = None
        self.selector: Optional[selectors.DefaultSelector] = None
        self.stop = False

        # Setup logging
        self._setup_logging()


    def _setup_logging(self):
        """Configure logging format."""
        logging.basicConfig(
            level=logging.INFO,
            format="\r[%(asctime)s] %(message)s\r",
            datefmt="%Y-%m-%d %H:%M:%S"
        )

    def connect(self, isreconnect=False) -> bool:
        """Initialize connections to the ESP32 device."""
        try:
            if self.ser.is_open:
                return True

            if not isreconnect:
                logging.info(f"[+] Opening serial port {self.port} at {self.baud} baud...")
            self.ser.open()

            try:
                self.selector.unregister(self.ser)
            except Exception:
                pass
            self.selector.register(self.ser, selectors.EVENT_READ, data="serial")

            logging.info(f"[+] Initializing Pyboard on port {self.port} at {self.baud} baud...")
            if self.pb:
                del self.pb
            self.pb = pyboard.Pyboard(self.port, baudrate=self.baud, exclusive=False)

            if not self.ser.is_open:
                return True
            # Perform handshake
            logging.info("[*] Attempting pyboard.py handshake to interrupt running code...")
            if self._pyboard_handshake():
                logging.info("[+] pyboard handshake succeeded.")
                return True
            else:
                logging.error("[!] pyboard handshake failed.")
                return False

        except Exception as e:
            if not isreconnect:
                logging.error(f"[!] Failed to connect: {e}")
            return False

    def _create_serial(self) -> serial.Serial:
        """Open serial connection to the device."""
        try:
            ser = serial.Serial()
            ser.port = self.port
            ser.baudrate = self.baud
            ser.timeout = 0
            ser.exclusive = False
            time.sleep(0.1)
            return ser
        except Exception as e:
            logging.exception(f"[Error] Could not open serial port {self.port}: {e}")
            raise

    def _pyboard_handshake(self) -> bool:
        """Perform pyboard handshake."""
        try:
            self.pb.enter_raw_repl(soft_reset=False)
            self.pb.exec_raw('print("<<<pyboard handshake>>>")')
            self.pb.exit_raw_repl()
            return True
        except Exception as e:
            logging.error(f"[pyboard handshake] failed: {e}")
            return False

    def _start_file_watcher(self):
        """Start watching the specified folder for file changes."""
        if not os.path.isdir(self.folder):
            raise ValueError(f"{self.folder!r} is not a directory")

        handler = DebouncedHandler(
            self._file_change_callback,
            include_pattern=self.watch_pattern,
            debounce_secs=self.debounce
        )

        self.observer = Observer()
        self.observer.schedule(handler, path=self.folder, recursive=True)
        self.observer.start()

        logging.info(
            "[+] Started watching '%s' pattern='%s' debounce=%.2fs",
            self.folder, self.watch_pattern, self.debounce
        )

    def _file_change_callback(self, event_type: str, path: str):
        """Handle file change events."""
        if self.console:
            self.console.disable_raw()


        if self.ser and not self.ser.is_open:
            return False

        mp = Path(self.folder)
        p = Path(path)
        dest = p.resolve().relative_to(mp.resolve())
        time.sleep(0.1)
        if event_type == "modified":
            logging.info(f"[*] Uploading: {path} -> {dest}")
            self.pb.enter_raw_repl(soft_reset=False)
            self.pb.fs_put(path, dest)
            self.pb.exit_raw_repl()
            logging.info(f"[+] Uploaded!")
            logging.info(f"[*] Soft rebooting")
            self.ser.write(b"\x04")

        if self.console:
            self.console.enable_raw()

        return True

    def print_banner(self):
        """Print the REPL banner and help information."""
        print("\r\n=== MicroPython ESP32 REPL ===")
        print("\r\nRaw-mode console active. Keystrokes are sent directly to device.")
        print("\r\nSpecial sequenced local commands: press Ctrl-] to enter command mode.")
        print("\r\nLocal commands (type after Ctrl-] prompt):")
        print("\r\n  help           show this help")
        print("\r\n  exit           quit this program")
        print("\r\n  handshake      re-run pyboard.py handshake (if available)")
        print("\r\n  send-ctrlc     send Ctrl-C to device")
        print("\r\n  send-ctrld     send Ctrl-D to device (soft reboot)")
        print("\r\n  status         show connection info")
        print("\r\nExamples: after hitting Ctrl-], type 'exit' and press Enter.")
        print("\r\n")

    def _read_command_line(self) -> Optional[str]:
        """Read command in raw mode after Ctrl-] is pressed."""
        sys.stdout.write("\r\ncmd> ")
        sys.stdout.flush()
        buf = bytearray()

        while True:
            try:
                ch = os.read(sys.stdin.fileno(), 1)
            except Exception:
                continue

            if not ch:
                continue

            # Enter / Return
            if ch in (b'\r', b'\n'):
                sys.stdout.write("\n")
                break

            # Backspace / delete
            if ch in (b'\x7f', b'\x08'):
                if buf:
                    buf.pop()
                    sys.stdout.write('\b \b')
                    sys.stdout.flush()
                continue

            # Ctrl-C cancels the command input
            if ch == b'\x03':
                sys.stdout.write("^C\n")
                return None

            # Echo printable portion
            try:
                decoded = ch.decode('utf-8', 'replace')
            except Exception:
                decoded = ''
            buf.extend(ch)
            sys.stdout.write(decoded)
            sys.stdout.flush()

        cmd = buf.decode('utf-8', errors='ignore').strip()
        if not cmd:
            return None

        return self._process_command(cmd)

    def _process_command(self, cmd: str) -> Optional[str]:
        """Process the entered command."""
        if cmd in ("x", "quit", "exit"):
            return "exit"

        if cmd == "help":
            self.print_banner()
            return None

        if cmd == "handshake":
            logging.info("[*] Running pyboard handshake...")
            ok = self._pyboard_handshake()
            logging.info("[+] Handshake succeeded." if ok else "[!] Handshake failed.")
            return None

        if cmd == "send-ctrlc":
            self.ser.write(b'\x03')
            logging.info("[+] Sent Ctrl-C to device.")
            return None

        if cmd == "send-ctrld":
            self.ser.write(b'\x04')
            logging.info("[+] Sent Ctrl-D to device.")
            return None

        if cmd == "status":
            logging.info(f"[+] Port: {self.port}, Baud: {self.baud}")
            return None

        logging.error(f"[!] Unknown command: {cmd}")
        return None

    def run(self):
        """Main REPL loop."""
        if not self.ser or not self.pb:
            raise RuntimeError("Not connected. Call connect() first.")

        self.stop = False

        try:
            self.console.enable_raw()

            while not self.stop:
                events = self.selector.select(timeout=0.1)
                for key, mask in events:
                    if key.data == "serial":
                        self._handle_serial_input()
                    elif key.data == "stdin":
                        if not self._handle_stdin_input():
                            break
            self.stop = False
            self.ser.close()
            self.console.disable_raw()
        except KeyboardInterrupt:
            self.cleanup()


    def _handle_serial_input(self):
        """Handle input from serial port."""

        if not self.ser.is_open:
            for i in range(50):
                time.sleep(0.1)
            return

        data = b''
        try:
            if not self.pb.working:
                data = self.ser.read(4096)
        except Exception as e:
            logging.error(f"[Reader] serial read error: {e}")
            self.stop = True
            return

        if data:
            try:
                text = data.decode("utf-8", errors="replace")
            except Exception:
                text = repr(data)
            sys.stdout.write(text)
            sys.stdout.flush()

    def _handle_stdin_input(self) -> bool:
        """Handle input from stdin. Returns False to stop the loop."""
        try:
            b = os.read(sys.stdin.fileno(), 1024)
        except Exception as e:
            logging.exception(f"[Stdin] read error: {e}")
            self.stop = True
            return False

        if not b:
            return True

        # Check for local-command trigger: Ctrl-] is 0x1D
        if b == b'\x1d':
            res = self._read_command_line()
            if res == "exit":
                self.stop = True
                return False
            return True

        # Forward all other bytes directly
        try:
            self.ser.write(b)
        except Exception as e:
            logging.error(f"[Writer] failed to write to serial: {e}")
            self.stop = True
            return False

        return True

    def _handle_exit(self, signum, frame):
        """Signal handler for clean exit."""
        self.stop = True

    def init(self):
        self.ser = self._create_serial()


        # Setup selector for async I/O
        self.selector = selectors.DefaultSelector()
        self.selector.register(sys.stdin, selectors.EVENT_READ, data="stdin")

        # Setup signal handlers
        signal.signal(signal.SIGTERM, self._handle_exit)
        signal.signal(signal.SIGHUP, self._handle_exit)

        # Enable raw console mode
        self.console = RawConsole()

        self.print_banner()


        self._start_file_watcher()



    def wait_for_device(self):
        while True:
            logging.info("[*] Waiting for device...")
            if self.connect(True):
                self.run()

            for i in range(50):
                try:
                    time.sleep(0.1)
                except KeyboardInterrupt:
                    return

    def cleanup(self):
        """Clean up resources."""
        if self.console:
            self.console.restore()

        try:
            if self.ser:
                self.ser.close()
            if self.observer:
                self.observer.stop()
                self.observer.join()
        except Exception:
            pass

        logging.info("[+] Exited. Serial closed, terminal restored.")


def main():
    """Example usage of the MicroPythonESP32REPL class."""
    parser = argparse.ArgumentParser(
        description="ESP32 MicroPython REPL with proper Ctrl handling."
    )
    parser.add_argument("port", help="Serial port (e.g. /dev/cu.SLAB_USBtoUART)")
    parser.add_argument("--folder", "-f", type=str, default=".", help="Folder to monitor")
    parser.add_argument("--pattern", "-p", type=str, default="*.py", help="File name pattern to monitor")
    parser.add_argument("--baud", "-b", type=int, default=460800, help="Baud rate")

    args = parser.parse_args()

    # Create and use the REPL
    repl = MicroPythonESP32REPL(
        port=args.port,
        baud=args.baud,
        folder=args.folder,
        watch_pattern=args.pattern
    )
    repl.init()

    repl.wait_for_device()

    repl.console.disable_raw()

if __name__ == "__main__":
    main()
