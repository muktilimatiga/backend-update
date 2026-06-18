# core/__init__.py
# Central export point for the core module
# Usage: from core import settings, OLT_OPTIONS, get_olt_info, etc.

# --- Settings ---
from core.config import settings

# --- OLT Configuration ---
from core.olt_config import (
    OLT_OPTIONS,
    MODEM_OPTIONS,
    PACKAGE_OPTIONS,
    OLT_ALIASES,
    COMMAND_TEMPLATES,
    get_olt_info,
)

# --- Switch Configuration ---
from core.switch_config import (
    SWITCH_CONFIG,
    COMMAND_TEMPLATE,
    get_switch_connection,
)

# Re-export common typing utilities
from typing import Dict, Any, Optional, List

__all__ = [
    # Settings
    "settings",
    
    # OLT
    "OLT_OPTIONS",
    "MODEM_OPTIONS", 
    "PACKAGE_OPTIONS",
    "OLT_ALIASES",
    "COMMAND_TEMPLATES",
    "get_olt_info",
    
    # Switch
    "SWITCH_CONFIG",
    "COMMAND_TEMPLATE",
    "get_switch_connection",
    
    # Typing
    "Dict",
    "Any", 
    "Optional",
    "List",
]