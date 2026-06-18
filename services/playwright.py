import logging
import re
from datetime import datetime
from typing import List, Dict, Optional, Tuple
from core.config import settings
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError
from concurrent.futures import ThreadPoolExecutor
import asyncio
import time
import requests
from bs4 import BeautifulSoup
from api.v1.endpoints.ocr import _process_image_ocr as ocr
from schemas.customers_scrapper import (
    Customer,
    TicketItem,
    InvoiceItem,
    BillingSummary,
    CustomerwithInvoices,
)

# Month mapping for Indonesian to English
MONTH_MAP_ID = {
    "januari": "January",
    "februari": "February",
    "maret": "March",
    "april": "April",
    "mei": "May",
    "juni": "June",
    "juli": "July",
    "agustus": "August",
    "september": "September",
    "oktober": "October",
    "november": "November",
    "desember": "December",
}

# Configure logging to show messages
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)



LOGIN_URL = settings.LOGIN_URL_BILLING
DASHBOARD_URL_GLOB = "**/billing2/**"  # pattern for dashboard after login
INVOICES_URL = settings.DETAIL_URL_BILLING
TICKET_URL = settings.TICKET_NOC_URL
DATA_PSB_URL = settings.DATA_PSB_URL
LOGIN_URL_BILLING = settings.LOGIN_URL_BILLING

username_cs = settings.NMS_USERNAME_BILING
password_cs = settings.NMS_PASSWORD_BILING

username_noc = settings.NMS_USERNAME
password_noc = settings.NMS_PASSWORD


# Session storage paths
SESSION_DIR = Path(__file__).parent / "sessions"
SESSION_CS_FILE = SESSION_DIR / "session_cs.json"
SESSION_NOC_FILE = SESSION_DIR / "session_noc.json"


def _evaluate_math_captcha(text: str) -> Optional[int]:
    """
    Evaluate if CAPTCHA text is a math expression and return the answer.
    """
    try:
        # Remove all whitespace for easier parsing
        clean = text.replace(" ", "").replace("=", "").replace("?", "")

        # Check if it matches a simple math pattern: number operator number
        # Supports: +, -, *, /, x (as multiplication)
        math_pattern = r'^(\d+)\s*([+\-*/x×])\s*(\d+)$'
        match = re.search(math_pattern, clean, re.IGNORECASE)

        if match:
            num1 = int(match.group(1))
            operator = match.group(2).lower()
            num2 = int(match.group(3))

            # Map operators
            if operator == '+':
                result = num1 + num2
            elif operator == '-':
                result = num1 - num2
            elif operator in ['*', 'x', '×']:
                result = num1 * num2
            elif operator == '/':
                result = num1 // num2  # Integer division
            else:
                return None

            return int(result)

        return None

    except Exception as e:
        print(f"⚠️ Math evaluation failed: {e}")
        return None

# Thread pool for running sync playwright in async context
_executor = ThreadPoolExecutor(max_workers=2)


def run_sync(func, *args, **kwargs):
    """Run a sync function in thread pool, return awaitable."""
    loop = asyncio.get_event_loop()
    return loop.run_in_executor(_executor, lambda: func(*args, **kwargs))


class CustomerService:
    """Sync Playwright service for customer operations.
    Use run_sync() wrapper when calling from async FastAPI endpoints.
    """

    def __init__(self, username: str = None, password: str = None):
        self.username = username or username_cs
        self.password = password or password_cs
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.session_file = SESSION_CS_FILE
        self._logged_in = False

    def start(self, headless: bool = True):
        """Start browser (sync). Call this first."""
        # Ensure session directory exists
        SESSION_DIR.mkdir(exist_ok=True)

        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(headless=headless)

        # Try to load existing session
        if self.session_file.exists():
            logging.info(f"Loading session from {self.session_file}")
            self.context = self.browser.new_context(
                storage_state=str(self.session_file)
            )
        else:
            logging.info("No existing session found, creating new context")
            self.context = self.browser.new_context()

        self.page = self.context.new_page()

    def save_session(self):
        """Save current session (cookies, localStorage) to file."""
        if self.context:
            self.context.storage_state(path=str(self.session_file))
            logging.info(f"Session saved to {self.session_file}")

    def close(self, save: bool = True):
        """Close browser and cleanup."""
        if save and self.context:
            self.save_session()
        if self.context:
            self.context.close()
        if self.browser:
            self.browser.close()
        if self.playwright:
            self.playwright.stop()

    def is_logged_in(self) -> bool:
        """Check if already logged in by trying to access a protected page."""
        if self._logged_in:
            return True
        try:
            # Try accessing a protected page directly
            self.page.goto(LOGIN_URL.replace('login', ''), wait_until="domcontentloaded", timeout=5000)

            # If we're NOT on the login page, session is valid
            current_url = self.page.url.lower()
            if "login" not in current_url and "billing2" in current_url:
                logging.info("Already logged in (session restored)")
                self._logged_in = True
                return True
            return False
        except Exception:
            return False

    def login(self) -> bool:
        """Login to the billing system."""
        if not self.page:
            raise RuntimeError("Call start() first")

        # Skip if already logged in this session
        if self._logged_in:
            return True

        # Check if saved session is still valid
        if self.is_logged_in():
            return True

        self.page.goto(LOGIN_URL, wait_until="domcontentloaded")
        logging.info("Going to Login Page")

        max_attempts = 3
        for attempt in range(max_attempts):
            logging.info(f"Login attempt {attempt + 1}/{max_attempts}")

            # Check for CAPTCHA image
            captcha_img = self.page.locator('img[src*="captcha.php"]').first
            captcha_input = self.page.locator('input[name="captcha"]').first

            captcha_text = None

            if captcha_img.count() > 0 and captcha_img.is_visible():
                logging.info("CAPTCHA detected, solving...")
                try:
                    # Take screenshot of the CAPTCHA element
                    captcha_bytes = captcha_img.screenshot()

                    # Solve using OCR
                    ocr_text = ocr(captcha_bytes)
                    logging.info(f"OCR Result: '{ocr_text}'")

                    if ocr_text:
                        # Check for math expression
                        math_answer = _evaluate_math_captcha(ocr_text)

                        if math_answer is not None:
                            captcha_text = str(math_answer)
                            logging.info(f"Math solution: {captcha_text}")
                        else:
                            captcha_text = ocr_text.strip()
                            logging.info(f"CAPTCHA text: {captcha_text}")

                        # Fill CAPTCHA field
                        if captcha_input.count() > 0:
                            captcha_input.fill(captcha_text)
                except Exception as e:
                    logging.error(f"Error solving CAPTCHA: {e}")

            self.page.get_by_placeholder("Username").fill(self.username)
            logging.info("Username filled")
            self.page.get_by_placeholder("Password").fill(self.password)
            logging.info("Password filled")
            self.page.get_by_role("button", name="Sign In").click()

            try:
                self.page.wait_for_url(DASHBOARD_URL_GLOB, timeout=5000)
                # Save session after successful login
                self.save_session()
                self._logged_in = True
                logging.info("Login successful")
                return True
            except PWTimeoutError:
                err = self.page.get_by_text("Invalid username or password")
                if err.is_visible():
                    raise ValueError("Invalid username or password")

                # If we are still on login page, it's likely a temporary failure or CAPTCHA mismatch
                if "login" in self.page.url.lower():
                    logging.warning("Login failed (likely CAPTCHA), retrying...")
                    if attempt < max_attempts - 1:
                        time.sleep(1)
                        # Reload page to get new CAPTCHA
                        self.page.reload()
                        continue
                    else:
                        logging.error("Max login attempts reached")

        raise ValueError("Login failed after multiple attempts")

    def search_user(self, query: str):
        """Search for customers by name or number.
        Note: Caller must ensure login() has been called first.
        """

        field = self.page.get_by_placeholder("Name Or No Internet")
        field.fill(query)
        field.press("Enter")

        # Wait for search results to load
        self.page.wait_for_load_state("networkidle")
        self.page.get_by_text(query, exact=False).first.wait_for(timeout=10_000)

    def get_invoices(self, query: str = None, customer_id: str = None):
        """Get invoice data for a customer.

        Args:
            query: Internet number to search for
            customer_id: Customer ID to search for
        """
        ok = self.login()
        if not ok:
            return None

        # Use customer_id or query as search term
        search_term = customer_id or query
        if not search_term:
            logging.error("Either query or customer_id must be provided")
            return None

        # Search for the user first
        logging.info(f"Searching for: {search_term}")
        self.search_user(search_term)

        # Get the Detail User link href and navigate to it
        # (the link is inside a hidden dropdown menu, so we extract the href directly)
        detail_link = self.page.locator("a.dropdown-item[href*='deusr']").first
        if detail_link.count() > 0:
            href = detail_link.get_attribute("href")
            if href:
                # Build full URL from relative href
                base_url = self.page.url.rsplit("/", 1)[0]
                detail_url = f"{base_url}/{href}" if not href.startswith("http") else href
                logging.info(f"Navigating to Detail User: {detail_url}")
                self.page.goto(detail_url, wait_until="networkidle")
                logging.info("Navigated to Detail User page")
            else:
                logging.error("Detail User link has no href")
                return None
        else:
            logging.error(f"Could not find Detail User link for: {search_term}")
            return None

        # Helper to extract profile values
        def get_profile_value(label_text: str) -> str:
            try:
                label = self.page.locator(f"strong:has-text('{label_text}')").first
                if label.count() > 0:
                    value_span = label.locator("xpath=following-sibling::span").first
                    if value_span.count() > 0:
                        return value_span.inner_text().strip()
            except:
                pass
            return ""

        # Extract profile data
        data = {
            "user_join": get_profile_value("User Join"),
            "no_internet": get_profile_value("No Internet"),
            "mobile": get_profile_value("Mobile"),
            "nik": get_profile_value("NIK"),
            "paket": get_profile_value("Paket"),
            "last_payment": get_profile_value("Last Payment"),
            "uptime": get_profile_value("Uptime"),
            "bw_usage": get_profile_value("Bw Usage Up/Down"),
            "sn_modem": get_profile_value("SN Modem"),
        }

        # Get the invoice description from textarea
        textarea = self.page.locator("textarea[name='deskripsi_edit']").first
        invoices = ""
        if textarea.count() > 0:
            invoices = textarea.input_value()

        data["invoices"] = invoices

        logging.info(f"Invoice data retrieved for: {query}")
        return data

    def create_ticket(
        self, query: str, description: str, priority: str = "LOW", jenis: str = "FREE"
    ) -> bool:
        """Create a ticket for a customer using the Playwright browser session."""
        if not self.login():
            logging.error("[CS] Login failed. Cannot create ticket.")
            return False

        try:
            logging.info(f"[CS] Starting ticket creation for '{query}'.")
            
            # Go to dashboard and search
            self.page.goto(settings.BILLING_MODULE_BASE, wait_until="networkidle")
            self.page.locator("input[name='type_cari']").fill(query)
            self.page.locator("button[name='cari_tagihan']").click()
            
            # Find result row by text
            self.page.wait_for_selector("table#create_note tbody tr", timeout=10000)
            row = self.page.locator("table#create_note tbody tr", has_text=query).first
            
            if row.count() == 0:
                logging.error(f"[CS] No customer found for query '{query}'.")
                return False

            # Open Dropdown and click 'Ticket Gangguan'
            row.locator("a.table-action-btn.dropdown-toggle").click(force=True)
            ticket_item = self.page.locator(".dropdown-menu a", has_text="Ticket Gangguan").first
            
            # Get the dynamic modal ID
            modal_target = ticket_item.get_attribute("data-target") # e.g., #create_tiga_modal1494
            if not modal_target:
                logging.error("[CS] Ticket modal target not found in HTML.")
                return False
                
            ticket_item.click(force=True)
            
            # Fill the Modal
            self.page.wait_for_selector(modal_target, state="visible", timeout=5000)
            self.page.locator(f"{modal_target} select[name='priority']").select_option(value=priority.upper())
            self.page.locator(f"{modal_target} select[name='jenis_ticket']").select_option(value=jenis.upper())
            self.page.locator(f"{modal_target} textarea[name='deskripsi']").fill(description)
            
            # Submit and wait for modal to close
            self.page.locator(f"{modal_target} button[name='create_ticket_gangguan']").click()
            self.page.wait_for_selector(modal_target, state="hidden", timeout=10000)
            
            logging.info(f"[CS] Ticket for '{query}' submitted successfully.")
            return True

        except Exception as e:
            logging.error(f"[CS] Error during ticket creation: {e}")
            try:
                self.page.screenshot(path=f"cs_error_{query}.png")
                logging.info(f"Saved error screenshot to cs_error_{query}.png")
            except:
                pass
            return False
    @staticmethod
    def _parse_month_year(
        text: str,
    ) -> Tuple[Optional[str], Optional[int], Optional[int]]:
        """Parse month and year from text like 'Januari 2025'."""
        if not text:
            return None, None, None
        t = text.strip()
        low = t.lower()
        for indo, eng in MONTH_MAP_ID.items():
            if indo in low:
                t = low.replace(indo, eng).title()
                break
        m = re.search(r"([A-Za-z]+)\s+(\d{4})", t)
        if not m:
            return None, None, None
        mname, y = m.group(1), m.group(2)
        try:
            dt = datetime.strptime(f"{mname} {y}", "%B %Y")
            return m.group(0), dt.month, dt.year
        except Exception:
            return m.group(0), None, None

    @staticmethod
    def _parser_whatsapp_url(mobile: str) -> Optional[str]:
        """Generate WhatsApp URL from mobile number."""
        if not mobile:
            return None
        clean_number = mobile.strip()
        if clean_number == "0":
            return None
        return f"https://wa.me/{clean_number}"

    @staticmethod
    def _parser_maps_url(coordinate: str) -> Optional[str]:
        """Generate Google Maps URL from coordinates."""
        if not coordinate:
            return None
        clean_coordinate = coordinate.strip()
        if clean_coordinate == "0":
            return None
        return f"https://www.google.com/maps?q={clean_coordinate}"


# Convenience functions for running sync methods from async endpoints
def search_customer_sync(query: str, headless: bool = True):
    """Run customer search synchronously (for use with run_sync)."""
    service = CustomerService()
    try:
        service.start(headless=headless)
        return service.search_user(query)
    finally:
        service.close()


def get_customer_with_invoices_sync(query: str, headless: bool = True):
    """Search and get invoices for single customer (for use with run_sync)."""
    service = CustomerService()
    try:
        service.start(headless=headless)
        results = service.search_user(query)
        if not results:
            return None, None

        if len(results) == 1:
            invoices = service.get_invoices(results[0]["id"])
            return results, invoices

        return results, None
    finally:
        service.close()


def get_customer_details_sync(
    customer_id: str, headless: bool = True
) -> Optional[Customer]:
    """Get comprehensive customer details (for use with run_sync)."""
    service = CustomerService()
    try:
        service.start(headless=headless)
        return service.get_invoices(customer_id=customer_id)
    finally:
        service.close()


def get_invoice_data_sync(
    customer_id: str, headless: bool = True
) -> Optional[CustomerwithInvoices]:
    """Get detailed invoice data for customer (for use with run_sync)."""
    service = CustomerService()
    try:
        service.start(headless=headless)
        return service.get_invoices(customer_id=customer_id)
    finally:
        service.close()

