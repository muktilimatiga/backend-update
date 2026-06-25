# /api/v1/endpoints/config

from fastapi import APIRouter, HTTPException
from typing import List
import asyncio
import logging

from core import settings, OLT_OPTIONS, MODEM_OPTIONS, PACKAGE_OPTIONS, OLT_ALIASES
from schemas.config_handler import (
    UnconfiguredOnt,
    ConfigurationRequest,
    ConfigurationResponse,
    ConfigurationSummary,
    OptionsResponse,
    ConfigurationBridgeRequest,
    BatchConfigurationRequest,
    BatchItemResult,
    BatchConfigurationResponse,
    CustomerInfo,
    ReconfigRequest,
    ReconfigItemResult,
    ReconfigResponse,
)
from services.telnet import TelnetClient
from services.connection_manager import olt_manager
from services.database import (
    get_customers_by_sns,
    save_customer_config_async,
    fetch_paket_from_billing,
)
from services.supabase_client import get_losi_client as fetch_losi_clients_from_db
from api.v1.endpoints.customer_scrapper import get_customer_data_noc_billing_test

router = APIRouter()


@router.get("/api/options", response_model=OptionsResponse)
async def get_options():
    """Mengembalikan semua opsi yang dibutuhkan untuk form di frontend."""
    return {
        "olt_options": list(OLT_OPTIONS.keys()),
        "modem_options": MODEM_OPTIONS,
        "package_options": list(PACKAGE_OPTIONS.keys()),
    }


@router.get("/api/olts/{olt_name}/detect-onts", response_model=List[UnconfiguredOnt])
async def detect_uncfg_onts(olt_name: str):
    """Mendeteksi semua unconfigured ONT pada OLT yang dipilih."""
    input_name = olt_name.upper()
    actual_olt_name = OLT_ALIASES.get(input_name, input_name)

    olt_info = OLT_OPTIONS.get(actual_olt_name)
    if not olt_info:
        raise HTTPException(
            status_code=404, detail=f"OLT '{olt_name}' tidak ditemukan."
        )

    max_retries = 2
    last_error = None

    for attempt in range(max_retries):
        try:
            handler = await asyncio.wait_for(
                olt_manager.get_connection(
                    host=olt_info["ip"],
                    username=settings.OLT_USERNAME,
                    password=settings.OLT_PASSWORD,
                    is_c600=olt_info["c600"],
                    olt_name=actual_olt_name,
                ),
                timeout=30,  # 30 second timeout for connection
            )

            ont_list = await asyncio.wait_for(
                handler.find_unconfigured_onts(),
                timeout=60,  # 60 second timeout for the command
            )
            return ont_list

        except asyncio.TimeoutError:
            last_error = "Timeout saat menghubungi OLT"
            logging.warning(
                f"[DETECT-ONT] Attempt {attempt + 1} timeout for {olt_name}"
            )
            # Clear stale connection on timeout
            if olt_info["ip"] in olt_manager._connections:
                del olt_manager._connections[olt_info["ip"]]

        except ConnectionError as e:
            last_error = str(e)
            logging.warning(f"[DETECT-ONT] Attempt {attempt + 1} connection error: {e}")
            # Clear stale connection
            if olt_info["ip"] in olt_manager._connections:
                del olt_manager._connections[olt_info["ip"]]

        except Exception as e:
            last_error = str(e)
            logging.error(f"[DETECT-ONT] Attempt {attempt + 1} error: {e}")
            # Clear connection on any error
            if olt_info["ip"] in olt_manager._connections:
                del olt_manager._connections[olt_info["ip"]]

        # Wait a bit before retry
        if attempt < max_retries - 1:
            await asyncio.sleep(1)

    # All retries failed
    raise HTTPException(
        status_code=504,
        detail=f"Gagal terhubung ke OLT setelah {max_retries} percobaan: {last_error}",
    )


@router.get("/api/olts/detect-all", response_model=List[UnconfiguredOnt])
async def detect_all_onts():
    """Scan ALL OLTs in parallel for unconfigured ONTs."""

    async def _scan_one(olt_name: str, olt_info: dict) -> List[UnconfiguredOnt]:
        try:
            handler = await asyncio.wait_for(
                olt_manager.get_connection(
                    host=olt_info["ip"],
                    username=settings.OLT_USERNAME,
                    password=settings.OLT_PASSWORD,
                    is_c600=olt_info["c600"],
                    olt_name=olt_name,
                ),
                timeout=30,
            )
            ont_list = await asyncio.wait_for(
                handler.find_unconfigured_onts(),
                timeout=60,
            )
            for ont in ont_list:
                ont.olt_name = olt_name
            return ont_list
        except Exception as e:
            logging.warning(f"[DETECT-ALL] Failed {olt_name}: {e}")
            if olt_info["ip"] in olt_manager._connections:
                del olt_manager._connections[olt_info["ip"]]
            return []

    tasks = [_scan_one(name, info) for name, info in OLT_OPTIONS.items()]
    results = await asyncio.gather(*tasks)
    return [ont for batch in results for ont in batch]


@router.post("/api/olts/{olt_name}/configure", response_model=ConfigurationResponse)
async def run_configuration(olt_name: str, request: ConfigurationRequest):
    """Menjalankan proses konfigurasi untuk satu ONT."""
    input_name = olt_name.upper()
    actual_olt_name = OLT_ALIASES.get(input_name, input_name)

    olt_info = OLT_OPTIONS.get(actual_olt_name)

    if not olt_info:
        raise HTTPException(
            status_code=404, detail=f"OLT '{olt_name}' tidak ditemukan."
        )

    try:
        handler = await olt_manager.get_connection(
            host=olt_info["ip"],
            username=settings.OLT_USERNAME,
            password=settings.OLT_PASSWORD,
            is_c600=olt_info["c600"],
            olt_name=actual_olt_name,
        )
        logs, summary = await handler.apply_configuration(request)

        # Check if configuration failed (error is now returned in summary, not raised)
        if summary["status"] == "error":
            logs.append("ERROR < Konfigurasi gagal. Lihat report untuk detail.")
        else:
            # Save to Supabase on success
            from services.supabase_client import save_customer_config

            db_saved = await save_customer_config(
                user_pppoe=request.customer.pppoe_user,
                nama=request.customer.name,
                alamat=request.customer.address,
                olt_name=olt_name.upper(),
                interface=summary["location"],
                onu_sn=request.sn,
                pppoe_password=request.customer.pppoe_pass,
                paket=request.package,
            )
            if db_saved:
                logs.append("INFO < Data pelanggan berhasil disimpan ke Supabase.")
            else:
                logs.append(
                    "WARN < Gagal menyimpan ke Supabase, konfigurasi OLT tetap berhasil."
                )

        import logging

        logging.info(f"[CONFIG] Summary: {summary}")
        logging.info(f"[CONFIG] Logs count: {len(logs)}")

        response = ConfigurationResponse(
            message=summary["message"],
            summary=ConfigurationSummary(**summary),
            logs=logs,
        )
        logging.info(f"[CONFIG] Response: {response.model_dump_json()}")
        return response
    except (ConnectionError, asyncio.TimeoutError) as e:
        # This catches connection errors BEFORE apply_configuration runs
        raise HTTPException(status_code=504, detail=f"Gagal terhubung ke OLT: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"System error: {e}")


@router.post(
    "/api/olts/{olt_name}/config_bridge", response_model=ConfigurationResponse
)
async def run_configuration_bridge(olt_name: str, request: ConfigurationBridgeRequest):
    """Menjalankan konfigurasi bridge untuk satu ONT."""
    olt_info = OLT_OPTIONS.get(olt_name.upper())
    if not olt_info:
        raise HTTPException(
            status_code=404, detail=f"OLT '{olt_name}' tidak ditemukan."
        )

    try:
        handler = await olt_manager.get_connection(
            host=olt_info["ip"],
            username=settings.OLT_USERNAME,
            password=settings.OLT_PASSWORD,
            is_c600=olt_info["c600"],
            olt_name=olt_name.upper(),
        )
        logs, summary = await handler.config_bridge(request)

        if summary["status"] == "error":
            logs.append("ERROR < Konfigurasi bridge gagal. Lihat report untuk detail.")
        else:
            from services.supabase_client import save_customer_config

            db_saved = await save_customer_config(
                user_pppoe=request.customer.pppoe_user,
                nama=request.customer.name,
                alamat=request.customer.address,
                olt_name=olt_name.upper(),
                interface=summary["location"],
                onu_sn=request.sn,
                pppoe_password=request.customer.pppoe_pass,
                paket=request.package,
            )
            if db_saved:
                logs.append("INFO < Data pelanggan berhasil disimpan ke Supabase.")
            else:
                logs.append(
                    "WARN < Gagal menyimpan ke Supabase, konfigurasi bridge tetap berhasil."
                )

        return ConfigurationResponse(
            message=summary["message"],
            summary=ConfigurationSummary(**summary),
            logs=logs,
        )

    except (ConnectionError, asyncio.TimeoutError) as e:
        raise HTTPException(
            status_code=504, detail=f"Gagal terhubung ke OLT: {e}"
        )
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"System error: {e}")


@router.post(
    "/api/olts/{olt_name}/configure/batch", response_model=BatchConfigurationResponse
)
async def run_batch_configuration(olt_name: str, batch: BatchConfigurationRequest):
    """Menjalankan konfigurasi untuk BANYAK ONT dalam satu koneksi Telnet."""

    # 1. Validate OLT exists
    olt_info = OLT_OPTIONS.get(olt_name.upper())
    if not olt_info:
        raise HTTPException(
            status_code=404, detail=f"OLT '{olt_name}' tidak ditemukan."
        )

    results = []
    success_count = 0
    fail_count = 0

    try:
        # 2. Open Telnet Connection ONCE
        async with TelnetClient(
            host=olt_info["ip"],
            username=settings.OLT_USERNAME,
            password=settings.OLT_PASSWORD,
            is_c600=olt_info["c600"],
        ) as handler:
            # 3. Loop through the batch items using the SAME handler
            for request_item in batch.items:
                # Use SN or Username as identifier for the report
                item_id = getattr(request_item, "sn", "Unknown")

                try:
                    # Apply config
                    logs, summary = await handler.apply_configuration(
                        request_item, vlan=olt_info["vlan"]
                    )

                    # Check if this item succeeded or failed
                    if summary["status"] == "success":
                        results.append(
                            BatchItemResult(
                                identifier=item_id,
                                success=True,
                                message=summary["message"],
                                logs=logs,
                            )
                        )
                        success_count += 1
                    else:
                        # Configuration returned error in summary
                        results.append(
                            BatchItemResult(
                                identifier=item_id,
                                success=False,
                                message=summary["message"],
                                logs=logs,
                            )
                        )
                        fail_count += 1

                except Exception as e:
                    # Catch unexpected errors per item so one failure doesn't stop the whole batch
                    fail_count += 1
                    results.append(
                        BatchItemResult(
                            identifier=item_id,
                            success=False,
                            message=f"Unexpected error: {str(e)}",
                            logs=[
                                f"ERROR < Unexpected error processing {item_id}: {str(e)}"
                            ],
                        )
                    )

    except (ConnectionError, asyncio.TimeoutError) as e:
        # If the MAIN connection fails, the whole batch fails
        raise HTTPException(
            status_code=504, detail=f"Critical: Gagal koneksi ke OLT: {e}"
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"System Error: {e}")

    # 5. Return aggregated results
    return BatchConfigurationResponse(
        total=len(batch.items),
        success_count=success_count,
        fail_count=fail_count,
        results=results,
    )


@router.post("/api/olts/{olt_name}/reconfig-batch", response_model=ReconfigResponse)
async def run_reconfig_batch(olt_name: str, request: ReconfigRequest):
    """
    Reconfig endpoint: Configure ONTs by SN list (lookup from database).

    Flow:
    1. Take list of SNs from request
    2. Bulk lookup customer data from data_fiber table
    3. For each found customer, build config and apply
    4. Return summary of results
    """
    olt_info = OLT_OPTIONS.get(olt_name.upper())
    if not olt_info:
        raise HTTPException(
            status_code=404, detail=f"OLT '{olt_name}' tidak ditemukan."
        )

    if not request.sn_list:
        raise HTTPException(status_code=400, detail="sn_list tidak boleh kosong.")

    results = []
    stats = {
        "total_unconfigured": len(request.sn_list),
        "found_in_db": 0,
        "not_in_db": 0,
        "configured": 0,
        "failed": 0,
        "skipped": 0,
    }

    try:
        # 1. Connect to OLT
        handler = await olt_manager.get_connection(
            host=olt_info["ip"],
            username=settings.OLT_USERNAME,
            password=settings.OLT_PASSWORD,
            is_c600=olt_info["c600"],
        )

        # 2. Bulk lookup customers from database by SN list
        customers = await asyncio.to_thread(get_customers_by_sns, request.sn_list)

        # 3. Process each SN from request
        for sn in request.sn_list:
            sn_upper = sn.upper()
            customer_data = customers.get(sn_upper)

            if not customer_data:
                stats["not_in_db"] += 1
                results.append(
                    ReconfigItemResult(
                        sn=sn,
                        status="not_found",
                        message=f"SN {sn} tidak ditemukan di database",
                    )
                )
                continue

            stats["found_in_db"] += 1

            # Check if we have enough data to configure
            if not customer_data.get("user_pppoe") or not customer_data.get(
                "pppoe_password"
            ):
                stats["skipped"] += 1
                results.append(
                    ReconfigItemResult(
                        sn=sn,
                        user_pppoe=customer_data.get("user_pppoe"),
                        status="skipped",
                        message="Data PPPoE tidak lengkap di database",
                    )
                )
                continue

            # Get paket: DB → Billing → Default
            paket = customer_data.get("paket")
            if not paket:
                # Try fetching from billing system
                paket = await asyncio.to_thread(
                    fetch_paket_from_billing, customer_data["user_pppoe"]
                )
            if not paket:
                # Fallback to default
                paket = request.default_paket

            # Build ConfigurationRequest from database
            config_request = ConfigurationRequest(
                sn=sn,
                customer=CustomerInfo(
                    name=customer_data.get("nama") or "",
                    address=customer_data.get("alamat") or "",
                    pppoe_user=customer_data["user_pppoe"],
                    pppoe_pass=customer_data["pppoe_password"],
                ),
                package=paket,
                modem_type=request.modem_type,
                eth_locks=request.eth_locks,
            )

            try:
                # 4. Apply configuration
                logs, summary = await handler.apply_configuration(config_request)

                if summary["status"] == "success":
                    stats["configured"] += 1

                    # Update database with new interface
                    await save_customer_config_async(
                        user_pppoe=config_request.customer.pppoe_user,
                        nama=config_request.customer.name,
                        alamat=config_request.customer.address,
                        olt_name=olt_name.upper(),
                        interface=summary["location"],
                        onu_sn=sn,
                        pppoe_password=config_request.customer.pppoe_pass,
                        paket=paket,
                    )

                    results.append(
                        ReconfigItemResult(
                            sn=sn,
                            user_pppoe=customer_data["user_pppoe"],
                            status="success",
                            message=summary["message"],
                            logs=logs,
                        )
                    )
                else:
                    stats["failed"] += 1
                    results.append(
                        ReconfigItemResult(
                            sn=sn,
                            user_pppoe=customer_data["user_pppoe"],
                            status="error",
                            message=summary["message"],
                            logs=logs,
                        )
                    )

            except Exception as e:
                stats["failed"] += 1
                results.append(
                    ReconfigItemResult(
                        sn=sn,
                        user_pppoe=customer_data.get("user_pppoe"),
                        status="error",
                        message=f"Unexpected error: {str(e)}",
                        logs=[f"ERROR < {str(e)}"],
                    )
                )

    except (ConnectionError, asyncio.TimeoutError) as e:
        raise HTTPException(status_code=504, detail=f"Gagal koneksi ke OLT: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"System Error: {e}")

    return ReconfigResponse(**stats, results=results)


@router.get("/customer-los", response_model=List[dict])
async def get_losi_client(
    olt_name: str,
    interface: str,
):
    input_name = olt_name.upper()
    actual_olt_name = OLT_ALIASES.get(input_name, input_name)
    olt_info = OLT_OPTIONS.get(actual_olt_name)
    if not olt_info:
        raise HTTPException(status_code=404, detail=f"OLT '{olt_name}' not found")

    try:
        async with TelnetClient(
            host=olt_info["ip"],
            username=settings.OLT_USERNAME,
            password=settings.OLT_PASSWORD,
            is_c600=olt_info.get("c600", False),
            olt_name=actual_olt_name,
        ) as handler:
            los_list = await handler.get_losi_interface(interface)
            if not los_list:
                return []
            interfaces = [item["interface"] for item in los_list]
            customers = await fetch_losi_clients_from_db(interfaces, actual_olt_name)
            return [
                {
                    "interface": c.get("interface"),
                    "nama": c.get("nama"),
                    "user_pppoe": c.get("user_pppoe"),
                }
                for c in customers
            ]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get LOS clients: {e}")


@router.get("/customer_los_coords", response_model=List[dict])
async def get_losi_client_coords(
    olt_name: str,
    interface: str,
):
    input_name = olt_name.upper()
    actual_olt_name = OLT_ALIASES.get(input_name, input_name)
    olt_info = OLT_OPTIONS.get(actual_olt_name)
    if not olt_info:
        raise HTTPException(status_code=404, detail=f"OLT '{olt_name}' not found")

    try:
        async with TelnetClient(
            host=olt_info["ip"],
            username=settings.OLT_USERNAME,
            password=settings.OLT_PASSWORD,
            is_c600=olt_info.get("c600", False),
            olt_name=actual_olt_name,
        ) as handler:
            los_list = await handler.get_losi_interface(interface)
            if not los_list:
                return []
            interfaces = [item["interface"] for item in los_list]
            customers = await fetch_losi_clients_from_db(interfaces, actual_olt_name)
            if not customers:
                return []

            from services.biling_scaper import BillingScraper
            from services.supabase_client import save_billing_data_sync

            # Semaphore to throttle concurrent billing scrape sessions (max 3)
            sem = asyncio.Semaphore(3)

            def _scrape_coords(user_pppoe: str):
                if not user_pppoe:
                    return None
                try:
                    billing = BillingScraper()
                    results = billing.search(user_pppoe)
                    logging.info(f"[COORDS] Billing search for {user_pppoe}: {results}")
                    if not results:
                        return None
                    customer_id = results[0].get("id")
                    if not customer_id:
                        return None
                    customer = billing.get_customer_details(customer_id)
                    if not customer:
                        return None
                    coord = customer.coordinate if hasattr(customer, "coordinate") else customer.get("coordinate")
                    logging.info(f"[COORDS] Coord: {coord}")
                    if coord:
                        # Cache coordinates back to Supabase
                        try:
                            save_billing_data_sync({
                                "user_pppoe": user_pppoe,
                                "coordinates": coord,
                            })
                        except Exception:
                            pass
                    logging.info(f"[COORDS] Got coordinate for {user_pppoe}: {coord}")
                    return coord
                except Exception as e:
                    logging.warning(f"[COORDS] Failed to scrape billing for {user_pppoe}: {e}")
                return None

            async def _scrape_with_sem(user_pppoe: str):
                async with sem:
                    return await asyncio.to_thread(_scrape_coords, user_pppoe)

            # Scrape coordinates concurrently (max 3 at a time)
            scrape_tasks = [
                _scrape_with_sem(c.get("user_pppoe"))
                for c in customers
            ]
            scraped_coords = await asyncio.gather(*scrape_tasks)
            logging.info(f"[COORDS] Scraped coordinates: {scraped_coords}")

            return [
                {
                    "nama": c.get("nama"),
                    "user_pppoe": c.get("user_pppoe"),
                    "interface": c.get("interface"),
                    "coordinates": coord or c.get("coordinates"),
                }
                for c, coord in zip(customers, scraped_coords)
            ]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get LOS clients: {e}")
    