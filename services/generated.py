import os
import logging
import asyncio
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Dict, Any
from jinja2 import Environment, FileSystemLoader
import yaml

from core import PACKAGE_OPTIONS
from core.olt_config import OLT_OPTIONS, get_olt_info
from services.database import get_customer_by_sn, get_customers_by_sns

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

# Jinja2 environment for templates
try:
    jinja_env = Environment(loader=FileSystemLoader('templates'), trim_blocks=True, lstrip_blocks=True)
except Exception as e:
    logging.error(f"[FATAL ERROR] Tidak dapat memuat folder 'templates' Jinja2: {e}")
    jinja_env = None

# Output directory for generated config files
OUTPUT_DIR = Path(__file__).parent / "generated_configs"


class BatchConfigGenerator:
    """
    Generate batch configuration commands for unconfigured ONUs.
    
    Workflow:
    1. User inputs interface (e.g., "1/1/1")
    2. Find all unconfigured ONUs on that interface
    3. Lookup customer data from database by SN
    4. Generate configuration commands for each ONU
    5. Save all commands to a .txt file
    """
    
    def __init__(self, telnet_client):
        """
        Initialize with an existing TelnetClient instance.
        
        Args:
            telnet_client: Connected TelnetClient from telnet.py
        """
        self.telnet = telnet_client
        self.is_c600 = telnet_client.is_c600
        self.olt_name = telnet_client.olt_name
        
        # Ensure output directory exists
        OUTPUT_DIR.mkdir(exist_ok=True)
    
    async def find_uncfg_on_interface(self, interface: str) -> List[dict]:
        """
        Find unconfigured ONUs on a specific interface.
        
        The `show gpon onu uncfg` command ONLY returns ONUs that are:
        - Physically connected to the OLT
        - NOT yet configured/registered
        
        So this will NOT return already-configured ONUs.
        
        Args:
            interface: Interface to check (e.g., "1/1/1" or "1/2/3")
            
        Returns:
            List of unconfigured ONUs with sn, pon_slot, pon_port
        """
        # Get all unconfigured ONUs from OLT
        # This uses "show gpon onu uncfg" which ONLY shows unregistered ONUs
        all_uncfg = await self.telnet.find_unconfigured_onts()
        
        logging.info(f"Total unconfigured ONUs on OLT: {len(all_uncfg)}")
        for ont in all_uncfg:
            logging.debug(f"  Uncfg ONU: SN={ont.sn}, slot={ont.pon_slot}, port={ont.pon_port}")
        
        # Parse interface to get slot and port
        parts = interface.split("/")
        if len(parts) == 3:
            # Format: "1/slot/port" -> extract slot and port
            target_slot = parts[1]
            target_port = parts[2]
        elif len(parts) == 2:
            # Format: "slot/port" -> use as is
            target_slot = parts[0]
            target_port = parts[1]
        else:
            logging.warning(f"Invalid interface format: {interface}")
            return []
        
        # Note: For C600, the find_unconfigured_onts already handles the slot/port swap
        # So we compare directly with what the telnet function returns
        logging.info(f"Looking for ONUs on slot={target_slot}, port={target_port}")
        
        # Filter ONUs matching the interface
        matching_onus = []
        for ont in all_uncfg:
            # Direct comparison - telnet already parsed correctly
            if ont.pon_slot == target_slot and ont.pon_port == target_port:
                matching_onus.append({
                    "sn": ont.sn,
                    "pon_slot": ont.pon_slot,
                    "pon_port": ont.pon_port
                })
                logging.info(f"  ✓ Matched: SN={ont.sn}")
            else:
                logging.debug(f"  ✗ Skipped: SN={ont.sn} (slot={ont.pon_slot}, port={ont.pon_port})")
        
        logging.info(f"Found {len(matching_onus)} unconfigured ONUs on interface {interface}")
        return matching_onus
    
    async def lookup_customers_by_sns(self, sns: List[str]) -> Dict[str, Dict[str, Any]]:
        """
        Lookup customer data from database by list of SNs.
        
        Args:
            sns: List of ONU serial numbers
            
        Returns:
            Dict mapping SN -> customer data
        """
        if not sns:
            return {}
        
        # Use async wrapper to call sync database function
        customer_map = await asyncio.to_thread(get_customers_by_sns, sns)
        
        found_count = len(customer_map)
        logging.info(f"Found {found_count}/{len(sns)} customers in database")
        
        return customer_map
    
    @staticmethod
    def _parse_package_from_billing(package_str: str) -> Optional[str]:
        """
        Parse package string from billing to PACKAGE_OPTIONS format.
        
        Examples:
            "10MB" -> "10M"
            "PAKET 10M" -> "10M"
            "20 MB" -> "20M"
            "100MB-FIX" -> "100M"
        """
        import re
        
        if not package_str:
            return None
        
        # Extract number followed by M or MB
        match = re.search(r'(\d+)\s*M(?:B)?', package_str.upper())
        if match:
            return f"{match.group(1)}M"
        
        return None
    
    async def lookup_package_from_billing(self, user_pppoe: str) -> Optional[str]:
        """
        Lookup package from billing system by user_pppoe.
        
        Args:
            user_pppoe: Customer PPPoE username
            
        Returns:
            Parsed package string (e.g., "10M") or None if not found
        """
        if not user_pppoe:
            return None
        
        try:
            from services.biling_scaper import BillingScraper
            
            def _fetch_from_billing():
                scraper = BillingScraper()
                # Search by pppoe to get customer ID
                results = scraper.search(user_pppoe)
                if not results:
                    return None
                
                # Get full customer details
                cid = results[0].get("id")
                if not cid:
                    return None
                
                customer = scraper.get_customer_details(cid)
                if not customer:
                    return None
                
                return customer.package
            
            package_raw = await asyncio.to_thread(_fetch_from_billing)
            
            if package_raw:
                parsed = self._parse_package_from_billing(package_raw)
                logging.info(f"Billing package for {user_pppoe}: '{package_raw}' -> '{parsed}'")
                return parsed
            
            return None
            
        except Exception as e:
            logging.warning(f"Failed to lookup package from billing for {user_pppoe}: {e}")
            return None
    
    async def enrich_customer_with_billing(
        self, 
        customer_data: Dict[str, Any],
        user_pppoe: str
    ) -> Dict[str, Any]:
        """
        Enrich customer data with package from billing if not present.
        
        Args:
            customer_data: Customer data dict from database
            user_pppoe: Customer PPPoE username
            
        Returns:
            Enriched customer data dict
        """
        if customer_data.get("paket"):
            # Already has package, no need to fetch
            return customer_data
        
        # Fetch from billing
        package = await self.lookup_package_from_billing(user_pppoe)
        if package:
            customer_data["paket"] = package
            logging.info(f"Enriched {user_pppoe} with package: {package}")
        
        return customer_data
    
    def _generate_commands_from_template(
        self,
        context: Dict[str, Any]
    ) -> List[str]:
        """
        Generate configuration commands from Jinja2 template.
        
        Args:
            context: Template context dict
            
        Returns:
            List of configuration commands
        """
        if jinja_env is None:
            raise RuntimeError("Jinja2 environment not loaded. Cek folder templates.")
        
        template_name = "config_c600.yaml" if self.is_c600 else "config_c300.yaml"
        template = jinja_env.get_template(template_name)
        rendered = template.render(context)
        
        return yaml.safe_load(rendered)
    
    async def generate_config_for_onu(
        self,
        sn: str,
        pon_slot: str,
        pon_port: str,
        onu_id: int,
        customer_data: Dict[str, Any],
        eth_locks: List[bool] = None
    ) -> List[str]:
        """
        Generate configuration commands for a single ONU.
        
        Args:
            sn: ONU serial number
            pon_slot: PON slot
            pon_port: PON port
            onu_id: ONU ID
            customer_data: Customer data from database
            eth_locks: Ethernet port lock states
            
        Returns:
            List of configuration commands
        """
        if eth_locks is None:
            eth_locks = [False, False, False, False]  # All unlocked by default
        
        # Build interface names
        if self.is_c600:
            base_iface = f"gpon_olt-1/{pon_port}/{pon_slot}"
            iface_onu = f"gpon_onu-1/{pon_port}/{pon_slot}:{onu_id}"
        else:
            base_iface = f"gpon-olt_1/{pon_slot}/{pon_port}"
            iface_onu = f"gpon-onu_1/{pon_slot}/{pon_port}:{onu_id}"
        
        # Get DBA rate to determine profile suffix
        rate = await self.telnet.get_dba_rate(f"1/{pon_slot}/{pon_port}")
        up_profile_suffix = "-MBW" if rate > 75.0 else "-FIX"
        
        # Get package from customer data or default to "10M"
        package = customer_data.get("paket", "10M")
        if not package:
            package = "10M"
        
        # Get package profile
        base_paket_name = PACKAGE_OPTIONS.get(package)
        if not base_paket_name:
            # Try direct match
            base_paket_name = package.replace("M", "MB")
            logging.warning(f"Package '{package}' not in PACKAGE_OPTIONS, using '{base_paket_name}'")
        
        up_paket = f"{base_paket_name}{up_profile_suffix}"
        down_paket = base_paket_name.replace("MB", "M")
        
        # Get VLAN from OLT config (olt_config.py is source of truth)
        olt_info = get_olt_info(self.olt_name)
        if not olt_info:
            raise ValueError(f"OLT '{self.olt_name}' tidak ditemukan di olt_config.py. Pastikan nama OLT benar.")
        
        vlan = olt_info["vlan"]
        logging.info(f"Using VLAN: {vlan} for OLT: {self.olt_name}")
        
        # Prepare context for template
        context = {
            "interface_olt": base_iface,
            "interface_onu": iface_onu,
            "pon_slot": pon_slot,
            "pon_port": pon_port,
            "onu_id": onu_id,
            "sn": sn,
            "customer": {
                "name": customer_data.get("nama", "UNKNOWN"),
                "address": customer_data.get("alamat", "-"),
                "pppoe_user": customer_data.get("user_pppoe", ""),
                "pppoe_pass": customer_data.get("pppoe_password", "123456") or "123456"
            },
            "vlan": vlan,
            "up_profile": up_paket,
            "down_profile": down_paket,
            "jenismodem": "ALL",  # Default modem type
            "eth_locks": eth_locks
        }
        
        return self._generate_commands_from_template(context)
    
    async def generate_batch_config(
        self,
        interface: str,
        default_package: str = "10M",
        skip_missing_customers: bool = False
    ) -> Dict[str, Any]:
        """
        Generate batch configuration for all unconfigured ONUs on an interface.
        Automatically looks up customer data from database by SN.
        
        Args:
            interface: Target interface (e.g., "1/1/1")
            default_package: Default package if not found in database
            skip_missing_customers: If True, skip ONUs without customer data
            
        Returns:
            Dict with:
                - filepath: Path to generated config file
                - total_uncfg: Total unconfigured ONUs found
                - total_configured: ONUs with config generated
                - missing_customers: List of SNs not found in database
        """
        result = {
            "filepath": None,
            "total_uncfg": 0,
            "total_configured": 0,
            "missing_customers": [],
            "configured_onus": []
        }
        
        # Step 1: Find unconfigured ONUs on interface
        uncfg_onus = await self.find_uncfg_on_interface(interface)
        result["total_uncfg"] = len(uncfg_onus)
        
        if not uncfg_onus:
            logging.warning(f"No unconfigured ONUs found on interface {interface}")
            return result
        
        # Step 2: Extract all SNs and lookup customer data from database
        all_sns = [onu["sn"] for onu in uncfg_onus]
        customer_map = await self.lookup_customers_by_sns(all_sns)
        
        # Step 3: Get active ONU IDs on the interface (to find gaps)
        active_onu_ids = await self._get_active_onu_ids(interface)
        reserved_ids = []  # Track IDs we're reserving for this batch
        
        # Step 4: Generate config for each ONU
        all_configs = []
        
        for onu in uncfg_onus:
            sn = onu["sn"]
            
            # Get customer data from database lookup
            customer_data = customer_map.get(sn.upper())
            
            if not customer_data:
                result["missing_customers"].append(sn)
                
                if skip_missing_customers:
                    logging.warning(f"No customer data for SN {sn}, skipping...")
                    continue
                else:
                    # Use placeholder data
                    customer_data = {
                        "nama": f"CUSTOMER_{sn[-8:]}",
                        "alamat": f"Interface {interface}",
                        "user_pppoe": f"user_{sn[-8:].lower()}",
                        "pppoe_password": "123456",
                        "paket": default_package
                    }
                    logging.warning(f"No customer data for SN {sn}, using placeholder")
            else:
                # Customer found in database - enrich with billing data if paket is missing
                user_pppoe = customer_data.get("user_pppoe")
                if user_pppoe and not customer_data.get("paket"):
                    logging.info(f"Package missing for {sn}, fetching from billing...")
                    customer_data = await self.enrich_customer_with_billing(customer_data, user_pppoe)
            
            try:
                # Find next available ONU ID (considering gaps and already reserved IDs)
                onu_id = self._find_next_available_id(active_onu_ids, reserved_ids)
                reserved_ids.append(onu_id)  # Reserve this ID for batch
                
                # Generate config for this ONU
                commands = await self.generate_config_for_onu(
                    sn=sn,
                    pon_slot=onu["pon_slot"],
                    pon_port=onu["pon_port"],
                    onu_id=onu_id,
                    customer_data=customer_data
                )
                
                # Add commands directly (no comment headers for clean plug-and-play)
                all_configs.append("")  # Empty line separator between ONUs
                all_configs.extend(commands)
                
                result["configured_onus"].append({
                    "sn": sn,
                    "onu_id": onu_id,
                    "name": customer_data.get("nama"),
                    "pppoe": customer_data.get("user_pppoe")
                })
                
                result["total_configured"] += 1
                logging.info(f"✓ Generated config for SN {sn} -> ONU ID {onu_id}")
                
            except Exception as e:
                logging.error(f"Error generating config for SN {sn}: {e}")
                continue
        
        if not all_configs:
            logging.warning("No configurations generated")
            return result
        
        # Step 5: Save to file
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"batch_config_{interface.replace('/', '_')}_{timestamp}.txt"
        filepath = OUTPUT_DIR / filename
        
        with open(filepath, "w") as f:
            # Write commands directly - clean plug-and-play output
            for cmd in all_configs:
                if cmd:  # Skip empty strings
                    f.write(f"{cmd}\n")
            
            # Append summary at end (after all commands) for reference
            f.write("\n\n")
            f.write("!" + "=" * 60 + "\n")
            f.write(f"! Generated: {datetime.now().isoformat()}\n")
            f.write(f"! OLT: {self.olt_name} | Interface: {interface}\n")
            f.write(f"! Total: {result['total_configured']} ONUs configured\n")
            f.write("!" + "=" * 60 + "\n")
            for onu_info in result["configured_onus"]:
                f.write(f"! ONU {onu_info['onu_id']}: {onu_info['sn']} - {onu_info['name']} ({onu_info['pppoe']})\n")
            
            if result["missing_customers"]:
                f.write(f"\n! WARNING: {len(result['missing_customers'])} ONUs tidak ditemukan di database:\n")
                for missing_sn in result["missing_customers"]:
                    f.write(f"!   - {missing_sn}\n")
        
        result["filepath"] = str(filepath)
        logging.info(f"Batch config saved to: {filepath}")
        
        return result
    
    async def _get_active_onu_ids(self, interface: str) -> List[int]:
        """
        Get all active ONU IDs on the interface.
        Uses same logic as find_next_available_onu_id but returns full list.
        """
        import re
        
        parts = interface.split("/")
        if len(parts) == 3:
            base_iface = f"gpon-olt_1/{parts[1]}/{parts[2]}"
            if self.is_c600:
                base_iface = f"gpon_olt-1/{parts[2]}/{parts[1]}"
        else:
            base_iface = f"gpon-olt_1/{parts[0]}/{parts[1]}"
            if self.is_c600:
                base_iface = f"gpon_olt-1/{parts[1]}/{parts[0]}"
        
        logging.info(f"🔍 Getting active ONU IDs on {base_iface}...")
        cmd = f"show gpon onu state {base_iface}"
        output = await self.telnet._execute_command(cmd)
        
        active_onus = []
        identifier = 'enable' if self.is_c600 else '1(GPON)'
        
        for line in output.splitlines():
            if identifier in line:
                try:
                    splitter_1 = re.split(r"\s+", line.strip())[0]
                    splitter_2 = re.split(":", splitter_1)[-1]
                    active_onus.append(int(splitter_2))
                except (IndexError, ValueError):
                    continue
        
        active_onus.sort()
        logging.info(f"Active ONU IDs on {base_iface}: {active_onus}")
        return active_onus
    
    def _find_next_available_id(self, active_ids: List[int], reserved_ids: List[int]) -> int:
        """
        Find next available ONU ID that is not in active_ids or reserved_ids.
        Same algorithm as TelnetClient.find_next_available_onu_id but with reservation tracking.
        """
        all_used = set(active_ids) | set(reserved_ids)
        
        calculation = 1
        while calculation in all_used:
            calculation += 1
        
        if calculation > 128:
            raise ValueError("Port PON penuh. Maksimal 128 ONU per port.")
        
        return calculation
    
    def get_generated_files(self) -> List[str]:
        """Get list of all generated config files."""
        return [str(f) for f in OUTPUT_DIR.glob("*.txt")]


# Convenience function for standalone usage
async def generate_batch_config_standalone(
    host: str,
    username: str,
    password: str,
    is_c600: bool,
    olt_name: str,
    interface: str,
    default_package: str = "10M",
    skip_missing: bool = False
) -> Dict[str, Any]:
    """
    Standalone function to generate batch config without managing TelnetClient.
    Automatically fetches customer data from database by SN.
    
    Returns result dict with filepath, counts, and missing customers.
    """
    from services.telnet import TelnetClient
    
    client = TelnetClient(host, username, password, is_c600, olt_name)
    
    try:
        await client.connect()
        await client._login()
        await client._disable_pagination()
        
        generator = BatchConfigGenerator(client)
        
        result = await generator.generate_batch_config(
            interface=interface,
            default_package=default_package,
            skip_missing_customers=skip_missing
        )
        
        return result
        
    finally:
        await client.close()


if __name__ == "__main__":
    # Test example
    async def test():
        from services.telnet import TelnetClient
        from core.olt_config import OLT_OPTIONS
        
        # Example: Use first OLT from config
        olt_name = list(OLT_OPTIONS.keys())[0]
        olt_config = OLT_OPTIONS[olt_name]
        
        print(f"Using OLT: {olt_name}")
        print(f"IP: {olt_config['ip']}")
        print(f"VLAN: {olt_config['vlan']}")
        
        # Note: You need to provide username/password
        # These are typically in settings, not in OLT_OPTIONS
        from core import settings
        
        client = TelnetClient(
            host=olt_config["ip"],
            username=settings.OLT_USERNAME,
            password=settings.OLT_PASSWORD,
            is_c600=olt_config.get("c600", False),
            olt_name=olt_name
        )
        
        try:
            await client.connect()
            await client._login()
            await client._disable_pagination()
            
            generator = BatchConfigGenerator(client)
            
            # Find unconfigured ONUs on interface 1/1/1
            print("\n=== Finding unconfigured ONUs ===")
            uncfg = await generator.find_uncfg_on_interface("1/1/1")
            print(f"Found unconfigured ONUs: {uncfg}")
            
            if uncfg:
                # Lookup customers from database
                print("\n=== Looking up customers from database ===")
                sns = [o["sn"] for o in uncfg]
                customers = await generator.lookup_customers_by_sns(sns)
                print(f"Found customers: {list(customers.keys())}")
                
                # Generate batch config
                print("\n=== Generating batch config ===")
                result = await generator.generate_batch_config(
                    interface="1/1/1",
                    default_package="10M"
                )
                
                print(f"\nResult:")
                print(f"  Filepath: {result['filepath']}")
                print(f"  Total Unconfigured: {result['total_uncfg']}")
                print(f"  Total Configured: {result['total_configured']}")
                print(f"  Missing Customers: {result['missing_customers']}")
                
                # Show file content
                if result['filepath']:
                    print("\n=== Generated Config ===")
                    with open(result['filepath']) as f:
                        print(f.read())
            
        finally:
            await client.close()
    
    asyncio.run(test())
