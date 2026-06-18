# backend-fastapi/services/open_ticket.py

import os
import sys
import time
import argparse
import logging
from typing import Optional

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
from webdriver_manager.core.os_manager import ChromeType
from selenium.webdriver.chrome.service import Service 
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import TimeoutException, NoSuchElementException

from core import settings

log = logging.getLogger("lexxa.selenium")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

DEFAULT_BASE = settings.BILLING_MODULE_BASE
LOGIN_URL    = settings.LOGIN_URL_BILLING
TICKET_NOC_URL= settings.TICKET_NOC_URL
cs_username   = settings.NMS_USERNAME_BILING
cs_password   = settings.NMS_PASSWORD_BILING


def build_driver(headless: bool) -> webdriver.Chrome:
    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1400,1000")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    # be a bit less “botty”
    opts.add_argument("--disable-blink-features=AutomationControlled")

    # Use Chromium driver for Docker/Linux, Chrome for Windows
    import platform
    if platform.system() == "Linux":
        # For Debian/Docker with Chromium installed
        service = Service(ChromeDriverManager(chrome_type=ChromeType.CHROMIUM).install())
    else:
        # For Windows/Mac with Chrome
        service = Service(ChromeDriverManager().install())
    
    driver = webdriver.Chrome(service=service, options=opts)
    driver.set_page_load_timeout(60)

    try:
        # reduce webdriver fingerprint
        driver.execute_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
    except Exception as e:
        log.error(f"Failed to initialize ChromeDriver: {e}")
        raise e

    return driver

def _debug_dump(driver, label="debug"):
    try:
        p = f"selenium_{label}.png"
        driver.save_screenshot(p)
        log.info("Saved screenshot: %s (url=%s title=%s)", p, driver.current_url, driver.title)
    except Exception:
        pass

def wait(driver, timeout=25):
    return WebDriverWait(driver, timeout)

def _set_value_js(driver, el, value: str):
    driver.execute_script("""
        const el = arguments[0], val = arguments[1];
        el.focus();
        el.value = '';
        el.dispatchEvent(new Event('input', {bubbles:true}));
        el.value = val;
        el.dispatchEvent(new Event('input', {bubbles:true}));
        el.dispatchEvent(new Event('change', {bubbles:true}));
    """, el, value)

def maybe_login(driver, base_url: str, username: str, password: str):
    log.info("Opening %s", base_url)
    driver.get(base_url)

    try:
        # Prefer the login form itself so we don’t pick hidden fields
        form = wait(driver, 20).until(
            EC.visibility_of_element_located((By.CSS_SELECTOR, "form[action*='cek_login_baru']"))
        )
        user_box = form.find_element(By.CSS_SELECTOR, "input[name='username']")
        pass_box = form.find_element(By.CSS_SELECTOR, "input[name='password']")

        # Ensure they’re interactable
        wait(driver, 10).until(EC.element_to_be_clickable((By.CSS_SELECTOR, "input[name='username']")))
        wait(driver, 10).until(EC.element_to_be_clickable((By.CSS_SELECTOR, "input[name='password']")))

        # Try standard typing first
        try:
            user_box.clear(); user_box.click(); user_box.send_keys(username)
            pass_box.clear(); pass_box.click(); pass_box.send_keys(password)
        except Exception:
            # Fallback: JS set + events (some pages block keystrokes until events fire)
            _set_value_js(driver, user_box, username)
            _set_value_js(driver, pass_box, password)

        # Submit: prefer the button inside the same form; else press Enter
        submit = None
        try:
            submit = form.find_element(By.CSS_SELECTOR, "button[type='submit'], input[type='submit']")
        except Exception:
            pass

        if submit:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", submit)
            submit.click()
        else:
            pass_box.send_keys(Keys.ENTER)

        # After login, wait for the dashboard search box (type_cari)
        wait(driver, 25).until(EC.presence_of_element_located((By.NAME, "type_cari")))
        log.info("Login successful; dashboard visible.")
    except Exception as e:
        # Dump page for debugging
        import datetime, pathlib
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        outdir = pathlib.Path("selenium_artifacts"); outdir.mkdir(exist_ok=True)
        png = outdir / f"login-fail-{ts}.png"
        html = outdir / f"login-fail-{ts}.html"
        try:
            driver.save_screenshot(str(png))
            with open(html, "w", encoding="utf-8") as f:
                f.write(driver.page_source)
            log.error("Saved artifacts: %s and %s", png, html)
        except Exception:
            pass
        log.exception("Could not find login nor dashboard. Check base URL or credentials.")
        raise


def maybe_login_noc(driver, login_url: str, username: str, password: str):
    """ 
    Logs into the NOC portal. 
    Robust version: Checks for success AND failure (alerts/text) in a loop.
    """
    log.info(f"[NOC] Opening login page: {login_url}")
    driver.get(login_url)
    time.sleep(2)

    # 1. Check if already logged in
    if "logout" in driver.page_source.lower():
        log.info("[NOC] Already logged in — skipping login.")
        return

    try:
        # 2. Fill Username/Password
        wait(driver, 10).until(EC.presence_of_element_located((By.NAME, "username")))
        
        user_field = driver.find_element(By.NAME, "username")
        pass_field = driver.find_element(By.NAME, "password")

        user_field.clear(); user_field.send_keys(username)
        pass_field.clear(); pass_field.send_keys(password)

        # 3. Submit
        login_btn = driver.find_element(By.CSS_SELECTOR, "button[type='submit'], input[type='submit']")
        driver.execute_script("arguments[0].click();", login_btn)
        log.info("[NOC] Login submitted. Checking result...")

        # 4. Wait for Success OR Failure (Polling Loop)
        # We check the status every 1 second for up to 15 seconds
        for i in range(15):
            time.sleep(1)
            current_url = driver.current_url
            # Grab body text safely (handle StaleElementReferenceException automatically by refetching)
            try:
                body_text = driver.find_element(By.TAG_NAME, "body").text.lower()
            except Exception:
                continue

            # --- SUCCESS CONDITIONS ---
            if "dashboard" in current_url or "ticket" in current_url:
                log.info("[NOC] Login successful (URL detected).")
                return
            if len(driver.find_elements(By.CSS_SELECTOR, "table, div.modal")) > 0:
                log.info("[NOC] Login successful (Table detected).")
                return

            # --- FAILURE CONDITIONS ---
            # Add any specific Indonesian or English error terms your app uses
            if "wrong username" in body_text or "invalid" in body_text or "gagal" in body_text:
                log.error(f"!!! LOGIN FAILED: Server said '{body_text[:100]}...'")
                raise Exception(f"Login credentials rejected: {body_text[:50]}...")
                
            # --- ALERT POPUP CHECK ---
            try:
                alert = driver.switch_to.alert
                alert_text = alert.text
                alert.accept()
                log.error(f"!!! LOGIN BLOCKED BY ALERT: {alert_text}")
                raise Exception(f"Login blocked by alert: {alert_text}")
            except Exception as e:
                # If the exception is the one we just raised, re-raise it
                if "blocked by alert" in str(e): raise e
                # Otherwise, it just means no alert is present, which is good
                pass

        # 5. Timeout Fallback
        log.error("[NOC] Login timed out. Dumping page state.")
        log.error(f"Final URL: {driver.current_url}")
        raise TimeoutException("Login transition never completed (Dashboard not found).")

    except Exception as e:
        log.error(f"[NOC] Login Process Failed: {e}")
        raise
    
def search_user(driver, query: str):
    """
    Use 'Cari User' form: input[name='type_cari'] + button[name='cari_tagihan'].
    Wait for the results table '#create_note'.
    """
    log.info("Searching for: %s", query)
    box = wait(driver, 20).until(EC.element_to_be_clickable((By.NAME, "type_cari")))
    box.clear()
    box.send_keys(query)

    btn = driver.find_element(By.NAME, "cari_tagihan")
    btn.click()

    # Results table appears/reloads
    wait(driver, 20).until(EC.presence_of_element_located((By.CSS_SELECTOR, "table#create_note tbody tr")))

def find_result_row(driver, query: str):
    """
    Find the row in #create_note whose text contains the query (Name or PPPoE).
    Returns the <tr> WebElement.
    """
    rows = driver.find_elements(By.CSS_SELECTOR, "table#create_note tbody tr")
    for tr in rows:
        if query.strip().lower() in tr.text.lower():
            return tr
    # Fallback: if only one row exists, use it
    if rows:
        return rows[0]
    return None

def extract_search_results(driver, max_rows: int = 20) -> list[dict]:
    """Return rows from #create_note as list of dicts (best-effort)."""
    # wait until table rows exist
    wait(driver, 30).until(
        EC.presence_of_all_elements_located((By.CSS_SELECTOR, "table#create_note tbody tr"))
    )

    headers = [th.text.strip() for th in driver.find_elements(By.CSS_SELECTOR, "table#create_note thead th")]
    rows    = driver.find_elements(By.CSS_SELECTOR, "table#create_note tbody tr")

    results: list[dict] = []
    for tr in rows[:max_rows]:
        tds = tr.find_elements(By.TAG_NAME, "td")
        # map by header if sizes match; else fallback to col1/col2...
        if headers and len(headers) == len(tds):
            data = {headers[i] or f"col{i+1}": tds[i].text.strip() for i in range(len(tds))}
        else:
            data = {f"col{i+1}": td.text.strip() for i, td in enumerate(tds)}
        results.append(data)

    return results

def print_results(results: list[dict]) -> None:
    """Pretty-print rows to console."""
    if not results:
        print("No rows.")
        return
    # choose a few common fields if present
    preferred = ["ID", "Nama", "Name", "User PPPoE", "PPPoE", "Alamat", "Address"]
    for i, row in enumerate(results, 1):
        pieces = []
        for key in preferred:
            if key in row and row[key]:
                pieces.append(f"{key}: {row[key]}")
        # fallback: show first 3 cols if nothing matched
        if not pieces:
            first_keys = list(row.keys())[:3]
            pieces = [f"{k}: {row[k]}" for k in first_keys]
        print(f"{i}. " + " | ".join(pieces))

def open_ticket_gangguan_modal(driver, result_row) -> Optional[str]:
    # open dropdown (button with dots)
    menu_btn = result_row.find_element(By.CSS_SELECTOR, "a.table-action-btn.dropdown-toggle")
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", menu_btn)
    menu_btn.click()

    # click 'Ticket Gangguan' item
    item = wait(driver, 10).until(
        EC.visibility_of_element_located(
            (By.XPATH, ".//div[contains(@class,'dropdown-menu')]/a[contains(.,'Ticket Gangguan')]")
        )
    )
    data_target = item.get_attribute("data-target")  # like '#create_tiga_modal1494'
    if not data_target:
        log.error("Ticket Gangguan item has no data-target.")
        return None
    modal_id = data_target.lstrip("#")
    item.click()

    # wait modal visible
    wait(driver, 15).until(EC.visibility_of_element_located((By.ID, modal_id)))
    return modal_id

def fill_and_submit_gangguan(driver, modal_id: str, priority: str, jenis_ticket: str, description: str):

    modal = wait(driver, 15).until(EC.visibility_of_element_located((By.ID, modal_id)))

    # Priority
    sel_priority = Select(modal.find_element(By.NAME, "priority"))
    sel_priority.select_by_value(priority.upper())

    # Type (jenis_ticket)
    sel_type = Select(modal.find_element(By.NAME, "jenis_ticket"))
    sel_type.select_by_value(jenis_ticket.upper())

    # Description
    ta = modal.find_element(By.NAME, "deskripsi")
    ta.clear(); ta.send_keys(description)

    # Save
    save_btn = modal.find_element(By.NAME, "create_ticket_gangguan")
    save_btn.click()

    # Wait for modal to close or become hidden
    wait(driver, 20).until(EC.invisibility_of_element_located((By.ID, modal_id)))
    log.info("Ticket Gangguan submitted.")

def logout(driver) -> bool:

    try:
        log.info("[Auth] Starting logout procedure...")
        
        # Clicks the user profile dropdown
        profile_dropdown_locator = (By.CSS_SELECTOR, "a.nav-user")
        WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable(profile_dropdown_locator)
        ).click()
        log.info("[Auth] Clicked user profile dropdown.")

        # Clicks the logout link
        logout_link_locator = (By.CSS_SELECTOR, "a[href*='log_out.php']")
        WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable(logout_link_locator)
        ).click()
        log.info("[Auth] Clicked logout link. Logout successful.")
        
        return True

    except TimeoutException:
        log.error("[Auth] Logout failed. Could not find logout elements.")
        driver.save_screenshot("logout_failure.png")
        return False
    except TimeoutException:
        log.error("[Auth] Logout failed. Could not find logout elements.")
        driver.save_screenshot("logout_failure.png")
        return False

def create_ticket_as_cs(
    cs_username: str,
    cs_password: str,
    query: str,
    description: str,
    priority: str = "LOW",
    jenis: str = "FREE",
    headless: bool = True
) -> str:

    driver = build_driver(headless)
    try:
        log.info(f"[CS] Starting ticket creation for '{query}' with user '{cs_username}'.")
        maybe_login(driver, settings.BILLING_MODULE_BASE, cs_username, cs_password)
        search_user(driver, query)
        
        row = find_result_row(driver, query)
        if not row:
            return f"Failed: [CS] No customer found for query '{query}'."

        modal_id = open_ticket_gangguan_modal(driver, row)
        if not modal_id:
            return "Failed: [CS] Could not open the ticket creation modal."

        fill_and_submit_gangguan(driver, modal_id, priority, jenis, description)
        log.info(f"[CS] Ticket for '{query}' submitted successfully.")

        logout(driver)
        
        return f"OK: [CS] Ticket for '{query}' was created."

    except Exception as e:
        log.error(f"[CS] An error occurred during ticket creation: {e}")
        driver.save_screenshot(f"cs_creation_error_{query}.png")
        return f"Failed: [CS] An error occurred: {type(e).__name__}"
    finally:
        # driver.quit() will now be called after the logout is attempted.
        try:
            driver.quit()
        except Exception:
            pass

def process_ticket_as_noc(noc_username: str, noc_password: str, query: str, headless: bool = True) -> str:
    driver = build_driver(headless)
    try:
        # Use settings variable for consistency
        maybe_login_noc(driver, settings.LOGIN_URL, noc_username, noc_password)
        log.info("[NOC] Logged in successfully. Navigating to ticket page...")
        driver.get(settings.TICKET_NOC_URL)
        
        log.info("[NOC] Waiting for ticket table to load...")
        try:
            WebDriverWait(driver, 30).until(
                EC.presence_of_all_elements_located((By.CSS_SELECTOR, "#tickets-note tbody tr"))
            )
            # --- Screenshot 1: Table is loaded ---
            driver.save_screenshot(f"log_{query}_01_table_loaded.png")
            log.info(f"Saved screenshot: log_{query}_01_table_loaded.png")

        except TimeoutException:
            log.error("[NOC] Ticket table did not load within 30 seconds.")
            driver.save_screenshot(f"log_{query}_00_table_load_failed.png")
            return f"Failed: [NOC] No ticket table loaded for '{query}'."

        time.sleep(2)

        # --- Step 2: Find the specific ticket row ---
        ticket_row = None
        query_upper = query.upper().strip()
        
        rows = driver.find_elements(By.CSS_SELECTOR, "#tickets-note tbody tr")
        log.info(f"[NOC] Found {len(rows)} rows, scanning for ticket containing '{query_upper}'...")

        for row in rows:
            row_text = row.text.upper().strip()
            # Check if text contains Query AND is in valid status
            if query_upper in row_text and ("FORWARD TO NOC" in row_text or "OPEN" in row_text):
                ticket_row = row
                log.info(f"[NOC] Found matching ticket row for '{query_upper}'.")
                break # Stop searching once found
            
        # --- FIX: This check must be OUTSIDE the for loop ---
        if not ticket_row:
            log.error(f"[NOC] Ticket '{query_upper}' with an actionable status was not found.")
            driver.save_screenshot(f"log_{query}_02_ticket_not_found.png")
            return f"Failed: [NOC] Could not find actionable ticket for '{query_upper}'."

        # --- Screenshot 2: Row found and scrolled into view ---
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", ticket_row)
        time.sleep(1) # Allow scroll to finish
        driver.save_screenshot(f"log_{query}_02_row_found.png")
        log.info(f"Saved screenshot: log_{query}_02_row_found.png")

        # --- Step 3: Open dropdown and click 'Details' ---
        try:
            dropdown_toggle = ticket_row.find_element(By.CSS_SELECTOR, "a.table-action-btn")
            driver.execute_script("arguments[0].click();", dropdown_toggle)
            log.info("[NOC] Clicked the action dropdown toggle.")

            # --- Screenshot 3: Dropdown is open ---
            time.sleep(1) 
            driver.save_screenshot(f"log_{query}_03_dropdown_opened.png")

            details_link_locator = (By.CSS_SELECTOR, "a[data-target*='create_ticket_modal']")
            details_link = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable(details_link_locator)
            )

            modal_id = details_link.get_attribute("data-target").lstrip("#")
            details_link.click()
            log.info(f"[NOC] Clicked 'Details'. Opening modal ID: {modal_id}")

        except (NoSuchElementException, TimeoutException) as e:
            log.error(f"[NOC] Could not find or click the 'Details' link: {e}")
            driver.save_screenshot(f"log_{query}_03_details_link_error.png")
            return "Failed: [NOC] Could not open the details modal."

        # --- Step 4: Interact with the modal ---
        try:
            modal = WebDriverWait(driver, 15).until(EC.visibility_of_element_located((By.ID, modal_id)))
            
            # --- Screenshot 4: Modal is visible ---
            driver.save_screenshot(f"log_{query}_04_modal_visible.png")
            
            action_field = modal.find_element(By.NAME, "action_ticket")
            action_field.clear()
            action_field.send_keys("cek")

            # --- Screenshot 5: Text entered in modal ---
            driver.save_screenshot(f"log_{query}_05_modal_filled.png")

            save_button = modal.find_element(By.NAME, "proses_ticket")
            driver.execute_script("arguments[0].click();", save_button)
            log.info("[NOC] Clicked the save button.")

            WebDriverWait(driver, 20).until(EC.invisibility_of_element_located((By.ID, modal_id)))
            log.info(f"[NOC] Modal '{modal_id}' closed. Ticket processed successfully.")
            
            # --- Screenshot 6: Modal has closed ---
            time.sleep(1)
            driver.save_screenshot(f"log_{query}_06_modal_closed.png")

        except (NoSuchElementException, TimeoutException) as e:
            log.error(f"[NOC] Failed to interact with the modal elements: {e}")
            driver.save_screenshot(f"log_{query}_04_modal_error.png")
            return "Failed: [NOC] A timeout occurred while processing the modal."
            
        return f"OK: [NOC] Ticket '{query}' processed successfully."

    except Exception as e:
        log.error(f"[NOC] An unexpected error occurred: {e}", exc_info=True)
        driver.save_screenshot(f"log_{query}_99_unexpected_error.png")
        return f"Failed: [NOC] {type(e).__name__}: {e}"

    finally:
        log.info("[NOC] Automation finished. Closing browser.")
        if 'driver' in locals() and driver:
            driver.quit()
            
def close_ticket_as_noc(
    noc_username: str,
    noc_password: str,
    query: str,
    onu_sn: str,
    action_close_notes: str,
    headless: bool = True
) -> str:

    driver = build_driver(headless)
    try:
        maybe_login_noc(driver, settings.TICKET_NOC_URL, noc_username, noc_password)
        driver.get(settings.TICKET_NOC_URL)
        log.info(f"[NOC-CLOSE] Navigated to ticket page to find '{query}'.")

        # Step 2: Find the specific ticket row
        WebDriverWait(driver, 30).until(
            EC.presence_of_all_elements_located((By.CSS_SELECTOR, "#tickets-note tbody tr"))
        )
        time.sleep(2) # Allow JS to render

        ticket_row = None
        query_upper = query.upper().strip()
        rows = driver.find_elements(By.CSS_SELECTOR, "#tickets-note tbody tr")
        log.info(f"[NOC-CLOSE] Found {len(rows)} rows, scanning for ticket '{query_upper}'...")

        for row in rows:
            row_text = row.text.upper().strip()
            # Look for a ticket that has been processed and is ready to be closed
            if query_upper in row_text and "PROCESSED BY NOC" in row_text:
                ticket_row = row
                log.info(f"[NOC-CLOSE] Found matching ticket row for '{query_upper}'.")
                break
        
        if not ticket_row:
            log.error(f"[NOC-CLOSE] Ticket '{query}' with status 'PROCESSED BY NOC' not found.")
            driver.save_screenshot(f"noc_close_ticket_not_found_{query}.png")
            return f"Failed: [NOC-CLOSE] Could not find a processable ticket for '{query}'."

        # Step 3: Open the 'Close Ticket' modal
        try:
            dropdown_toggle = ticket_row.find_element(By.CSS_SELECTOR, "a.table-action-btn")
            driver.execute_script("arguments[0].click();", dropdown_toggle)

            # [cite_start]Wait for and click the "Close Ticket" link [cite: 202]
            close_link_locator = (By.PARTIAL_LINK_TEXT, "Close Ticket")
            close_link = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable(close_link_locator)
            )
            
            modal_id = close_link.get_attribute("data-target").lstrip("#")
            close_link.click()
            log.info(f"[NOC-CLOSE] Clicked 'Close Ticket'. Opening modal ID: {modal_id}")

        except (NoSuchElementException, TimeoutException):
            log.error("[NOC-CLOSE] Could not open the 'Close Ticket' modal.")
            driver.save_screenshot(f"noc_close_modal_open_error_{query}.png")
            return "Failed: [NOC-CLOSE] Could not click the 'Close Ticket' link."

        # Step 4: Fill the modal and submit
        try:
            modal = WebDriverWait(driver, 15).until(EC.visibility_of_element_located((By.ID, modal_id)))
            
            # Fill ONU Index with '-'
            onu_index_field = modal.find_element(By.NAME, "onu_index")
            onu_index_field.clear()
            onu_index_field.send_keys("-")
            log.info("[NOC-CLOSE] Filled ONU Index with '-'.")

            # Fill ONU SN from the function argument
            onu_sn_field = modal.find_element(By.NAME, "sn_modem")
            onu_sn_field.clear()
            onu_sn_field.send_keys(onu_sn)
            log.info(f"[NOC-CLOSE] Filled ONU SN with '{onu_sn}'.")

            # Fill Action Close notes from the function argument
            action_close_field = modal.find_element(By.NAME, "update_ticket")
            action_close_field.clear()
            action_close_field.send_keys(action_close_notes)
            log.info("[NOC-CLOSE] Filled Action Close notes.")
            
            # Click the final 'Close Ticket' button
            submit_button = modal.find_element(By.NAME, "closed_ticket")
            driver.execute_script("arguments[0].click();", submit_button)
            log.info("[NOC-CLOSE] Clicked the final 'Close Ticket' submit button.")

            # Wait for the modal to close to confirm success
            WebDriverWait(driver, 20).until(EC.invisibility_of_element_located((By.ID, modal_id)))
            log.info(f"[NOC-CLOSE] Modal '{modal_id}' closed. Ticket closed successfully.")

        except (NoSuchElementException, TimeoutException) as e:
            log.error(f"[NOC-CLOSE] Failed to fill or submit the modal: {e}")
            driver.save_screenshot(f"noc_close_modal_fill_error_{query}.png")
            return "Failed: [NOC-CLOSE] A timeout occurred while filling the close ticket modal."

        return f"OK: [NOC-CLOSE] Ticket '{query}' was closed successfully."

    except Exception as e:
        log.error(f"[NOC-CLOSE] An unexpected error occurred: {e}", exc_info=True)
        driver.save_screenshot(f"noc_close_unexpected_error_{query}.png")
        return f"Failed: [NOC-CLOSE] {type(e).__name__}: {e}"

    finally:
        log.info("[NOC-CLOSE] Automation finished. Closing browser.")
        if 'driver' in locals() and driver:
            driver.quit()

def forward_ticket_as_noc(
    noc_username: str,
    noc_password: str,
    ticket_page_url: str,
    query: str,
    service_impact: str,
    root_cause: str,
    network_impact: str,
    onu_index: str,
    sn_modem: str,
    priority: str,
    person_in_charge: str,
    recomended_action: str,
    headless: bool = True,
) -> str:
    """
    Logs in as NOC, finds a ticket, and forwards it using the 'Forward Ticket' modal.
    """
    driver = build_driver(headless)
    try:
        maybe_login(driver, settings.BILLING_MODULE_BASE, noc_username, noc_password)
        driver.get(ticket_page_url)

        time.sleep(3)  # Wait for the table to load

        log.info(f"[NOC] Searching for ticket '{query}' to forward.")
        wait(driver, 20).until(EC.presence_of_element_located((By.ID, "tickets-note")))

        ticket_row = None
        rows = driver.find_elements(By.CSS_SELECTOR, "#tickets-note tbody tr")
        if not rows:
            raise NoSuchElementException("Ticket table is empty.")

        for row in rows:
            if query in row.text:
                ticket_row = row
                break

        if not ticket_row:
            raise NoSuchElementException(f"Could not find a ticket row for query '{query}'")

        log.info(f"[NOC] Found ticket for '{query}'. Opening forward modal.")

        # Step 1: Click the dropdown menu
        dropdown_toggle = ticket_row.find_element(By.CSS_SELECTOR, "a.table-action-btn")
        dropdown_toggle.click()

        # Step 2: Click the 'Forward Ticket' link in the dropdown
        forward_link = wait(driver, 10).until(
            EC.element_to_be_clickable((By.XPATH, "//div[contains(@class, 'dropdown-menu') and contains(@class, 'show')]//a[contains(., 'Forward Ticket')]"))
        )
        modal_id = forward_link.get_attribute("data-target").lstrip("#")
        forward_link.click()

        log.info(f"[NOC] Forwarding ticket '{query}' in modal '{modal_id}'")
        modal = wait(driver, 15).until(EC.visibility_of_element_located((By.ID, modal_id)))

        # Step 3: Fill out the form in the 'Forward Ticket' modal
        modal.find_element(By.NAME, "service_impact").send_keys(service_impact)
        modal.find_element(By.NAME, "root_cause").send_keys(root_cause)
        modal.find_element(By.NAME, "network_impact").send_keys(network_impact)
        modal.find_element(By.NAME, "onu_index").send_keys(onu_index)
        modal.find_element(By.NAME, "sn_modem").send_keys(sn_modem)
        
        Select(modal.find_element(By.NAME, "priority")).select_by_value(priority.upper())
        Select(modal.find_element(By.NAME, "person_in_charge")).select_by_value(person_in_charge.upper())
        
        modal.find_element(By.NAME, "recomended_action").send_keys(recomended_action)

        # Step 4: Click the submit button to forward the ticket
        forward_button = modal.find_element(By.NAME, "forward_ticket")
        forward_button.click()

        wait(driver, 20).until(EC.invisibility_of_element_located((By.ID, modal_id)))

        return f"OK: Ticket '{query}' forwarded by NOC."

    except (TimeoutException, NoSuchElementException) as e:
        log.error(f"[NOC] A Selenium error occurred while forwarding ticket: {e}")
        driver.save_screenshot(f"noc_forward_error_{query}.png")
        return f"Failed: [NOC] Could not forward ticket for '{query}'. Error: {type(e).__name__}"

    except Exception as e:
        log.error(f"[NOC] An unexpected error occurred while forwarding ticket: {e}")
        driver.save_screenshot(f"noc_forward_error_unexpected_{query}.png")
        return f"Failed: [NOC] An unexpected error occurred. Error: {type(e).__name__}"

    finally:
        try:
            driver.quit()
        except Exception:
            pass

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.getenv("LEXXA_BASE_URL", DEFAULT_BASE),
                    help="Landing URL (dashboard or any page that redirects to login).")
    ap.add_argument("--user", default=os.getenv("LEXXA_USERNAME"), required=True)
    ap.add_argument("--password", default=os.getenv("LEXXA_PASSWORD"), required=True)
    ap.add_argument("--query", required=True, help="Name or PPPoE to search (matches table text).")
    ap.add_argument("--priority", default="LOW", choices=["HIGH", "MEDIUM", "LOW"])
    ap.add_argument("--jenis", default="FREE", choices=["CHARGED", "FREE"])
    ap.add_argument("--desc", required=True, help="Ticket description (deskripsi).")
    ap.add_argument("--headless", action="store_true")
    
    args = ap.parse_args()

    driver = build_driver(args.headless)
    try:
        maybe_login(driver, args.base, args.user, args.password)
        time.sleep(2)
        search_user(driver, args.query)
        row = find_result_row(driver, args.query)
        if not row:
            log.error("No rows matched query: %s", args.query)
            sys.exit(2)

        modal_id = open_ticket_gangguan_modal(driver, row)
        if not modal_id:
            log.error("Could not determine modal id for Ticket Gangguan.")
            sys.exit(3)

        fill_and_submit_gangguan(driver, modal_id, args.priority, args.jenis, args.desc)
        log.info("Done.")
    finally:
        try:
            time.sleep(1)
            driver.quit()
        except Exception:
            pass

if __name__ == "__main__":
    main()