# 本地编辑计划：字段与示例

入口为 `helpers/edit_media.py`，流程与正确性要求见 [local-editing.md](local-editing.md)。
JSON 未知字段、非法时间、工作区外文件和不存在的素材均会报错。所有时间单位为秒，像素单位为整数。

## 最小计划

```json
{
  "version": 1,
  "sources": {"main": "sources/main.mp4"},
  "clips": [{"source": "main", "start": 0, "end": 8}],
  "output": {"width": 1280, "height": 720, "fps": 30}
}
```

`sources` 是别名到本地文件的映射，最多 50 个，支持音视频。
`clips` 是顺序片段或分屏视图，最多 100 个。相对路径相对于 `--workspace`。
`transcripts` 可选：`{"main":"transcripts/真实缓存文件.json"}`，格式为原 Scribe 逐词 JSON。
提供缓存后，所有片段端点都检查是否切进单词；不会调用 ASR。缓存必须对应所选音轨。

## clips 中每段的字段

| 字段 | 默认值／约束 |
| --- | --- |
| `source` | 必填，来自 `sources` |
| `start`, `end` | 默认整段；结束必须晚于开始，且不超过源时长 |
| `words` | 可替代 start/end；`[0, 8]` 包括首末词，只统计 `type: word`；须提供该源缓存 |
| `padding` | words 模式默认 0.08，范围 0.03–0.2；边缘限制在素材和相邻词的空隙内 |
| `audio_track` | 0，相对于音频轨道的序号 |
| `speed` | 1，范围 0.25–4，使用保持音调的音频变速 |
| `reverse` | false；每段原始范围最多 60 秒 |
| `reverse_audio` | false；需 reverse=true，否则倒放画面默认静音 |
| `freeze_end` | 0，追加末帧与静音，最多 30 秒 |
| `repeat` | 1–20，重复包含定格在内的整段 |
| `crop` | `[x,y,width,height]`；显示方向修正后的源坐标，均为偶数，不越界；旋转/镜像前执行 |
| `rotate` | 0、90、180、270，顺时针 |
| `flip` | none、horizontal、vertical；旋转后执行 |
| `fit` | contain：留黑边完整保留；cover：居中裁满；blur：完整主体＋模糊背景 |
| `mute` | false；true 静音该片段 |
| `volume` | 1，范围 0–4，作用于该段原声 |
| `grade` | none、subtle、neutral_punch、warm_cinematic，复用原调色脚本 |

在输出中间定格：把源拆成前后两段，对前段设置 `freeze_end`。
多段不同速度实现分段变速；这不是连续变速曲线。

## output、布局与转场

- `output.width/height`：默认首素材显示尺寸；偶数，16–4096。
- `output.fps`：默认 30，整数 1–60；此辅助工具不接受分数帧率，原 `render.py` 仍支持。
- `output.crf`：默认 20，12–35，越小质量越高、体积越大。
- `output.audio_only`：默认 false。视频 `format` 为 mp4；纯音频为 wav（默认）、mp3 或 m4a。
- `layout`：sequence（默认）、horizontal（左右两段）、vertical（上下两段）、grid（四段）。
- 分屏各格等大，相应画布维度须能被 4 整除；以最短段结束。
- `split_audio`：默认 0，保留第几个视图的音轨；配乐在分屏之后处理。
- `transition`：默认 `{"type":"cut","duration":0}`。支持 fade、dissolve、wipeleft、wiperight、slideleft、slideright、fadeblack。
- 非硬切转场默认 0.4 秒，最多 3 秒且不超过最短段的一半；至少两段，仅 sequence；音频同步交叉淡化。
- 转场占用两段重叠时间，不额外增加片长。`repeat` 内部的重复边界仍然硬切。

## 音频

顶层 `audio` 可省略。每段原声先处理，之后才运行这里的全片音频处理。

| 字段 | 含义 |
| --- | --- |
| `source` | 可选，另一份含音轨的素材别名；视频中的音轨也能使用 |
| `mode` | mix（默认）保留原声并混音；replace 仅保留新音轨 |
| `audio_track` | 新音轨序号，默认 0 |
| `start/end` | 新音轨使用的源范围，默认整段 |
| `offset` | 在成片上延迟几秒开始，默认 0 |
| `loop` | false；循环所选 start/end 范围，补足到成片结束 |
| `volume` | 有 source 时为新音轨音量，默认 0.2；无 source 时为成片原声音量，默认 1；范围 0–4 |
| `duck` | false；仅 mix，有原声音量时自动压低新音轨，原声安静时恢复 |
| `denoise` | false；FFT 底噪削减，作用于替换/混合后的音频 |
| `highpass` | 0 表示关闭；最高 300 Hz，过滤低频嗡声 |
| `fade_in/fade_out` | 全片首尾淡化，各默认 0.03，最多半个片长 |

混合后使用峰值限制器避免直接削波，不等同于响度归一化。需要严格响度目标时复用原 `render.py` 两遍 loudnorm。
每段仍有 30ms 切点淡化。输出音频统一 48kHz、双声道，视频中无声素材补静音轨。

## 遮挡、标题、字幕

`masks` 最多 30 项，坐标基于**最终输出**，每项：

```json
{"x": 40, "y": 30, "width": 240, "height": 80,
 "start": 1, "end": 5, "mode": "pixelate"}
```

mode 支持 pixelate、blur、black。默认为 pixelate。start/end 默认覆盖整个成片。矩形坐标和尺寸均须为偶数，避免色度采样对裁剪区域取整。

`titles` 最多 30 项，每项：

```json
{"text": "寻找旅伴", "position": "bottom", "size": 48,
 "color": "#FFFFFF", "background": true, "start": 0, "end": 8}
```

position 为 top、center、bottom；文本最多 500 字，支持换行，超宽自动折行。颜色必须为 #RRGGBB。
字号默认随输出高度变化，可设置 10–200。自动选择已安装的中文字库（优先 Noto Sans CJK、文泉驿正黑），缺少字库时明确报错。过高标题会报错，需减少字数/字号或分时显示。

`subtitles` 二选一：

```json
{"from_transcripts": true, "words_per_line": 6,
 "size": 36, "color": "#FFFFFF", "highlight": "#FFE066",
 "position": "bottom", "outline": 2}
```

```json
{"file": "edit/edited-output.srt", "size": 36, "color": "#FFFFFF",
 "position": "bottom", "outline": 2}
```

缓存模式自动重排输出时间，按词数、说话人和停顿分组；保留原文大小写。
highlight 是 ASS 卡拉 OK 式逐词填色，当前句已说过的词保留高亮。仅缓存模式可用。
手工 SRT 必须是 UTF-8，采用成片时间，双语可写成同一条目的两行；超过成片时长将被拒绝。
字幕最后合成，并一并导出 SRT 和 ASS。倒放与分屏不自动套用缓存字幕，使用成片时间 SRT。

## 可组合的示例

以下片段是在最小计划上替换/增加字段，源文件名需要换成真实素材。

### 两倍速接慢放，叠化转场

```json
{
  "version": 1,
  "sources": {"main": "sources/main.mp4"},
  "clips": [
    {"source": "main", "start": 0, "end": 4, "speed": 2},
    {"source": "main", "start": 4, "end": 6, "speed": 0.5}
  ],
  "transition": {"type": "fade", "duration": 0.4},
  "output": {"width": 1280, "height": 720, "fps": 30}
}
```

预期时长 2+4−0.4=5.6 秒。若有讲话，先检查词边界，避免在话中间改变节奏。

### 竖屏、标题与固定打码

```json
{
  "version": 1,
  "sources": {"main": "sources/main.mp4"},
  "clips": [{"source": "main", "start": 0, "end": 8, "fit": "blur"}],
  "output": {"width": 720, "height": 1280, "fps": 30},
  "titles": [{"text": "寻找旅伴", "size": 48, "position": "bottom"}],
  "masks": [{"x": 30, "y": 480, "width": 180, "height": 80,
             "start": 2, "end": 5, "mode": "black"}]
}
```

### 左右对照＋音乐

```json
{
  "version": 1,
  "sources": {"a": "sources/a.mp4", "b": "sources/b.mp4", "music": "sources/music.wav"},
  "clips": [{"source": "a", "start": 0, "end": 6}, {"source": "b", "start": 1, "end": 7}],
  "layout": "horizontal", "split_audio": 0,
  "output": {"width": 1280, "height": 720, "fps": 30},
  "audio": {"source": "music", "volume": 0.15, "loop": true, "duck": true,
            "fade_in": 0.5, "fade_out": 0.8}
}
```

### 文字剪辑＋逐词字幕

```json
{
  "version": 1,
  "sources": {"talk": "sources/talk.mp4"},
  "transcripts": {"talk": "transcripts/talk.json"},
  "clips": [{"source": "talk", "words": [0, 8]}, {"source": "talk", "words": [15, 23]}],
  "subtitles": {"from_transcripts": true, "words_per_line": 4, "highlight": "#FFE066"}
}
```

文字筛选由 Agent 根据用户意图完成；脚本只做精确词时间映射，不自动判断哪些话该删。

### 提取并清理音频

```json
{
  "version": 1,
  "sources": {"talk": "sources/talk.mp4"},
  "clips": [{"source": "talk", "start": 0, "end": 8}],
  "output": {"audio_only": true, "format": "mp3"},
  "audio": {"denoise": true, "highpass": 80, "fade_in": 0.1, "fade_out": 0.2}
}
```

导出：`python /opt/video-use/helpers/edit_media.py render edit/audio-plan.json -o edit/voice-v1.mp3`。
独立音频剪辑同样支持 WAV/MP3 等音频作为 clips 的 source。
