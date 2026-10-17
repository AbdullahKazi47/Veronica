"""
GPS for VERONICA. This laptop has no GPS chip, so a real fix comes from a GPS receiver she
finds at run time:

  * a USB GPS receiver (any that speaks NMEA over a serial/COM port, e.g. u-blox dongles),
    found by scanning the COM ports, nothing configured;
  * the user's phone, running a "GPS to network" app that sends NMEA sentences over UDP
    to this PC (Android: "Share GPS", "GPSd Forwarder"; iPhone: "GPS2IP"). Turned on by
    voice ("use my phone as GPS"), because listening on the network is the user's choice;
  * a phone app that serves NMEA over TCP (host:port in config, `gps.tcp`).

Everything received is untrusted data: only checksummed NMEA position sentences with
sane values are used, UDP packets are only accepted from the local network, and nothing
in them is ever treated as an instruction.
"""
from __future__ import annotations

import ipaddress
import re
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from veronica.logging_setup import get_logger

logger = get_logger("integrations.gps")

DEFAULT_UDP_PORT = 10110          # the registered NMEA-over-IP port
FRESH_S = 120                     # a fix older than this is not "where you are now"
_SENTENCE = re.compile(r"^\$(?P<body>[A-Z]{2}(?P<kind>GGA|RMC)[^*]*)\*(?P<sum>[0-9A-Fa-f]{2})\s*$")


@dataclass
class Fix:
    lat: float
    lon: float
    accuracy_m: float
    satellites: Optional[int] = None
    source: str = ""
    at: float = field(default_factory=time.time)

    def age_s(self, now: Optional[float] = None) -> float:
        return (now if now is not None else time.time()) - self.at


def _checksum_ok(body: str, given: str) -> bool:
    calc = 0
    for ch in body:
        calc ^= ord(ch)
    return f"{calc:02X}" == given.upper()


def _coord(value: str, hemi: str, degree_digits: int) -> Optional[float]:
    if not value or hemi not in ("N", "S", "E", "W"):
        return None
    try:
        deg = float(value[:degree_digits])
        minutes = float(value[degree_digits:])
    except ValueError:
        return None
    if minutes >= 60:
        return None
    out = deg + minutes / 60.0
    return -out if hemi in ("S", "W") else out


def parse_nmea(line: str, source: str = "", now: Optional[float] = None) -> Optional[Fix]:
    """A Fix from one GGA or RMC sentence, or None (bad checksum, no fix, nonsense values)."""
    m = _SENTENCE.match((line or "").strip())
    if not m or not _checksum_ok(m.group("body"), m.group("sum")):
        return None
    f = m.group("body").split(",")
    kind = m.group("kind")
    try:
        if kind == "GGA":       # $..GGA,time,lat,N,lon,E,quality,sats,hdop,alt,...
            if len(f) < 9 or not f[6] or f[6] == "0":
                return None
            lat, lon = _coord(f[2], f[3], 2), _coord(f[4], f[5], 3)
            sats = int(f[7]) if f[7] else None
            hdop = float(f[8]) if f[8] else 5.0
            acc = max(3.0, hdop * 5.0)          # rough: HDOP x ~5 m receiver error
        else:                    # $..RMC,time,status,lat,N,lon,E,...
            if len(f) < 7 or f[2] != "A":
                return None
            lat, lon = _coord(f[3], f[4], 2), _coord(f[5], f[6], 3)
            sats, acc = None, 15.0
    except (ValueError, IndexError):
        return None
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        return None
    return Fix(lat=lat, lon=lon, accuracy_m=acc, satellites=sats, source=source,
               at=now if now is not None else time.time())


def lan_ip() -> Optional[str]:
    """This PC's address on the local network (what the phone app sends to). No packet is
    sent: connecting a UDP socket only picks the outgoing interface."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        return None if ip.startswith("127.") else ip
    except OSError:
        return None
    finally:
        s.close()


def _private(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local


class GpsHub:
    """Collects fixes from every source that is on; `latest()` is the newest fresh one."""

    def __init__(self, udp_port: int = DEFAULT_UDP_PORT, tcp: str = "", scan_serial: bool = True,
                 list_ports: Optional[Callable[[], list]] = None) -> None:
        self.udp_port = udp_port
        self.tcp = tcp
        self.scan_serial = scan_serial
        self._list_ports = list_ports
        self._fix: Optional[Fix] = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._udp_stop = threading.Event()
        self._udp_thread: Optional[threading.Thread] = None
        self._started = False
        self.udp_error = ""
        self.serial_port = ""

    # -- fixes ---------------------------------------------------------------
    def feed(self, line: str, source: str) -> Optional[Fix]:
        fix = parse_nmea(line, source)
        if fix is not None:
            with self._lock:
                first = self._fix is None or self._fix.age_s() > FRESH_S
                self._fix = fix
            if first:
                logger.info("GPS fix from %s (about %d m).", source, round(fix.accuracy_m))
        return fix

    def latest(self, max_age_s: float = FRESH_S) -> Optional[Fix]:
        with self._lock:
            fix = self._fix
        return fix if fix is not None and fix.age_s() <= max_age_s else None

    # -- sources ---------------------------------------------------------------
    def start(self, udp: bool = False) -> None:
        if not self._started:
            self._started = True
            if self.scan_serial:
                threading.Thread(target=self._serial_loop, name="gps-serial", daemon=True).start()
            if self.tcp:
                threading.Thread(target=self._tcp_loop, name="gps-tcp", daemon=True).start()
        if udp:
            self.start_udp()

    def stop(self) -> None:
        self._stop.set()

    @property
    def udp_on(self) -> bool:
        return self._udp_thread is not None and self._udp_thread.is_alive()

    def start_udp(self) -> bool:
        if self.udp_on:
            return True
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind(("0.0.0.0", self.udp_port))
            sock.settimeout(1.0)
        except OSError as e:
            self.udp_error = str(e)
            logger.warning("Can't listen for phone GPS on UDP %d: %s", self.udp_port, e)
            return False
        self.udp_error = ""
        self._udp_stop.clear()
        self._udp_thread = threading.Thread(target=self._udp_loop, args=(sock,), name="gps-udp", daemon=True)
        self._udp_thread.start()
        logger.info("Listening for phone GPS on UDP port %d.", self.udp_port)
        return True

    def stop_udp(self) -> None:
        self._udp_stop.set()
        if self._udp_thread is not None:
            self._udp_thread.join(timeout=3)

    def _udp_loop(self, sock: socket.socket) -> None:
        with sock:
            while not self._stop.is_set() and not self._udp_stop.is_set():
                try:
                    data, (addr, _port) = sock.recvfrom(4096)
                except socket.timeout:
                    continue
                except OSError:
                    return
                if not _private(addr):          # only devices on this network
                    continue
                for line in data.decode("ascii", "ignore").splitlines():
                    self.feed(line, f"your phone ({addr})")

    def _tcp_loop(self) -> None:
        host, _, port = self.tcp.rpartition(":")
        while not self._stop.is_set():
            try:
                with socket.create_connection((host, int(port)), timeout=10) as s:
                    s.settimeout(30)
                    buf = b""
                    while not self._stop.is_set():
                        chunk = s.recv(4096)
                        if not chunk:
                            break
                        buf += chunk
                        *lines, buf = buf.split(b"\n")
                        for line in lines:
                            self.feed(line.decode("ascii", "ignore"), f"your phone ({host})")
                        buf = buf[-4096:]
            except (OSError, ValueError):
                pass
            self._stop.wait(30)

    def _ports(self) -> list:
        if self._list_ports is not None:
            return self._list_ports()
        try:
            from serial.tools import list_ports
        except ImportError:
            return []
        return [p.device for p in list_ports.comports()]

    def _serial_loop(self) -> None:
        """Find a COM port that speaks NMEA (a USB GPS) and read it; look again every
        minute when none is plugged in or it is unplugged."""
        try:
            import serial  # pyserial
        except ImportError:
            return
        while not self._stop.is_set():
            for port in self._ports():
                for baud in (9600, 4800, 38400, 115200):
                    try:
                        with serial.Serial(port, baud, timeout=1.5) as ser:
                            deadline, nmea = time.time() + 3, False
                            while time.time() < deadline and not nmea:
                                nmea = ser.readline().startswith(b"$G")
                            if not nmea:
                                continue
                            self.serial_port = port
                            logger.info("GPS receiver found on %s.", port)
                            while not self._stop.is_set():
                                raw = ser.readline()
                                if raw:
                                    self.feed(raw.decode("ascii", "ignore"), f"the GPS receiver on {port}")
                    except Exception:  # noqa: BLE001 - busy, unplugged or not a GPS
                        pass
                    if self.serial_port == port:
                        self.serial_port = ""
                        break
            self._stop.wait(60)


_HUB: Optional[GpsHub] = None
_HUB_LOCK = threading.Lock()


def get_hub(config=None, start: bool = True) -> Optional[GpsHub]:
    """The one GpsHub for this run. Its sources start when VERONICA starts (start_gps);
    with start=False nothing is created, so tests and tools never open ports on their own."""
    global _HUB
    with _HUB_LOCK:
        if _HUB is None and start:
            g = getattr(config, "gps", None)
            _HUB = GpsHub(udp_port=getattr(g, "udp_port", DEFAULT_UDP_PORT) or DEFAULT_UDP_PORT,
                          tcp=getattr(g, "tcp", "") or "",
                          scan_serial=getattr(g, "scan_serial", True))
        return _HUB


def start_gps(config) -> Optional[GpsHub]:
    """Called once at startup: look for a USB GPS, and listen for the phone if the user
    turned that on before."""
    g = getattr(config, "gps", None)
    if g is not None and not getattr(g, "enabled", True):
        return None
    hub = get_hub(config)
    hub.start(udp=_phone_wanted(config))
    return hub


def current_fix() -> Optional[Fix]:
    hub = get_hub(start=False)
    return hub.latest() if hub is not None else None


# -- "use my phone as GPS" is remembered ------------------------------------------
def _state_path(config):
    from pathlib import Path
    return config.resolve_path("data/gps.json") if config is not None and hasattr(config, "resolve_path")         else Path("data") / "gps.json"


def _phone_wanted(config) -> bool:
    from veronica.core import jsonsafe
    try:
        data, _err = jsonsafe.read_json(_state_path(config), {})
        return bool((data or {}).get("phone_udp"))
    except Exception:  # noqa: BLE001
        return False


def set_phone_wanted(config, on: bool) -> None:
    from veronica.core import jsonsafe
    path = _state_path(config)
    data, _err = jsonsafe.read_json(path, {})
    data = dict(data or {})
    data["phone_udp"] = bool(on)
    path.parent.mkdir(parents=True, exist_ok=True)
    jsonsafe.write_json(path, data)
