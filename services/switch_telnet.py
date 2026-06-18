import asyncio
import logging
import re
import telnetlib3

from core import COMMAND_TEMPLATE


logging.basicConfig(level=logging.INFO)
logging.getLogger("telnetlib3").setLevel(logging.ERROR)

class SwitchClient:
    def __init__(self, host: str, username: str, password: str, is_huawei: bool, is_ruijie: bool = False):
        self.host = host
        self.username = username
        self.password = password
        self.is_huawei = is_huawei
        self.is_ruijie = is_ruijie
        self._lock = None
        self.reader = None
        self.writer = None
        self.last_activity = 0
        # Different devices use different pagination prompts
        self._pagination_prompts = [
            "---- More ----",  # Huawei
            "--More--",        # Cisco/Ruijie
            "-- More --",      # Some Cisco
            "---More---",      # Some devices
        ]
    
    @property
    def lock(self):
        try:
            current_loop = asyncio.get_running_loop()
            if self._lock is None or self._lock._loop is not current_loop:
                self._lock = asyncio.Lock()
        except RuntimeError:
            if self._lock is None:
                self._lock = asyncio.Lock()
        return self._lock

    def _get_device_type(self) -> str:
        """Get device type string for command templates"""
        if self.is_ruijie:
            return "ruijie"
        elif self.is_huawei:
            return "huawei"
        return "cisco"

    def _get_action_command(self, action: str, **kwargs) -> str:
        """Get command for device type"""
        device = self._get_device_type()
        template = COMMAND_TEMPLATE.get(action, {}).get(device, [])
        if template:
            return template[0].format(**kwargs)
        return ""

    async def connect(self):
        """Connect ke device"""
        if self.writer and not self.writer.is_closing():
            return  # Already connected

        logging.info(f"Membuka koneksi baru ke {self.host}...")
        self.reader, self.writer = await asyncio.wait_for(
            telnetlib3.open_connection(self.host, 23), timeout=20
        )
        await self._login()
        self.last_activity = asyncio.get_event_loop().time()
    
    async def close(self):
        """Close Manual"""
        if self.writer:
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except:
                pass
        self.writer = None
        self.reader = None

    async def _read_until_pattern(self, patterns: list[str], timeout: int = 20) -> tuple[str, str]:
        """
        Read until one of the patterns is found.
        Returns: (data, matched_pattern)
        """
        if not self.reader:
            raise ConnectionError("Telnet reader tidak tersedia.")
        
        data = ""
        try:
            while True:
                chunk = await asyncio.wait_for(self.reader.read(1024), timeout=timeout)
                if not chunk:
                    break
                data += chunk
                data_lower = data.lower()
                
                for pattern in patterns:
                    if pattern.lower() in data_lower:
                        return data, pattern
                
                # Handle pagination
                for pag_prompt in self._pagination_prompts:
                    if pag_prompt in data:
                        if self.writer:
                            self.writer.write(" ")
                            await self.writer.drain()
                            data = data.replace(pag_prompt, "")
                        break
                        
        except asyncio.TimeoutError:
            logging.warning(f"Timeout waiting for patterns {patterns}. Data: {data[:200]}")
            raise
        
        return data, ""

    async def _read_until_prompt(self, timeout: int = 20) -> str:
        """Read until device prompt (>, #, ])"""
        if not self.reader:
            raise ConnectionError("Telnet reader tidak tersedia.")
        
        data = ""
        try:
            while True:
                chunk = await asyncio.wait_for(self.reader.read(1024), timeout=timeout)
                if not chunk:
                    break
                data += chunk

                # Check for common prompts: >, #, ] (Huawei uses <name>)
                if re.search(r"[>\#\]]\s*$", data):
                    break
                    
                # Handle pagination
                for pag_prompt in self._pagination_prompts:
                    if pag_prompt in data:
                        if self.writer:
                            self.writer.write(" ")
                            await self.writer.drain()
                            data = data.replace(pag_prompt, "")
                        break
                        
            return data
        except asyncio.TimeoutError:
            logging.warning(f"Timeout waiting for prompt from {self.host}. Data: {data[:200]}")
            raise

    async def _login_huawei(self, timeout: int = 20):
        """
        Huawei login sequence:
        expect "Username" -> send user
        expect "Password" -> send pass
        handle "Change now? Y/N" -> send n
        expect <...>
        """
        logging.info(f"[{self.host}] Huawei login sequence...")
        
        # Wait for Username prompt
        await self._read_until_pattern(["username"], timeout)
        self.writer.write(self.username + '\n')
        await self.writer.drain()
        
        # Wait for Password prompt
        await self._read_until_pattern(["password"], timeout)
        self.writer.write(self.password + '\n')
        await self.writer.drain()
        
        # Read response - might have "Change now? Y/N" prompt
        data = ""
        while True:
            chunk = await asyncio.wait_for(self.reader.read(1024), timeout=timeout)
            if not chunk:
                break
            data += chunk
            
            # Handle password change prompt
            if "change now" in data.lower() and "y/n" in data.lower():
                self.writer.write('n\n')
                await self.writer.drain()
                data = ""
                continue
            
            # Check for Huawei prompt <...>
            if re.search(r"<.+>\s*$", data):
                break
        
        logging.info(f"[{self.host}] Huawei login successful")

    async def _login_cisco(self, timeout: int = 20):
        """
        Cisco login sequence:
        expect "login:" or "Username:" -> send user
        expect "Password:" -> send pass
        expect # or >
        """
        logging.info(f"[{self.host}] Cisco login sequence...")
        
        # Wait for login/username prompt
        await self._read_until_pattern(["login:", "username:"], timeout)
        self.writer.write(self.username + '\n')
        await self.writer.drain()
        
        # Wait for Password prompt
        await self._read_until_pattern(["password:"], timeout)
        self.writer.write(self.password + '\n')
        await self.writer.drain()
        
        # Wait for prompt (# or >)
        await self._read_until_prompt(timeout)
        
        logging.info(f"[{self.host}] Cisco login successful")

    async def _login_ruijie(self, timeout: int = 20):
        """
        Ruijie login sequence:
        sleep 1
        expect "Password:" -> send username (yes, username first)
        expect ">" -> send "en"
        expect "Password:" -> send password
        expect #
        """
        logging.info(f"[{self.host}] Ruijie login sequence...")
        
        # Small delay like in bash script
        await asyncio.sleep(1)
        
        # First Password prompt - send USERNAME
        await self._read_until_pattern(["password:"], timeout)
        self.writer.write(self.username + '\n')
        await self.writer.drain()
        
        # Wait for > prompt
        await self._read_until_pattern([">"], timeout)
        
        # Send enable command
        self.writer.write('en\n')
        await self.writer.drain()
        
        # Wait for Password prompt again - send actual PASSWORD
        await self._read_until_pattern(["password:"], timeout)
        self.writer.write(self.password + '\n')
        await self.writer.drain()
        
        # Wait for # prompt
        await self._read_until_prompt(timeout)
        
        logging.info(f"[{self.host}] Ruijie login successful")

    async def _login(self, timeout: int = 20):
        """Login based on device type"""
        try:
            if self.is_ruijie:
                await self._login_ruijie(timeout)
            elif self.is_huawei:
                await self._login_huawei(timeout)
            else:
                await self._login_cisco(timeout)
                
        except asyncio.TimeoutError:
            await self.close()
            raise ConnectionError(f"Timeout during login to {self.host}")
        except Exception as e:
            await self.close()
            raise ConnectionError(f"Failed to login to {self.host}: {e}")

    async def _execute_command(self, command: str, timeout: int = 20) -> str:
        """Execute command on device"""
        if not self.reader or not self.writer:
            raise ConnectionError("Connection not established to execute command.")
        if not command:
            return ""
        
        self.writer.write(command + "\n")
        await asyncio.wait_for(self.writer.drain(), timeout=10)
        raw_output = await self._read_until_prompt(timeout=timeout)
        
        # Clean output (remove command echo and prompt)
        cleaned_lines = []
        lines = raw_output.splitlines()
        if len(lines) > 2:
            for line in lines[1:-1]:
                stripped = line.strip()
                if stripped:
                    cleaned_lines.append(stripped)
        
        return "\n".join(cleaned_lines)
    
    async def get_full_status(self) -> str:
        """Get full status from device"""
        if not self.reader or not self.writer:
            raise ConnectionError("Connection not established to get full status.")
        
        command = self._get_action_command("cek_description")
        return await self._execute_command(command)
    
    async def get_interface_status(self, interface: str) -> str:
        """Get interface status from device"""
        if not self.reader or not self.writer:
            raise ConnectionError("Connection not established to get interface status.")
        
        command = self._get_action_command("cek_interface", interface)
        return await self._execute_command(command)