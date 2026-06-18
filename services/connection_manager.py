# connection_manager.py

import asyncio
from typing import Dict, Optional
import logging
from services.telnet import TelnetClient

class ConnectionManager:
    def __init__(self):
        self._connections: Dict[str, TelnetClient] = {}
        self._current_loop_id: Optional[int] = None

    def _check_loop_change(self):
        """Check if the event loop has changed (e.g., after hot-reload) and clear stale connections."""
        try:
            current_loop = asyncio.get_running_loop()
            current_loop_id = id(current_loop)
            
            if self._current_loop_id is not None and self._current_loop_id != current_loop_id:
                logging.warning("♻️ Event loop changed! Clearing all stale connections...")
                # Don't await close() here since old connections are on dead loop
                self._connections.clear()
            
            self._current_loop_id = current_loop_id
        except RuntimeError:
            pass  # No running loop

    async def get_connection(self, host, username, password, is_c600, olt_name: str = "") -> TelnetClient:
        # First, check if event loop changed and clear stale connections
        self._check_loop_change()
        
        if host in self._connections:
            client = self._connections[host]
            if client.writer and not client.writer.is_closing():
                # Update olt_name in case it changed or wasn't set before
                if olt_name:
                    client.olt_name = olt_name
                return client
            else:
                del self._connections[host]

        logging.info(f"✨ Membuat session object baru untuk {host}")
        client = TelnetClient(host, username, password, is_c600, olt_name)
        
        await client.connect()
        
        self._connections[host] = client
        
        asyncio.create_task(self._keepalive_worker(client))
        
        return client

    async def _keepalive_worker(self, client: TelnetClient):
            """
            Mengirim Enter setiap 60 detik agar tidak ditendang OLT.
            VERSI FIX: Tidak melakukan read() agar tidak bentrok dengan main thread.
            """
            try:
                while True:
                    await asyncio.sleep(60)
                    
                    if not client.writer or client.writer.is_closing():
                        break

                    now = asyncio.get_event_loop().time()
                    if now - client.last_activity > 50:
                        
                        if client.lock.locked():
                            continue

                        async with client.lock:
                            if client.writer and not client.writer.is_closing():
                                try:
                                    client.writer.write("\n")
                                    await client.writer.drain()

                                    client.last_activity = asyncio.get_event_loop().time()
                                    
                                except Exception as e:
                                    logging.warning(f"Gagal kirim keepalive: {e}")
                                    break
            except Exception as e:
                logging.error(f"Keepalive Worker Crash pada {client.host}: {e}")

# Global Instance
olt_manager = ConnectionManager()


class SwitchManager:
    def __init__(self):
        self._connections: Dict[str, "SwitchClient"] = {}
        self._current_loop_id: Optional[int] = None

    def _check_loop_change(self):
        """Check if the event loop has changed and clear stale connections."""
        try:
            current_loop = asyncio.get_running_loop()
            current_loop_id = id(current_loop)
            
            if self._current_loop_id is not None and self._current_loop_id != current_loop_id:
                logging.warning("♻️ Event loop changed! Clearing all stale switch connections...")
                self._connections.clear()
            
            self._current_loop_id = current_loop_id
        except RuntimeError:
            pass

    async def get_connection(self, host: str, username: str, password: str, is_huawei: bool, is_ruijie: bool = False) -> "SwitchClient":
        from services.switch_telnet import SwitchClient
        
        self._check_loop_change()
        
        if host in self._connections:
            client = self._connections[host]
            if client.writer and not client.writer.is_closing():
                return client
            else:
                del self._connections[host]

        logging.info(f"✨ Membuat session baru untuk switch {host}")
        client = SwitchClient(host, username, password, is_huawei, is_ruijie)
        
        await client.connect()
        
        self._connections[host] = client
        
        asyncio.create_task(self._keepalive_worker(client))
        
        return client

    async def _keepalive_worker(self, client: "SwitchClient"):
        """Keepalive untuk switch - kirim enter setiap 60 detik."""
        try:
            while True:
                await asyncio.sleep(60)
                
                if not client.writer or client.writer.is_closing():
                    break

                now = asyncio.get_event_loop().time()
                if now - client.last_activity > 50:
                    
                    if client.lock.locked():
                        continue

                    async with client.lock:
                        if client.writer and not client.writer.is_closing():
                            try:
                                client.writer.write("\n")
                                await client.writer.drain()
                                client.last_activity = asyncio.get_event_loop().time()
                            except Exception as e:
                                logging.warning(f"Gagal kirim keepalive ke switch: {e}")
                                break
        except Exception as e:
            logging.error(f"Keepalive Worker Crash pada switch {client.host}: {e}")


# Global Instance for Switch
switch_manager = SwitchManager()