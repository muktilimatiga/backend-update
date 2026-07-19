#!/usr/bin/env python3
"""
Test script for billing_scraper.py APIs with query 'nasrul'
Each API is tested with a timeout to avoid hanging.
"""
import json
import sys
import signal
import traceback
import io
from pprint import pprint
from services.biling_scaper import BillingScraper, NOCScrapper

# Force unbuffered output
sys.stdout = io.TextIOWrapper(open(sys.stdout.fileno(), 'wb', 0), write_through=True)
sys.stderr = io.TextIOWrapper(open(sys.stderr.fileno(), 'wb', 0), write_through=True)


class TimeoutError(Exception):
    pass


def timeout_handler(signum, frame):
    raise TimeoutError("Operation timed out")


def with_timeout(func, timeout_sec=90):
    """Run func with a timeout."""
    old_handler = signal.signal(signal.SIGALRM, timeout_handler)
    signal.alarm(timeout_sec)
    try:
        result = func()
    except TimeoutError:
        result = "TIMEOUT"
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)
    return result


def test_billing_search(scraper, query="nasrul"):
    """Test BillingScraper.search()"""
    print("\n" + "=" * 60)
    print("TEST 1: BillingScraper.search()")
    print("=" * 60, flush=True)
    try:
        results = scraper.search(query)
        print(f"✅ Search returned {len(results)} result(s)", flush=True)
        for i, r in enumerate(results):
            print(f"\n--- Result {i + 1} ---", flush=True)
            pprint(r)
        return results
    except Exception as e:
        print(f"❌ Search failed: {e}", flush=True)
        traceback.print_exc()
        return []


def test_billing_customer_details(scraper, customer_id):
    """Test BillingScraper.get_customer_details()"""
    print("\n" + "=" * 60)
    print("TEST 2: BillingScraper.get_customer_details()")
    print("=" * 60, flush=True)
    try:
        result = scraper.get_customer_details(customer_id)
        if result:
            print(f"✅ Customer details retrieved", flush=True)
            print(f"   Name: {result.name}", flush=True)
            print(f"   Address: {result.address}", flush=True)
            print(f"   User PPPoE: {result.user_pppoe}", flush=True)
            print(f"   Package: {result.package}", flush=True)
            print(f"   Mobile: {result.mobile}", flush=True)
            print(f"   Coordinate: {result.coordinate}", flush=True)
            print(f"   IP Address: {result.ip_address}", flush=True)
            print(f"   WA Link: {result.wa_link}", flush=True)
            print(f"   Maps Link: {result.maps_link}", flush=True)
            print(f"   Detail URL: {result.detail_url}", flush=True)
            print(f"   Tickets: {len(result.tickets) if result.tickets else 0}", flush=True)
        else:
            print("⚠️ Customer details returned None", flush=True)
        return result
    except Exception as e:
        print(f"❌ Customer details failed: {e}", flush=True)
        traceback.print_exc()
        return None


def test_billing_invoice_data(scraper, url):
    """Test BillingScraper.get_invoice_data()"""
    print("\n" + "=" * 60)
    print("TEST 3: BillingScraper.get_invoice_data()")
    print("=" * 60, flush=True)
    try:
        result = scraper.get_invoice_data(url)
        if result:
            print(f"✅ Invoice data retrieved", flush=True)
            print(f"   Paket: {result.get('paket')}", flush=True)
            print(f"   Coordinate: {result.get('coordinate')}", flush=True)
            print(f"   User Join: {result.get('user_join')}", flush=True)
            print(f"   Mobile: {result.get('mobile')}", flush=True)
            invoices = result.get("invoices", [])
            print(f"   Invoices count: {len(invoices)}", flush=True)
            summary = result.get("summary", {})
            print(f"   Summary: {json.dumps(summary, indent=2)}", flush=True)
            if invoices:
                print(f"\n   First invoice:", flush=True)
                pprint(invoices[0])
        else:
            print("⚠️ Invoice data returned empty", flush=True)
        return result
    except Exception as e:
        print(f"❌ Invoice data failed: {e}", flush=True)
        traceback.print_exc()
        return None


def test_noc_search(query="nasrul"):
    """Test NOCScrapper.get_customer_data_noc()"""
    print("\n" + "=" * 60)
    print("TEST 4: NOCScrapper.get_customer_data_noc()")
    print("=" * 60, flush=True)
    try:
        noc = NOCScrapper()
        results = noc.get_customer_data_noc(query)
        print(f"✅ NOC search returned {len(results)} result(s)", flush=True)
        for i, r in enumerate(results):
            print(f"\n--- NOC Result {i + 1} ---", flush=True)
            pprint(r)
        return results
    except Exception as e:
        print(f"❌ NOC search failed: {e}", flush=True)
        traceback.print_exc()
        return []


def main():
    query = "nasrul"
    print(f"🔍 Testing billing_scraper.py APIs with query: '{query}'", flush=True)
    print("=" * 60, flush=True)

    # Test 1: BillingScraper Search
    print("\n--- Starting BillingScraper (login + search) ---", flush=True)
    try:
        def init_and_search():
            from services.biling_scaper import BillingScraper
            print("   Initializing BillingScraper (CAPTCHA login)...", flush=True)
            scraper = BillingScraper()
            print("   ✅ BillingScraper initialized", flush=True)
            results = scraper.search(query)
            return scraper, results

        result = with_timeout(init_and_search, timeout_sec=150)
        if result == "TIMEOUT":
            print("❌ BillingScraper init+search TIMED OUT after 150s", flush=True)
            return
        scraper, search_results = result
    except Exception as e:
        print(f"❌ Failed to initialize BillingScraper: {e}", flush=True)
        traceback.print_exc()
        return

    print(f"\n✅ Search returned {len(search_results)} result(s)", flush=True)
    for i, r in enumerate(search_results):
        print(f"\n--- Result {i + 1} ---", flush=True)
        pprint(r)

    # Test 2: Customer Details
    customer_detail = None
    if search_results:
        customer_id = search_results[0].get("id")
        if customer_id:
            print(f"\n--- Testing get_customer_details for ID: {customer_id} ---", flush=True)
            result = with_timeout(
                lambda: test_billing_customer_details(scraper, customer_id),
                timeout_sec=60
            )
            if result == "TIMEOUT":
                print("❌ get_customer_details TIMED OUT", flush=True)
            else:
                customer_detail = result

    # Test 3: Invoice Data
    if customer_detail and customer_detail.detail_url:
        url = str(customer_detail.detail_url)
        print(f"\n--- Testing get_invoice_data ---", flush=True)
        result = with_timeout(
            lambda: test_billing_invoice_data(scraper, url),
            timeout_sec=60
        )
        if result == "TIMEOUT":
            print("❌ get_invoice_data TIMED OUT", flush=True)

    # Test 4: NOC Search
    print(f"\n--- Testing NOCScrapper ---", flush=True)
    result = with_timeout(
        lambda: test_noc_search(query),
        timeout_sec=150
    )
    if result == "TIMEOUT":
        print("❌ NOCScrapper TIMED OUT after 150s", flush=True)

    print("\n" + "=" * 60)
    print("🏁 All tests completed!", flush=True)
    print("=" * 60, flush=True)


if __name__ == "__main__":
    main()
