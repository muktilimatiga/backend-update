import asyncio
import re
import telnetlib3
import logging
from typing import Optional, Dict, Any
from jinja2 import Environment, FileSystemLoader

from core import PACKAGE_OPTIONS, OLT_OPTIONS, COMMAND_TEMPLATES
from schemas.config_handler import (
    UnconfiguredOnt,
    ConfigurationRequest,
    ConfigurationBridgeRequest,
)
import yaml

logging.basicConfig(level=logging.INFO)
logging.getLogger("telnetlib3").setLevel(logging.ERROR)

try:
    jinja_env = Environment(
        loader=FileSystemLoader("templates"), trim_blocks=True, lstrip_blocks=True
    )
except Exception as e:
    logging.error(f"[FATAL ERROR] Tidak dapat memuat folder 'templates' Jinja2: {e}")
    jinja_env = None


class TelnetClient:
    def __init__(
        self, host: str, username: str, password: str, is_c600: bool, olt_name: str = ""
    ):
        self.host = host
        self.username = username
        self.password = password
        self.is_c600 = is_c600
        self.olt_name = olt_name
        self._lock = None
        self.reader = None
        self.writer = None
        self.last_activity = 0
        self._prompt_re = re.compile(r"(.+[>#])\s*$")
        self._pagination_prompt = "--More--"

    @property
    def lock(self):
        # Lazy Load: Lock baru dibuat saat pertama kali dipanggil di dalam Loop yang benar
        # Also recreate if the loop has changed (fixes "Future attached to different loop" error)
        try:
            current_loop = asyncio.get_running_loop()
            if self._lock is None or self._lock._loop is not current_loop:
                self._lock = asyncio.Lock()
        except RuntimeError:
            # No running loop, create lock anyway (will attach to the loop when used)
            if self._lock is None:
                self._lock = asyncio.Lock()
        return self._lock

    def _format_olt_interface(self, interface: str) -> str:
        """Format interface with OLT prefix (gpon_olt- or gpon-olt_)"""
        if interface.startswith("gpon"):
            return interface
        prefix = "gpon_olt-" if self.is_c600 else "gpon-olt_"
        return f"{prefix}{interface}"

    def _format_onu_interface(self, interface: str) -> str:
        """Format interface with ONU prefix (gpon_onu- or gpon-onu_)"""
        if interface.startswith("gpon"):
            return interface
        prefix = "gpon_onu-" if self.is_c600 else "gpon-onu_"
        return f"{prefix}{interface}"

    def _format_vport_interface(self, interface: str) -> str:
        """
        Format interface with vport prefix.
        Example: 1/2/1:2 -> vport-1/2/1:2:1
        The ':1' is always appended for vport.
        """
        if interface.startswith("vport"):
            return interface
        # Keep ':' separator and append ':1'
        logging.info(f"Formatted interface: {interface}")
        return f"vport-{interface}:1"

    @staticmethod
    def _parse_onu_id(interface: str) -> int:
        """Parse ONU ID from interface string like '1/1/1:1' -> returns 1"""
        if ":" in interface:
            return int(interface.split(":")[-1])
        raise ValueError(
            f"Invalid interface format: {interface}, expected format like '1/1/1:1'"
        )

    @staticmethod
    def _parse_base_interface(interface: str) -> str:
        """Parse base interface without ONU ID: '1/1/1:111' -> '1/1/1'"""
        if ":" in interface:
            return interface.split(":")[0]
        return interface

    def _config_interface_commands(self, interface: str) -> list[str]:
        """Generate common 'configure terminal' + 'interface' command list"""
        return ["configure terminal\n", f"interface {interface}"]

    def _get_action_commands(self, action: str, **kwargs) -> list[str]:
        """Get action-specific commands from templates with placeholder substitution"""
        device = "c600" if self.is_c600 else "c300"
        template = COMMAND_TEMPLATES.get(action, {}).get(device, [])
        return [cmd.format(**kwargs) for cmd in template]

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()
        return False

    async def connect(self):
        """Fungsi connect manual"""
        if self.writer and not self.writer.is_closing():
            return  # Sudah konek, skip

        logging.info(f"🔌 Membuka koneksi baru ke {self.host}...")
        self.reader, self.writer = await asyncio.wait_for(
            telnetlib3.open_connection(self.host, 23), timeout=20
        )
        await self._login()
        await self._disable_pagination()
        self.last_activity = asyncio.get_event_loop().time()

    async def close(self):
        """Fungsi close manual"""
        if self.writer:
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except:
                pass
        self.writer = None
        self.reader = None

    async def _read_until_prompt(self, timeout: int = 20) -> str:
        """
        Simplified reader. It ONLY looks for the main prompt.
        It does NOT check for "Username:"
        """
        if not self.reader:
            raise ConnectionError("Telnet reader is not available.")
        try:
            data = ""
            while True:
                chunk = await asyncio.wait_for(self.reader.read(1024), timeout=timeout)
                if not chunk:
                    break
                data += chunk

                # --- Re-login check is REMOVED ---

                if re.search(self._prompt_re, data):
                    break

                if self._pagination_prompt in data:
                    if not self.writer:
                        raise ConnectionError("Writer closed during pagination.")
                    self.writer.write(" ")
                    await self.writer.drain()
                    data = data.replace(self._pagination_prompt, "")
            return data
        except asyncio.TimeoutError:
            logging.warning(f"Timeout waiting for prompt from {self.host}")
            # This will now just raise the error and fail the request,
            # which is what you want.
            raise
        except Exception as e:
            raise ConnectionError(f"Error reading from OLT {self.host}: {e}")

    async def _login(self, timeout: int = 20):
        """
        Simple, one-time login function.
        """
        try:
            await asyncio.wait_for(self.reader.readuntil(b"Username:"), timeout=timeout)
            self.writer.write(self.username + "\n")

            await asyncio.wait_for(self.reader.readuntil(b"Password:"), timeout=timeout)
            self.writer.write(self.password + "\n")

            # Use the simple reader to wait for the main prompt
            await self._read_until_prompt(timeout=timeout)

            logging.info(f"Successfully logged in to OLT {self.host}")

        except asyncio.TimeoutError:
            await self.close()
            raise ConnectionError(f"Timeout during login to {self.host}")
        except Exception as e:
            await self.close()
            raise ConnectionError(f"Failed to login: {e}")

    async def _disable_pagination(self):
        if not self.writer:
            raise ConnectionError("Writer not available to disable pagination.")

        logging.info(f"Disabling pagination on {self.host}...")
        await self._execute_command("terminal length 0", timeout=20)
        logging.info(f"Pagination disabled on {self.host}.")

    async def _execute_command(self, command: str, timeout: int = 20) -> str:
        """
        Simplified executor. It does NOT try to re-login.
        """
        if not self.reader or not self.writer:
            raise ConnectionError("Connection not established to execute command.")
        if not command:
            return ""

        # --- Re-login try/except block is REMOVED ---

        self.writer.write(command + "\n")
        await asyncio.wait_for(self.writer.drain(), timeout=10)
        raw_output = await self._read_until_prompt(timeout=timeout)

        cleaned_lines = []
        lines = raw_output.splitlines()

        if len(lines) > 2:
            for line in lines[1:-1]:
                stripped = line.strip()
                if stripped:
                    cleaned_lines.append(stripped)

        return "\n".join(cleaned_lines)

    @staticmethod
    def _parse_onu_detail_output(raw_output: str) -> Dict[str, Any]:
        kv_regex = re.compile(r"^\s*([^:]+?):\s+(.*?)\s*$")
        log_regex = re.compile(
            r"^\s*(\d+)\s+([\d-]{10}\s[\d:]{8})\s+([\d-]{10}\s[\d:]{8})\s*(.*)$"
        )

        parsed_data = {}
        log_lines = []

        for line in raw_output.splitlines():
            log_match = log_regex.search(line)
            if log_match:
                log_lines.append(line)
                continue

            kv_match = kv_regex.search(line)
            if kv_match:
                key = kv_match.group(1).strip()
                value = kv_match.group(2).strip()

                if value:
                    parsed_data[key] = value

        final_result = {
            "type": parsed_data.get("Type"),
            "phase_state": parsed_data.get("Phase state"),
            "serial_number": parsed_data.get("Serial number"),
            "onu_distance": parsed_data.get("ONU Distance"),
            "online_duration": parsed_data.get("Online Duration"),
            "modem_logs": "\n".join(log_lines[-2:]),
        }

        return final_result

    @staticmethod
    def _parse_onu_ip_host(raw_output: str) -> str:
        # Regex to find lines starting with "Current IP address:"
        # and capture the value
        ip_regex = re.compile(r"^\s*Current IP address:\s+(\S+)", re.MULTILINE)

        # Find all matches (because there can be multiple Host IDs)
        matches = ip_regex.finditer(raw_output)

        for match in matches:
            ip_address = match.group(1)
            # Check if it's a real, assigned IP
            if ip_address and ip_address != "0.0.0.0" and ip_address != "N/A":
                return ip_address  # Return the first valid IP found

        # If no valid IP was found, return a default
        return "0.0.0.0"

    @staticmethod
    def _parse_onu_attenuation(raw_output: str) -> str:
        # Regex to find the line starting with "down",
        # then capture the (Rx:...) part
        attenuation_regex = re.compile(
            r"^\s*down\s+.*\s+(Rx:[-.\d]+\(dbm\))", re.MULTILINE
        )

        match = attenuation_regex.search(raw_output)

        if match:
            # Return the captured group, e.g., "Rx:-24.317(dbm)"
            return match.group(1)

        # Return N/A if the line wasn't found
        return "N/A"

    @staticmethod
    def _parse_interface_admin_status(raw_output: str, target_interface: str) -> dict:
        parser_regex = re.compile(
            rf"Interface\s+:\s+({re.escape(target_interface)}).*?Admin status\s+:\s+(\S+)",
            re.DOTALL,
        )

        match = parser_regex.search(raw_output)

        is_unlocked_status = False

        if match:
            admin_status_str = match.group(2)

            if admin_status_str.lower() == "unlock":
                is_unlocked_status = True

        return {"is_unlocked": is_unlocked_status}

    @staticmethod
    def _parse_onu_dba(raw_output: str) -> str:
        """
        Parse DBA rate from bandwidth output.
        Gets the rate from the second data line (GPON channel).
        Example:
          gpon_olt-1/3/1    1(XG-PON)   0            2488320    FALSE    0.0   <- skip
          gpon_olt-1/3/1    2(GPON)     485100       759060     FALSE    39.0  <- get this
        Returns: "39.0%"
        """
        # Find all lines with rate values at the end (format: ...  XX.X)
        rate_regex = re.compile(r"gpon[_-]olt[_-]\S+.*\s+([\d.]+)\s*$", re.MULTILINE)

        matches = rate_regex.findall(raw_output)

        # Get the second match (GPON line, index 1)
        if len(matches) >= 2:
            return f"{matches[1]}%"
        elif len(matches) == 1:
            return f"{matches[0]}%"

        return "0.0%"

    @staticmethod
    def _parse_eth_port_statuses(raw_output: str) -> list[dict]:
        """
        Parses all eth ports with admin status and speed status.
        Returns list of dicts with:
        - interface: eth port name (e.g., "eth_0/1")
        - is_unlocked: True if admin status is "unlock", False if "lock"
        - speed_status: raw speed value (e.g., "full-100", "full-10", "unknown")
        - lan_detected: True if speed_status is not "unknown" (cable connected)
        - speed_mbps: detected speed in Mbps (100, 10, 1000) or None if unknown
        """
        results = []

        # Regex to capture Interface, Speed status, and Admin status
        parser_regex = re.compile(
            r"Interface\s+:\s+(eth_\d+/\d+).*?"
            r"Speed status\s+:\s+(\S+).*?"
            r"Admin status\s+:\s+(\S+)",
            re.DOTALL,
        )

        matches = parser_regex.finditer(raw_output)

        for match in matches:
            interface_name = match.group(1)
            speed_status = match.group(2)
            admin_status = match.group(3)

            is_unlocked = admin_status.lower() == "unlock"

            # LAN detected only if speed shows actual connection (full-100, half-10, etc.)
            # "auto" or "unknown" means no cable connected
            speed_lower = speed_status.lower()
            lan_detected = "full-" in speed_lower or "half-" in speed_lower

            # Parse speed in Mbps from speed_status like "full-100", "full-10", "half-100"
            speed_mbps = None
            if lan_detected:
                speed_match = re.search(r"(\d+)", speed_status)
                if speed_match:
                    speed_mbps = int(speed_match.group(1))

            results.append(
                {
                    "interface": interface_name,
                    "is_unlocked": is_unlocked,
                    "speed_status": speed_status,
                    "lan_detected": lan_detected,
                    "speed_mbps": speed_mbps,
                }
            )

        return results

    # MAIN ONU COMMNAD

    async def get_onu_detail(self, interface: str) -> str:
        """cek ONU detail"""
        full_interface = self._format_onu_interface(interface)
        commands = self._get_action_commands("detail_onu", interface=full_interface)

        try:
            for cmd in commands:
                output = await self._execute_command(cmd)
            return output
        except Exception as e:
            logging.error(f"Failed to get ONU detail for {full_interface}: {e}")
            return f"Error: {e}"

    async def get_gpon_onu_state(self, interface: str) -> str:
        """
        Cek 1 port
        """
        full_interface = self._format_olt_interface(interface)
        commands = self._get_action_commands("port_state", interface=full_interface)

        try:
            for cmd in commands:
                output = await self._execute_command(cmd)
            return output
        except Exception as e:
            logging.error(f"Failed during reboot for {full_interface}: {e}")
            return f"cek 1 port failed: {e}"

    async def get_olt_state(self) -> str:
        """
        Cek state OLT
        """
        commands = self._get_action_commands("cek_state_olt")

        try:
            for cmd in commands:
                output = await self._execute_command(cmd)
            return output
        except Exception as e:
            logging.error(f"Failed to get OLT state: {e}")
            return f"cek state olt failed: {e}"

    async def get_attenuation(self, interface: str) -> str:
        """
        Cek redaman onu
        """
        full_interface = self._format_onu_interface(interface)
        commands = self._get_action_commands("redaman_onu", interface=full_interface)

        try:
            for cmd in commands:
                output = await self._execute_command(cmd)
            return output
        except Exception as e:
            logging.error(f"Failed during reboot for {full_interface}: {e}")
            return f"cek redaman failed: {e}"

    async def get_onu_rx(self, interface: str) -> str:
        """
        Cek redaman 1 port
        """
        full_interface = self._format_olt_interface(interface)
        commands = self._get_action_commands("port_redaman", interface=full_interface)

        try:
            for cmd in commands:
                output = await self._execute_command(cmd)
            return output
        except Exception as e:
            logging.error(f"Failed during reboot for {full_interface}: {e}")
            return f"cek redaman 1 port failed: {e}"

    async def send_reboot_command(self, interface: str) -> str:
        """Memberi perintah reboot ke ONU"""
        full_interface = self._format_onu_interface(interface)
        commands = self._config_interface_commands(full_interface)
        commands.extend(self._get_action_commands("reboot", interface=full_interface))
        logging.info(f"Reboot command for {interface}: {commands}")

        try:
            for cmd in commands:
                await self._execute_command(cmd)
            return "Reboot success"
        except Exception as e:
            logging.error(f"Failed during reboot for {interface}: {e}")
            return f"Reboot failed: {e}"

    async def send_no_onu(self, interface: str) -> str:
        """Delete an ONU from OLT"""
        base_interface = self._parse_base_interface(interface)  # "1/1/1:111" -> "1/1/1"
        full_interface = self._format_olt_interface(
            base_interface
        )  # "1/1/1" -> "gpon_olt-1/1/1"
        onu_id = self._parse_onu_id(interface)  # "1/1/1:111" -> 111

        commands = self._config_interface_commands(full_interface)
        commands.extend(
            self._get_action_commands(
                "delete_onu", onu_id=onu_id, interface=full_interface
            )
        )

        try:
            for cmd in commands:
                await self._execute_command(cmd)
            logging.info(f"Deleted ONU {onu_id} from {full_interface}")
            return "No Onu Success"
        except Exception as e:
            logging.error(f"Failed to delete ONU {onu_id} from {full_interface}: {e}")
            return f"No Onu Failed: {e}"

    async def send_new_sn(self, interface: str, sn: str) -> str:
        """Re-register ONU with new serial number"""
        full_interface = self._format_onu_interface(interface)
        commands = self._config_interface_commands(full_interface)
        commands.extend(self._get_action_commands("change_sn", sn=sn))

        try:
            for cmd in commands:
                await self._execute_command(cmd)
            logging.info(f"Changed SN to {sn} on {full_interface}")
            return "Reconfig Success dengan SN: {sn}"
        except Exception as e:
            logging.error(f"Failed to change SN on {full_interface}: {e}")
            return f"Reconfig Failed: {e}"

    async def get_eth_port_statuses(self, interface: str) -> list[dict]:
        """
        Cek eth port lock/unlock dan deteksi LAN.
        Returns list of dicts with interface, is_unlocked, speed_status, lan_detected, speed_mbps
        """
        full_interface = self._format_onu_interface(interface)
        commands = self._get_action_commands("cek_port", interface=full_interface)

        try:
            for cmd in commands:
                output = await self._execute_command(cmd)

            parsed_statuses = TelnetClient._parse_eth_port_statuses(output)
            return parsed_statuses
        except Exception as e:
            logging.error(f"Failed to check port statuses for {full_interface}: {e}")
            return []

    async def edit_eth_port(self, interface: str, is_unlocked: bool) -> str:
        """
        Edit port lock/unlock for all 4 ethernet ports.
        Args:
            interface: ONU interface (e.g., "gpon-onu_1/1/1:1")
            is_unlocked: True = unlock all ports, False = lock all ports
        """
        full_interface = self._format_onu_interface(interface)
        state = "unlock" if is_unlocked else "lock"
        commands = self._config_interface_commands(full_interface)
        commands.extend(
            self._get_action_commands(
                "edit_port", interface=full_interface, state=state
            )
        )

        try:
            for cmd in commands:
                await self._execute_command(cmd)
            logging.info(f"Edited port lock/unlock on {full_interface}")
            return "Edit port lock/unlock berhasil"
        except Exception as e:
            logging.error(f"Failed to edit port lock/unlock on {full_interface}: {e}")
            return f"Edit port lock/unlock gagal: {e}"

    async def edit_capacity_onu(self, interface: str, new_capacity: str) -> str:
        """
        Edit capacity ONU.
        Args:
            interface: ONU interface (e.g., "1/1/1:1")
            new_capacity: Package from frontend (e.g., "10M")

        UP profile: Uses PACKAGE_OPTIONS + rate suffix (e.g., "UP-10MB-FIX")
        DOWN profile: Uses frontend value directly (e.g., "DOWN-10M")
        """
        full_interface = self._format_onu_interface(interface)
        vport_interface = self._format_vport_interface(interface)

        # Get DBA rate to determine suffix
        base_interface = self._parse_base_interface(interface)
        rate = await self.get_dba_rate(base_interface)
        up_profile_suffix = "-MBW" if rate > 75.0 else "-FIX"

        # Lookup PACKAGE_OPTIONS: "10M" -> "10MB"
        base_paket_name = PACKAGE_OPTIONS.get(new_capacity)
        if not base_paket_name:
            raise ValueError(
                f"Paket '{new_capacity}' tidak valid. Pilihan: {list(PACKAGE_OPTIONS.keys())}"
            )

        # UP profile: "10MB" + "-FIX" = "10MB-FIX"
        up_profile = f"{base_paket_name}{up_profile_suffix}"
        # DOWN profile: use frontend value directly = "10M"
        down_profile = new_capacity

        logging.info(f"Edit capacity: UP-{up_profile} / DOWN-{down_profile}")

        commands = self._config_interface_commands(full_interface)
        commands.extend(
            self._get_action_commands(
                "change_capacity",
                interface=full_interface,
                vport_interface=vport_interface,
                up_profile=up_profile,
                down_profile=down_profile,
            )
        )

        try:
            for cmd in commands:
                await self._execute_command(cmd)
            logging.info(f"Edited capacity on {full_interface}")
            return "Edit capacity berhasil"
        except Exception as e:
            logging.error(f"Failed to edit capacity on {full_interface}: {e}")
            return f"Edit capacity gagal: {e}"

    async def get_onu_ip_host(self, interface: str) -> str:
        """
        Cek IP Host ONU (Current IP address)
        """
        full_interface = self._format_onu_interface(interface)
        commands = self._get_action_commands("cek_ip", interface=full_interface)

        try:
            for cmd in commands:
                output = await self._execute_command(cmd)

            # Parse to get Current IP address
            parsed_ip = TelnetClient._parse_onu_ip_host(output)
            return parsed_ip
        except Exception as e:
            logging.error(f"Failed to get IP host for {full_interface}: {e}")
            return "0.0.0.0"

    async def get_running_config(self, interface: str) -> dict:
        """
        Cek running config ONU, onu running config.
        Returns dict with:
        - running_config: output from "show running-config interface"
        - onu_running_config: output from "show onu running config"
        """
        full_interface = self._format_onu_interface(interface)
        vport_interface = self._format_vport_interface(interface)
        commands = self._get_action_commands(
            "running_config", interface=full_interface, vport_interface=vport_interface
        )

        try:
            outputs = []
            for cmd in commands:
                output = await self._execute_command(cmd)
                outputs.append(output)

            logging.info(f"Get running config for {full_interface}")

            # Return as separate keys
            return {
                "running_config": outputs[0] if len(outputs) > 0 else "",
                "onu_running_config": outputs[1] if len(outputs) > 1 else "",
            }
        except Exception as e:
            logging.error(f"Failed to get running config for {full_interface}: {e}")
            return {"running_config": "", "onu_running_config": ""}

    async def get_onu_dba(self, interface: str) -> str:
        """
        Cek DBA ONU
        """
        full_interface = self._format_olt_interface(interface)
        commands = self._get_action_commands("cek_dba", interface=full_interface)

        try:
            for cmd in commands:
                output = await self._execute_command(cmd)

            # Parse to get DBA information
            parsed_dba = TelnetClient._parse_onu_dba(output)
            return parsed_dba
        except Exception as e:
            logging.error(f"Failed to get DBA for {full_interface}: {e}")
            return ""

    # Cek monitoring & mitra

    @staticmethod
    def _parse_olt_monitoring(raw_output: str) -> str:
        """
        Parse OLT monitoring output to extract interface status and description.

        Example input:
            gei_1/19/3 is up,  line protocol is up,  detect status is OK
              Description is TO-MONITORING-KAUMAN
              The port negotiation is enable
              ... (more lines)

        Returns:
            First two lines: interface status + description
        """
        lines = raw_output.strip().splitlines()

        if not lines:
            return ""

        result_lines = []

        # First line: interface status (e.g., "gei_1/19/3 is up,  line protocol is up,  detect status is OK")
        if lines:
            result_lines.append(lines[0].strip())

        # Second line: description (e.g., "Description is TO-MONITORING-KAUMAN")
        if len(lines) > 1:
            result_lines.append(lines[1].strip())

        return "\n".join(result_lines)

    @staticmethod
    def _parse_rx_monitoring(raw_output: str) -> str:
        """
        Parse optical module info output to extract Diagnostic-info section.

        Example input:
            ...
            Diagnostic-info:
             RxPower        : -7.693    (dbm)          TxPower      : -1.850(dbm)
             TxBias-Current : 6.142     (mA)           Laser-Rate   : 103(100Mb/s)
             Temperature    : 38.914    (c)            Supply-Vol   : 3.251(v)
            Alarm-thresh:
            ...

        Returns:
            Diagnostic-info section with header and 3 data lines
        """
        lines = raw_output.strip().splitlines()

        result_lines = []
        in_diagnostic_section = False

        for line in lines:
            stripped = line.strip()

            # Start capturing when we hit "Diagnostic-info:"
            if stripped.startswith("Diagnostic-info"):
                in_diagnostic_section = True
                result_lines.append(stripped)
                continue

            # Stop capturing when we hit the next section (Alarm-thresh, etc.)
            if (
                in_diagnostic_section
                and stripped.endswith(":")
                and not stripped.startswith("RxPower")
                and not stripped.startswith("TxBias")
                and not stripped.startswith("Temperature")
            ):
                break

            # Capture lines within Diagnostic-info section
            if in_diagnostic_section and stripped:
                result_lines.append(stripped)

        return "\n".join(result_lines)

    async def get_olt_monitoring(self, interface: str) -> str:
        """cek interface dari monitroing / mitra"""
        interface = interface
        commands = self._get_action_commands("cek_monitoring", interface=interface)

        try:
            for cmd in commands:
                output = await self._execute_command(cmd)

            parsed_monitoring = TelnetClient._parse_olt_monitoring(output)
            return parsed_monitoring
        except Exception as e:
            logging.error(f"Failed to get monitoring for {interface}: {e}")
            return ""

    async def get_rx_monitoring(self, interface: str) -> str:
        """cek rx monitoring"""
        interface = interface
        commands = self._get_action_commands("cek_rx_monitoring", interface=interface)

        try:
            for cmd in commands:
                output = await self._execute_command(cmd)

            parsed_rx_monitoring = TelnetClient._parse_rx_monitoring(output)
            return parsed_rx_monitoring
        except Exception as e:
            logging.error(f"Failed to get rx monitoring for {interface}: {e}")
            return ""

    # Config

    async def find_unconfigured_onts(self) -> list[UnconfiguredOnt]:
        command = "show pon onu uncfg" if self.is_c600 else "show gpon onu uncfg"
        full_output = await self._execute_command(command)
        found_onts = []

        for item in full_output.strip().splitlines():
            if ("GPON" in item and self.is_c600) or (
                "unknown" in item and not self.is_c600
            ):
                pon_slot, pon_port, sn = None, None, None
                try:
                    if self.is_c600:
                        parts = re.split(r"\s+", item.strip())
                        if len(parts) >= 2:
                            interface_str, sn = parts[0], parts[1]
                            match = re.search(r"1/(\d+)/(\d+)", interface_str)
                            if match:
                                pon_port, pon_slot = match.groups()
                    else:
                        x = item.replace("        ", " ").replace(" ", ";")
                        splitter_1 = re.split(";", x)
                        splitter_2 = re.split("/", splitter_1[0])
                        splitter_3 = re.split(":", splitter_2[2])
                        sn = (
                            splitter_1[1] if int(splitter_3[0]) >= 10 else splitter_1[2]
                        )
                        pon_slot, pon_port = splitter_2[1], splitter_3[0]

                    if all((pon_slot, pon_port, sn)):
                        found_onts.append(
                            UnconfiguredOnt(sn=sn, pon_port=pon_port, pon_slot=pon_slot)
                        )
                except (IndexError, ValueError):
                    continue

        logging.info(f"📱 Ditemukan {len(found_onts)} ONT uncfg.")
        return found_onts

    async def find_next_available_onu_id(self, interface: str) -> int:
        logging.info(f"🔍 Mencari ID ONU yang kosong di {interface}...")
        cmd = f"show gpon onu state {interface}"
        output = await self._execute_command(cmd)
        active_onus = []
        identifier = "enable" if self.is_c600 else "1(GPON)"

        for line in output.splitlines():
            if identifier in line:
                try:
                    splitter_1 = re.split(r"\s+", line.strip())[0]
                    splitter_2 = re.split(":", splitter_1)[-1]
                    active_onus.append(int(splitter_2))
                except (IndexError, ValueError):
                    continue

        if not active_onus:
            return 1

        active_onus.sort()
        calculation = 1
        for onu_id in active_onus:
            if onu_id != calculation:
                break
            calculation += 1

        if calculation > 128:
            raise ValueError(f"Port PON {interface} penuh.")

        logging.info(f"Onu ID kosong ditemukan pada {interface}:{calculation}")
        return calculation

    async def get_dba_rate(self, interface: str) -> float:
        # The command to check bandwidth
        full_interface = self._format_olt_interface(interface)
        command = f"show pon bandwidth dba interface {full_interface}"

        output = await self._execute_command(command)

        # Debug log to see what the script actually saw
        logging.info(f"DBA OUTPUT RAW: {output} || {command}")

        # Find all rates from lines containing the interface
        # Format: interface | channel | configured(kbps) | free(kbps) | rate(x.x%)
        # Example: gpon-olt_1/3/3      1(GPON)      683760           560400          55.0
        rate_regex = re.compile(
            rf"{re.escape(interface)}\s+\S*GPON\S*\s+\d+\s+\d+\s+([\d.]+)",
            re.IGNORECASE,
        )

        match = rate_regex.search(output)

        if match:
            rate_str = match.group(1)
            logging.info(f"DBA Rate found: {rate_str}%")
            return float(rate_str)

        # Fallback: try simpler pattern - just get the last number on lines with the interface
        fallback_regex = re.compile(
            rf"{re.escape(interface)}\s+.*?([\d.]+)\s*$", re.MULTILINE
        )
        fallback_match = fallback_regex.search(output)

        if fallback_match:
            rate_str = fallback_match.group(1)
            logging.info(f"DBA Rate found (fallback): {rate_str}%")
            return float(rate_str)

        logging.warning(f"Could not parse DBA rate for {interface}. Defaulting to 0.0")
        return 0.0

    async def apply_configuration(self, config_request: ConfigurationRequest):
        logs = []
        current_step = "Inisialisasi"

        try:
            # Step 1: Find unconfigured ONTs
            current_step = "Mencari ONT unconfigured"
            logs.append(f"STEP > {current_step}...")
            ont_list = await self.find_unconfigured_onts()

            target_ont = next(
                (ont for ont in ont_list if ont.sn == config_request.sn), None
            )
            if not target_ont:
                raise LookupError(
                    f"ONT dengan SN '{config_request.sn}' tidak ditemukan di daftar unconfigured. Pastikan ONT sudah terpasang dan belum dikonfigurasi."
                )
            logs.append(
                f"INFO < ONT ditemukan: Slot {target_ont.pon_slot}, Port {target_ont.pon_port}"
            )

            # Step 2: Build interface names
            current_step = "Menyiapkan interface"
            base_iface = f"gpon-olt_1/{target_ont.pon_slot}/{target_ont.pon_port}"
            if self.is_c600:
                base_iface = f"gpon_olt-1/{target_ont.pon_port}/{target_ont.pon_slot}"
            logs.append(f"INFO < Base interface: {base_iface}")

            # Step 3: Find available ONU ID
            current_step = "Mencari ONU ID kosong"
            logs.append(f"STEP > {current_step}...")
            onu_id = await self.find_next_available_onu_id(base_iface)
            logs.append(f"INFO < ONU ID tersedia: {onu_id}")



            # Modem type mapping
            modem_mapping = {"F670L": "ZTEG-F670", "F609": "ZTEG-F609"}
            olt_profile_type = modem_mapping.get(config_request.modem_type, "ALL")
            vlan = OLT_OPTIONS[self.olt_name]["vlan"]

            iface_onu = f"{'gpon_onu-1' if self.is_c600 else 'gpon-onu_1'}/{target_ont.pon_slot}/{target_ont.pon_port}:{onu_id}"
            if self.is_c600:
                iface_onu = (
                    f"gpon_onu-1/{target_ont.pon_port}/{target_ont.pon_slot}:{onu_id}"
                )

            # Prepare ETH locks (copy to avoid mutating the request object)
            locks = list(config_request.eth_locks)
            if len(locks) == 1:
                locks = locks * 4
            elif len(locks) < 4:
                locks.extend([False] * (4 - len(locks)))

            # Step 6: Render template
            current_step = "Render template konfigurasi"
            logs.append(f"STEP > {current_step}...")

            context = {
                "interface_olt": base_iface,
                "interface_onu": iface_onu,
                "pon_slot": target_ont.pon_slot,
                "pon_port": target_ont.pon_port,
                "onu_id": onu_id,
                "sn": config_request.sn,
                "customer": config_request.customer,
                "vlan": vlan,
                "jenismodem": olt_profile_type,
                "eth_locks": locks,
            }

            template_name = "config_c600.yaml" if self.is_c600 else "config_c300.yaml"

            def _render_and_parse_yaml():
                if jinja_env is None:
                    raise RuntimeError(
                        "Jinja2 environment not loaded. Cek folder templates."
                    )
                template = jinja_env.get_template(template_name)
                rendered = template.render(context)
                return yaml.safe_load(rendered)

            commands = await asyncio.to_thread(_render_and_parse_yaml)
            logs.append(
                f"INFO < Template berhasil di-render. Total commands: {len(commands)}"
            )

            # Step 7: Execute commands
            current_step = "Eksekusi perintah konfigurasi"
            logs.append(f"STEP > {current_step}...")

            for idx, cmd in enumerate(commands, 1):
                current_step = f"Eksekusi command {idx}/{len(commands)}: {cmd[:50]}..."
                logs.append(f"CMD > {cmd}")
                logging.info(f"➡️ Executing: {cmd}")
                output = await self._execute_command(cmd)
                if output:
                    logs.append(f"LOG < {output}")
                    # Check for common error patterns in OLT output
                    if (
                        "error" in output.lower()
                        or "invalid" in output.lower()
                        or "failed" in output.lower()
                    ):
                        raise RuntimeError(
                            f"OLT mengembalikan error pada command: {cmd}\nOutput: {output}"
                        )
                await asyncio.sleep(0.3)

            # SUCCESS - Build report
            logs.append("STEP > Konfigurasi selesai!")

            report = "\n".join(
                [
                    "=========================================================",
                    "              KONFIGURASI BERHASIL                       ",
                    "=========================================================",
                    f"  Serial Number      : {config_request.sn}",
                    f"  ID Pelanggan       : {config_request.customer.pppoe_user}",
                    f"  Nama Pelanggan     : {config_request.customer.name}",
                    f"  OLT dan ONU        : {iface_onu}",
                    "=========================================================",
                ]
            )

            summary = {
                "status": "success",
                "message": "Konfigurasi berhasil",
                "serial_number": config_request.sn,
                "pppoe_user": config_request.customer.pppoe_user,
                "name": config_request.customer.name,
                "location": iface_onu,
                "profile": olt_profile_type,
                "report": report,
            }

            return logs, summary

        except LookupError as e:
            # ONT not found - specific error
            error_report = "\n".join(
                [
                    "=========================================================",
                    "              KONFIGURASI GAGAL                          ",
                    "=========================================================",
                    f"  Error Type         : ONT Tidak Ditemukan",
                    f"  Step               : {current_step}",
                    f"  Serial Number      : {config_request.sn}",
                    f"  Detail             : {str(e)}",
                    "=========================================================",
                ]
            )
            logs.append(f"ERROR < {str(e)}")

            summary = {
                "status": "error",
                "message": f"Gagal: {str(e)}",
                "serial_number": config_request.sn,
                "pppoe_user": config_request.customer.pppoe_user,
                "name": config_request.customer.name,
                "location": "-",
                "profile": "-",
                "report": error_report,
            }
            return logs, summary

        except (ConnectionError, asyncio.TimeoutError) as e:
            # Connection/timeout error
            error_report = "\n".join(
                [
                    "=========================================================",
                    "              KONFIGURASI GAGAL                          ",
                    "=========================================================",
                    f"  Error Type         : Koneksi / Timeout",
                    f"  Step               : {current_step}",
                    f"  Serial Number      : {config_request.sn}",
                    f"  Detail             : {str(e)}",
                    "=========================================================",
                ]
            )
            logs.append(f"ERROR < Connection/Timeout: {str(e)}")

            summary = {
                "status": "error",
                "message": f"Gagal: Koneksi timeout - {str(e)}",
                "serial_number": config_request.sn,
                "pppoe_user": config_request.customer.pppoe_user,
                "name": config_request.customer.name,
                "location": "-",
                "profile": "-",
                "report": error_report,
            }
            return logs, summary

        except Exception as e:
            # Generic error
            error_report = "\n".join(
                [
                    "=========================================================",
                    "              KONFIGURASI GAGAL                          ",
                    "=========================================================",
                    f"  Error Type         : {type(e).__name__}",
                    f"  Step               : {current_step}",
                    f"  Serial Number      : {config_request.sn}",
                    f"  Detail             : {str(e)}",
                    "=========================================================",
                    "",
                    "Silakan cek logs untuk detail lebih lanjut.",
                    "=========================================================",
                ]
            )
            logs.append(f"ERROR < {type(e).__name__}: {str(e)}")
            logging.error(f"Configuration failed at step '{current_step}': {e}")

            summary = {
                "status": "error",
                "message": f"Gagal pada step '{current_step}': {str(e)}",
                "serial_number": config_request.sn,
                "pppoe_user": config_request.customer.pppoe_user,
                "name": config_request.customer.name,
                "location": "-",
                "profile": "-",
                "report": error_report,
            }
            return logs, summary

    async def config_bridge(
        self, config_bridge_request: ConfigurationBridgeRequest
    ):
        ont_list = await self.find_unconfigured_onts()
        target_ont = next(
            (ont for ont in ont_list if ont.sn == config_bridge_request.sn), None
        )
        if not target_ont:
            raise LookupError(
                f"ONT dengan SN {config_bridge_request.sn} tidak ditemukan."
            )

        base_iface = f"gpon-olt_1/{target_ont.pon_slot}/{target_ont.pon_port}"
        if self.is_c600:
            base_iface = f"gpon_olt-1/{target_ont.pon_port}/{target_ont.pon_slot}"

        onu_id = await self.find_next_available_onu_id(base_iface)
        package = PACKAGE_OPTIONS[config_bridge_request.package]
        olt_profile_type = (
            "F670" if config_bridge_request.modem_type == "ZTEG-F670" else "ALL"
        )

        iface_onu = f"gpon-onu_1/{target_ont.pon_slot}/{target_ont.pon_port}:{onu_id}"
        if self.is_c600:
            iface_onu = f"gpon_onu-1/{target_ont.pon_port}/{target_ont.pon_slot}:{onu_id}"

        context = {
            "interface_olt": base_iface,
            "interface_onu": iface_onu,
            "pon_slot": target_ont.pon_slot,
            "pon_port": target_ont.pon_port,
            "onu_id": onu_id,
            "sn": config_bridge_request.sn,
            "customer": config_bridge_request.customer,
            "vlan": config_bridge_request.vlan,
            "paket": config_bridge_request.package,
            "jenismodem": olt_profile_type,
        }

        template_name = "config_bridge.yaml"

        def _render_and_parse_yaml():
            if jinja_env is None:
                raise RuntimeError("Jinja2 environment not loaded!")
            template = jinja_env.get_template(template_name)
            rendered = template.render(context)
            return yaml.safe_load(rendered)

        commands = await asyncio.to_thread(_render_and_parse_yaml)
        logs = [
            f"Memulai konfigurasi untuk SN: {config_bridge_request.sn} di {iface_onu}"
        ]
        logging.info(f"Memulai konfigurasi. Total Command: {len(commands)}")

        for cmd in commands:
            logs.append(f"CMD > {cmd}")
            logging.info(f"EXECUTING: {cmd}")
            output = await self._execute_command(cmd)
            if output:
                logs.append(f"LOG < {output}")
            await asyncio.sleep(0.3)

        summary = {
            "status": "success",
            "message": "Konfigurasi bridge berhasil",
            "serial_number": config_bridge_request.sn,
            "pppoe_user": config_bridge_request.customer.pppoe_user,
            "name": config_bridge_request.customer.name,
            "location": iface_onu,
            "profile": olt_profile_type,
            "report": "\n".join([
                "=========================================================",
                "              KONFIGURASI BRIDGE SELESAI                 ",
                "=========================================================",
                f"  Serial Number      : {config_bridge_request.sn}",
                f"  ID Pelanggan       : {config_bridge_request.customer.pppoe_user}",
                f"  Nama Pelanggan     : {config_bridge_request.customer.name}",
                f"  OLT dan ONU        : {iface_onu}",
                f"  Profil             : {olt_profile_type}",
                "=========================================================",
            ]),
        }

        logs.extend([
            "KONFIGURASI SELESAI",
            "=========================================================",
            f"Serial Number         : {config_bridge_request.sn}",
            f"ID pelanggan          : {config_bridge_request.customer.pppoe_user}",
            f"Nama pelanggan        : {config_bridge_request.customer.name}",
            f"OLT dan ONU           : {iface_onu}",
            f"Profil yang dipakai   : {config_bridge_request.package}",
            "=========================================================",
        ])

        return logs, summary

    async def get_losi_interface(self, interface: str) -> list[dict]:
        """
        Mengirimkan perintah gpon onu state dan mendapatkan data customer
        dari Supabase yang status Phase State-nya LOS.
        """
        # Format if necessary, e.g., '1/2/5' -> 'gpon-olt_1/2/5'
        full_interface = self._format_olt_interface(interface)
        
        # Eksekusi command
        cmd = f"show gpon onu state {full_interface} LOS"
        output = await self._execute_command(cmd)
        
        los_interfaces = []
        for line in output.splitlines():
            # Cari baris yang mengandung LOS (biasanya huruf besar)
            if "LOS" in line.upper():
                parts = line.strip().split()
                if not parts:
                    continue
                
                # parts[0] contohnya "gpon-onu_1/2/5:4"
                onu_index_raw = parts[0]
                
                # Bersihkan prefix untuk mendapatkan interface (contoh: "1/2/5:4")
                clean_iface = onu_index_raw
                if "gpon-onu_" in clean_iface:
                    clean_iface = clean_iface.replace("gpon-onu_", "")
                elif "gpon_onu-" in clean_iface:
                    clean_iface = clean_iface.replace("gpon_onu-", "")
                    
                los_interfaces.append({"olt_name": self.olt_name, "interface": clean_iface})
                logging.info(f"{los_interfaces}")
        
        if not los_interfaces:
            logging.info(f"Tidak ada client LOS ditemukan pada interface {interface}.")
            return []
            
        logging.info(f"Ditemukan {len(los_interfaces)} client LOS: {los_interfaces}")
        
        return los_interfaces
