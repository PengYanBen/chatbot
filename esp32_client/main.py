import ujson
import utime
import uasyncio as asyncio
import network
from machine import I2S, Pin

# 需要将 uwebsockets/client.py 上传到设备
import uwebsockets.client as ws_client

# ========== 用户配置 ==========
WIFI_SSID = "YOUR_WIFI_SSID"
WIFI_PASSWORD = "YOUR_WIFI_PASSWORD"
SERVER_WS_URL = "ws://192.168.1.20:8765/ws/audio?device=esp32-s3-01"

SAMPLE_RATE = 16000
BITS = 16
CHANNELS = I2S.MONO

# I2S 引脚（根据你的开发板改）
I2S_SCK_PIN = 5
I2S_WS_PIN = 6
I2S_SD_PIN = 4

# 每帧 20ms：16000 * 0.02 * 2byte = 640 byte
CHUNK_BYTES = 640
I2S_BUFFER_BYTES = CHUNK_BYTES * 20

RECONNECT_SECONDS_MIN = 2
RECONNECT_SECONDS_MAX = 20


def _fmt_exc(e):
    args = getattr(e, "args", None)
    if args:
        return "{} args={}".format(type(e).__name__, args)
    return "{}: {}".format(type(e).__name__, e)


def connect_wifi(ssid, password, force_reconnect=False):
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)

    if force_reconnect and wlan.isconnected():
        try:
            wlan.disconnect()
            utime.sleep_ms(300)
        except Exception:
            pass

    if wlan.isconnected():
        return wlan

    print("[wifi] connecting...")
    wlan.connect(ssid, password)

    timeout_s = 20
    start = utime.time()
    while not wlan.isconnected():
        if utime.time() - start > timeout_s:
            raise RuntimeError("WiFi connect timeout")
        utime.sleep_ms(200)

    print("[wifi] connected:", wlan.ifconfig())
    return wlan


def ensure_wifi(ssid, password):
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if wlan.isconnected():
        return wlan
    return connect_wifi(ssid, password, force_reconnect=True)


def build_i2s():
    return I2S(
        0,
        sck=Pin(I2S_SCK_PIN),
        ws=Pin(I2S_WS_PIN),
        sd=Pin(I2S_SD_PIN),
        mode=I2S.RX,
        bits=BITS,
        format=CHANNELS,
        rate=SAMPLE_RATE,
        ibuf=I2S_BUFFER_BYTES,
    )


async def stream_audio_once():
    audio_in = None
    ws = None
    try:
        ensure_wifi(WIFI_SSID, WIFI_PASSWORD)

        print("[ws] connecting:", SERVER_WS_URL)
        ws = ws_client.connect(SERVER_WS_URL)
        print("[ws] connected")

        start_msg = {
            "type": "start",
            "sample_rate": SAMPLE_RATE,
            "bits": BITS,
            "channels": 1,
            "format": "pcm_s16le",
            "ts_ms": utime.ticks_ms(),
        }
        ws.send(ujson.dumps(start_msg))

        audio_in = build_i2s()
        buf = bytearray(CHUNK_BYTES)
        mv = memoryview(buf)

        while True:
            if not network.WLAN(network.STA_IF).isconnected():
                raise OSError("wifi disconnected")

            n = audio_in.readinto(buf)
            if n is None or n <= 0:
                await asyncio.sleep_ms(5)
                continue

            # readinto 可能小于目标长度
            ws.send(mv[:n])
            await asyncio.sleep_ms(0)

    except Exception as e:
        print("[stream] error:", _fmt_exc(e))
    finally:
        if ws is not None:
            try:
                ws.send(ujson.dumps({"type": "stop", "ts_ms": utime.ticks_ms()}))
            except Exception:
                pass
            try:
                ws.close()
            except Exception:
                pass

        if audio_in is not None:
            try:
                audio_in.deinit()
            except Exception:
                pass


async def main():
    connect_wifi(WIFI_SSID, WIFI_PASSWORD)
    retry_s = RECONNECT_SECONDS_MIN

    while True:
        await stream_audio_once()
        print("[main] reconnect in {}s".format(retry_s))
        await asyncio.sleep(retry_s)
        retry_s = min(retry_s * 2, RECONNECT_SECONDS_MAX)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        asyncio.new_event_loop()