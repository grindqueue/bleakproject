# ble_proxy_dynamic_fixed.py
import asyncio
import json
import base64
import time
from aiohttp import web
from bleak import BleakClient, BleakScanner, BleakError

BLE_CHUNK_SIZE = 120
SCAN_TIMEOUT = 10.0
REQUEST_TIMEOUT = 90.0

def chunk_bytes(b: bytes, n=BLE_CHUNK_SIZE):
    for i in range(0, len(b), n):
        yield b[i:i + n]

# 🔍 Scan and select device
async def select_device():
    print("🔍 Scanning for nearby Bluetooth devices...")
    devices = await BleakScanner.discover(timeout=SCAN_TIMEOUT)
    if not devices:
        print("❌ No Bluetooth devices found.")
        return None

    print("\n✅ Found devices:")
    for i, d in enumerate(devices):
        print(f"[{i}] {d.name or 'Unknown'} - {d.address}")

    try:
        index = int(input("\nEnter the number of the device to connect: "))
        return devices[index].address
    except (ValueError, IndexError):
        print("❌ Invalid selection.")
        return None


# ⚙️ Safe connect with retries and wait
async def safe_connect(address, retries=3):
    for attempt in range(retries):
        try:
            await asyncio.sleep(2)  # Let Windows finalize pairing
            client = BleakClient(address, timeout=30)
            await client.connect()
            if client.is_connected:
                print(f"✅ Connected on attempt {attempt + 1}")
                await asyncio.sleep(2)
                return client
        except Exception as e:
            print(f"⚠️ Attempt {attempt + 1} failed: {e}")
            await asyncio.sleep(3)
    raise BleakError(f"❌ Could not connect to {address} after retries")


# 🔌 Detect characteristics for write/notify automatically
async def detect_characteristics(address):
    client = await safe_connect(address)
    print("✅ Detecting characteristics...")

    await asyncio.sleep(2)  # ⚙️ Give stack time to fetch services
    services = client.services

    if not services:
        raise RuntimeError("❌ Could not read any BLE services from device.")

    possible_chars = []
    for service in services:
        for char in service.characteristics:
            if "write" in char.properties or "notify" in char.properties:
                possible_chars.append(char.uuid)

    if not possible_chars:
        raise RuntimeError("❌ No suitable characteristics found.")

    cmd_char = None
    resp_char = None
    for char in possible_chars:
        props = next(
            (c.properties for s in services for c in s.characteristics if c.uuid == char),
            [],
        )
        if "write" in props and cmd_char is None:
            cmd_char = char
        if "notify" in props and resp_char is None:
            resp_char = char

    if not cmd_char:
        cmd_char = possible_chars[0]
    if not resp_char:
        resp_char = cmd_char

    print(f"\n✨ CMD_CHAR: {cmd_char}")
    print(f"✨ RESP_CHAR: {resp_char}")

    await client.disconnect()
    return cmd_char, resp_char


# 📤 Send command and get response
async def send_command_and_get_response(address, cmd_char, resp_char, command_bytes, timeout=REQUEST_TIMEOUT):
    fragments = {}
    finished = asyncio.Event()

    def notif_handler(sender, data: bytearray):
        try:
            msg = json.loads(data.decode("utf-8"))
            rid = msg.get("id")
            seq = int(msg.get("seq", 0))
            payload_b64 = msg.get("payload_b64", "")
            final = bool(msg.get("final", False))
            payload = base64.b64decode(payload_b64) if payload_b64 else b""
            fragments.setdefault(rid, {})[seq] = payload
            if final:
                finished.set()
        except Exception as e:
            print("⚠️ Notification parse error:", e)

    # ⚙️ Reuse safe_connect to handle timing issues
    client = await safe_connect(address)
    try:
        await asyncio.sleep(1)
        await client.start_notify(resp_char, notif_handler)
        await asyncio.sleep(1)  # ⚙️ Let notification settle

        for chunk in chunk_bytes(command_bytes, BLE_CHUNK_SIZE):
            await client.write_gatt_char(cmd_char, chunk, response=True)
            await asyncio.sleep(0.05)

        print("📨 Command sent. Waiting for response...")

        try:
            await asyncio.wait_for(finished.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            raise TimeoutError("Timed out waiting for response")

        req_json = json.loads(command_bytes.decode("utf-8"))
        rid = req_json["id"]
        parts = fragments.get(rid, {})
        assembled = b"".join(parts[i] for i in sorted(parts.keys()))
        return assembled
    finally:
        try:
            await client.stop_notify(resp_char)
            await client.disconnect()
        except Exception:
            pass


# 🌐 HTTP-to-BLE Proxy
async def handle_request(request):
    body = await request.read()
    req_id = f"req-{int(time.time()*1000)}"

    cmd = {
        "id": req_id,
        "method": request.method,
        "url": str(request.url),
        "headers": dict(request.headers)
    }
    if body:
        cmd["body_b64"] = base64.b64encode(body).decode("ascii")

    cmd_bytes = json.dumps(cmd).encode("utf-8")

    global connected_address, CMD_CHAR, RESP_CHAR
    if not connected_address:
        connected_address = await select_device()
        CMD_CHAR, RESP_CHAR = await detect_characteristics(connected_address)

    try:
        resp_bytes = await send_command_and_get_response(connected_address, CMD_CHAR, RESP_CHAR, cmd_bytes)
    except Exception as e:
        return web.Response(text=f"Error communicating with device: {e}", status=504)

    try:
        msg = json.loads(resp_bytes.decode("utf-8"))
        status = int(msg.get("status", 200))
        headers = msg.get("headers", {})
        body_b64 = msg.get("body_b64", "")
        body = base64.b64decode(body_b64) if body_b64 else b""
        return web.Response(body=body, status=status, headers=headers)
    except Exception:
        return web.Response(body=resp_bytes, status=200)


connected_address = None
CMD_CHAR = None
RESP_CHAR = None

async def main():
    app = web.Application()
    app.router.add_route('*', '/{tail:.*}', handle_request)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 8080)
    print("\n🚀 Proxy listening on http://127.0.0.1:8080")
    await site.start()
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
