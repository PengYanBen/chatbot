import argparse
import asyncio
import json
import time
import wave
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import websockets


def default_audio_config():
    return {
        "sample_rate": 16000,
        "bits": 16,
        "channels": 1,
    }


def build_output_path(out_dir: Path, device: str, prefix: str = "") -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S", time.localtime())
    safe_device = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in device)
    name = "{}_{}{}.wav".format(safe_device, "{}".format(prefix) if prefix else "", ts)
    return out_dir / name


def open_wav(path: Path, cfg):
    wf = wave.open(str(path), "wb")
    wf.setnchannels(cfg["channels"])
    wf.setsampwidth(cfg["bits"] // 8)
    wf.setframerate(cfg["sample_rate"])
    return wf


def frame_rms_s16le(frame):
    # 输入为16-bit little-endian PCM
    samples = len(frame) // 2
    if samples <= 0:
        return 0
    acc = 0
    for i in range(0, samples * 2, 2):
        v = frame[i] | (frame[i + 1] << 8)
        if v & 0x8000:
            v -= 0x10000
        acc += v * v
    return int((acc // samples) ** 0.5)


class TurnDetector:
    def __init__(self, cfg, threshold=900, speech_frames=6, silence_frames=18):
        self.cfg = cfg
        self.threshold = threshold
        self.speech_frames = speech_frames
        self.silence_frames = silence_frames
        self._active = False
        self._speech_count = 0
        self._silence_count = 0

    def feed(self, frame):
        rms = frame_rms_s16le(frame)
        voiced = rms >= self.threshold

        event = None
        if not self._active:
            if voiced:
                self._speech_count += 1
                if self._speech_count >= self.speech_frames:
                    self._active = True
                    self._silence_count = 0
                    event = "turn_start"
            else:
                self._speech_count = 0
        else:
            if voiced:
                self._silence_count = 0
            else:
                self._silence_count += 1
                if self._silence_count >= self.silence_frames:
                    self._active = False
                    self._speech_count = 0
                    event = "turn_end"

        return event, rms


async def handle_ws_record(websocket, out_dir: Path):
    parsed = urlparse(websocket.request.path)
    query = parse_qs(parsed.query)
    device = query.get("device", ["unknown-device"])[0]

    cfg = default_audio_config()
    wf = None
    total_bytes = 0

    try:
        print("[connect] device={} path={}".format(device, parsed.path))
        if parsed.path != "/ws/audio":
            await websocket.close(code=1008, reason="unsupported path")
            return

        async for message in websocket:
            if isinstance(message, str):
                try:
                    payload = json.loads(message)
                except json.JSONDecodeError:
                    print("[warn] invalid json text message")
                    continue

                msg_type = payload.get("type")
                if msg_type == "start":
                    cfg = {
                        "sample_rate": int(payload.get("sample_rate", cfg["sample_rate"])),
                        "bits": int(payload.get("bits", cfg["bits"])),
                        "channels": int(payload.get("channels", cfg["channels"])),
                    }
                    out_path = build_output_path(out_dir, device)
                    wf = open_wav(out_path, cfg)
                    print("[start] device={} -> {} cfg={}".format(device, out_path, cfg))
                elif msg_type == "stop":
                    print("[stop] device={} total_bytes={}".format(device, total_bytes))
                    break
                else:
                    print("[info] unknown text type={}".format(msg_type))
            else:
                if wf is None:
                    out_path = build_output_path(out_dir, device)
                    wf = open_wav(out_path, cfg)
                    print("[implicit-start] device={} -> {} cfg={}".format(device, out_path, cfg))

                wf.writeframes(message)
                total_bytes += len(message)

    except websockets.ConnectionClosed as e:
        print("[disconnect] device={} code={} reason={}".format(device, e.code, e.reason))
    finally:
        if wf is not None:
            wf.close()
        print("[final] device={} bytes={}".format(device, total_bytes))


async def fake_asr_and_llm(turn_path: Path):
    # 这里是示例占位：你可以替换成 whisper/funasr + LLM 调用
    await asyncio.sleep(0.15)
    user_text = "（示例）识别到一段语音"
    assistant_text = "（示例）好的，我在。你可以继续问。"
    return user_text, assistant_text


async def handle_ws_assistant(websocket, out_dir: Path):
    parsed = urlparse(websocket.request.path)
    query = parse_qs(parsed.query)
    device = query.get("device", ["unknown-device"])[0]

    cfg = default_audio_config()
    detector = TurnDetector(cfg)

    raw_wav = None
    turn_wav = None
    in_turn = False
    tts_playing = False

    try:
        if parsed.path != "/ws/audio":
            await websocket.close(code=1008, reason="unsupported path")
            return

        raw_path = build_output_path(out_dir, device, prefix="raw_")
        raw_wav = open_wav(raw_path, cfg)
        print("[assistant-connect] device={} raw={}".format(device, raw_path))

        async for message in websocket:
            if isinstance(message, str):
                payload = json.loads(message)
                if payload.get("type") == "start":
                    cfg = {
                        "sample_rate": int(payload.get("sample_rate", cfg["sample_rate"])),
                        "bits": int(payload.get("bits", cfg["bits"])),
                        "channels": int(payload.get("channels", cfg["channels"])),
                    }
                    detector = TurnDetector(cfg)
                    if raw_wav is not None:
                        raw_wav.close()
                    raw_path = build_output_path(out_dir, device, prefix="raw_")
                    raw_wav = open_wav(raw_path, cfg)
                    print("[assistant-start] cfg={}".format(cfg))
                elif payload.get("type") == "stop":
                    break
                continue

            frame = message
            if raw_wav is not None:
                raw_wav.writeframes(frame)

            event, rms = detector.feed(frame)

            if event == "turn_start":
                in_turn = True
                turn_path = build_output_path(out_dir, device, prefix="turn_")
                turn_wav = open_wav(turn_path, cfg)
                print("[turn-start] device={} rms={}".format(device, rms))

                if tts_playing:
                    # 小爱式打断关键：用户开口时，立即中断当前播报
                    await websocket.send(json.dumps({"type": "barge_in", "reason": "user_speaking"}))
                    tts_playing = False

            if in_turn and turn_wav is not None:
                turn_wav.writeframes(frame)

            if event == "turn_end" and in_turn:
                in_turn = False
                if turn_wav is not None:
                    turn_wav.close()
                    turn_wav = None

                user_text, assistant_text = await fake_asr_and_llm(turn_path)
                print("[asr] {}".format(user_text))
                print("[llm] {}".format(assistant_text))

                # 这里给客户端发控制信号。客户端若实现下行播放，即可做到可打断对话。
                await websocket.send(json.dumps({"type": "assistant_reply", "text": assistant_text}))
                tts_playing = True

    except websockets.ConnectionClosed as e:
        print("[assistant-disconnect] device={} code={} reason={}".format(device, e.code, e.reason))
    finally:
        if turn_wav is not None:
            turn_wav.close()
        if raw_wav is not None:
            raw_wav.close()


def build_handler(mode, out_dir):
    if mode == "assistant":
        return lambda ws: handle_ws_assistant(ws, out_dir)
    return lambda ws: handle_ws_record(ws, out_dir)


async def run_server(host: str, port: int, out_dir: Path, mode: str):
    handler = build_handler(mode, out_dir)
    async with websockets.serve(handler, host, port, max_size=None):
        print("[server] mode={} listening on ws://{}:{}/ws/audio".format(mode, host, port))
        await asyncio.Future()


def parse_args():
    parser = argparse.ArgumentParser(description="WebSocket audio ingestion server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--out", type=Path, default=Path("./recordings"))
    parser.add_argument("--mode", choices=["record", "assistant"], default="record")
    return parser.parse_args()


def main():
    args = parse_args()
    asyncio.run(run_server(args.host, args.port, args.out, args.mode))


if __name__ == "__main__":
    main()