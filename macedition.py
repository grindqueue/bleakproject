# ble_proxy_mac_only.py
import asyncio
import json
import base64
import time
import platform
from aiohttp import web
from bleak import BleakClient, BleakScanner, BleakError

# Ensure macOS
if platform.system() != "Darwin":
    raise RuntimeError("❌ This script only works on macOS")

BLE_CHUNK_SIZE = 120
SCAN_TIMEOUT = 10.0
REQUEST_TIMEOUT = 90.0


# ---------- Utils ----------
def chunk_bytes(b: bytes, n=BLE_CHUNK_SIZE):
    for i in range(0, len(b), n):
        yield b[i:i + n]


# ---------- Device Selection ----------
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


# ---------- Connect and Detect ----------
async def detect_characteristics(address):
    async with BleakClient(address, timeout=30) as client:
        if not client.is_connected:
            raise BleakError(f"❌ Could not connect to {address}")

        print("✅ Connected. Detecting characteristics...")
        await asyncio.sleep(1)  # Allow macOS stack to populate services

        services = client.services
        if not services:
            raise RuntimeError("❌ Could not read any BLE services from device.")

        print("\n📋 Available Services and Characteristics:")
        possible_chars = []
        for service in services:
            print(f"\n🔹 Service: {service.uuid}")
            for char in service.characteristics:
                props = char.properties
                print(f"   ↳ Char: {char.uuid} | Props: {props}")
                if "read" in props or "write" in props or "notify" in props:
                    possible_chars.append(char)

        if not possible_chars:
            raise RuntimeError("❌ No readable/writeable/notifiable characteristics found.")

        # Explicitly find best matches
        cmd_char = next((c.uuid for c in possible_chars if "write" in c.properties), None)
        resp_char = next((c.uuid for c in possible_chars if "notify" in c.properties), None)

        # fallback to readable ones if nothing fits
        if not cmd_char:
            cmd_char = next((c.uuid for c in possible_chars if "read" in c.properties), None)
        if not resp_char:
            resp_char = cmd_char

        print(f"\n✨ CMD_CHAR: {cmd_char}")
        print(f"✨ RESP_CHAR: {resp_char}")

        # --- Optional direct read test ---
        if cmd_char:
            try:
                print("\n📖 Testing read on CMD_CHAR...")
                data = await client.read_gatt_char(cmd_char)
                print(f"   ✅ Read success ({len(data)} bytes): {data[:30]!r}")
            except Exception as e:
                print(f"   ⚠️ Read test failed: {e}")

        # --- Optional write test ---
        if cmd_char:
            try:
                print("\n✏️ Testing write on CMD_CHAR...")
                test_data = b"test"
                await client.write_gatt_char(cmd_char, test_data, response=True)
                print("   ✅ Write success.")
            except Exception as e:
                print(f"   ⚠️ Write test failed: {e}")

        # --- Optional notify test ---
        if resp_char:
            async def notif_handler(sender, data):
                print(f"🔔 Notification from {sender}: {data}")

            try:
                print("\n📡 Testing notify on RESP_CHAR (5 seconds)...")
                await client.start_notify(resp_char, notif_handler)
                await asyncio.sleep(5)
                await client.stop_notify(resp_char)
                print("   ✅ Notify test complete.")
            except Exception as e:
                print(f"   ⚠️ Notify test failed: {e}")

        return cmd_char, resp_char


# ---------- Send Command ----------
async def send_command_and_get_response(address, cmd_char, resp_char, command_bytes, timeout=REQUEST_TIMEOUT):
    fragments = {}
    finished = asyncio.Event()

    # Allow decode data responses non uft-8 format
    def notif_handler(sender, data: bytearray):
        try:
            try:
                decoded = data.decode("utf-8")
            except UnicodeDecodeError:
                print("⚠️ Non-UTF8 notification received:", data.hex())
                return

            msg = json.loads(decoded)
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

    async with BleakClient(address, timeout=30) as client:
        await client.start_notify(resp_char, notif_handler)
        await asyncio.sleep(0.5)

        for chunk in chunk_bytes(command_bytes, BLE_CHUNK_SIZE):
            await client.write_gatt_char(cmd_char, chunk, response=True)
            await asyncio.sleep(0.02)

        print("📨 Command sent. Waiting for response...")
        try:
            await asyncio.wait_for(finished.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            raise TimeoutError("Timed out waiting for response")

        rid = json.loads(command_bytes.decode("utf-8"))["id"]
        parts = fragments.get(rid, {})
        assembled = b"".join(parts[i] for i in sorted(parts.keys()))
        return assembled


# ---------- HTTP-to-BLE Proxy ----------
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

# ---------- Entry Point ----------
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
