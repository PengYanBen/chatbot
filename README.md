# ESP32-S3 + MicroPython + INMP441 实时语音助手（WebSocket）

这个项目提供一个最小可运行的“类似小爱同学”的语音链路：

1. **ESP32-S3（MicroPython）** 从 **INMP441** 采集音频。
2. 通过 **WebSocket** 将 PCM 音频流实时上传到服务端。
3. 服务端按会话保存为 `.wav`，并可扩展接入 ASR / LLM / TTS。

> 目标是先打通稳定的实时音频链路，后续再叠加“唤醒词、语音识别、对话、大模型回复、语音播报”。

---

## 目录结构

- `esp32_client/main.py`：ESP32-S3 端采集 + WebSocket 上传
- `server/server.py`：Python WebSocket 服务端（接收音频并落盘）
- `server/requirements.txt`：服务端依赖

服务端脚本使用 Python 标准库（`argparse/json/time/wave/pathlib` 等）+ `websockets`，不再依赖 `dataclasses` 与 `datetime`。

---

## 一、硬件连接（INMP441 -> ESP32-S3）

INMP441 常见引脚：`VDD / GND / SD / SCK(BCLK) / WS(LRCL) / L/R`

示例连接（可按你的板子改）：

- `INMP441 VDD` -> `3V3`
- `INMP441 GND` -> `GND`
- `INMP441 SD` -> `GPIO4`（I2S data in）
- `INMP441 SCK` -> `GPIO5`（I2S BCLK）
- `INMP441 WS` -> `GPIO6`（I2S LRCLK）
- `INMP441 L/R` -> `GND`（左声道）

> 注意：不同开发板 I2S 引脚矩阵支持不同，如果无声或异常，请优先调整 `sck/ws/sd` 引脚。

---

## 二、ESP32-S3 端（MicroPython v1.26）

### 1) 准备依赖

`esp32_client/main.py` 使用了：

- `network`, `machine`, `uasyncio`
- `uwebsockets.client`（仓库已提供：`esp32_client/uwebsockets/client.py`，上传到设备后路径应为 `/uwebsockets/client.py`）

你可以通过 mpremote / Thonny 上传 `main.py` 和 `uwebsockets/` 目录。

### 2) 修改配置

在 `esp32_client/main.py` 顶部修改：

- `WIFI_SSID`, `WIFI_PASSWORD`
- `SERVER_WS_URL`（例如 `ws://192.168.1.20:8765/ws/audio?device=esp32-s3-01`）
- I2S 引脚配置

### 3) 运行

将 `main.py` 上传后复位开发板即可开始采集并上传。

---

## 三、服务端运行

### 1) 安装依赖

```bash
cd server
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2) 启动

```bash
python server.py --host 0.0.0.0 --port 8765 --out ./recordings --mode record
```

### 3) 结果

每个连接会生成一个 WAV 文件，路径类似：

`recordings/esp32-s3-01_20260101_120000.wav`

---

## 四、协议说明（当前版本）

客户端连接建立后：

1. 先发一条 JSON 文本消息（`type=start`）声明采样参数：

```json
{
  "type": "start",
  "sample_rate": 16000,
  "bits": 16,
  "channels": 1,
  "format": "pcm_s16le"
}
```

2. 持续发送二进制音频帧（PCM 16-bit little-endian）
3. 结束时发送：`{"type": "stop"}`

服务端会将二进制帧顺序写入 WAV。

---

## 五、如何扩展成“完整小爱同学链路”

建议按下面顺序迭代：

1. **VAD（语音活动检测）**：减少静音上传和服务端负载
2. **ASR**：如 faster-whisper / FunASR
3. **NLP / LLM**：意图识别 + 对话管理
4. **TTS**：返回语音流给 ESP32（可再开一个 WS 下行流）
5. **唤醒词**：板端或服务端实现“你好小X”

---

## 六、常见问题

1. **WS 连不上**
   - 检查局域网连通性、防火墙、端口
   - 确认 `SERVER_WS_URL` IP 不是 `127.0.0.1`

2. **声音失真/空白/听不清**
   - INMP441 建议按 `32-bit I2S` 采集，再转 `16-bit PCM` 上传（当前代码已内置）
   - 调整 `PCM_SHIFT_BITS`（推荐在 `15~17` 之间微调）
   - 调整增益 `PCM_GAIN_NUM/PCM_GAIN_DEN`（例如 1/1、2/1、3/2）避免过小或爆音
   - 核查接线和供电（`SCK/WS/SD` 任意错位都会出现噪声或断续）

3. **延迟大**
   - 减小 chunk 大小（如 20ms）
   - 服务端异步处理，不要阻塞事件循环


4. **反复出现 `ECONNABORTED`**
   - 新版本已增加 Wi-Fi 在线检查和指数退避重连（2s -> 20s）
   - 确认服务端地址可达：ESP32 与服务端必须在同一网段，且 `SERVER_WS_URL` 使用服务端实际局域网 IP
   - 路由器/热点信号弱时会频繁中断，建议先近距离测试并固定信道

### 音频清晰度调参建议（重点）

如果你能录到声音但“非常小、发闷、刺耳或像噪声”，优先调这两个参数：

- `PCM_SHIFT_BITS`：决定把 32-bit 原始样本右移多少位再变成 16-bit。
- `PCM_GAIN_NUM/PCM_GAIN_DEN`：数字增益。

建议顺序：

1. 先固定 `PCM_GAIN_NUM=1, PCM_GAIN_DEN=1`，只调 `PCM_SHIFT_BITS`（15/16/17）
2. 选出最不失真的 shift 后，再把增益调到合适响度（如 2/1）
3. 每次只改一个参数，录 5~10 秒 A/B 对比



---

## 七、如何做到“像小爱一样可打断、可重新提问”

你现在已经能把音频落到 `recordings/`。要做到“中间打断+重新提问”，核心是把录音改成**流式回合管理**：

1. **VAD 分段**：连续音频中检测“用户开始说话/说完了”
2. **ASR 实时识别**：每个回合转成文本
3. **LLM 生成回复**
4. **TTS 播放时监听打断**：如果检测到新一轮人声，立即停止当前播报（barge-in）

当前 `server.py` 已内置一个 `assistant` 示例模式：

```bash
python server.py --host 0.0.0.0 --port 8765 --out ./recordings --mode assistant
```

在这个模式下：

- 会持续保存全量原始音频 `raw_*.wav`
- VAD 检测到一个说话回合后，另存 `turn_*.wav`
- 回合结束后会触发示例 `fake_asr_and_llm()`（你要替换成真实 ASR/LLM）
- 服务端会通过 WebSocket 下发：
  - `{"type":"assistant_reply","text":"..."}`
  - 若用户在播报期间再次开口，会下发 `{"type":"barge_in"...}`

> 你只要在 ESP32 客户端补一个“下行消息处理 + TTS 播放控制”，收到 `barge_in` 就立刻停播，即可实现“小爱式打断”。

### 推荐你下一步替换的模块

- `fake_asr_and_llm()` -> Whisper/FunASR + 你的 LLM API
- 客户端增加 `recv` 协程：处理 `assistant_reply` / `barge_in`
- TTS 可先放服务端合成，返回 URL 或音频分片给客户端播放
