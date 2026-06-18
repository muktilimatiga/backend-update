Purpose

Fast lookup by user_pppoe or customer name, then run ONU check and provide action menu (reboot / port status / redaman / refresh / exit).

Trigger

User message formats:

c <query>

cek <query>

Where <query> can be:

user_pppoe (preferred exact match)

or part/full of customer name

Examples:

c budi01

cek Sari

c budi

If user types only c / cek without query:

Bot replies: “Format: c <user_pppoe / nama>”

APIs Used
1) Search customer by query

POST or GET /customer/customer-data

Body/query (example):

{ "query": "budi01" }


Expected response:

Array of matches:

[
  {
    "customer_id": 1001,
    "name": "Budi",
    "user_pppoe": "budi01",
    "olt_name": "boyolangu",
    "interface": "gpon-olt_1/1/1",
    "onu_id": "1/1/1:12",
    "sn": "ZTEG...."
  }
]

2) ONU check

POST /onu/cek

Body (example):

{
  "olt_name": "boyolangu",
  "onu_id": "1/1/1:12",
  "interface": "gpon-olt_1/1/1",
  "sn": "ZTEG....",
  "customer_id": 1001
}

3) Actions (after check)

POST /{olt_name}/onu/reboot

POST /{olt_name}/onu/port_state

POST /{olt_name}/onu/port_rx

Flow
Step A — User Search

User sends: c <query>

Bot calls: /customer/customer-data with <query>

Cases:

Case A1 — 0 results

Bot:

“Data tidak ditemukan untuk: <query>”

Case A2 — >1 results

Bot sends a selection list (buttons):

budi01 | Budi | boyolangu | 1/1/1:12

budi02 | Budi Santoso | tulungagung | 1/1/2:9

...

User picks one item → proceed to Step B

Case A3 — exactly 1 result

Bot proceeds immediately → Step B

Step B — Run ONU Check

Bot calls:

/onu/cek using the selected customer payload

Bot replies with results (format example):

Customer: Budi (budi01)

OLT: boyolangu

ONU: 1/1/1:12

Status: Online

RX: -18.2 dBm

Last seen: …

Then bot shows Action Menu (inline keyboard).

Action Menu (shown after /onu/cek)

Buttons:

Reboot modem

Show 1 port status

Show 1 port redaman

Refresh

Exit

Action Behavior
1) Reboot modem

Call: POST /{olt_name}/onu/reboot

Body uses the same selected customer/ONU info (at minimum onu_id, maybe interface, sn if needed)

Bot replies with reboot response

Then show the Action Menu again

2) Show 1 port status

Call: POST /{olt_name}/onu/port_state

Bot replies with port state results (LAN1..LAN4 / ETH status etc)

Then show Action Menu again

3) Show 1 port redaman

Call: POST /{olt_name}/onu/port_rx

Bot replies with optical/port RX attenuation result

Then show Action Menu again

4) Refresh

Repeat the last ONU check request:

Re-call /onu/cek with the exact same payload as Step B

Bot replies updated check results

Then show Action Menu again

5) Exit

Clear this command session state (or mark as idle)

Bot replies: “✅ Selesai.”

State Management (Session for c/cek)

Store per-user temporary state:

mode: "CEK"

selected_customer: full object from /customer/customer-data

last_cek_payload: payload sent to /onu/cek

last_action: last action endpoint called (optional)

step: SEARCHING | SELECTING | CHECKED | MENU

Notes

If user clicks menu button but session expired:

“Session habis, ulangi dengan c <pppoe/nama>”

TTL recommended: 10–20 minutes

Callback Data Convention (Recommended)

Use short callback ids to keep Telegram limits safe.

Examples:

cek_pick:<customer_id>

cek_action:reboot

cek_action:port_state

cek_action:port_rx

cek_action:refresh

cek_action:exit

When receiving callback:

Load selected_customer from session (or by customer_id lookup if needed)

Error Handling Rules

API error/timeout → show:

“❌ Error: <message>”

Buttons: Refresh + Exit

For reboot action, consider confirmation:

“Yakin reboot modem?” → Confirm / Cancel