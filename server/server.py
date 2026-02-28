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


def build_output_path(out_dir: Path, device: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S", time.localtime())
    safe_device = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in device)
    return out_dir / "{}_{}.wav".format(safe_device, ts)


def open_wav(path: Path, cfg):
    wf = wave.open(str(path), "wb")
    wf.setnchannels(cfg["channels"])
    wf.setsampwidth(cfg["bits"] // 8)
    wf.setframerate(cfg["sample_rate"])
    return wf


async def handle_ws(websocket, out_dir: Path):
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


async def run_server(host: str, port: int, out_dir: Path):
    async with websockets.serve(lambda ws: handle_ws(ws, out_dir), host, port, max_size=None):
        print("[server] listening on ws://{}:{}/ws/audio".format(host, port))
        await asyncio.Future()


def parse_args():
    parser = argparse.ArgumentParser(description="WebSocket audio ingestion server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--out", type=Path, default=Path("./recordings"))
    return parser.parse_args()


def main():
    args = parse_args()
    asyncio.run(run_server(args.host, args.port, args.out))


if __name__ == "__main__":
    main()