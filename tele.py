"""
Telegram Bot for OLT Provisioning (PSB Flow)
Run this file independently: python tele.py

Requires:
- pip install python-telegram-bot httpx python-dotenv
"""

import os
import sys
import logging
from datetime import datetime, timedelta
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Any
from pathlib import Path

import httpx
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# Load environment from parent directory .env
load_dotenv(Path(__file__).parent / ".env")

# Configuration
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8002")
SESSION_TTL_MINUTES = 30
ITEMS_PER_PAGE = 5

# Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)


# ============ ENUMS & DATA CLASSES ============


class Step(Enum):
    IDLE = "idle"
    # PSB flow
    SELECT_OLT = "select_olt"
    SELECT_ONT = "select_ont"
    SELECT_PSB = "select_psb"
    SELECT_MODEM = "select_modem"
    CONFIRM = "confirm"
    CONFIGURING = "configuring"
    # Cek flow
    CEK_SEARCHING = "cek_searching"
    CEK_SELECTING = "cek_selecting"
    CEK_CHECKED = "cek_checked"
    CEK_MENU = "cek_menu"
    CEK_CONFIRM_REBOOT = "cek_confirm_reboot"


@dataclass
class UserSession:
    step: Step = Step.IDLE
    mode: str = ""  # "PSB" or "CEK"
    is_busy: bool = False  # Concurrency guard
    # PSB flow fields
    olt_name: Optional[str] = None
    ont: Optional[Dict] = None
    psb: Optional[Dict] = None
    selected_modem: Optional[str] = None
    options: Optional[Dict] = None
    ont_list: List[Dict] = field(default_factory=list)
    psb_list: List[Dict] = field(default_factory=list)
    psb_map: Dict[str, Dict] = field(
        default_factory=dict
    )  # pppoe -> psb for stable lookup
    page: int = 0
    # Cek flow fields
    selected_customer: Optional[Dict] = None
    customer_list: List[Dict] = field(default_factory=list)
    customer_map: Dict[str, Dict] = field(
        default_factory=dict
    )  # pppoe -> customer for stable lookup
    last_cek_result: Optional[str] = None
    last_action: Optional[str] = None  # "cek" | "port_state" | "port_rx"
    # Timestamps
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)

    def update(self):
        self.updated_at = datetime.now()

    def is_expired(self) -> bool:
        return datetime.now() - self.updated_at > timedelta(minutes=SESSION_TTL_MINUTES)

    def clear_cek(self):
        """Clear only CEK-related fields."""
        self.selected_customer = None
        self.customer_list = []
        self.customer_map = {}
        self.last_cek_result = None
        self.last_action = None
        if self.mode == "CEK":
            self.step = Step.IDLE
            self.mode = ""


def escape_telegram(text: str) -> str:
    """Escape special characters for Telegram Markdown (basic)."""
    # Remove or escape backticks and underscores that could break formatting
    return text.replace("`", "'").replace("_", "-")


def safe_output(text: str, max_len: int = 3500) -> str:
    """Truncate and escape output for safe Telegram display."""
    if len(text) > max_len:
        text = text[:max_len] + "\n... (truncated)"
    return escape_telegram(text)


# In-memory session storage
sessions: Dict[int, UserSession] = {}


def get_session(user_id: int) -> UserSession:
    """Get or create session for user."""
    if user_id not in sessions or sessions[user_id].is_expired():
        sessions[user_id] = UserSession()
    return sessions[user_id]


def clear_session(user_id: int):
    """Clear user session."""
    if user_id in sessions:
        del sessions[user_id]


# ============ API CLIENT ============


class APIClient:
    """HTTP client for backend API calls."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def client(self) -> httpx.AsyncClient:
        """Lazy initialization of httpx client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=60.0)
        return self._client

    async def close(self):
        """Close the httpx client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def get_options(self) -> Dict:
        """GET /api/options"""
        try:
            resp = await self.client.get(f"{self.base_url}/api/options")
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"API get_options error: {e}")
            raise

    async def detect_onts(self, olt_name: str) -> List[Dict]:
        """POST /api/olts/{olt_name}/detect-onts"""
        try:
            resp = await self.client.post(
                f"{self.base_url}/api/olts/{olt_name}/detect-onts"
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"API detect_onts error: {e}")
            raise

    async def get_psb_list(self, search: str = "", limit: int = 50) -> List[Dict]:
        """GET /customers/psb - from Supabase (backup)"""
        try:
            params = {"limit": limit}
            if search:
                params["search"] = search
            resp = await self.client.get(
                f"{self.base_url}/customers/psb", params=params
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"API get_psb_list error: {e}")
            raise

    async def get_real_psb_list(self) -> List[Dict]:
        """GET /customer/psb - from NMS scraper (source of truth)"""
        try:
            resp = await self.client.get(f"{self.base_url}/customer/psb")
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"API get_real_psb_list error: {e}")
            raise

    async def configure_ont(self, olt_name: str, payload: Dict) -> Dict:
        """POST /api/olts/{olt_name}/configure"""
        try:
            resp = await self.client.post(
                f"{self.base_url}/api/olts/{olt_name}/configure", json=payload
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"API configure_ont error: {e}")
            raise

    # ========== CEK FEATURE APIs ==========

    async def search_customers(self, query: str, limit: int = 20) -> List[Dict]:
        """GET /customer/customers-data?search=..."""
        try:
            params = {"search": query, "limit": limit}
            resp = await self.client.get(
                f"{self.base_url}/customer/customers-data", params=params
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"API search_customers error: {e}")
            raise

    async def cek_onu(self, olt_name: str, interface: str) -> str:
        """POST /onu/cek - returns plain text"""
        try:
            payload = {"olt_name": olt_name, "interface": interface}
            resp = await self.client.post(f"{self.base_url}/onu/cek", json=payload)
            resp.raise_for_status()
            return resp.text
        except Exception as e:
            logger.error(f"API cek_onu error: {e}")
            raise

    async def reboot_onu(self, olt_name: str, interface: str) -> str:
        """POST /api/v1/onu/{olt_name}/onu/reboot"""
        try:
            payload = {"olt_name": olt_name, "interface": interface}
            resp = await self.client.post(
                f"{self.base_url}/api/v1/onu/{olt_name}/onu/reboot", json=payload
            )
            resp.raise_for_status()
            return resp.json().get("result", "OK")
        except Exception as e:
            logger.error(f"API reboot_onu error: {e}")
            raise

    async def get_port_state(self, olt_name: str, interface: str) -> str:
        """POST /{olt_name}/onu/port_state - returns plain text"""
        try:
            payload = {"olt_name": olt_name, "interface": interface}
            resp = await self.client.post(
                f"{self.base_url}/{olt_name}/onu/port_state", json=payload
            )
            resp.raise_for_status()
            return resp.text
        except Exception as e:
            logger.error(f"API get_port_state error: {e}")
            raise

    async def get_port_rx(self, olt_name: str, interface: str) -> str:
        """POST /{olt_name}/onu/port_rx - returns plain text"""
        try:
            payload = {"olt_name": olt_name, "interface": interface}
            resp = await self.client.post(
                f"{self.base_url}/{olt_name}/onu/port_rx", json=payload
            )
            resp.raise_for_status()
            return resp.text
        except Exception as e:
            logger.error(f"API get_port_rx error: {e}")
            raise


api = APIClient(API_BASE_URL)


# ============ KEYBOARD BUILDERS ============


def build_main_menu() -> InlineKeyboardMarkup:
    """Build main menu keyboard."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📡 PSB Config", callback_data="menu_psb")],
            [InlineKeyboardButton("🔍 Cek ONU", callback_data="menu_cek")],
            [InlineKeyboardButton("❓ Help", callback_data="menu_help")],
        ]
    )


def build_cek_action_menu() -> InlineKeyboardMarkup:
    """Build action menu for cek feature."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔄 Reboot Modem", callback_data="cek_action:reboot"
                )
            ],
            [
                InlineKeyboardButton(
                    "📊 Port Status", callback_data="cek_action:port_state"
                )
            ],
            [
                InlineKeyboardButton(
                    "📶 Port Redaman", callback_data="cek_action:port_rx"
                )
            ],
            [InlineKeyboardButton("🔃 Refresh", callback_data="cek_action:refresh")],
            [InlineKeyboardButton("❌ Exit", callback_data="cek_action:exit")],
        ]
    )


def build_customer_selection_keyboard(
    customers: List[Dict], customer_map: Dict[str, Dict], page: int = 0
) -> InlineKeyboardMarkup:
    """Build customer selection keyboard for cek using stable pppoe IDs."""
    total_pages = max(1, (len(customers) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)
    start = page * ITEMS_PER_PAGE
    end = start + ITEMS_PER_PAGE
    page_items = customers[start:end]

    buttons = []
    for c in page_items:
        name = c.get("name", c.get("nama", "?"))[:15]
        pppoe = c.get("pppoe_user", c.get("user_pppoe", ""))
        # Store in map for lookup
        if pppoe:
            customer_map[pppoe] = c
        label = f"{pppoe[:12]} | {name}"
        # Use pppoe as stable ID (max 64 bytes for callback_data)
        buttons.append(
            [InlineKeyboardButton(label, callback_data=f"cek_pick:{pppoe[:50]}")]
        )

    # Navigation
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️", callback_data=f"cek_page:{page - 1}"))
    nav_row.append(
        InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop")
    )
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("➡️", callback_data=f"cek_page:{page + 1}"))
    if nav_row:
        buttons.append(nav_row)

    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
    return InlineKeyboardMarkup(buttons)


def build_reboot_confirm_keyboard() -> InlineKeyboardMarkup:
    """Build reboot confirmation keyboard."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Ya, Reboot", callback_data="cek_reboot_confirm")],
            [InlineKeyboardButton("❌ Batal", callback_data="cek_action:back")],
        ]
    )


def build_modem_keyboard(modem_options: List[str]) -> InlineKeyboardMarkup:
    """Build modem type selection keyboard with numbered buttons."""
    buttons = []
    for idx, modem in enumerate(modem_options, start=1):
        buttons.append(
            [InlineKeyboardButton(f"{idx}. {modem}", callback_data=f"modem_{modem}")]
        )
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
    return InlineKeyboardMarkup(buttons)


def build_olt_keyboard(olt_list: List[str]) -> InlineKeyboardMarkup:
    """Build OLT selection keyboard."""
    buttons = []
    for olt in olt_list:
        buttons.append([InlineKeyboardButton(olt, callback_data=f"olt_{olt}")])
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
    return InlineKeyboardMarkup(buttons)


def build_ont_keyboard(ont_list: List[Dict], page: int) -> InlineKeyboardMarkup:
    """Build ONT selection keyboard with details."""
    total_pages = (len(ont_list) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
    start = page * ITEMS_PER_PAGE
    end = start + ITEMS_PER_PAGE
    page_items = ont_list[start:end]

    buttons = []
    for idx, ont in enumerate(page_items):
        real_idx = start + idx
        interface = ont.get("interface", ont.get("onu_id", f"ONT-{real_idx}"))
        sn = ont.get("sn", "")[:8]
        label = f"{interface} | {sn}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"ont_{real_idx}")])

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️", callback_data=f"page_ont_{page - 1}"))
    nav_row.append(
        InlineKeyboardButton(f"{page + 1}/{max(1, total_pages)}", callback_data="noop")
    )
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("➡️", callback_data=f"page_ont_{page + 1}"))
    if nav_row:
        buttons.append(nav_row)

    buttons.append([InlineKeyboardButton("🔄 Refresh", callback_data="refresh_ont")])
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
    return InlineKeyboardMarkup(buttons)


def build_psb_keyboard(psb_list: List[Dict], page: int) -> InlineKeyboardMarkup:
    """Build PSB selection keyboard."""
    total_pages = max(1, (len(psb_list) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)
    start = page * ITEMS_PER_PAGE
    end = start + ITEMS_PER_PAGE
    page_items = psb_list[start:end]

    buttons = []
    for idx, psb in enumerate(page_items):
        real_idx = start + idx
        name = psb.get("name", psb.get("nama", "Unknown"))[:20]
        pppoe = psb.get("pppoe_user", psb.get("user_pppoe", ""))[:15]
        label = f"{name} | {pppoe}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"psb_{real_idx}")])

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️", callback_data=f"page_psb_{page - 1}"))
    nav_row.append(
        InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop")
    )
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("➡️", callback_data=f"page_psb_{page + 1}"))
    if nav_row:
        buttons.append(nav_row)

    buttons.append([InlineKeyboardButton("🔍 Search", callback_data="search_psb")])
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
    return InlineKeyboardMarkup(buttons)


def build_confirm_keyboard() -> InlineKeyboardMarkup:
    """Build confirmation keyboard."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Confirm Configure", callback_data="confirm_yes")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
        ]
    )


def build_result_keyboard() -> InlineKeyboardMarkup:
    """Build result action keyboard."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔄 New PSB", callback_data="menu_psb")],
            [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")],
        ]
    )


# ============ COMMAND HANDLERS ============


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    user = update.effective_user
    logger.info(f"User {user.id} ({user.username}) started bot")

    await update.message.reply_text(
        f"👋 Halo {user.first_name}!\n\n"
        "Selamat datang di *OLT Provisioning Bot*.\n"
        "Bot ini membantu proses konfigurasi ONT untuk pelanggan baru (PSB).\n\n"
        "Pilih menu di bawah:",
        parse_mode="Markdown",
        reply_markup=build_main_menu(),
    )


async def cmd_psb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /psb command - start provisioning flow."""
    user_id = update.effective_user.id
    session = get_session(user_id)

    await update.message.reply_text("⏳ Mengambil daftar OLT...")

    try:
        options = await api.get_options()
        session.options = options
        session.step = Step.SELECT_OLT
        session.update()

        olt_list = options.get("olt_options", [])

        await update.message.reply_text(
            "📡 *Pilih OLT:*",
            parse_mode="Markdown",
            reply_markup=build_olt_keyboard(olt_list),
        )
    except Exception as e:
        await update.message.reply_text(
            f"❌ Gagal mengambil data OLT:\n`{e}`\n\nCoba lagi dengan /psb",
            parse_mode="Markdown",
        )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /cancel command."""
    user_id = update.effective_user.id
    clear_session(user_id)

    await update.message.reply_text(
        "🚫 Proses dibatalkan.\n\nKetik /start untuk memulai lagi.",
        reply_markup=build_main_menu(),
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /status command - show current session state."""
    user_id = update.effective_user.id
    session = get_session(user_id)

    status_text = (
        f"📊 *Session Status*\n\n"
        f"Step: `{session.step.value}`\n"
        f"OLT: `{session.olt_name or '-'}`\n"
        f"ONT: `{session.ont.get('interface', '-') if session.ont else '-'}`\n"
        f"PSB: `{session.psb.get('name', session.psb.get('nama', '-')) if session.psb else '-'}`\n"
        f"Last Update: `{session.updated_at.strftime('%H:%M:%S')}`"
    )

    await update.message.reply_text(status_text, parse_mode="Markdown")


# ============ CALLBACK HANDLERS ============


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all callback queries."""
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    session = get_session(user_id)
    data = query.data

    logger.info(f"Callback from {user_id}: {data}")

    # Route to appropriate handler
    if data == "noop":
        return
    elif data == "main_menu":
        await show_main_menu(query, session)
    elif data == "menu_psb":
        await start_psb_flow(query, session)
    elif data == "menu_help":
        await show_help(query)
    elif data == "cancel":
        await handle_cancel(query, user_id)
    elif data.startswith("olt_"):
        await handle_olt_selection(query, session, data)
    elif data.startswith("ont_"):
        await handle_ont_selection(query, session, data)
    elif data.startswith("psb_"):
        await handle_psb_selection(query, session, data)
    elif data.startswith("modem_"):
        await handle_modem_selection(query, session, data)
    elif data.startswith("page_"):
        await handle_pagination(query, session, data)
    elif data == "refresh_ont":
        await refresh_ont_list(query, session)
    elif data == "refresh_psb":
        await refresh_psb_list(query, session)
    elif data == "confirm_yes":
        await handle_confirm(query, session)
    # === CEK FEATURE CALLBACKS ===
    elif data == "menu_cek":
        await prompt_cek_search(query, session)
    elif data.startswith("cek_pick:"):
        await handle_cek_pick(query, session, data)
    elif data.startswith("cek_page:"):
        await handle_cek_page(query, session, data)
    elif data.startswith("cek_action:"):
        await handle_cek_action(query, session, data)
    elif data == "cek_reboot_confirm":
        await handle_cek_reboot_confirm(query, session)


async def show_main_menu(query, session: UserSession):
    """Show main menu."""
    session.step = Step.IDLE
    await query.edit_message_text(
        "🏠 *Menu Utama*\n\nPilih menu di bawah:",
        parse_mode="Markdown",
        reply_markup=build_main_menu(),
    )


async def start_psb_flow(query, session: UserSession):
    """Start PSB configuration flow."""
    await query.edit_message_text("⏳ Mengambil daftar OLT...")

    try:
        options = await api.get_options()
        session.options = options
        session.step = Step.SELECT_OLT
        session.page = 0
        session.update()

        olt_list = options.get("olt_options", [])

        await query.edit_message_text(
            "📡 *Step 1: Pilih OLT*",
            parse_mode="Markdown",
            reply_markup=build_olt_keyboard(olt_list),
        )
    except Exception as e:
        await query.edit_message_text(
            f"❌ Gagal mengambil data OLT:\n`{e}`",
            parse_mode="Markdown",
            reply_markup=build_main_menu(),
        )


async def show_help(query):
    """Show help message."""
    help_text = (
        "❓ *Bantuan*\n\n"
        "*Commands:*\n"
        "/start - Mulai bot\n"
        "/psb - Mulai proses PSB\n"
        "/cancel - Batalkan proses\n"
        "/status - Lihat status sesi\n\n"
        "*Flow PSB:*\n"
        "1️⃣ Pilih OLT\n"
        "2️⃣ Pilih ONT yang terdeteksi\n"
        "3️⃣ Pilih data pelanggan (PSB)\n"
        "4️⃣ Konfirmasi & jalankan config"
    )
    await query.edit_message_text(
        help_text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")]]
        ),
    )


async def handle_cancel(query, user_id: int):
    """Handle cancel action."""
    clear_session(user_id)
    await query.edit_message_text(
        "🚫 Proses dibatalkan.", reply_markup=build_main_menu()
    )


async def handle_olt_selection(query, session: UserSession, data: str):
    """Handle OLT selection."""
    olt_name = data.replace("olt_", "")
    session.olt_name = olt_name
    session.step = Step.SELECT_ONT
    session.page = 0
    session.update()

    await query.edit_message_text(
        f"⏳ Mendeteksi ONT di *{olt_name}*...", parse_mode="Markdown"
    )

    try:
        ont_list = await api.detect_onts(olt_name)
        session.ont_list = ont_list

        if not ont_list:
            await query.edit_message_text(
                f"⚠️ Tidak ada ONT unconfigured di *{olt_name}*",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "🔄 Refresh", callback_data=f"olt_{olt_name}"
                            )
                        ],
                        [InlineKeyboardButton("⬅️ Back", callback_data="menu_psb")],
                    ]
                ),
            )
            return

        await query.edit_message_text(
            f"📡 *Step 2: Pilih ONT* (OLT: {olt_name})\n"
            f"Ditemukan: {len(ont_list)} ONT unconfigured",
            parse_mode="Markdown",
            reply_markup=build_ont_keyboard(ont_list, 0),
        )
    except Exception as e:
        await query.edit_message_text(
            f"❌ Gagal mendeteksi ONT:\n`{e}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🔄 Retry", callback_data=f"olt_{olt_name}")],
                    [InlineKeyboardButton("⬅️ Back", callback_data="menu_psb")],
                ]
            ),
        )


async def refresh_ont_list(query, session: UserSession):
    """Refresh ONT list."""
    if not session.olt_name:
        await query.edit_message_text("Session expired. /psb to restart.")
        return

    await handle_olt_selection(query, session, f"olt_{session.olt_name}")


async def handle_ont_selection(query, session: UserSession, data: str):
    """Handle ONT selection."""
    idx = int(data.replace("ont_", ""))

    if idx >= len(session.ont_list):
        await query.edit_message_text("Session expired. /psb to restart.")
        return

    session.ont = session.ont_list[idx]
    session.step = Step.SELECT_PSB
    session.page = 0
    session.update()

    await fetch_and_show_psb(query, session)


async def fetch_and_show_psb(query, session: UserSession):
    """Fetch PSB list from NMS and display."""
    await query.edit_message_text("⏳ Mengambil daftar PSB dari NMS...")

    try:
        # Use real PSB endpoint from NMS scraper
        psb_list = await api.get_real_psb_list()
        session.psb_list = psb_list

        if not psb_list:
            await query.edit_message_text(
                "⚠️ Tidak ada data PSB tersedia dari NMS.",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "🔄 Refresh PSB", callback_data="refresh_psb"
                            )
                        ],
                        [InlineKeyboardButton("⬅️ Back", callback_data="refresh_ont")],
                    ]
                ),
            )
            return

        ont_info = session.ont.get("interface", session.ont.get("onu_id", "?"))
        await query.edit_message_text(
            f"👤 *Step 3: Pilih Pelanggan PSB*\n"
            f"ONT: `{ont_info}`\n"
            f"Total PSB: {len(psb_list)}",
            parse_mode="Markdown",
            reply_markup=build_psb_keyboard(psb_list, 0),
        )
    except Exception as e:
        await query.edit_message_text(
            f"❌ Gagal mengambil data PSB:\n{str(e)[:300]}",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🔄 Refresh PSB", callback_data="refresh_psb"
                        )
                    ],
                    [InlineKeyboardButton("⬅️ Back", callback_data="refresh_ont")],
                ]
            ),
        )


async def refresh_psb_list(query, session: UserSession):
    """Refresh PSB list from NMS."""
    if not session.ont:
        await query.edit_message_text("Session expired. /psb to restart.")
        return
    await fetch_and_show_psb(query, session)


async def handle_psb_selection(query, session: UserSession, data: str):
    """Handle PSB selection - then show modem type selection."""
    idx = int(data.replace("psb_", ""))

    if idx >= len(session.psb_list):
        await query.edit_message_text("Session expired. /psb to restart.")
        return

    session.psb = session.psb_list[idx]
    session.step = Step.SELECT_MODEM
    session.update()

    # Get modem options from cached options
    modem_options = (
        session.options.get("modem_options", ["F609", "F670L", "C-DATA"])
        if session.options
        else ["F609", "F670L", "C-DATA"]
    )

    psb = session.psb
    await query.edit_message_text(
        f"📱 *Step 4: Pilih Tipe Modem*\n\n"
        f"Pelanggan: `{psb.get('nama', psb.get('name', '?'))}`\n"
        f"PPPoE: `{psb.get('pppoe_user', psb.get('user_pppoe', '?'))}`\n\n"
        "Pilih tipe modem:",
        parse_mode="Markdown",
        reply_markup=build_modem_keyboard(modem_options),
    )


async def handle_modem_selection(query, session: UserSession, data: str):
    """Handle modem type selection - then show confirmation."""
    modem_type = data.replace("modem_", "")

    session.selected_modem = modem_type
    session.step = Step.CONFIRM
    session.update()

    # Build confirmation message
    ont = session.ont
    psb = session.psb

    confirm_text = (
        "✅ *Konfirmasi Provisioning*\n\n"
        f"*OLT:* `{session.olt_name}`\n"
        f"*ONT:* `{ont.get('interface', ont.get('onu_id', '?'))}`\n"
        f"*SN:* `{ont.get('sn', '?')}`\n\n"
        f"*Pelanggan:* `{psb.get('nama', psb.get('name', '?'))}`\n"
        f"*Alamat:* `{psb.get('alamat', '-')}`\n"
        f"*PPPoE:* `{psb.get('pppoe_user', psb.get('user_pppoe', '?'))}`\n"
        f"*Paket:* `{psb.get('paket', '-')}`\n"
        f"*Modem:* `{modem_type}`\n\n"
        "Tekan *Confirm* untuk menjalankan konfigurasi."
    )

    await query.edit_message_text(
        confirm_text, parse_mode="Markdown", reply_markup=build_confirm_keyboard()
    )


async def handle_pagination(query, session: UserSession, data: str):
    """Handle pagination navigation."""
    parts = data.split("_")
    # page_ont_0 or page_psb_1
    list_type = parts[1]
    page = int(parts[2])
    session.page = page
    session.update()

    if list_type == "ont":
        await query.edit_message_text(
            f"📡 *Step 2: Pilih ONT* (OLT: {session.olt_name})\n"
            f"Ditemukan: {len(session.ont_list)} ONT unconfigured",
            parse_mode="Markdown",
            reply_markup=build_ont_keyboard(session.ont_list, page),
        )
    elif list_type == "psb":
        ont_info = (
            session.ont.get("interface", session.ont.get("onu_id", "?"))
            if session.ont
            else "?"
        )
        await query.edit_message_text(
            f"👤 *Step 3: Pilih Pelanggan PSB*\n"
            f"ONT: `{ont_info}`\n"
            f"Total PSB: {len(session.psb_list)}",
            parse_mode="Markdown",
            reply_markup=build_psb_keyboard(session.psb_list, page),
        )


async def handle_confirm(query, session: UserSession):
    """Handle configuration confirmation."""
    # Busy guard
    if session.is_busy:
        await query.answer("⏳ Sedang memproses, tunggu sebentar...")
        return

    session.is_busy = True
    session.step = Step.CONFIGURING
    session.update()

    await query.edit_message_text("⚙️ Menjalankan konfigurasi... Mohon tunggu.")

    try:
        # Build configuration payload
        ont = session.ont
        psb = session.psb

        payload = {
            "interface": ont.get("interface", ont.get("onu_id", "")),
            "sn": ont.get("sn", ""),
            "username": psb.get("pppoe_user", psb.get("user_pppoe", "")),
            "package": psb.get("paket", "10M"),
            "modem_type": session.selected_modem or "F609",
        }

        result = await api.configure_ont(session.olt_name, payload)

        # Success (plain text)
        result_text = (
            "✅ Konfigurasi Berhasil!\n\n"
            f"OLT: {session.olt_name}\n"
            f"ONT: {payload['interface']}\n"
            f"Username: {payload['username']}\n\n"
            f"Message: {result.get('message', 'OK')}"
        )

        await query.edit_message_text(result_text, reply_markup=build_result_keyboard())

    except Exception as e:
        error_text = (
            "❌ Konfigurasi Gagal!\n\n"
            f"Error: {str(e)[:500]}\n\n"
            "Coba lagi atau hubungi admin."
        )

        await query.edit_message_text(
            error_text,
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🔄 Retry", callback_data="confirm_yes")],
                    [InlineKeyboardButton("⬅️ Back", callback_data="menu_psb")],
                    [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")],
                ]
            ),
        )

    finally:
        session.is_busy = False
        session.step = Step.IDLE


# ============ CEK FEATURE HANDLERS ============


async def prompt_cek_search(query, session: UserSession):
    """Prompt user to enter search query for cek."""
    session.mode = "CEK"
    session.step = Step.CEK_SEARCHING
    session.update()

    await query.edit_message_text(
        "🔍 *Cek ONU*\n\n"
        "Ketik PPPoE atau nama pelanggan:\n"
        "Contoh: `c budi01` atau `cek Sari`\n\n"
        "_Atau kirim langsung nama/pppoe tanpa command_",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ Cancel", callback_data="cancel")]]
        ),
    )


async def handle_cek_pick(query, session: UserSession, data: str):
    """Handle customer selection for cek using stable pppoe ID."""
    pppoe = data.replace("cek_pick:", "")

    # Lookup from map (stable) or fallback to list search
    customer = session.customer_map.get(pppoe)
    if not customer:
        # Fallback: search in list
        for c in session.customer_list:
            if c.get("pppoe_user", c.get("user_pppoe", "")) == pppoe:
                customer = c
                break

    if not customer:
        await query.edit_message_text(
            "Session expired. Ketik `c <pppoe/nama>` untuk memulai ulang.",
            parse_mode="Markdown",
        )
        return

    session.selected_customer = customer
    session.update()

    await run_cek_onu(query, session)


async def handle_cek_page(query, session: UserSession, data: str):
    """Handle cek customer list pagination."""
    page = int(data.replace("cek_page:", ""))
    session.page = page
    session.update()

    await query.edit_message_text(
        f"👤 *Pilih Pelanggan* ({len(session.customer_list)} hasil)",
        parse_mode="Markdown",
        reply_markup=build_customer_selection_keyboard(
            session.customer_list, session.customer_map, page
        ),
    )


async def run_cek_onu(query, session: UserSession):
    """Run ONU check and show result with action menu."""
    customer = session.selected_customer
    if not customer:
        await query.edit_message_text("Session expired.")
        return

    olt_name = customer.get("olt_name", "")
    interface = customer.get("interface", customer.get("olt_port", ""))
    name = customer.get("name", customer.get("nama", "?"))
    pppoe = customer.get("pppoe_user", customer.get("user_pppoe", ""))

    if not olt_name or not interface:
        await query.edit_message_text(
            f"⚠️ Data OLT/interface tidak lengkap untuk {name}\n\n"
            f"OLT: {olt_name or 'N/A'}\n"
            f"Interface: {interface or 'N/A'}",
            reply_markup=build_main_menu(),
        )
        return

    session.is_busy = True
    await query.edit_message_text(f"⏳ Mengecek ONU untuk {name}...")

    try:
        result = await api.cek_onu(olt_name, interface)
        session.last_cek_result = result
        session.last_action = "cek"
        session.step = Step.CEK_MENU
        session.update()

        # Use plain text for device output (no Markdown to avoid breaking)
        response_text = (
            f"👤 {name} ({pppoe})\n"
            f"📡 OLT: {olt_name}\n"
            f"🔌 Interface: {interface}\n\n"
            f"{result[:3500]}"
        )

        await query.edit_message_text(
            response_text, reply_markup=build_cek_action_menu()
        )
    except Exception as e:
        await query.edit_message_text(
            f"❌ Gagal cek ONU:\n{str(e)[:500]}",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🔃 Retry", callback_data="cek_action:refresh"
                        )
                    ],
                    [InlineKeyboardButton("❌ Exit", callback_data="cek_action:exit")],
                ]
            ),
        )
    finally:
        session.is_busy = False


async def handle_cek_action(query, session: UserSession, data: str):
    """Handle cek action menu buttons."""
    action = data.replace("cek_action:", "")
    customer = session.selected_customer

    if not customer:
        await query.edit_message_text(
            "Session habis, ulangi dengan c <pppoe/nama>",
            reply_markup=build_main_menu(),
        )
        return

    # Busy guard
    if session.is_busy:
        await query.answer("⏳ Sedang memproses, tunggu sebentar...")
        return

    olt_name = customer.get("olt_name", "")
    interface = customer.get("interface", customer.get("olt_port", ""))
    name = customer.get("name", customer.get("nama", "?"))
    pppoe = customer.get("pppoe_user", customer.get("user_pppoe", ""))

    if action == "exit":
        session.clear_cek()
        session.update()
        await query.edit_message_text("✅ Selesai.", reply_markup=build_main_menu())
        return

    elif action == "back":
        # Back to action menu - show last result without re-fetching (plain text)
        if session.last_cek_result:
            response_text = (
                f"👤 {name} ({pppoe})\n"
                f"📡 OLT: {olt_name}\n"
                f"🔌 Interface: {interface}\n\n"
                f"{session.last_cek_result[:3500]}"
            )
            await query.edit_message_text(
                response_text, reply_markup=build_cek_action_menu()
            )
        else:
            await run_cek_onu(query, session)
        return

    elif action == "reboot":
        # Show reboot confirmation (plain text)
        session.step = Step.CEK_CONFIRM_REBOOT
        session.update()
        await query.edit_message_text(
            f"⚠️ Konfirmasi Reboot\n\n"
            f"Yakin ingin reboot modem untuk:\n"
            f"👤 {name}\n"
            f"🔌 {interface}",
            reply_markup=build_reboot_confirm_keyboard(),
        )
        return

    elif action == "refresh":
        # True refresh based on last_action
        if session.last_action == "port_state":
            # Re-run port_state
            session.is_busy = True
            await query.edit_message_text(f"⏳ Mengambil port status...")
            try:
                result = await api.get_port_state(olt_name, interface)
                response_text = f"📊 Port Status - {name}\n\n{result[:3500]}"
                await query.edit_message_text(
                    response_text, reply_markup=build_cek_action_menu()
                )
            except Exception as e:
                await query.edit_message_text(
                    f"❌ Error: {str(e)[:200]}", reply_markup=build_cek_action_menu()
                )
            finally:
                session.is_busy = False
        elif session.last_action == "port_rx":
            # Re-run port_rx
            session.is_busy = True
            await query.edit_message_text(f"⏳ Mengambil data redaman...")
            try:
                result = await api.get_port_rx(olt_name, interface)
                response_text = f"📶 Port Redaman - {name}\n\n{result[:3500]}"
                await query.edit_message_text(
                    response_text, reply_markup=build_cek_action_menu()
                )
            except Exception as e:
                await query.edit_message_text(
                    f"❌ Error: {str(e)[:200]}", reply_markup=build_cek_action_menu()
                )
            finally:
                session.is_busy = False
        else:
            # Default: re-run cek_onu
            await run_cek_onu(query, session)
        return

    elif action == "port_state":
        session.is_busy = True
        await query.edit_message_text(f"⏳ Mengambil port status...")
        try:
            result = await api.get_port_state(olt_name, interface)
            session.last_action = "port_state"
            # Plain text for device output
            response_text = f"📊 Port Status - {name}\n\n{result[:3500]}"
            await query.edit_message_text(
                response_text, reply_markup=build_cek_action_menu()
            )
        except Exception as e:
            await query.edit_message_text(
                f"❌ Error: {str(e)[:200]}", reply_markup=build_cek_action_menu()
            )
        finally:
            session.is_busy = False
        return

    elif action == "port_rx":
        session.is_busy = True
        await query.edit_message_text(f"⏳ Mengambil data redaman...")
        try:
            result = await api.get_port_rx(olt_name, interface)
            session.last_action = "port_rx"
            # Plain text for device output
            response_text = f"📶 Port Redaman - {name}\n\n{result[:3500]}"
            await query.edit_message_text(
                response_text, reply_markup=build_cek_action_menu()
            )
        except Exception as e:
            await query.edit_message_text(
                f"❌ Error: {str(e)[:200]}", reply_markup=build_cek_action_menu()
            )
        finally:
            session.is_busy = False
        return


async def handle_cek_reboot_confirm(query, session: UserSession):
    """Handle reboot confirmation."""
    customer = session.selected_customer
    if not customer:
        await query.edit_message_text("Session expired.")
        return

    olt_name = customer.get("olt_name", "")
    interface = customer.get("interface", customer.get("olt_port", ""))
    name = customer.get("name", customer.get("nama", "?"))

    await query.edit_message_text(f"🔄 Rebooting modem {name}...")

    try:
        result = await api.reboot_onu(olt_name, interface)
        session.step = Step.CEK_MENU
        session.update()

        await query.edit_message_text(
            f"✅ Reboot Berhasil\n\n"
            f"👤 {name}\n"
            f"📡 {olt_name} - {interface}\n\n"
            f"Response: {result}",
            reply_markup=build_cek_action_menu(),
        )
    except Exception as e:
        await query.edit_message_text(
            f"❌ Reboot gagal:\n{str(e)[:300]}", reply_markup=build_cek_action_menu()
        )


async def process_cek_search(
    update_or_query, session: UserSession, search_term: str, is_callback: bool = False
):
    """Process cek search query."""
    session.mode = "CEK"
    session.step = Step.CEK_SEARCHING
    session.update()

    if is_callback:
        await update_or_query.edit_message_text(f"🔍 Mencari: {search_term}...")
    else:
        await update_or_query.message.reply_text(f"🔍 Mencari: {search_term}...")

    try:
        customers = await api.search_customers(search_term)

        if not customers:
            msg = f"⚠️ Data tidak ditemukan untuk: {search_term}"
            if is_callback:
                await update_or_query.edit_message_text(
                    msg, reply_markup=build_main_menu()
                )
            else:
                await update_or_query.message.reply_text(
                    msg, reply_markup=build_main_menu()
                )
            return

        session.customer_list = customers
        session.customer_map = {}  # Reset map
        session.page = 0

        if len(customers) == 1:
            # Single result - proceed directly
            customer = customers[0]
            pppoe = customer.get("pppoe_user", customer.get("user_pppoe", ""))
            if pppoe:
                session.customer_map[pppoe] = customer
            session.selected_customer = customer
            session.update()

            if is_callback:
                await run_cek_onu(update_or_query, session)
            else:
                # For message, we need to send a new message
                olt_name = customer.get("olt_name", "")
                interface = customer.get("interface", customer.get("olt_port", ""))
                name = customer.get("name", customer.get("nama", "?"))

                if not olt_name or not interface:
                    await update_or_query.message.reply_text(
                        f"⚠️ Data tidak lengkap untuk {name}",
                        reply_markup=build_main_menu(),
                    )
                    return

                msg = await update_or_query.message.reply_text(
                    f"⏳ Mengecek ONU untuk {name}..."
                )

                try:
                    result = await api.cek_onu(olt_name, interface)
                    session.last_cek_result = result
                    session.last_action = "cek"
                    session.step = Step.CEK_MENU
                    session.update()

                    # Plain text for device output
                    response_text = (
                        f"👤 {name} ({pppoe})\n"
                        f"📡 OLT: {olt_name}\n"
                        f"🔌 Interface: {interface}\n\n"
                        f"{result[:3500]}"
                    )

                    await msg.edit_text(
                        response_text, reply_markup=build_cek_action_menu()
                    )
                except Exception as e:
                    await msg.edit_text(
                        f"❌ Gagal cek ONU: {str(e)[:200]}",
                        reply_markup=build_main_menu(),
                    )
        else:
            # Multiple results - show selection
            session.step = Step.CEK_SELECTING
            session.update()

            msg = f"👤 *Ditemukan {len(customers)} pelanggan*\nPilih salah satu:"
            if is_callback:
                await update_or_query.edit_message_text(
                    msg,
                    parse_mode="Markdown",
                    reply_markup=build_customer_selection_keyboard(
                        customers, session.customer_map, 0
                    ),
                )
            else:
                await update_or_query.message.reply_text(
                    msg,
                    parse_mode="Markdown",
                    reply_markup=build_customer_selection_keyboard(
                        customers, session.customer_map, 0
                    ),
                )

    except Exception as e:
        msg = f"❌ Error: {str(e)[:300]}"
        if is_callback:
            await update_or_query.edit_message_text(msg, reply_markup=build_main_menu())
        else:
            await update_or_query.message.reply_text(
                msg, reply_markup=build_main_menu()
            )


# ============ MESSAGE HANDLER (for search) ============


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text messages (for PSB search and cek commands)."""
    user_id = update.effective_user.id
    session = get_session(user_id)
    text = update.message.text.strip()

    # Check for c/cek command pattern
    if text.lower().startswith("c ") or text.lower().startswith("cek "):
        # Extract search term
        if text.lower().startswith("cek "):
            search_term = text[4:].strip()
        else:
            search_term = text[2:].strip()

        if not search_term:
            await update.message.reply_text(
                "Format: `c <user_pppoe / nama>`\nContoh: `c budi01` atau `cek Sari`",
                parse_mode="Markdown",
            )
            return

        await process_cek_search(update, session, search_term, is_callback=False)
        return

    # Handle just "c" or "cek" without query
    if text.lower() in ["c", "cek"]:
        await update.message.reply_text(
            "Format: `c <user_pppoe / nama>`\nContoh: `c budi01` atau `cek Sari`",
            parse_mode="Markdown",
        )
        return

    # Handle CEK searching state
    if session.step == Step.CEK_SEARCHING:
        await process_cek_search(update, session, text, is_callback=False)
        return

    # Handle PSB search (legacy - PSB now comes from NMS, not searchable)
    if session.step == Step.SELECT_PSB:
        # PSB data comes from NMS, cannot search. Inform user.
        await update.message.reply_text(
            "📋 Data PSB diambil dari NMS dan tidak bisa dicari.\n"
            "Gunakan tombol *Refresh PSB* untuk memuat ulang data.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "� Refresh PSB", callback_data="refresh_psb"
                        )
                    ],
                    [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
                ]
            ),
        )


# ============ MAIN ============


async def post_init(application: Application):
    """Called after bot initialization - nothing needed here as client is lazy."""
    logger.info("Bot initialized")


async def post_shutdown(application: Application):
    """Called after bot shutdown - close API client."""
    await api.close()
    logger.info("Bot shutdown, API client closed")


async def cmd_cek(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /cek command - shortcut for cek ONU."""
    user_id = update.effective_user.id
    session = get_session(user_id)

    # Check if query provided
    if context.args:
        search_term = " ".join(context.args)
        await process_cek_search(update, session, search_term, is_callback=False)
    else:
        session.mode = "CEK"
        session.step = Step.CEK_SEARCHING
        session.update()

        await update.message.reply_text(
            "🔍 *Cek ONU*\n\n"
            "Cara pakai: `/cek <pppoe/nama>`\n"
            "Contoh: `/cek budi01`\n\n"
            "Atau ketik langsung nama/pppoe:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("❌ Cancel", callback_data="cancel")]]
            ),
        )


def main():
    """Run the bot."""
    if not BOT_TOKEN:
        print("ERROR: BOT_TOKEN not found in environment variables!")
        print("Make sure BOT_TOKEN is set in your .env file")
        sys.exit(1)

    print(f"Starting bot...")
    print(f"API Base URL: {API_BASE_URL}")

    # Create application with lifecycle hooks
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # Add handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("psb", cmd_psb))
    app.add_handler(CommandHandler("cek", cmd_cek))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Run
    print("Bot is running! Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
