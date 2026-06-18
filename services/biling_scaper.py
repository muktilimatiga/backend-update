import os
import re
import pickle
import time
from datetime import datetime
from typing import List, Dict, Optional, Tuple
from urllib.parse import urlparse, parse_qs

import requests
import urllib3
from bs4 import BeautifulSoup
import logging

from core import settings
from schemas.customers_scrapper import Customer, TicketItem

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)

BILLING_COOKIE_FILE = "billing_session.pkl"
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


class BillingScraper:
    def __init__(
        self,
        session: Optional[requests.Session] = None,
        login_url: Optional[str] = None,
    ):
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            }
        )
        self.reused_session = session is not None
        if not self.reused_session:
            self.login_url = login_url or settings.LOGIN_URL_BILLING
            self._login()

    def _save_cookies(self):
        with open(BILLING_COOKIE_FILE, "wb") as f:
            pickle.dump(self.session.cookies, f)

    def _load_cookies(self) -> bool:
        if os.path.exists(BILLING_COOKIE_FILE):
            with open(BILLING_COOKIE_FILE, "rb") as f:
                self.session.cookies.update(pickle.load(f))
            return True
        return False

    def _is_logged(self) -> bool:
        try:
            r = self.session.get(
                settings.BILLING_MODULE_BASE,
                verify=False,
                allow_redirects=False,
                timeout=10,
            )
            return r.status_code == 200 and "login" not in r.url.lower()
        except requests.RequestException:
            return False

    def _solve_captcha(self, captcha_url: str) -> Optional[str]:
        """
        Download CAPTCHA image and solve it using direct OCR function import.

        Args:
            captcha_url: URL of the CAPTCHA image (e.g., 'captcha.php')

        Returns:
            Extracted CAPTCHA text, or None if failed
        """
        try:
            print(f"[BillingScraper] 🔍 Starting CAPTCHA solve from: {captcha_url}")

            # Import OCR function directly (avoids HTTP call to same process)
            from api.v1.endpoints.ocr import _process_image_ocr

            # Step 1: Download CAPTCHA image using the same session (to maintain cookies)
            print(f"[BillingScraper] 📥 Downloading CAPTCHA image...")
            captcha_response = self.session.get(captcha_url, verify=False, timeout=10)
            captcha_response.raise_for_status()
            captcha_bytes = captcha_response.content

            print(
                f"[BillingScraper] ✅ Downloaded {len(captcha_bytes)} bytes (Content-Type: {captcha_response.headers.get('Content-Type')})"
            )

            # Save CAPTCHA image for debugging
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            debug_filename = f"captcha_debug_{timestamp}.png"
            with open(debug_filename, "wb") as f:
                f.write(captcha_bytes)
            print(f"[BillingScraper] 💾 Saved CAPTCHA to: {debug_filename}")

            # Step 2: Process image directly with OCR function
            print(f"[BillingScraper] 🤖 Processing image with OCR...")
            captcha_text = _process_image_ocr(captcha_bytes)

            print(
                f"[BillingScraper] 📝 OCR raw output: '{captcha_text}' (length: {len(captcha_text) if captcha_text else 0})"
            )

            if captcha_text and captcha_text.strip():
                cleaned = captcha_text.strip()

                # Step 3: Check if CAPTCHA is a math expression and evaluate it
                math_answer = self._evaluate_math_captcha(cleaned)

                if math_answer is not None:
                    print(
                        f"[BillingScraper] 🧮 Math expression detected: '{cleaned}' = {math_answer}"
                    )
                    return str(math_answer)
                else:
                    print(f"[BillingScraper] ✅ CAPTCHA text (non-math): '{cleaned}'")
                    return cleaned
            else:
                print(
                    "[BillingScraper] ⚠️ OCR returned empty text - CAPTCHA not recognized"
                )
                return None

        except Exception as e:
            import traceback

            print(f"[BillingScraper] ❌ CAPTCHA solving failed with error: {e}")
            print(f"[BillingScraper] 📋 Traceback:\n{traceback.format_exc()}")
            return None

    @staticmethod
    def _evaluate_math_captcha(text: str) -> Optional[int]:
        """
        Evaluate if CAPTCHA text is a math expression and return the answer.

        Args:
            text: CAPTCHA text (e.g., "10 - 2", "5 + 3", "6 * 2")

        Returns:
            Integer result if valid math expression, None otherwise
        """
        try:
            # Remove all whitespace for easier parsing
            clean = text.replace(" ", "").replace("=", "").replace("?", "")

            # Check if it matches a simple math pattern: number operator number
            # Supports: +, -, *, /, x (as multiplication)
            math_pattern = r"^(\d+)\s*([+\-*/x×])\s*(\d+)$"
            match = re.match(math_pattern, clean, re.IGNORECASE)

            if match:
                num1 = int(match.group(1))
                operator = match.group(2).lower()
                num2 = int(match.group(3))

                # Map operators
                if operator == "+":
                    result = num1 + num2
                elif operator == "-":
                    result = num1 - num2
                elif operator in ["*", "x", "×"]:
                    result = num1 * num2
                elif operator == "/":
                    result = num1 // num2  # Integer division
                else:
                    return None

                return int(result)

            return None

        except Exception as e:
            print(f"[BillingScraper] ⚠️ Math evaluation failed: {e}")
            return None

    def _login(self):
        if self._load_cookies() and self._is_logged():
            print("[BillingScraper] 🔐 Already logged in (using saved cookies)")
            return

        print(f"[BillingScraper] 🚀 Starting login process to: {self.login_url}")

        # Extract base URL from login_url for constructing CAPTCHA URL
        from urllib.parse import urljoin

        # Construct CAPTCHA URL (assuming it's in the same directory as login page)
        captcha_url = urljoin(self.login_url, "captcha.php")
        print(f"[BillingScraper] 🔗 CAPTCHA URL: {captcha_url}")

        # Try login with CAPTCHA solving (with retries)
        max_attempts = 3
        last_error = None

        for attempt in range(max_attempts):
            print(f"\n[BillingScraper] 🔄 Login attempt {attempt + 1}/{max_attempts}")

            try:
                # Solve CAPTCHA
                captcha_text = self._solve_captcha(captcha_url)

                if not captcha_text:
                    print(
                        f"[BillingScraper] ⚠️ CAPTCHA solve failed (attempt {attempt + 1}/{max_attempts})"
                    )
                    if attempt < max_attempts - 1:
                        print(f"[BillingScraper] ⏳ Waiting 1 second before retry...")
                        time.sleep(1)  # Brief pause before retry
                        continue
                    else:
                        raise ConnectionError(
                            "Failed to solve CAPTCHA after multiple attempts"
                        )

                # Prepare login payload with CAPTCHA
                payload = {
                    "username": settings.NMS_USERNAME_BILING,
                    "password": settings.NMS_PASSWORD_BILING,
                    "captcha": captcha_text,  # Add CAPTCHA field
                }

                print(f"[BillingScraper] 📤 Submitting login with:")
                print(f"  - Username: {settings.NMS_USERNAME_BILING}")
                print(f"  - Password: {'*' * len(settings.NMS_PASSWORD_BILING)}")
                print(f"  - CAPTCHA: {captcha_text}")

                # Submit login
                r = self.session.post(
                    self.login_url, data=payload, verify=False, timeout=10
                )

                print(
                    f"[BillingScraper] 📨 Login response: Status {r.status_code}, URL: {r.url}"
                )

                # Check if login was successful
                if r.status_code not in (200, 302):
                    print(f"[BillingScraper] ❌ Bad status code: {r.status_code}")
                elif "login" in r.url.lower() or "pesan=" in r.url.lower():
                    print(
                        f"[BillingScraper] ❌ Login failed - error in URL: {r.url}"
                    )
                    if attempt < max_attempts - 1:
                        print(f"[BillingScraper] ⏳ Waiting 1 second before retry...")
                        time.sleep(1)
                        continue
                    else:
                        raise ConnectionError(
                            f"Billing login failed. Check BILLING credentials and LOGIN_URL_BILLING."
                        )
                else:
                    # Success!
                    print(f"[BillingScraper] ✅ Login successful with CAPTCHA!")
                    print(f"[BillingScraper] 💾 Saving session cookies...")
                    self._save_cookies()
                    return

            except requests.RequestException as e:
                last_error = e
                print(
                    f"[BillingScraper] ❌ Request error (attempt {attempt + 1}/{max_attempts}): {e}"
                )
                if attempt < max_attempts - 1:
                    print(f"[BillingScraper] ⏳ Waiting 1 second before retry...")
                    time.sleep(1)
                    continue

        # If we get here, all attempts failed
        print(f"[BillingScraper] 💀 All {max_attempts} login attempts failed")
        raise ConnectionError(f"Failed to connect to billing login page: {last_error}")

    @staticmethod
    def _parse_month_year(
        text: str,
    ) -> Tuple[Optional[str], Optional[int], Optional[int]]:
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
        if not mobile:
            return None
        clean_number = mobile.strip()
        if clean_number == "0":
            return None

        return f"https://wa.me/{clean_number}"

    @staticmethod
    def _parser_maps_url(coordinate: str) -> Optional[str]:
        if not coordinate:
            return None
        clean_coordinate = coordinate.strip()
        if clean_coordinate == "0":
            return None

        return f"https://www.google.com/maps?q={clean_coordinate}"

    def search(self, search_value: str) -> List[Dict]:
        search_payload = {"type_cari": search_value, "cari_tagihan": ""}
        try:
            res = self.session.post(
                settings.BILLING_MODULE_BASE,
                data=search_payload,
                verify=False,
                timeout=15,
                allow_redirects=True,
            )
            res.raise_for_status()
        except requests.RequestException as e:
            raise ConnectionError(f"Search request failed: {e}")

        soup = BeautifulSoup(res.text, "html.parser")

        final_url_params = parse_qs(urlparse(res.url).query)

        if "csp" in final_url_params and "id" in final_url_params:
            customer_id = final_url_params["id"][0]
            name_tag = soup.select_one("h5.font-size-15.mb-0")
            address_tag = soup.select_one("p.text-muted.mb-4")
            pppoe_tag = soup.find(lambda tag: "User PPPoE" in tag.text)
            return [
                {
                    "id": customer_id,
                    "name": name_tag.get_text(strip=True) if name_tag else "N/A",
                    "address": address_tag.get_text(strip=True)
                    if address_tag
                    else "N/A",
                    "user_pppoe": pppoe_tag.find_next_sibling("p").get_text(strip=True)
                    if pppoe_tag
                    else "N/A",
                }
            ]

        table = soup.find("table", id="create_note")

        # If table not found, try to find any table with tbody containing search results
        if not table:
            all_tables = soup.find_all("table")
            for t in all_tables:
                has_tbody = t.tbody is not None
                row_count = len(t.tbody.find_all("tr")) if t.tbody else 0
                # Try to use first table with tbody containing rows
                if has_tbody and row_count > 0:
                    table = t
                    break

        if not table or not table.tbody:
            return []

        collected_data = []
        for row in table.tbody.find_all("tr"):
            cols = row.find_all("td")
            if len(cols) < 5:
                continue
            name_tag = cols[0].find("h5")
            address_tag = cols[0].find("p")
            pppoe_tags = cols[1].find_all("p")

            # Find detail link - ID can be base64 encoded (not just digits)
            details_link_tag = cols[4].find("a", href=re.compile(r"deusr"))
            if (
                not all([name_tag, address_tag, details_link_tag])
                or len(pppoe_tags) < 2
            ):
                continue

            # Extract ID, handling whitespace in base64 encoded IDs
            href = details_link_tag.get("href", "")
            match = re.search(r"id=([^\s\"&]+)", href)
            if not match:
                # Try getting everything after id= and strip whitespace
                match = re.search(r"id=\s*(.+)", href, re.DOTALL)
                if match:
                    customer_id = re.sub(
                        r"\s+", "", match.group(1)
                    )  # Remove all whitespace
                else:
                    continue
            else:
                customer_id = match.group(1).strip()

            collected_data.append(
                {
                    "id": customer_id,
                    "name": name_tag.get_text(strip=True),
                    "address": address_tag.get_text(strip=True),
                    "user_pppoe": pppoe_tags[1].get_text(strip=True),
                }
            )
        return collected_data

    def create_ticket(
        self, query: str, description: str, priority: str = "LOW", jenis: str = "FREE"
    ) -> dict:
        """Create a ticket for a customer.

        Workflow: Search → Parse modal → Extract form fields → POST with all fields

        Args:
            query: Customer name or user_pppoe/no_internet to search
            description: Ticket description
            priority: LOW, MEDIUM, or HIGH
            jenis: FREE or CHARGED

        Returns:
            dict with status and message
        """
        # Step 1: Search to get HTML with modals
        search_payload = {"type_cari": query, "cari_tagihan": ""}
        try:
            res = self.session.post(
                settings.BILLING_MODULE_BASE,
                data=search_payload,
                verify=False,
                timeout=15,
                allow_redirects=True,
            )
            res.raise_for_status()
        except requests.RequestException as e:
            return {"success": False, "message": f"Search failed: {e}"}

        # Step 2: Parse HTML to find the Ticket Gangguan modal
        soup = BeautifulSoup(res.text, "html.parser")

        # Check if multiple results - look for multiple modals
        modals = soup.select('div[id^="create_tiga_modal"]')

        if not modals:
            return {
                "success": False,
                "message": f"No customer found for query: {query}",
            }

        if len(modals) > 1:
            return {
                "success": False,
                "message": f"Multiple customers found for query: {query}",
            }

        modal = modals[0]

        # Step 3: Extract all form fields from modal
        form = modal.select_one("form")
        if not form:
            return {"success": False, "message": "Modal form not found"}

        payload = {}

        # Extract all input fields (hidden + visible)
        for input_tag in form.select("input"):
            name = input_tag.get("name")
            value = input_tag.get("value", "")
            if name:
                payload[name] = value

        # Step 4: Override/add user-provided values
        payload["priority"] = priority.upper()
        payload["jenis_ticket"] = jenis.upper()
        payload["deskripsi"] = description
        payload["create_ticket_gangguan"] = ""  # Submit button

        # Step 5: POST to create ticket
        try:
            res = self.session.post(
                settings.BILLING_MODULE_BASE,
                data=payload,
                verify=False,
                timeout=15,
                allow_redirects=True,
            )
            res.raise_for_status()

            if "berhasil" in res.text.lower():
                return {"success": True, "message": f"Ticket created for {query}"}
            else:
                return {
                    "success": False,
                    "message": "Ticket creation may have failed - check billing system",
                }

        except requests.RequestException as e:
            return {"success": False, "message": f"Request failed: {e}"}

    def _prime_module(self):
        try:
            module_base = "https://nms.lexxadata.net.id/billing2/04/04101"
            self.session.get(module_base + "/index.php", verify=False, timeout=15)
        except Exception:
            pass

    def _find_modal_for_li(self, li, soup):
        btn = li.select_one("button[data-target]")
        if not btn:
            return None
        target_id = (btn.get("data-target") or "").lstrip("#").strip()
        if not target_id:
            return None
        return soup.select_one(f"#{target_id}")

    def _extract_from_textarea(self, ta_text: str) -> dict:
        if not ta_text:
            return {}
        text = re.sub(r"\r", "", ta_text).strip()
        m_name = re.search(r"^\s*Nama\s*:\s*(.+)$", text, re.M)
        customer_name = (
            m_name.group(1).strip()
            if m_name
            else (re.search(r"Pelanggan Yth,\s*\*(.*?)\*", text) or [None, None])[1]
        )
        m_no = re.search(r"No\s+Internet\s*:\s*([0-9]+)", text, re.I)
        no_internet = m_no.group(1) if m_no else None
        m_amt = re.search(r"Tagihan\s*:\s*Rp\.?\s*([0-9\.\,]+)", text, re.I)
        amount_text = m_amt.group(1) if m_amt else None
        m_period = re.search(r"bulan\s+([A-Za-z]+(?:\s+\d{4})?)", text, re.I)
        period_text = m_period.group(1) if m_period else None
        if period_text and not re.search(r"\d{4}", period_text):
            m_y = re.search(r"\b(\d{4})\b", text)
            if m_y:
                period_text = f"{period_text} {m_y.group(1)}"
        period_norm, period_month, period_year = self._parse_month_year(
            period_text or ""
        )
        m_due = re.search(
            r"sebelum\s+tanggal\s+(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", text, re.I
        )
        due_iso = None
        if m_due:
            d, mname, y = int(m_due.group(1)), m_due.group(2), int(m_due.group(3))
            mname_en = MONTH_MAP_ID.get(mname.lower(), mname)
            try:
                due_iso = (
                    datetime.strptime(f"{d} {mname_en} {y}", "%d %B %Y")
                    .date()
                    .isoformat()
                )
            except Exception:
                pass
        m_link = re.search(r"(https://payment\.lexxadata\.net\.id/\?id=[\w-]+)", text)
        link_from_text = m_link.group(1) if m_link else None
        return {
            "customer_name": customer_name,
            "no_internet": no_internet,
            "amount_text": amount_text,
            "period_text": period_norm,
            "period_month": period_month,
            "period_year": period_year,
            "due_date_iso": due_iso,
            "payment_link_from_text": link_from_text,
        }

    def _payment_link_from_li_or_modal(
        self, li, soup
    ) -> Tuple[Optional[str], Optional[str]]:
        inp = li.find("input", attrs={"type": "text"})
        if inp and inp.get("value", "").startswith("https://payment.lexxadata.net.id/"):
            modal = self._find_modal_for_li(li, soup)
            ta = modal.select_one('textarea[name="deskripsi_edit"]') if modal else None
            return inp.get("value").strip(), (ta.get_text() if ta else None)
        modal = self._find_modal_for_li(li, soup)
        if modal:
            ta = modal.select_one('textarea[name="deskripsi_edit"]')
            ta_text = ta.get_text() if ta else None
            if ta_text:
                m = re.search(
                    r"(https://payment\.lexxadata\.net\.id/\?id=[\w-]+)", ta_text
                )
                if m:
                    return m.group(1), ta_text
            return None, ta_text
        return None, None

    def parse_tickets(self, html_content: str) -> List[TicketItem]:
        soup = BeautifulSoup(html_content, "html.parser")
        tickets = []

        # Iterate through the table rows
        rows = soup.select("table.table-bordered tbody tr")

        for row in rows:
            cols = row.find_all("td")
            if len(cols) < 4:
                continue

            # --- 1. Basic Info ---
            ref_id = cols[0].get_text(strip=True)

            # Only include tickets that start with "TN"
            if not ref_id.startswith("TN"):
                continue

            date_created = cols[1].get_text(strip=True)

            # --- 2. Modal Extraction ---
            modal = row.find("div", class_="modal")

            ticket_description = None
            ticket_action = None

            if modal:
                # Parse Timeline for Description and Action
                timeline_items = modal.select(".track-order-list ul li")

                for item in timeline_items:
                    # Get Header (Actor) and Body (Message)
                    h5_tag = item.find("h5")
                    header_text = h5_tag.get_text(strip=True).upper() if h5_tag else ""

                    date_p = item.find("p", class_="text-muted")
                    body_text = ""
                    if date_p:
                        # The content is in the paragraph immediately following the date
                        detail_p = date_p.find_next_sibling("p", class_="text-muted")
                        if detail_p:
                            body_text = detail_p.get_text(strip=True)

                    # --- LOGIC ---

                    # 1. Description: Comes from "OPENED"
                    if "OPENED" in header_text and not ticket_description:
                        ticket_description = body_text

                    # 2. Action: Only capture if closed by TECHNICIAN or NOC
                    # This explicitly ignores "CLOSED BY CS"
                    if (
                        "CLOSED BY TECHNICIAN" in header_text
                        or "CLOSED BY NOC" in header_text
                    ):
                        ticket_action = body_text

            # Append result
            tickets.append(
                TicketItem(
                    ref_id=ref_id,
                    date_created=date_created,
                    description=ticket_description or "N/A",
                    action=ticket_action or "Pending/Check Timeline",
                )
            )

        return tickets

    def get_invoice_data(self, url: str) -> dict:
        try:
            # Added shorter timeout for direct lookups
            res = self.session.get(url, verify=False, timeout=10)
            res.raise_for_status()
        except requests.RequestException as e:
            # Return empty structure on failure so API doesn't crash
            return {
                "paket": None,
                "coordinate": None,
                "user_join": None,
                "mobile": None,
                "invoices": [],
                "summary": {
                    "this_month": "Error",
                    "arrears_count": 0,
                    "last_paid_month": None,
                },
            }

        soup = BeautifulSoup(res.text, "html.parser")

        # Helper function to extract profile values (strong -> sibling span pattern)
        def get_profile_value(label_text: str) -> str:
            strong = soup.find("strong", string=lambda t: t and label_text in t)
            if strong:
                value_span = strong.find_next_sibling("span")
                if value_span:
                    return value_span.get_text(strip=True)
            return None

        # Extract profile values
        package_current = get_profile_value("Paket")
        last_paid = get_profile_value("Last Payment")
        user_join = get_profile_value("User Join")
        mobile_raw = get_profile_value("Mobile")

        # Normalize mobile to 62 format
        mobile = None
        if mobile_raw:
            if mobile_raw.startswith("0"):
                mobile = "62" + mobile_raw[1:]
            else:
                mobile = mobile_raw

        # Extract coordinate from input name="coordinat" with value="lat,lng"
        coord_input = soup.find("input", {"name": "coordinat"})
        if coord_input and coord_input.get("value"):
            coord_value = coord_input.get("value", "").strip()
            # Format: "-8.122402,111.913993"
            if coord_value and "," in coord_value:
                coordinate = coord_value

        # Fallback: Try finding latitude/longitude from table rows and combine
        if not coordinate:
            latitude = None
            longitude = None
            for row in soup.find_all("tr"):
                cells = row.find_all(["td", "th"])
                for i, cell in enumerate(cells):
                    cell_text = cell.get_text(strip=True).lower()
                    if "lattitude" in cell_text or "latitude" in cell_text:
                        if i + 1 < len(cells):
                            latitude = cells[i + 1].get_text(strip=True)
                        elif cell.find_next_sibling():
                            latitude = cell.find_next_sibling().get_text(strip=True)
                    elif "longitude" in cell_text:
                        if i + 1 < len(cells):
                            longitude = cells[i + 1].get_text(strip=True)
                        elif cell.find_next_sibling():
                            longitude = cell.find_next_sibling().get_text(strip=True)

            # Try paragraphs if table didn't work
            if not latitude:
                lat_tag = soup.find(
                    lambda tag: tag.name == "p"
                    and (
                        "lattitude" in tag.get_text().lower()
                        or "latitude" in tag.get_text().lower()
                    )
                )
                if lat_tag and lat_tag.span:
                    latitude = lat_tag.span.get_text(strip=True)

            if not longitude:
                lng_tag = soup.find(
                    lambda tag: tag.name == "p"
                    and "longitude" in tag.get_text().lower()
                )
                if lng_tag and lng_tag.span:
                    longitude = lng_tag.span.get_text(strip=True)

            # Combine into coordinate if both found
            if latitude and longitude:
                coordinate = f"{latitude},{longitude}"

        invoices = []
        timeline_items = soup.select(
            "ul.list-unstyled.timeline-sm > li.timeline-sm-item"
        )
        for item in timeline_items:
            status_tag = item.select_one("span.timeline-sm-date span.badge")
            status = status_tag.get_text(strip=True) if status_tag else None
            package_tag = item.select_one("h5")
            package_name = package_tag.get_text(strip=True) if package_tag else None
            period_tag = package_tag.find_next_sibling("p") if package_tag else None
            period = period_tag.get_text(strip=True) if period_tag else None
            link_tag = item.select_one(
                "input[value^='https://payment.lexxadata.net.id']"
            )
            payment_link = link_tag["value"] if link_tag else None

            description = None
            bc_wa_button = item.select_one("button[data-target*='modaleditt']")
            if bc_wa_button and bc_wa_button.get("data-target"):
                modal_id = bc_wa_button["data-target"]
                modal = soup.select_one(modal_id)
                if modal:
                    textarea = modal.select_one('textarea[name="deskripsi_edit"]')
                    if textarea:
                        description = textarea.get_text(strip=True)

            period_norm, month, year = self._parse_month_year(period or "")

            invoices.append(
                {
                    "status": status,
                    "package": package_name,
                    "period": period,
                    "month": month,
                    "year": year,
                    "payment_link": payment_link,
                    "amount": None,
                    "description": description,
                    "desc_parsed": {},
                }
            )

        now = datetime.now()
        this_month_invoice = next(
            (
                inv
                for inv in invoices
                if inv.get("year") == now.year and inv.get("month") == now.month
            ),
            None,
        )
        arrears_count = sum(
            1
            for inv in invoices
            if inv.get("status") == "Unpaid"
            and inv.get("year") is not None
            and inv.get("month") is not None
            and (inv["year"], inv["month"]) < (now.year, now.month)
        )

        return {
            "paket": package_current,
            "coordinate": coordinate,
            "user_join": user_join,
            "mobile": mobile,
            "invoices": invoices,
            "summary": {
                "this_month": this_month_invoice.get("status")
                if this_month_invoice
                else None,
                "arrears_count": arrears_count,
                "last_paid_month": last_paid,
            },
        }

    def get_customer_details(self, customer_id: str, detail_url: str = None) -> dict:
        url = detail_url or settings.DETAIL_URL_BILLING.format(id=customer_id)

        try:
            res = self.session.get(url, verify=False, timeout=15)
            res.raise_for_status()
        except requests.RequestException as e:
            print(f"Failed to fetch customer details: {e}")
            return None

        logger.info(f"[BillingScraper] Detail URL: {url}")
        logger.info(f"[BillingScraper] Response status: {res.status_code}, length: {len(res.text)}")

        soup = BeautifulSoup(res.text, "html.parser")

        # --- A. Basic Profile Info ---
        profile_box = soup.select_one("div.card-box.text-center")
        name = "N/A"
        address = "N/A"

        if profile_box:
            name_tag = profile_box.find("h4", class_="mb-0")
            if name_tag:
                name = name_tag.get_text(strip=True)

            addr_tag = profile_box.find("p", class_="text-muted")
            if addr_tag:
                address = addr_tag.get_text(strip=True)

        # --- B. Key-Value Profile Details ---
        # These are stored in <p> tags with <strong> labels inside div.text-left.mt-3 [cite: 67-69]
        def get_profile_value(label_text):
            # Find the strong tag containing the label
            label = soup.find("strong", string=lambda t: t and label_text in t)
            if label:
                # The value is in the next <span> sibling [cite: 67-69]
                value_span = label.find_next_sibling("span")
                if value_span:
                    return value_span.get_text(strip=True)
            return None

        user_join = get_profile_value("User Join")
        # 'No Internet' in the HTML maps to 'user_pppoe' or 'id' in your model [cite: 68]
        user_pppoe = get_profile_value("No Internet")
        mobile = get_profile_value("Mobile")
        package = get_profile_value("Paket")
        last_payment = get_profile_value("Last Payment")

        # --- C. Coordinate ---
        # Explicitly stored in <input name="coordinat"> in the settings tab
        coord_input = soup.find("input", {"name": "coordinat"})
        coordinate = coord_input.get("value", "").strip() if coord_input else None
        wa_link = BillingScraper._parser_whatsapp_url(mobile)
        maps_link = BillingScraper._parser_maps_url(coordinate)

        # --- D. Detail URL (Payment Link) ---
        # The link is hidden inside the textarea of the "BC WA" modal for the LATEST invoice
        detail_url = None
        invoices = None
        invoice_links = []  # Collect ALL payment links

        # Get ALL timeline items (invoices)
        all_invoice_items = soup.select("ul.timeline-sm li.timeline-sm-item")

        for idx, invoice_item in enumerate(all_invoice_items):
            # Find the "BC WA" button to get the target modal ID
            wa_button = invoice_item.select_one("button[data-target^='#modaleditt']")

            if wa_button:
                modal_id = wa_button.get("data-target")
                modal = soup.select_one(modal_id)

                if modal:
                    textarea = modal.find("textarea", {"name": "deskripsi_edit"})
                    if textarea:
                        ta_text = textarea.get_text()
                        # Find payment link in textarea
                        match = re.search(
                            r"(https://payment\.lexxadata\.net\.id/\?id=[\w-]+)",
                            ta_text,
                        )
                        if match:
                            link = match.group(1)
                            invoice_links.append(link)
                            # First one is the latest (detail_url)
                            if idx == 0:
                                detail_url = link
                                invoices = ta_text

            # Also check for direct input with payment link
            link_input = invoice_item.select_one(
                "input[value^='https://payment.lexxadata.net.id']"
            )
            if link_input:
                link = link_input.get("value")
                if link and link not in invoice_links:
                    invoice_links.append(link)

        tickets = self.parse_tickets(res.text)

        # --- E. PPPoE Password & Framed IP ---
        pppoe_password = get_profile_value("Pass PPPoE")

        ip_address = None
        ip_kick = soup.select_one("input[name='ip_address_kick']")
        if ip_kick:
            ip_val = ip_kick.get("value", "").strip()
            if ip_val:
                ip_address = ip_val

        # Fallback: Framed IP from Radius Acct section
        if not ip_address:
            for fg in soup.select("div.form-group"):
                h6 = fg.select_one("h6")
                if h6 and "Framed IP" in h6.get_text():
                    ip_p = fg.select_one("p")
                    if ip_p:
                        ip_address = ip_p.get_text(strip=True)
                    break

        # Return the populated model
        return Customer(
            id=customer_id,
            name=name,
            address=address,
            user_pppoe=user_pppoe,
            pppoe_password=pppoe_password,
            package=package,
            coordinate=coordinate,
            ip_address=ip_address,
            user_join=user_join,
            mobile=mobile,
            last_payment=last_payment,
            detail_url=detail_url,  # None if no payment link found
            invoices=invoices,
            wa_link=wa_link,
            maps_link=maps_link,
            tickets=tickets,
        )


class NOCScrapper:
    def __init__(self, session: Optional[requests.Session] = None):
        self.session = session or requests.Session()
        self.session.headers.update(
            {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        )
        self.reused_session = session is not None
        if not self.reused_session:
            self._login()

    def _save_cookies(self):
        with open("noc_session.pkl", "wb") as f:
            pickle.dump(self.session.cookies, f)

    def _load_cookies(self) -> bool:
        if os.path.exists("noc_session.pkl"):
            with open("noc_session.pkl", "rb") as f:
                self.session.cookies.update(pickle.load(f))
            return True
        return False

    def _is_logged_in(self) -> bool:
        try:
            r = self.session.get(
                settings.DATA_PSB_URL,
                verify=False,
                allow_redirects=False,
                timeout=10,
            )
            return r.status_code == 200 and "login" not in r.url.lower()
        except requests.RequestException:
            return False

    def _solve_captcha(self, captcha_url: str) -> Optional[str]:
        """Download and solve CAPTCHA using OCR."""
        try:
            from api.v1.endpoints.ocr import _process_image_ocr

            captcha_response = self.session.get(captcha_url, verify=False, timeout=10)
            captcha_response.raise_for_status()
            captcha_bytes = captcha_response.content

            captcha_text = _process_image_ocr(captcha_bytes)

            if captcha_text and captcha_text.strip():
                cleaned = captcha_text.strip()
                math_answer = self._evaluate_math_captcha(cleaned)
                if math_answer is not None:
                    return str(math_answer)
                return cleaned
            return None
        except Exception as e:
            print(f"[NOCScrapper] CAPTCHA solving failed: {e}")
            return None

    @staticmethod
    def _evaluate_math_captcha(text: str) -> Optional[int]:
        try:
            clean = text.replace(" ", "").replace("=", "").replace("?", "")
            math_pattern = r"^(\d+)\s*([+\-*/x×])\s*(\d+)$"
            match = re.match(math_pattern, clean, re.IGNORECASE)
            if match:
                num1 = int(match.group(1))
                op = match.group(2).lower()
                num2 = int(match.group(3))
                if op == "+":
                    return num1 + num2
                elif op == "-":
                    return num1 - num2
                elif op in ["*", "x", "×"]:
                    return num1 * num2
                elif op == "/":
                    return num1 // num2
            return None
        except Exception:
            return None

    def _login(self):
        if self._load_cookies() and self._is_logged_in():
            return

        from urllib.parse import urljoin

        captcha_url = urljoin(settings.LOGIN_URL_BILLING, "captcha.php")
        max_attempts = 3

        for attempt in range(max_attempts):
            try:
                captcha_text = self._solve_captcha(captcha_url)

                payload = {
                    "username": settings.NMS_USERNAME,
                    "password": settings.NMS_PASSWORD,
                }
                if captcha_text:
                    payload["captcha"] = captcha_text

                r = self.session.post(
                    settings.LOGIN_URL_BILLING,
                    data=payload,
                    verify=False,
                    timeout=10,
                )

                if r.status_code not in (200, 302):
                    continue
                if "login" in r.url.lower() or "pesan=" in r.url.lower():
                    if attempt < max_attempts - 1:
                        time.sleep(1)
                        continue
                    else:
                        raise ConnectionError("NOC login failed after multiple attempts")
                else:
                    self._save_cookies()
                    return
            except requests.RequestException:
                if attempt < max_attempts - 1:
                    time.sleep(1)
                    continue

        raise ConnectionError("NOC login failed after multiple attempts")

    def _search_noc(self, query: str) -> List[Dict]:
        """Search for customers via NOC search page. Returns list of detail links."""
        search_payload = {"type_cari": query, "cari_tagihan": ""}
        try:
            res = self.session.post(
                settings.SEARCH_NOC_URL,
                data=search_payload,
                verify=False,
                timeout=15,
                allow_redirects=True,
            )
            res.raise_for_status()
        except requests.RequestException as e:
            raise ConnectionError(f"NOC search request failed: {e}")

        soup = BeautifulSoup(res.text, "html.parser")

        # Check if redirected directly to a detail page
        final_url_params = parse_qs(urlparse(res.url).query)
        if "tep" in final_url_params and "dp" in final_url_params.get("tep", []):
            customer_id = final_url_params.get("id", [None])[0]
            if customer_id:
                return [{"id": customer_id, "href": res.url}]

        # Parse search results table
        detail_links = []
        for link in soup.select("a.dropdown-item[href*='tep=dp&id=']"):
            href = link.get("href", "")
            if href:
                # Extract customer ID from href
                id_match = re.search(r"id=([^\s\"&]+)", href)
                customer_id = id_match.group(1).strip() if id_match else href

                # Build full URL
                base_url = res.url.rsplit("/", 1)[0]
                full_url = f"{base_url}/{href}" if not href.startswith("http") else href

                detail_links.append({"id": customer_id, "href": full_url})

        # Fallback: try table rows if no dropdown links found
        if not detail_links:
            table = soup.find("table", id="tickets-table")
            if not table:
                for t in soup.find_all("table"):
                    if t.tbody and len(t.tbody.find_all("tr")) > 0:
                        table = t
                        break

            if table and table.tbody:
                for row in table.tbody.find_all("tr"):
                    cols = row.find_all("td")
                    if len(cols) < 7:
                        continue
                    link_tag = cols[6].find("a", href=re.compile(r"tep=dp"))
                    if not link_tag:
                        continue
                    href = link_tag.get("href", "")
                    id_match = re.search(r"id=([^\s\"&]+)", href)
                    if not id_match:
                        id_match = re.search(r"id=\s*(.+)", href, re.DOTALL)
                        if id_match:
                            customer_id = re.sub(r"\s+", "", id_match.group(1))
                        else:
                            continue
                    else:
                        customer_id = id_match.group(1).strip()

                    base_url = res.url.rsplit("/", 1)[0]
                    full_url = f"{base_url}/{href}" if not href.startswith("http") else href
                    detail_links.append({"id": customer_id, "href": full_url})
        logger.info(f"detail_links: {detail_links}")

        return detail_links

    def _scrape_noc_detail(self, detail_url: str) -> dict:
        """Scrape customer data from a NOC detail page."""
        try:
            res = self.session.get(detail_url, verify=False, timeout=15)
            res.raise_for_status()
        except requests.RequestException as e:
            print(f"[NOCScrapper] Failed to fetch detail page: {e}")
            return {}

        soup = BeautifulSoup(res.text, "html.parser")
        data = {}

        # Card box: name, address, user_pppoe, password, coordinates
        card_box = soup.select_one(".card-box.text-center")
        if card_box:
            name_tag = card_box.select_one("h4.mb-0")
            if name_tag:
                data["Name"] = name_tag.get_text(strip=True)

            addr_tag = card_box.find("p", class_="text-muted")
            if addr_tag:
                data["Address"] = addr_tag.get_text(strip=True)

            # User PPPoE
            user_strong = card_box.find("strong", string=lambda t: t and "User PPPoE" in t)
            if user_strong:
                span = user_strong.find_next_sibling("span")
                if span:
                    data["Username"] = span.get_text(strip=True)

            # Password PPPoE
            pass_strong = card_box.find("strong", string=lambda t: t and "Pass PPPoE" in t)
            if pass_strong:
                span = pass_strong.find_next_sibling("span")
                if span:
                    data["Password"] = span.get_text(strip=True)

            # Coordinates
            coord_strong = card_box.find("strong", string=lambda t: t and "Coordinat" in t)
            if coord_strong:
                coord_span = coord_strong.find_next_sibling("span")
                if coord_span:
                    coord_a = coord_span.select_one("a")
                    if coord_a:
                        coords = coord_a.get_text(strip=True)
                        data["Maps"] = f"https://www.google.com/maps?q={coords}"

        # Framed IP Address
        for fg in soup.select("div.form-group"):
            h6 = fg.select_one("h6")
            if h6 and "Framed IP Address" in h6.get_text():
                ip_p = fg.select_one("p")
                if ip_p:
                    data["Framed Ip Adress"] = ip_p.get_text(strip=True)
                break

        # Framed Pool (ip_address_kick input)
        ip_kick = soup.select_one("input[name='ip_address_kick']")
        if ip_kick:
            ip_val = ip_kick.get("value", "").strip()
            if ip_val:
                data["Framed Pool"] = ip_val

        # Last 2 radius activity
        radius_items = soup.select(".inbox-item .media")
        radius_act = []
        for item in radius_items[:2]:
            time_el = item.select_one("h5 span")
            badge_el = item.select_one("p span.badge")
            if time_el and badge_el:
                time_str = time_el.get_text(strip=True)
                badge_str = badge_el.get_text(strip=True)
                radius_act.append(f"{time_str} - {badge_str}")
        if radius_act:
            data["Last 2 row radius act"] = radius_act

        # Radius Acct - last 2 rows from table
        radius_acct_table = None
        for h5 in soup.select("h5"):
            if "Radius Acct" in h5.get_text():
                # Check if next sibling is a div.table-responsive containing a table
                next_div = h5.find_next_sibling("div", class_="table-responsive")
                if next_div:
                    radius_acct_table = next_div.select_one("table")
                    break

        radius_acct = []
        if radius_acct_table and radius_acct_table.tbody:
            rows = radius_acct_table.tbody.find_all("tr")
            for row in rows[-2:]:
                cols = row.find_all("td")
                if len(cols) >= 6:
                    radius_acct.append({
                        "username": cols[0].get_text(strip=True),
                        "framed_ip": cols[1].get_text(strip=True),
                        "start": cols[2].get_text(strip=True),
                        "session_time": cols[3].get_text(strip=True),
                        "stop": cols[4].get_text(strip=True),
                        "status": cols[5].get_text(strip=True),
                        "terminating": cols[6].get_text(strip=True) if len(cols) > 6 else "",
                    })

        return {
            "user_pppoe": data.get("Username"),
            "nama": data.get("Name"),
            "alamat": data.get("Address"),
            "pppoe_password": data.get("Password"),
            "ip_address": data.get("Framed Pool") or data.get("Framed Ip Adress"),
            "coordinates": data.get("Maps"),
            "radius_acct": radius_acct,
        }
        

    def get_customer_data_noc(self, query: str) -> List[Dict]:
        """Search and scrape all matching NOC customers."""
        self._login()

        detail_links = self._search_noc(query)
        if not detail_links:
            return []

        results = []
        for link in detail_links:
            customer = self._scrape_noc_detail(link["href"])
            if customer and customer.get("nama"):
                results.append(customer)

        return results

    def _get_data_psb(self) -> List[Dict]:
        url_psb = settings.DATA_PSB_URL
        res = None

        for attempt in range(2):
            try:
                res = self.session.get(url_psb, verify=False, timeout=15)
                res.raise_for_status()
                break
            except requests.RequestException as e:
                if attempt == 0:
                    self._login()
                else:
                    return []

        if not res:
            return []

        soup = BeautifulSoup(res.text, "html.parser")
        table_rows = soup.select("#tickets-note tbody tr")

        if not table_rows:
            return []

        data_psb = []
        for row in table_rows:
            cols = [c.get_text(strip=True) for c in row.select("td")]

            if len(cols) < 5:
                continue

            details_link = row.select_one("a[data-target]")
            framed_pool = None
            if details_link:
                modal_id = details_link.get("data-target", "").strip("#")
                if modal_id:
                    modal = soup.select_one(f"div.modal#{modal_id}")
                    if modal:
                        for p in modal.select("p.mb-0"):
                            text = p.get_text(strip=True)
                            if "framed-pool" in text.lower():
                                match = re.search(r"(\d+M)", text)
                                if match:
                                    framed_pool = match.group(1)
                                break

            data_psb.append(
                {
                    "name": cols[0],
                    "address": cols[1],
                    "user_pppoe": cols[3],
                    "pppoe_password": cols[4],
                    "paket": framed_pool,
                }
            )

        return data_psb
