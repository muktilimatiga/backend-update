OLT_OPTIONS = {
    "BOYOLANGU": {"ip": "192.168.12.1", "vlan": "901", "c600": False},
    "BEJI": {"ip": "192.168.12.5", "vlan": "903", "c600": False},
    "DURENAN": {"ip": "192.168.12.6", "vlan": "911", "c600": False},
    "KALIDAWIR": {"ip": "192.168.12.7", "vlan": "902", "c600": False},
    "KAUMAN": {"ip": "192.168.12.4", "vlan": "920", "c600": False},
    "KEDIRI": {"ip": "192.168.12.8", "vlan": "905", "c600": False},
    "CAMPUR BARU": {"ip": "192.168.12.9", "vlan": "911", "c600": True},
    "BLITAR": {"ip": "192.168.12.2", "vlan": "904", "c600": False},
    "GANDUSARI": {"ip": "192.168.12.3", "vlan": "906", "c600": False},
}

MODEM_OPTIONS = ["F609", "F670L", "C-DATA"]

PACKAGE_OPTIONS = {
    "10M": "10MB",
    "15M": "15MB",
    "20M": "20MB",
    "25M": "25MB",
    "30M": "30MB",
    "35M": "35MB",
    "40M": "40MB",
    "50M": "50MB",
    "75M": "75MB",
    "100M": "100MB",
}

OLT_ALIASES = {
    "CAMPURDARAT": "CAMPUR BARU",
    "BOYOLANGU": "BOYOLANGU",  # Tambahkan juga nama yang sudah cocok
    "BEJI": "BEJI",
    "DURENAN": "DURENAN",
    "KALIDAWIR": "KALIDAWIR",
    "KAUMAN": "KAUMAN",
    "KEDIRI": "KEDIRI",
    "BLITAR": "BLITAR",
    "GANDUSARI": "GANDUSARI",
}


def get_olt_info(olt_name: str) -> dict | None:
    """
    Get OLT info by name with alias fallback.
    First checks OLT_OPTIONS, then falls back to OLT_ALIASES.
    Returns None if not found.
    """
    name_upper = olt_name.upper()

    # Try direct lookup first
    if name_upper in OLT_OPTIONS:
        return OLT_OPTIONS[name_upper]

    # Fallback to alias
    resolved_name = OLT_ALIASES.get(name_upper)
    if resolved_name:
        return OLT_OPTIONS.get(resolved_name)

    return None


# Command templates for OLT operations
# Use {placeholders} for dynamic values
COMMAND_TEMPLATES = {
    # Reboot ONU commands
    "reboot": {
        "c300": ["interface {interface}", "shutdown", "no shutdown", "exit"],
        "c600": ["interface {interface}", "shutdown", "no shutdown", "exit"],
    },
    # Delete ONU commands (used with _config_interface_commands)
    "delete_onu": {
        "c300": ["interface {interface}", "no onu {onu_id}", "exit"],
        "c600": ["interface {interface}", "no onu {onu_id}", "exit"],
    },
    # Change SN / Re-register ONU
    "change_sn": {
        "c300": ["interface {interface}", "registration-method sn {sn}", "exit"],
        "c600": ["interface {interface}", "registration-method sn {sn}", "exit"],
    },
    # Detail onu
    "detail_onu": {
        "c300": ["show gpon onu detail {interface}"],
        "c600": ["show gpon onu detail {interface}"],
    },
    # Redaman Onu
    "redaman_onu": {
        "c300": ["show pon power attenuation {interface}"],
        "c600": ["show pon power attenuation {interface}"],
    },
    # 1 port Detail
    "port_state": {
        "c300": ["show gpon onu state {interface}"],
        "c600": ["show gpon onu state {interface}"],
    },
    # 1 port Redaman
    "port_redaman": {
        "c300": ["show pon power onu-rx {interface}"],
        "c600": ["show pon power onu-rx {interface}"],
    },
    # Runnin confing
    "running_config": {
        "c300": [
            "show running-config interface {interface}",
            "show onu running config {interface}",
        ],
        "c600": [
            "show running-config-interface {interface}",
            "show running-config-interface {vport_interface}",
        ],
    },
    # Cek IP
    "cek_ip": {
        "c300": ["show gpon remote-onu ip-host {interface}"],
        "c600": ["show gpon remote-onu ip-host {interface}"],
    },
    # Cek Port Lock /or unlock
    "cek_port": {
        "c300": ["show gpon remote-onu interface eth {interface}"],
        "c600": ["show gpon remote-onu interface eth {interface}"],
    },
    "cek_dba": {
        "c300": ["show pon bandwidth dba interface {interface}"],
        "c600": ["show pon bandwidth dba interface {interface}"],
    },
    "cek_monitoring": {
        "c300": ["show interface {interface}"],
        "c600": ["show interface {interface}"],
    },
    "cek_rx_monitoring": {
        "c300": ["show interface optical-module-info {interface}"],
        "c600": ["show interface optical-module-info {interface}"],
    },
    "cek_state_olt": {"c300": ["show gpon onu state"], "c600": ["show gpon onu state"]},
    # Edit ethernet port lock/unlock (all 4 ports)
    "edit_port": {
        "c300": [
            "pon-onu-mng {interface}",
            "interface eth eth_0/1 state {state}",
            "interface eth eth_0/2 state {state}",
            "interface eth eth_0/3 state {state}",
            "interface eth eth_0/4 state {state}",
            "exit",
        ],
        "c600": [
            "pon-onu-mng {interface}",
            "interface eth eth_0/1 state {state}",
            "interface eth eth_0/2 state {state}",
            "interface eth eth_0/3 state {state}",
            "interface eth eth_0/4 state {state}",
            "exit",
        ],
    },
    "change_capacity": {
        "c300": [
            "interface {interface}",
            "tcont 1 name PPPOE profile UP-{up_profile}",
            "gemport 1 traffic-limit downstream DOWN-{down_profile}",
            "exit",
        ],
        "c600": [
            "interface {interface}",
            "tcont 1 name CIGNAL profile UP-{up_profile}",
            "exit",
            "interface {vport_interface}",
            "qos traffic-policy DOWN-{down_profile} direction egress",
        ],
    },
    "base_info": {
        "c300": ["show gpon onu baseinfo {interface}"],
        "c600": ["show gpon onu baseinfo {interface}"],
    },
}
