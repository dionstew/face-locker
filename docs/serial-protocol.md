# External controller serial contract — version 1

This project implements the Raspberry Pi **client only**. No solenoid controller firmware or
GPIO actuation is included. Adapt the serialization in `face_locker/serial_service.py` if an
existing controller uses a different protocol; its current command format was not supplied.

## Transport

- Default 115200 baud, 8 data bits, no parity, 1 stop bit, no flow control.
- UTF-8 JSON object followed by LF (`\n`), maximum 4096 bytes including LF.
- Every message includes integer `v: 1` and the configured integer `lock_id`.
- The service handles fragmented/multiple lines and drops entire oversized/malformed lines.
- One process exclusively opens the serial port. Read timeout is 50 ms; write timeout is 500 ms.
- The Pi queries status immediately after opening and every 2 seconds thereafter.
- The external controller must return status within `telemetry_timeout` (default 6 seconds).

## Status query and reply

Pi → controller:

```json
{"v":1,"type":"get_status","lock_id":1}
```

Controller → Pi:

```json
{"v":1,"type":"status","lock_id":1,"battery_voltage":12.4,"actuator":"secured","door":"closed"}
```

`battery_voltage` is a finite number from 0 to 100, or null if not measured. `actuator` is one of
`secured`, `released`, or `unknown`; `door` is `open`, `closed`, or `unknown`. These are reports
from the controller, not assumptions made by the app. Use `unknown` if the state cannot be
established. Do not report physical bolt or door confirmation without corresponding sensing.
Voltage measurement belongs to the external sensor/controller, not the serial client.

The optional heartbeat response is `{"v":1,"type":"pong","lock_id":1}`. It proves the connection
is responding but **does not refresh telemetry**. Stale telemetry hides the old voltage and
actuator state and blocks unlock requests.

## Unlock request, acknowledgment, and result

Pi → controller:

```json
{"v":1,"type":"unlock","request_id":"random-unique-id","lock_id":1,"duration_ms":3000}
```

Controller → Pi:

```json
{"v":1,"type":"ack","request_id":"random-unique-id","lock_id":1,"accepted":true}
{"v":1,"type":"result","request_id":"random-unique-id","lock_id":1,"result":"completed"}
```

- A request ID is a nonempty string of at most 64 characters; the app generates random IDs.
- An `ack` must arrive within `ack_timeout` (default 2 seconds). A negative acknowledgment uses
  `accepted: false` and ends the request as rejected.
- An accepted command remains in flight until `result`, with deadline measured from sending:
  `duration_ms / 1000 + ack_timeout + 2` seconds. `result` is `completed` or `failed`.
- The controller may send a correlated final result even if the acknowledgment was lost.
- `completed` means the controller reports completion under its documented semantics. It does
  not automatically prove a door opened. A corresponding status message updates actuator/door
  display independently. Do not show physical success based only on `ack`.
- The app allows only one in-flight request and does not issue another while the actuator is
  reported released. Unknown/mismatched request IDs cannot complete an operation.

## Failure behavior and controller responsibilities

The Pi never retries an unlock automatically, including after a timeout, serial reconnect, or
process restart. A missing response is an **unknown outcome**, because the controller might
have acted. Following a timeout/disconnection of an in-flight command, further unlocks are
blocked until the activation period plus a 2-second margin has passed and a fresh status reports
`secured`. Query status and inspect hardware rather than replaying the request.

The external controller must independently enforce activation limits, reject inappropriate
commands, deduplicate request IDs, and handle disconnection/power loss safely. Configure
the actual maximum safe duration there, regardless of the requested duration. The Pi allows
configured requests between 1 and 10,000 ms; that range is not a solenoid safety specification.

This serial protocol has no cryptographic authentication; assume a physically protected serial
link. Integrity/authentication for an exposed bus requires a controller-supported protocol.
Access policy and face matching run on the Pi; actuator timing and electrical behavior remain
entirely in the external system.
