---
name: dumb-english-2-chinese
description: 英文视频翻译为中文配音视频的全自动流水线。输入英文视频/音频，输出中文配音视频；也支持只提供视频，并按时间区间从视频截取讲者参考音频。使用 faster-whisper 提取英文字幕，VoxCPM2 音色克隆生成中文语音，ffmpeg 视频变速同步合成。当用户要求翻译英文视频、给视频配中文音、视频翻译配音时触发此技能。
---

# Dumb English-to-Chinese Video Translation Skill

将英文视频自动翻译为中文配音视频的全流水线。核心理念：**以中文音频为主时间轴，视频分段变速适配音频**。

## 前置依赖

| 组件 | 要求 |
|------|------|
| VoxCPM2 模型 | `<voxcpm2-runtime>\models\VoxCPM2` |
| VoxCPM2 Python 运行时 | `<voxcpm2-runtime>\python\python.exe` |
| faster-whisper | 需要在工作目录下创建 `.venv` 并安装 `faster-whisper` |
| ffmpeg | 系统 PATH 中需要有 ffmpeg |
| GPU | NVIDIA GPU + CUDA（VoxCPM2 需要约 8GB VRAM）|
| 默认参考音频 | `<project>\参考音频.mp3` |
| 参考音频截取 | 可由用户提供参考音频，或按用户给定的视频时间区间从视频截取；短参考音频（≤15 秒）整段转为 16kHz 单声道 WAV，长参考音频默认取前 12 秒 |

## Codex 默认约定

- Codex 执行本 skill 时，默认输出目录使用输入视频所在项目目录下的 `output-codex/`，不要使用 Antigravity 常用的 `output/`，避免互相覆盖。例如 `<project>\output-codex`。
- 主要脚本会创建 `.lock` 文件防止同一输出目录被并发写入；如果进程崩溃后确认没有流水线仍在运行，可以手动删除对应 `.lock` 后重试。
- 流程开始时先向用户确认：这次视频是否是 Andrew Wommack（用户也可能写作 Andrew Walmack）本人的视频？
  - 如果是，使用默认参考音频：`<project>\参考音频.mp3`
  - 如果不是，优先让用户提供新的参考音频路径；如果用户只提供视频和参考时间区间（如 `00:10` 到 `00:23`），先运行 Step 0 从视频截取参考音频，再继续 Step 3。
  - 如果用户已经在本轮明确提供了参考音频路径，则直接使用该路径，不再重复确认。
- 除开始确认讲者/参考音频外，中途默认自动执行到底；只有文件缺失、SRT 无法解析、翻译校验失败或外部命令失败时才暂停询问用户。

## 流程概览（6 步）

```
Step 0:  只给视频时，视频 → 完整英文音频 + 参考音频片段 (ffmpeg, 自动)
Step 1:  英文音频 → 分段 STT → 合并英文 SRT (faster-whisper, 自动续跑)
Step 1b: 英文 SRT → 合并短句 SRT           (脚本合并, 自动)
Step 2:  合并后英文 SRT → 中文 SRT        (LLM 自动翻译, 自动)
Step 3:  中文 SRT → 逐句中文音频 WAV      (VoxCPM2 克隆, 自动)
Step 4:  校验 + 中文时间轴 + 视频变速合成 → 最终视频 (ffmpeg, 自动)
```

## 详细执行步骤

### Step 0: 环境准备

1. 确认用户提供的输入文件：
   - 英文视频文件 (`.mp4`)
   - 英文音频文件 (`.mp3`，如果没有则用 Step 0 脚本从视频提取)
   - 参考音频文件，或参考音频在视频中的时间区间（如 `00:10` 到 `00:23`）

2. 在输入视频所在项目目录下创建 `output-codex/` 子目录；后文 `<output_dir>` 默认指该目录，例如 `<project>\output-codex`

3. 如果用户只提供视频，先运行 `scripts/step0_prepare_media.py`：
   ```powershell
   python '-u' '<skill_scripts_dir>\step0_prepare_media.py' '<video_path>' '<output_dir>' '00:10' '00:23'
   ```
   - 输出完整音频：`<output_dir>\source_audio.mp3`，用于 Step 1 转写。
   - 如果提供了参考区间，输出参考音频：`<output_dir>\reference_audio.mp3`，用于 Step 3 音色克隆。
   - 时间区间用 ffmpeg 的 `-ss <start> -to <end>` 截取；例如 `00:10` 到 `00:23` 会得到 13 秒参考音频。
   - 如果用户只给视频、没有给参考区间，应先询问参考音频区间；不要随意从视频中猜一段参考音频。

4. 确认 faster-whisper 环境：
   - 如果工作目录下没有 `.venv`，创建一个并安装 faster-whisper：
     ```powershell
     python -m venv .venv
     .\.venv\Scripts\pip.exe install faster-whisper
     ```

### Step 1: 分段提取英文 SRT

运行 `scripts/step1_transcribe.py`：

```powershell
& '<work_dir>\.venv\Scripts\python.exe' '-u' '<skill_scripts_dir>\step1_transcribe.py' '<mp3_path>' '<output_dir>\english.srt' 'medium' 4 3
```

- 使用 faster-whisper `medium` 模型，语言设为 `en`
- 如果用户只提供视频，`<mp3_path>` 使用 Step 0 生成的 `<output_dir>\source_audio.mp3`
- 默认把长音频切成 4 段，每段前后加 3 秒 overlap；也可以把 chunk 数设置为 3-5
- 每段会缓存到 `<output_dir>\_step1_chunks\chunk_XX.json`，中断或卡住后只重跑未完成分段，不要从头重跑整条音频
- 合并 overlap 时按“字幕片段 midpoint 属于哪个核心窗口”归属；核心窗口连续覆盖全片，避免重叠区域重复或漏掉
- Step 1 输出完整 `english.srt` 后，再执行 Step 1b 的短句合并
- 输出：`output-codex/english.srt`

### Step 1b: 合并短句

运行 `scripts/step1b_merge_srt.py`：

```powershell
& '<work_dir>\.venv\Scripts\python.exe' '-u' '<skill_scripts_dir>\step1b_merge_srt.py' '<output_dir>\english.srt' '<output_dir>\english_merged.srt'
```

**合并规则**：
- 相邻两句间隔 < 1.5 秒 → 合并候选
- 合并后总字符数 < 200 → 允许合并
- 连续合并直到不满足条件

输出：`output-codex/english_merged.srt`

### Step 2: 自动翻译

不要暂停等待用户手动翻译。读取 `<output_dir>\english_merged.srt`，由当前 agent 的 LLM 自动翻译为中文 SRT，并保存为 `<output_dir>\chinese.srt`。

自动翻译提示词：

```text
你是专业英文视频中文配音翻译。请把下面的英文 SRT 翻译成适合中文配音朗读的简体中文 SRT。

硬性要求：
1. 必须保持 SRT 段数、序号、时间戳完全不变。
2. 只翻译每个字幕块的正文文本，不改序号，不改时间戳。
3. 输出只能是完整 SRT 内容，不要 Markdown，不要解释，不要额外说明。
4. 中文要自然口语化，适合 TTS 朗读；避免书面腔和机器翻译腔。
5. 尽量简洁，不要明显扩写，避免中文音频过长导致视频变速压力过大。
6. 保留人名、地名、书名、经文章节等专有名词的常见中文译法；没有确定译法时保留英文或音译。
7. 不要添加原文没有的信息，不要删减关键信息。
```

执行要求：
- 如果 `english_merged.srt` 很长，分批翻译，但最终必须拼回一个完整的 `chinese.srt`。
- 分批时不得重排、跳号、合并或拆分字幕块。
- 写入 `chinese.srt` 后，快速检查段数、序号、时间戳是否与 `english_merged.srt` 一致；不一致时自动修正后再继续。
- 写入 `chinese.srt` 后运行 SRT 校验脚本：
  ```powershell
  python '-u' '<skill_scripts_dir>\validate_srt_pair.py' '<output_dir>\english_merged.srt' '<output_dir>\chinese.srt'
  ```
- 中途不要向用户确认翻译。只有在 SRT 无法解析、翻译后连续校验失败、或用户明确要求人工审阅时才暂停。

### Step 3: VoxCPM2 生成中文音频

运行 `scripts/step3_generate_chinese.py`：

```powershell
& '<voxcpm2-runtime>\python\python.exe' '-u' '<skill_scripts_dir>\step3_generate_chinese.py' '<project>\参考音频.mp3' '<output_dir>\chinese.srt' '<output_dir>\audio_segments' '<output_dir>\reference_voice.wav'
```

**流程**：
1. 使用用户提供的参考音频作为 VoxCPM2 音色克隆参考；默认使用 `<project>\参考音频.mp3`
2. 规范化参考音频为 16kHz 单声道 WAV，保存到 `output-codex/reference_voice.wav`；如果参考音频 ≤15 秒则整段使用，超过 15 秒则默认取前 12 秒
3. 启动 VoxCPM2 Server Mode（使用 Oopii 内置 Python 环境）
4. Warmup 模型（约 60 秒）
5. 逐句调用 `generate_voice_clone`，生成 WAV 文件
6. 支持断点续传（已生成的段落会跳过）
7. 送入 VoxCPM2 前会清理 TTS 文本中的英文干扰，但不改写 `chinese.srt`。例如 `杰米（Jamie）`、`斯普林斯（Springs）` 会作为 `杰米`、`斯普林斯` 送入 TTS；残留英文词也会在含中文句子里被移除，降低克隆声音突然读英文导致的违和。

输出：`output-codex/audio_segments/001.wav`, `002.wav`, ...

**关键参数**：
- `cfgValue`: 2.0
- `inferenceTimesteps`: 10
- 参考音频：优先使用用户提供的参考音频；如果用户只提供视频和明确时间区间，则用 Step 0 从原视频截取该区间作为参考音频。短参考音频（≤15 秒）整段使用；长参考音频取前 12 秒。
- 12 秒是推荐区间 3-15 秒内的平衡值：通常比 3-5 秒更稳，又避免过长参考音频拖慢 VoxCPM2 推理

### Step 4: 视频变速合成

运行 `scripts/step4_compose_video.py`：

```powershell
python '-u' '<skill_scripts_dir>\step4_compose_video.py' '<video_path>' '<output_dir>\english_merged.srt' '<output_dir>\chinese.srt' '<output_dir>\audio_segments' '<output_dir>'
```

Step 4 会先做严格 1:1 校验：
- `english_merged.srt` 和 `chinese.srt` 必须都是连续的 `1..N` 索引
- 英文段数、中文段数、`audio_segments/NNN.wav` 数量必须完全一致
- 缺失或多余的 WAV 会直接报错停止，不生成半成品
- 也可以在 Step 4 前手动运行完整校验：
  ```powershell
  python '-u' '<skill_scripts_dir>\validate_srt_pair.py' '<output_dir>\english_merged.srt' '<output_dir>\chinese.srt' '<output_dir>\audio_segments'
  ```

**核心算法**：

```
对于每个字幕段落 i:
  intervalA = english_srt[i].end - english_srt[i].start  （原视频段时长）
  audio_dur = wav[i] 的实际时长
  intervalB = audio_dur + 0.5s gap                        （最后一段不加 gap）
  speed_ratio = intervalA / intervalB
  speed_ratio = clamp(speed_ratio, 0.6, 1.8)              （限幅防违和）
  视频段先按 speed_ratio 变速，再 trim/pad 到 intervalB，避免限幅后漂移
```

**处理流程**：
1. 严格校验英文 SRT、中文 SRT、逐句 WAV 的 1:1 对应关系
2. 按逐句 WAV 时长累积生成 `chinese_timeline.srt`（句间 0.5 秒，最后一句后不补 gap）
3. 按英文 SRT 时间戳切分原始视频为 N 段
4. 每段用 `setpts=PTS*(1/speed)` 变速
5. 对限幅造成的长度差异执行 `trim` 或 `tpad=clone`，把视频段精确对齐中文时间槽
6. 拼接所有对齐后的视频段
7. 拼接所有中文音频（段间 0.5 秒）
8. 合并视频轨 + 音频轨

**保留非字幕空白片段**：
- 默认保持稳定路径：只按英文 SRT 的字幕窗口切分并变速，非字幕空白片段不进入最终视频。
- 如果原视频的无字幕空白包含重要画面，运行 Step 4 前设置 `PRESERVE_NON_SUBTITLE_GAPS=1`。脚本会把原视频无字幕区间作为 1x 静音视频/音频片段插入，并把 `chinese_timeline.srt` 的时间轴顺延。
- 开启该模式会让最终视频更接近原片时长，但可能保留长停顿；普通讲道/访谈翻译默认不开启。

输出：
- `output-codex/final_translated.mp4`（最终翻译视频）
- `output-codex/chinese_full.mp3`（完整中文音频）
- `output-codex/chinese_timeline.srt`（按中文音频累计时间生成的新中文字幕）

## 关键设计决策

1. **音频为准**：中文音频决定时间线，视频适配。不是视频为准硬塞音频。
2. **SRT 合并**：减少碎片化变速，增加翻译上下文。
3. **变速限幅 0.6x~1.8x**：太慢或太快都违和。
4. **限幅后不漂移**：如果变速被限制到 0.6x~1.8x，视频段会被裁剪或冻结最后一帧补齐到中文时间槽。
5. **句间 0.5 秒停顿**：自然语流节奏；最后一段后不额外补停顿。
6. **断点续传**：Step 3 生成的 WAV 如果已存在则跳过，方便中断后继续。
7. **段数 1:1 对应**：英文 N 段 → 中文 N 段 → WAV N 段，任何缺失或多余都会在 Step 4 直接报错。
8. **Codex 与 Antigravity 隔离**：Codex 默认使用 `output-codex/`，不要写入 `output/`。
9. **参考音频截取**：VoxCPM2 使用用户给定参考音频的前 12 秒，不再使用完整参考音频，也不从待翻译视频中截取。
10. **TTS 英文清理**：中文 SRT 可以保留英文括注或术语，但送入 VoxCPM2 前要剥离英文干扰，优先保证中文配音自然。
11. **并发保护**：重步骤使用 lock 文件保护输出目录，避免两个 agent 或两次运行同时写同一批中间文件。
12. **空白片段策略**：默认不保留非字幕空白以保证节奏紧凑；遇到重要无字幕画面时再显式开启 `PRESERVE_NON_SUBTITLE_GAPS=1`。
