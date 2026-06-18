# Lexxadata Backend API

A REST API for fiber-optic ISP management — integrating billing, NOC, OLT hardware, network switches, and Supabase into a single FastAPI service.

## Tech Stack

- **Framework:** FastAPI + Uvicorn
- **Language:** Python 3.11
- **Databases:** Supabase (PostgreSQL), direct PostgreSQL via psycopg2
- **OLT/Switch:** Async Telnet via telnetlib3 (ZTE C300/C600, Huawei, Cisco, Ruijie)
- **Scraping:** requests + BeautifulSoup, Playwright (Chromium), Selenium (ChromeDriver)
- **OCR:** PaddleOCR (Linux), Tesseract (macOS fallback), YOLO (modem detection)
- **Data:** pandas, openpyxl, Jinja2, PyYAML

## Quick Start

### Docker (Recommended)

```bash
# 1. Create .env file (see Environment Variables below)
cp .env.example .env
# edit .env with your credentials

# 2. Build and run
docker-compose up --build -d

# API: http://localhost:8002
# Docs: http://localhost:8002/docs
```

### Local Development

```bash
# 1. Install dependencies
poetry install

# 2. Install Playwright browsers
playwright install chromium && playwright install-deps

# 3. Run
python main.py
```

## API Endpoints

All endpoints are prefixed with `/api/v1/`.

### Customer Data (`/customer/`)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/customer/customers-billing?query=` | Search billing system, get customer details + invoices |
| `GET` | `/customer/customers-data?search=` | Search customers from Supabase |
| `GET` | `/customer/customer-fast?search=` | Fast search via Playwright |
| `GET` | `/customer/customer-noc?search=` | Search NOC portal |
| `GET` | `/customer/customer-noc-billing-test?search=` | NOC search via requests (speed test) |
| `GET` | `/customer/psb` | Get PSB customer data |

### OLT Configuration (`/config/`)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/config/api/options` | Get OLT/modem/package options |
| `GET` | `/config/api/olts/{olt_name}/detect-onts` | Detect unconfigured ONTs on OLT |
| `GET` | `/config/api/olts/detect-all` | Scan ALL OLTs for unconfigured ONTs |
| `POST` | `/config/api/olts/{olt_name}/configure` | Configure a single ONT |
| `POST` | `/config/api/olts/{olt_name}/config_bridge` | Configure ONT in bridge mode |
| `POST` | `/config/api/olts/{olt_name}/configure/batch` | Batch configure ONTs |
| `POST` | `/config/api/olts/{olt_name}/reconfig-batch` | Reconfigure ONTs by SN |
| `GET` | `/config/customer-los?olt_name=&interface=` | Get LOS clients (fast) |
| `GET` | `/config/customer_los_coords?olt_name=&interface=` | Get LOS clients with coordinates |

### ONU Management (`/onu/`)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/onu/{olt_name}/onu/cek` | Check ONU detail + attenuation |
| `POST` | `/onu/{olt_name}/onu/reboot` | Reboot ONU |
| `POST` | `/onu/{olt_name}/onu/no-onu` | Delete ONU from OLT |
| `POST` | `/onu/{olt_name}/onu/port_state` | Check GPON ONU state |
| `POST` | `/onu/{olt_name}/onu/port_rx` | Check ONU RX power |
| `POST` | `/onu/{olt_name}/onu/get-ip` | Get ONU IP host address |
| `POST` | `/onu/{olt_name}/onu/cek-eth` | Check Ethernet port status |
| `POST` | `/onu/{olt_name}/onu/get-dba` | Get DBA bandwidth rate |
| `POST` | `/onu/{olt_name}/onu/get-eth` | Get Ethernet port speeds |
| `POST` | `/onu/{olt_name}/onu/lock-eth` | Lock/unlock Ethernet ports |
| `POST` | `/onu/{olt_name}/onu/edit-capacity` | Edit ONU bandwidth capacity |
| `POST` | `/onu/{olt_name}/onu/get-running-config` | Get ONU running config |

### Ticket Management (`/ticket/`)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/ticket/create` | Create ticket (CS agent) |
| `POST` | `/ticket/create-and-process` | Create + process ticket |
| `POST` | `/ticket/process` | Process ticket (NOC) |
| `POST` | `/ticket/close` | Close ticket (NOC) |
| `POST` | `/ticket/forward` | Forward ticket |
| `POST` | `/ticket/search` | Search tickets |

### Bot/Monitoring (`/bot/`)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/bot/cek` | Check OLT interface status |
| `POST` | `/bot/redaman-monitoring` | Check optical Rx power |
| `POST` | `/bot/cek-dying` | Check DyingGasp/LOS/Offline counts |
| `POST` | `/bot/switch-description` | Get switch interface descriptions |

### OCR (`/ocr/`)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/ocr/ocr` | Extract text from image (PaddleOCR/Tesseract) |
| `POST` | `/ocr/ocr/detect` | Detect modem type + serial (YOLO + PaddleOCR) |
| `POST` | `/ocr/read-file` | Read text from uploaded files |

### File Operations (`/file/`)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/file/exceltodb` | Upload Excel to sync customer data to DB |
| `GET` | `/file/generate-batch-config` | Generate batch OLT config file |

### Terminal (`/cli/`)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/cli/start_terminal` | Start ttyd terminal session |
| `POST` | `/cli/stop_terminal/{port}` | Stop terminal session |
| `GET` | `/cli/running_terminals` | List active terminals |

## Architecture

```
main.py                     # FastAPI app, CORS, router mount
api/v1/endpoints/           # Route handlers
  ├── customer_scrapper.py  # Billing/NOC customer scraping
  ├── config_handler.py     # OLT config management
  ├── onu_handler.py        # ONU operations
  ├── ticket_api.py         # Ticket lifecycle
  ├── bot_api.py            # Monitoring endpoints
  ├── ocr.py                # OCR services
  ├── file_handler.py       # Excel upload, batch config
  └── cli.py                # Terminal management
services/                   # Business logic
  ├── biling_scaper.py      # BillingScraper + NOCScrapper (requests)
  ├── playwright.py         # Playwright browser automation
  ├── open_ticket.py        # Selenium ticket automation
  ├── telnet.py             # Async Telnet for ZTE OLT
  ├── switch_telnet.py      # Async Telnet for switches
  ├── connection_manager.py # Telnet connection pooling
  ├── supabase_client.py    # Supabase CRUD operations
  ├── database.py           # Direct PostgreSQL operations
  ├── exceltopostgress.py   # Excel → DB sync
  ├── generated.py          # Batch config file generation
  └── new_ocr.py            # YOLO + PaddleOCR pipeline
schemas/                    # Pydantic models
  ├── customers_scrapper.py # Customer, CustomerNOC, Invoice models
  ├── config_handler.py     # Configuration request/response
  ├── onu_handler.py        # ONU operation models
  └── ...
core/
  ├── config.py             # Settings (env vars via pydantic-settings)
  ├── olt_config.py         # OLT device definitions
  └── switch_config.py      # Switch device definitions
templates/                  # Jinja2 YAML templates for OLT config
```

## Key Features

### Billing/NOC Scraping
- **BillingScraper** — HTTP session-based scraper for the billing portal. Handles CAPTCHA solving via OCR, cookie persistence, customer search, invoice extraction.
- **NOCScrapper** — HTTP session-based scraper for the NOC portal. Same CAPTCHA/login flow, NOC-specific search and detail pages.
- **Playwright fallback** — Full browser automation for complex billing operations.

### OLT Management
- Async Telnet connections to ZTE C300/C600 OLTs
- ONT detection, configuration, reboot, deletion
- Batch configuration with Jinja2 YAML templates
- Connection pooling with keepalive

### OCR Pipeline
- **PaddleOCR** (Linux) / **Tesseract** (macOS) for general text extraction
- **YOLO + PaddleOCR** for modem type and serial number detection from photos
- Auto-detected at startup based on platform

### Database
- **Supabase** — Primary customer data store (`data_fiber` table)
- **PostgreSQL** — Direct access for batch operations and SN lookups
- UPSERT on `user_pppoe` for customer config
