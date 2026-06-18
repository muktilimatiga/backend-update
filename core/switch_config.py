# Switch configs by full IP address
# Each entry: {type, username, password}

SWITCH_CONFIG = {
    # JKT switches (192.168.116.x)
    "192.168.116.113": {"type": "cisco", "username": "noclex", "password": "noclx@1965"},
    "192.168.116.114": {"type": "huawei", "username": "noclex", "password": "Noclx@1965"},
    "192.168.116.115": {"type": "cisco", "username": "noclx", "password": "noclx@1971"},
    "192.168.116.116": {"type": "huawei", "username": "noclex", "password": "Noclx#1967!"},
    "192.168.116.117": {"type": "huawei", "username": "noclex", "password": "Noclx#1967!"},
    "192.168.116.118": {"type": "huawei", "username": "noclex", "password": "Noclx@1965"},
    "192.168.116.119": {"type": "huawei", "username": "noclex", "password": "Noclx#1967!"},
    
    # TAG switches (10.254.254.x)
    "10.254.254.16": {"type": "ruijie", "username": "noclex", "password": "noclx@1965"},
    "10.254.254.11": {"type": "cisco", "username": "noclex", "password": "noclx@1965"},
    "10.254.254.19": {"type": "huawei", "username": "noclex", "password": "Noclx@1965"},
    "10.254.254.13": {"type": "cisco", "username": "monster12", "password": "monster12"},
    
    # ATN switches (10.254.252.x)
    "10.254.252.3": {"type": "cisco", "username": "noclex", "password": "noclx@1965"},
    "10.254.252.4": {"type": "huawei", "username": "noclex", "password": "Noclx@1965"},
    "10.254.252.5": {"type": "huawei", "username": "noclex", "password": "Noclx@1965"},
    "10.254.252.8": {"type": "huawei", "username": "noclex", "password": "Noclx@1965"},
    "10.254.252.9": {"type": "huawei", "username": "noclex", "password": "Noclx@1965"},
    "10.254.252.11": {"type": "huawei", "username": "noclex", "password": "Noclx@1965"},
    "10.254.252.12": {"type": "huawei", "username": "noclex", "password": "Noclx@1965"},
    "10.254.252.15": {"type": "huawei", "username": "noclex", "password": "Noclx@1965"},
}

COMMAND_TEMPLATE = {
    "cek_description": {
        "huawei": ["display interface description"],
        "cisco": ["show interface description"],
        "ruijie": ["show interface description"],
    },
    "cek_interface": {
        "huawei": ["display interface {interface}"],
        "cisco": ["show interface {interface}"],
        "ruijie": ["show interface {interface}"],
    }
}


def get_switch_connection(ip: str) -> dict | None:
    """
    Get switch connection info by IP address.
    
    Args:
        ip: Full IP address (e.g., "192.168.116.113")
    
    Returns:
        {
            "ip": "192.168.116.113",
            "type": "cisco",
            "is_huawei": False,
            "is_ruijie": False,
            "username": "noclex",
            "password": "noclx@1965"
        }
    """
    switch_config = SWITCH_CONFIG.get(ip)
    if not switch_config:
        return None
    
    device_type = switch_config.get("type", "huawei")
    
    return {
        "ip": ip,
        "type": device_type,
        "is_huawei": device_type == "huawei",
        "is_ruijie": device_type == "ruijie",
        "username": switch_config.get("username", ""),
        "password": switch_config.get("password", ""),
    }
