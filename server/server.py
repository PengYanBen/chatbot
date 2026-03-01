import argparse
import asyncio
import json
import logging
import time
import wave
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import websockets


def build_ws_logger():
    logger = logging.getLogger("websockets.server")
    logger.setLevel(logging.ERROR)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("[ws] %(levelname)s: %(message)s"))
        logger.addHandler(handler)
    return logger


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
    def __init__(self, threshold=900, speech_frames=6, silence_frames=18):
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

        return event, rms, voiced


class TurnStats:
    def __init__(self):
        self.total_frames = 0
        self.voiced_frames = 0
        self.max_rms = 0
        self.rms_sum = 0

    def add(self, rms, voiced):
        self.total_frames += 1
        self.rms_sum += rms
        if voiced:
            self.voiced_frames += 1
        if rms > self.max_rms:
            self.max_rms = rms

    @property
    def mean_rms(self):
        if self.total_frames <= 0:
            return 0
        return self.rms_sum // self.total_frames

    @property
    def voiced_ratio(self):
        if self.total_frames <= 0:
            return 0.0
        return self.voiced_frames / self.total_frames


class WhisperASR:
    def __init__(self, model_name="base", language="zh", no_speech_threshold=0.6, logprob_threshold=-1.0):
        self.model_name = model_name
        self.language = language
        self.no_speech_threshold = no_speech_threshold
        self.logprob_threshold = logprob_threshold

        self._model = None
        self.enabled = False
        self._load_error = None

        try:
            import whisper  # type: ignore

            self._model = whisper.load_model(model_name)
            self.enabled = True
            print("[asr] whisper loaded model={}".format(model_name))
        except Exception as e:
            self._load_error = str(e)
            self.enabled = False
            print("[asr] whisper unavailable, fallback mode. error={}".format(self._load_error))

    def transcribe(self, wav_path: Path):
        if not self.enabled:
            return ""

        result = self._model.transcribe(
            str(wav_path),
            language=self.language,
            task="transcribe",
            fp16=False,
            temperature=0.0,
            condition_on_previous_text=False,
            no_speech_threshold=self.no_speech_threshold,
            logprob_threshold=self.logprob_threshold,
            compression_ratio_threshold=2.4,
            initial_prompt="这是中文家庭语音助手场景，尽量忽略环境噪音，仅输出清晰人声内容。",
        )
        text = (result.get("text") or "").strip()
        return text


def local_llm_reply(user_text):
    if not user_text:
        return "我没听清楚，你可以再说一遍。"

    text = user_text.lower()
    if "几点" in user_text or "time" in text:
        return "现在时间是 {}。".format(time.strftime("%H:%M", time.localtime()))
    if "天气" in user_text:
        return "我现在没有联网天气插件，但我可以先帮你记录问题。"
    if "你是谁" in user_text:
        return "我是你的 ESP32 语音助手，基于 Whisper 识别。"
    return "收到：{}".format(user_text)


def should_drop_turn(stats: TurnStats, cfg, min_turn_ms=350, min_voiced_ratio=0.35, min_peak_rms=1200):
    frame_ms = 20
    turn_ms = stats.total_frames * frame_ms
    too_short = turn_ms < min_turn_ms
    too_unvoiced = stats.voiced_ratio < min_voiced_ratio
    too_quiet = stats.max_rms < min_peak_rms
    return too_short or too_unvoiced or too_quiet, {
        "turn_ms": turn_ms,
        "voiced_ratio": round(stats.voiced_ratio, 3),
        "max_rms": stats.max_rms,
        "mean_rms": stats.mean_rms,
        "too_short": too_short,
        "too_unvoiced": too_unvoiced,
        "too_quiet": too_quiet,
    }


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


async def handle_ws_assistant(websocket, out_dir: Path, asr: WhisperASR):
    parsed = urlparse(websocket.request.path)
    query = parse_qs(parsed.query)
    device = query.get("device", ["unknown-device"])[0]

    cfg = default_audio_config()
    detector = TurnDetector()

    raw_wav = None
    turn_wav = None
    turn_path = None
    in_turn = False
    tts_playing = False
    turn_stats = TurnStats()

    try:
        if parsed.path != "/ws/audio":
            await websocket.close(code=1008, reason="unsupported path")
            return

        raw_path = build_output_path(out_dir, device, prefix="raw_")
        raw_wav = open_wav(raw_path, cfg)
        print("[assistant-connect] device={} raw={}".format(device, raw_path))

        async for message in websocket:
            if isinstance(message, str):
                try:
                    payload = json.loads(message)
                except json.JSONDecodeError:
                    print("[warn] invalid json text message in assistant mode")
                    continue
                msg_type = payload.get("type")

                if msg_type == "start":
                    cfg = {
                        "sample_rate": int(payload.get("sample_rate", cfg["sample_rate"])),
                        "bits": int(payload.get("bits", cfg["bits"])),
                        "channels": int(payload.get("channels", cfg["channels"])),
                    }
                    if raw_wav is not None:
                        raw_wav.close()
                    raw_path = build_output_path(out_dir, device, prefix="raw_")
                    raw_wav = open_wav(raw_path, cfg)
                    print("[assistant-start] cfg={}".format(cfg))
                elif msg_type == "stop":
                    break
                continue

            frame = message
            if raw_wav is not None:
                raw_wav.writeframes(frame)

            event, rms, voiced = detector.feed(frame)

            if event == "turn_start":
                in_turn = True
                turn_stats = TurnStats()
                turn_path = build_output_path(out_dir, device, prefix="turn_")
                turn_wav = open_wav(turn_path, cfg)
                print("[turn-start] device={} rms={} path={}".format(device, rms, turn_path))

                if tts_playing:
                    await websocket.send(json.dumps({"type": "barge_in", "reason": "user_speaking"}))
                    tts_playing = False

            if in_turn:
                turn_stats.add(rms, voiced)
                if turn_wav is not None:
                    turn_wav.writeframes(frame)

            if event == "turn_end" and in_turn:
                in_turn = False
                if turn_wav is not None:
                    turn_wav.close()
                    turn_wav = None

                drop, info = should_drop_turn(turn_stats, cfg)
                if drop:
                    print("[turn-drop] device={} info={}".format(device, info))
                    await websocket.send(json.dumps({"type": "asr_skipped", "reason": "noise", "meta": info}))
                    continue

                await websocket.send(json.dumps({"type": "asr_status", "status": "processing"}))

                if asr.enabled and turn_path is not None:
                    user_text = await asyncio.to_thread(asr.transcribe, turn_path)
                else:
                    user_text = ""

                if not user_text:
                    user_text = "（识别失败或未安装 whisper）"

                assistant_text = local_llm_reply(user_text)

                print("[asr] {}".format(user_text))
                print("[assistant] {}".format(assistant_text))

                await websocket.send(json.dumps({"type": "asr_result", "text": user_text}))
                await websocket.send(json.dumps({"type": "assistant_reply", "text": assistant_text}))
                tts_playing = True

    except websockets.ConnectionClosed as e:
        print("[assistant-disconnect] device={} code={} reason={}".format(device, e.code, e.reason))
    finally:
        if turn_wav is not None:
            turn_wav.close()
        if raw_wav is not None:
            raw_wav.close()


def build_handler(mode, out_dir, asr):
    if mode == "assistant":
        return lambda ws: handle_ws_assistant(ws, out_dir, asr)
    return lambda ws: handle_ws_record(ws, out_dir)


async def run_server(host: str, port: int, out_dir: Path, mode: str, asr: WhisperASR):
    handler = build_handler(mode, out_dir, asr)
    ws_logger = build_ws_logger()

    async with websockets.serve(handler, host, port, max_size=None, logger=ws_logger):
        print("[server] mode={} listening on ws://{}:{}/ws/audio".format(mode, host, port))
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            print("[server] shutdown requested")


def parse_args():
    parser = argparse.ArgumentParser(description="WebSocket audio ingestion server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--out", type=Path, default=Path("./recordings"))
    parser.add_argument("--mode", choices=["record", "assistant"], default="record")
    parser.add_argument("--asr", choices=["none", "whisper"], default="whisper")
    parser.add_argument("--whisper-model", default="base")
    parser.add_argument("--whisper-language", default="zh")
    parser.add_argument("--whisper-no-speech-threshold", type=float, default=0.6)
    parser.add_argument("--whisper-logprob-threshold", type=float, default=-1.0)
    return parser.parse_args()


def main():
    args = parse_args()

    asr = WhisperASR(
        model_name=args.whisper_model,
        language=args.whisper_language,
        no_speech_threshold=args.whisper_no_speech_threshold,
        logprob_threshold=args.whisper_logprob_threshold,
    )
    if not (args.asr == "whisper" and args.mode == "assistant"):
        asr.enabled = False

    try:
        asyncio.run(run_server(args.host, args.port, args.out, args.mode, asr))
    except KeyboardInterrupt:
        print("[server] stopped by keyboard interrupt")


if __name__ == "__main__":
    main()
