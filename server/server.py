import argparse
import asyncio
import json
import wave
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import websockets


@dataclass
class AudioConfig:
    sample_rate: int = 16000
    bits: int = 16
    channels: int = 1


def build_output_path(out_dir: Path, device: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_device = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in device)
    return out_dir / f"{safe_device}_{ts}.wav"


def open_wav(path: Path, cfg: AudioConfig):
    wf = wave.open(str(path), "wb")
    wf.setnchannels(cfg.channels)
    wf.setsampwidth(cfg.bits // 8)
    wf.setframerate(cfg.sample_rate)
    return wf


async def handle_ws(websocket, out_dir: Path):
    parsed = urlparse(websocket.request.path)
    query = parse_qs(parsed.query)
    device = query.get("device", ["unknown-device"])[0]

    cfg = AudioConfig()
    wf = None
    total_bytes = 0

    try:
        print(f"[connect] device={device} path={parsed.path}")
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
                    cfg = AudioConfig(
                        sample_rate=int(payload.get("sample_rate", cfg.sample_rate)),
                        bits=int(payload.get("bits", cfg.bits)),
                        channels=int(payload.get("channels", cfg.channels)),
                    )
                    out_path = build_output_path(out_dir, device)
                    wf = open_wav(out_path, cfg)
                    print(f"[start] device={device} -> {out_path} cfg={cfg}")
                elif msg_type == "stop":
                    print(f"[stop] device={device} total_bytes={total_bytes}")
                    break
                else:
                    print(f"[info] unknown text type={msg_type}")
            else:
                if wf is None:
                    # 如果客户端直接发二进制，按默认参数写入
                    out_path = build_output_path(out_dir, device)
                    wf = open_wav(out_path, cfg)
                    print(f"[implicit-start] device={device} -> {out_path} cfg={cfg}")

                wf.writeframes(message)
                total_bytes += len(message)

    except websockets.ConnectionClosed as e:
        print(f"[disconnect] device={device} code={e.code} reason={e.reason}")
    finally:
        if wf is not None:
            wf.close()
        print(f"[final] device={device} bytes={total_bytes}")


async def run_server(host: str, port: int, out_dir: Path):
    async with websockets.serve(lambda ws: handle_ws(ws, out_dir), host, port, max_size=None):
        print(f"[server] listening on ws://{host}:{port}/ws/audio")
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
