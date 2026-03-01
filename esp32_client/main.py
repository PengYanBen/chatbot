import ujson
import utime
import uasyncio as asyncio
import network
from machine import I2S, Pin

# 需要将 uwebsockets/client.py 上传到设备
import uwebsockets.client as ws_client

# ========== 用户配置 ==========
WIFI_SSID = "63s3DCU_qVCj67532353@"
WIFI_PASSWORD = "fg6jkqbhy59124@"
SERVER_WS_URL = "ws://192.168.1.46:8000/ws/audio?device=esp32-s3-01"

SAMPLE_RATE = 16000

# INMP441 常见输出是 24-bit 数据装在 32-bit slot 中。
I2S_BITS = 32
PCM_BITS = 16
CHANNELS = I2S.MONO

# 关键参数：根据实测切换，解决“声音小/失真/噪声”
# - le32_left24: 小端 32-bit，24-bit left-justified（ESP32 常见）
# - be32_left24: 大端 32-bit，24-bit left-justified
PCM_EXTRACT_MODE = "le32_left24"

# 24bit -> 16bit 默认右移 8；
# 若声音太小可改 7；若失真爆音可改 9。
PCM_DOWN_SHIFT = 8

# 固定数字增益（当 AUTO_GAIN=False 生效）
PCM_GAIN_NUM = 1
PCM_GAIN_DEN = 1

# 自动增益（推荐开启）：尽量把人声拉到可听，同时避免严重削顶
AUTO_GAIN = True
TARGET_PEAK = 12000      # 目标峰值
AUTO_GAIN_MIN_Q8 = 64    # 0.25x
AUTO_GAIN_MAX_Q8 = 1024  # 4.0x
ATTACK_STEP_Q8 = 8       # 提高增益速度
RELEASE_STEP_Q8 = 20     # 降低增益速度（更快防爆音）

# 一阶 DC-block（高通）: y[n] = x[n] - x[n-1] + a*y[n-1]
ENABLE_DC_BLOCK = True
DC_BLOCK_A_Q15 = 32440  # 约 0.99 * 32768

# I2S 引脚（根据你的开发板改）
I2S_SCK_PIN = 5
I2S_WS_PIN = 6
I2S_SD_PIN = 4

CHUNK_MS = 20
SAMPLES_PER_CHUNK = SAMPLE_RATE * CHUNK_MS // 1000
RAW_BYTES_PER_SAMPLE = I2S_BITS // 8
PCM_BYTES_PER_SAMPLE = PCM_BITS // 8
RAW_CHUNK_BYTES = SAMPLES_PER_CHUNK * RAW_BYTES_PER_SAMPLE
PCM_CHUNK_BYTES = SAMPLES_PER_CHUNK * PCM_BYTES_PER_SAMPLE
I2S_BUFFER_BYTES = RAW_CHUNK_BYTES * 20

RECONNECT_SECONDS_MIN = 2
RECONNECT_SECONDS_MAX = 20

# DSP 状态
_prev_x = 0
_prev_y = 0
_auto_gain_q8 = 256  # 1.0x


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
        bits=I2S_BITS,
        format=CHANNELS,
        rate=SAMPLE_RATE,
        ibuf=I2S_BUFFER_BYTES,
    )


def _read_i32(raw_buf, b):
    if PCM_EXTRACT_MODE == "le32_left24":
        x = raw_buf[b] | (raw_buf[b + 1] << 8) | (raw_buf[b + 2] << 16) | (raw_buf[b + 3] << 24)
    elif PCM_EXTRACT_MODE == "be32_left24":
        x = raw_buf[b + 3] | (raw_buf[b + 2] << 8) | (raw_buf[b + 1] << 16) | (raw_buf[b] << 24)
    else:
        raise ValueError("unsupported PCM_EXTRACT_MODE")

    if x & 0x80000000:
        x -= 0x100000000
    return x


def _dc_block(x):
    global _prev_x, _prev_y
    y = x - _prev_x + ((DC_BLOCK_A_Q15 * _prev_y) >> 15)
    _prev_x = x
    _prev_y = y
    return y


def _apply_gain(y):
    if AUTO_GAIN:
        return (y * _auto_gain_q8) >> 8
    return (y * PCM_GAIN_NUM) // PCM_GAIN_DEN


def _update_auto_gain(peak):
    global _auto_gain_q8
    if not AUTO_GAIN:
        return

    if peak <= 0:
        return

    desired_q8 = (TARGET_PEAK << 8) // peak

    if desired_q8 > _auto_gain_q8:
        _auto_gain_q8 = min(_auto_gain_q8 + ATTACK_STEP_Q8, desired_q8)
    else:
        _auto_gain_q8 = max(_auto_gain_q8 - RELEASE_STEP_Q8, desired_q8)

    if _auto_gain_q8 < AUTO_GAIN_MIN_Q8:
        _auto_gain_q8 = AUTO_GAIN_MIN_Q8
    elif _auto_gain_q8 > AUTO_GAIN_MAX_Q8:
        _auto_gain_q8 = AUTO_GAIN_MAX_Q8


def pcm32_to_pcm16le(raw_buf, n_raw, out_buf):
    """将 I2S 读取到的 32-bit 原始数据转成 16-bit little-endian PCM。"""
    samples = n_raw // 4
    out_i = 0
    peak = 0
    clip_count = 0

    for i in range(samples):
        b = i * 4
        x = _read_i32(raw_buf, b)

        # left-justified 24-bit -> 24-bit signed
        x24 = x >> 8

        if ENABLE_DC_BLOCK:
            x24 = _dc_block(x24)

        y = x24 >> PCM_DOWN_SHIFT
        y = _apply_gain(y)

        if y > 32767:
            y = 32767
            clip_count += 1
        elif y < -32768:
            y = -32768
            clip_count += 1

        ay = y if y >= 0 else -y
        if ay > peak:
            peak = ay

        out_buf[out_i] = y & 0xFF
        out_buf[out_i + 1] = (y >> 8) & 0xFF
        out_i += 2

    _update_auto_gain(peak)
    return out_i, peak, clip_count, samples


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
            "bits": PCM_BITS,
            "channels": 1,
            "format": "pcm_s16le",
            "ts_ms": utime.ticks_ms(),
            "i2s_bits": I2S_BITS,
            "extract_mode": PCM_EXTRACT_MODE,
            "down_shift": PCM_DOWN_SHIFT,
            "gain": [PCM_GAIN_NUM, PCM_GAIN_DEN],
            "auto_gain": AUTO_GAIN,
            "dc_block": ENABLE_DC_BLOCK,
        }
        ws.send(ujson.dumps(start_msg))

        audio_in = build_i2s()
        raw_buf = bytearray(RAW_CHUNK_BYTES)
        pcm_buf = bytearray(PCM_CHUNK_BYTES)
        pcm_mv = memoryview(pcm_buf)

        meter_ts = utime.ticks_ms()
        meter_peak = 0
        meter_clip = 0
        meter_samples = 0

        while True:
            if not network.WLAN(network.STA_IF).isconnected():
                raise OSError("wifi disconnected")

            n = audio_in.readinto(raw_buf)
            if n is None or n <= 0:
                await asyncio.sleep_ms(5)
                continue

            out_n, peak, clip_count, samples = pcm32_to_pcm16le(raw_buf, n, pcm_buf)
            if out_n > 0:
                ws.send(pcm_mv[:out_n])

            if peak > meter_peak:
                meter_peak = peak
            meter_clip += clip_count
            meter_samples += samples

            # 每2秒打印一次峰值/削顶比例，便于现场调参
            if utime.ticks_diff(utime.ticks_ms(), meter_ts) > 2000:
                clip_permille = (meter_clip * 1000 // meter_samples) if meter_samples > 0 else 0
                print("[audio] peak16={} clip={}‰ mode={} shift={} agc={} agc_q8={}".format(
                    meter_peak,
                    clip_permille,
                    PCM_EXTRACT_MODE,
                    PCM_DOWN_SHIFT,
                    AUTO_GAIN,
                    _auto_gain_q8,
                ))
                meter_ts = utime.ticks_ms()
                meter_peak = 0
                meter_clip = 0
                meter_samples = 0

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
