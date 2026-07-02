# 🎬 可灵 AI 抖音广告视频 - 一键成片

> **一句话说明**：输入产品信息，运行一条命令，自动生成广告脚本（支持 LLM 大模型文案）+ 调用可灵 API 生成角色定妆照 + 5 个分镜片段（并行 best-of 择优）+ 自动拼接转场字幕 BGM 配音（豆包 TTS / macOS say）调色稳像 → 输出抖音标准最终成片。

---

## 📦 项目结构

```
kling-ad-automation/
├── config.py                     # 配置文件（API Key、参数默认值、电影风格、调色预设、钩子模板、品牌配置）
├── kling_client.py               # 可灵 API 客户端（JWT 鉴权 + Bearer 兼容，图片/视频生成）
├── video_merger.py               # 视频拼接模块（ffmpeg 封装：拼接/字幕/调色/SFX/封面/水印/卡点/稳像）
├── one_click_create.py           # ⭐ 一键成片主脚本（模板/A·B版本/并行 best-of/preview/serial）
├── batch.py                      # ⭐ 批量生成脚本（YAML 配置 + 并发控制）
├── ad_script.py                  # ⭐ 广告脚本生成（5段式文案、痛点库、卖点拆解、标题/话题标签）
├── bgm_client.py                 # ⭐ BGM 背景音乐（FreeToUse API、风格匹配、BPM匹配、淡入淡出）
├── tts_client.py                 # ⭐ AI 口播配音（豆包 TTS V3 / macOS say 降级、5种风格、5种音色）
├── douyin_adapter.py             # ⭐ 抖音平台适配（字幕安全区、节奏模板、规格配置）
├── compliance_checker.py         # ⭐ 广告合规检测（极限词/敏感词、风险等级、替换建议）
├── quality_checker.py            # ⭐ 视频质量检测（清晰度/黑帧/冻结帧/音频质量/人脸初筛）
├── cinematic_language.py         # ⭐ 深度电影语言模块（18种风格精细化 Prompt 构建）
├── llm_client.py                 # ⭐ LLM 文案生成客户端（OpenAI 兼容，--no-llm 可降级模板）
├── tests/                        # 单元测试目录
│   └── test_core.py
├── references/                   # 参考文档
│   └── cinematic_styles/         # 电影风格详解
│       ├── hitchcock.md
│       ├── kubrick.md
│       ├── spielberg.md
│       └── aronofsky.md
├── prompts/                      # Prompt 模板库（备用参考）
│   ├── character_ref/
│   │   └── character_ref_template.md
│   └── clips/
│       └── clip_prompts_template.md
├── templates/                    # 工作流模板 + 产品 JSON 模板
│   └── editing_workflow_guide.md
├── output/                       # 输出目录
│   ├── character_ref/            # 角色定妆照
│   ├── clips/                    # 分镜片段（含 best-of 候选）
│   ├── final/                    # 最终成片
│   ├── batch/                    # 批量生成输出
│   ├── bgm_cache/                # BGM 本地缓存
│   └── sfx_cache/                # 音效本地缓存
├── examples/                     # 示例配置
│   ├── sample_batch.yaml         # 批量生成示例
│   └── sample_template.json      # 模板示例
└── README.md                     # 本文件
```

---

## 🚀 快速开始（4 步）

### 第一步：安装依赖

```bash
# 安装 Python 依赖
pip install -r requirements.txt

# 安装 ffmpeg（用于视频拼接）
# macOS
brew install ffmpeg

# Ubuntu/Debian
sudo apt install ffmpeg
```

### 第二步：配置 API Key

复制 `.env.example` 为 `.env`，按需填入以下配置：

```bash
cp .env.example .env
```

```ini
# ── 可灵 API（二选一）────────────────────────────────
# 【推荐】JWT 鉴权（AccessKey + SecretKey）
KLING_ACCESS_KEY=your_access_key_here
KLING_SECRET_KEY=your_secret_key_here

# 【兼容】旧版 Bearer 鉴权
KLING_API_KEY=your-api-key-kling

# ── 豆包 TTS（可选，不填自动降级到 macOS say）────────
VOLC_API_KEY=your_volc_api_key_here

# ── LLM 文案生成（可选，不填走内置模板）─────────────
LLM_API_KEY=your_llm_api_key_here
LLM_BASE_URL=https://your-llm-endpoint/v1
LLM_MODEL=your_model_name
```

> **获取 Key**：可灵 → [开放平台](https://app.klingai.com/cn/dev)，豆包 TTS → [火山引擎语音控制台](https://console.volcengine.com/speech/new/setting/apikeys)

### 第三步：安装 Python 依赖

```bash
pip install -r requirements.txt
```

### 第四步：一键成片（基础版）

```bash
cd kling-ad-automation
python one_click_create.py
```

按提示输入产品信息，脚本会自动完成：
1. LLM / 模板生成完整广告脚本（痛点+卖点+CTA）
2. 生成角色定妆照
3. 并行生成 5 个分镜视频片段（best-of 多候选自动择优）
4. 视频稳定化 + 去闪烁
5. 自动拼接 + 转场 + 卡点对齐
6. 添加字幕动画 + 关键词高亮
7. 智能选曲 BGM + 淡入淡出
8. AI 口播配音（豆包 TTS / macOS say，可选）
9. 后期调色（电影感预设）
10. 生成封面图 + 品牌水印
11. 质量检测 + 合规检测
12. 导出抖音标准成片 + 标题/话题标签

### 第五步：电影风格增强（推荐）

为视频注入经典电影运镜/转场风格，让 AI 生成质量提升一档：

```bash
# 列出所有可用风格
python one_click_create.py --list-styles

# 希区柯克风格：推轨变焦，心理悬疑
python one_click_create.py --style hitchcock

# 库布里克风格：对称构图，单点透视
python one_click_create.py --style kubrick

# 斯皮尔伯格风格：拉轨揭示，平民英雄
python one_click_create.py --style spielberg

# 阿伦诺夫斯基风格：快速推轨，分裂半透镜
python one_click_create.py --style aronofsky

# 斯科塞斯风格：跟踪镜头， steadicam
python one_click_create.py --style scorsese

# 诺兰风格：IMAX 比例，实景特效
python one_click_create.py --style nolan

# 韦斯·安德森风格：对称构图， pastel 色彩
python one_click_create.py --style anderson

# 王家卫风格：霓虹美学，都市孤独
python one_click_create.py --style wong-kar-wai

# 塔可夫斯基风格：诗意长镜头，自然意象
python one_click_create.py --style tarkovsky

# 张艺谋风格：东方色彩美学，民俗仪式
python one_click_create.py --style zhang-yimou

# 是枝裕和风格：日常诗意，家庭纽带
python one_click_create.py --style koreeda

# 昆汀风格：类型拼贴，暴力美学
python one_click_create.py --style tarantino

# 贾樟柯风格：中国社会变迁，纪实美学
python one_click_create.py --style jia-zhangke

# 侯孝贤风格：长镜头美学，历史记忆
python one_click_create.py --style hou-hsiao-hsien

# 奉俊昊风格：类型混合，社会讽刺
python one_click_create.py --style bong-joon-ho

# 维伦纽瓦风格：宏大尺度，静谧恐惧
python one_click_create.py --style denis-villeneuve

# 卢贝松风格：视觉诗歌，街头诗学
python one_click_create.py --style luc-besson

# 宫崎骏风格：手绘诗意，飞行幻想
python one_click_create.py --style miyazaki
```

**组合示例**：

```bash
# 希区柯克风格 + 高品质模式 + 8秒片段
python one_click_create.py --style hitchcock --mode pro --duration 8

# 库布里克风格 + 4K 画质
python one_click_create.py --style kubrick --mode 4k

# 双版本输出（9:16 竖屏 + 16:9 横屏）
python one_click_create.py --style kubrick --dual-output
```

**可选电影风格速查**：

| 风格 | 导演 | 核心运镜 | 适用场景 |
|------|------|----------|----------|
| `none` | 无 | 原始 Prompt | 通用/测试 |
| `hitchcock` | 希区柯克 | 推轨变焦、螺旋聚焦 | 钩子、悬疑广告 |
| `kubrick` | 库布里克 | 单点透视、匹配剪辑 | 科技、高端产品 |
| `spielberg` | 斯皮尔伯格 | 拉轨揭示、镜头光晕 | 情感、家庭产品 |
| `aronofsky` | 阿伦诺夫斯基 | 快速推轨、分裂半透镜 | 美妆、健身、紧迫感 |
| `scorsese` | 斯科塞斯 | 跟踪镜头、 steadicam | 街头、潮流、运动 |
| `nolan` | 诺兰 | IMAX 比例、实景特效 | 科技、汽车、高端 |
| `anderson` | 韦斯·安德森 | 对称构图、 pastel 色彩 | 时尚、甜品、清新 |
| `wong-kar-wai` | 王家卫 | 霓虹美学、慢快门 | 都市、浪漫、孤独 |
| `tarkovsky` | 塔可夫斯基 | 诗意长镜头、自然意象 | 艺术、哲学、深度 |
| `zhang-yimou` | 张艺谋 | 东方色彩、对称构图 | 国风、民俗、仪式 |
| `koreeda` | 是枝裕和 | 日常诗意、克制温情 | 家庭、生活、治愈 |
| `tarantino` | 昆汀 | 类型拼贴、暴力美学 | 潮流、年轻、复古 |
| `jia-zhangke` | 贾樟柯 | 纪实美学、社会变迁 | 纪实、社会、边缘 |
| `hou-hsiao-hsien` | 侯孝贤 | 长镜头、历史记忆 | 历史、文艺、哲思 |
| `bong-joon-ho` | 奉俊昊 | 类型混合、空间政治 | 社会讽刺、阶级 |
| `denis-villeneuve` | 维伦纽瓦 | 宏大尺度、宇宙诗意 | 科幻、史诗、恐惧 |
| `luc-besson` | 卢贝松 | 视觉诗歌、街头诗学 | 动作、浪漫、欧洲 |
| `miyazaki` | 宫崎骏 | 手绘诗意、飞行幻想 | 动画、自然、治愈 |

---

## 🎯 完整工作流

```
┌─────────────────┐
│  输入产品信息    │
│  (交互式提示)    │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ 生成广告脚本     │
│ (痛点+卖点+CTA)  │
└────────┬────────┘
         │
         ▼
┌─────────────────┐     ┌──────────────┐
│ 生成角色定妆照   │────▶│ 可灵图片 API │
│ (character_ref) │     │ /v1/images/  │
└────────┬────────┘     └──────────────┘
         │
         ▼
┌─────────────────┐     ┌──────────────┐
│ 生成5个分镜片段  │────▶│ 可灵视频 API │
│ (clip_01~05)    │     │ /v1/videos/  │
└────────┬────────┘     └──────────────┘
         │
         ▼
┌─────────────────────────────┐
│ 后期处理（ffmpeg）           │
│ ├ 自动拼接 + 转场 + 卡点对齐 │
│ ├ 字幕动画 + 关键词高亮      │
│ ├ BGM 智能选曲 + 淡入淡出    │
│ ├ AI 口播配音（可选）        │
│ ├ 后期调色（电影感预设）     │
│ ├ 音效设计 SFX               │
│ ├ 封面图生成                 │
│ └ 品牌 Logo 水印             │
└────────┬────────────────────┘
         │
         ▼
┌─────────────────┐
│ 质量检测         │
│ ├ 黑帧/冻结帧检测│
│ ├ 清晰度检测     │
│ ├ 音频质量检测   │
│ └ 合规检测       │
└────────┬────────┘
         │
         ▼
┌───────────────────────────┐
│ 导出最终成片               │
│ final_*.mp4 + 标题 + 话题  │
└───────────────────────────┘
```

---

## 📝 使用示例

### 示例 1：美妆产品

```bash
$ python one_click_create.py

请输入产品信息（直接回车使用默认值）：

产品名称 [我的产品]：水润保湿面霜
产品类型 [美妆/食品/科技/服装/app]：美妆
核心卖点 [一句话描述]：24小时深层保湿，肌肤水润透亮
目标人群 [如：18-35岁女性]：25-40岁女性
广告风格 [现代简约/清新自然/温暖治愈/极客未来]：清新自然
角色年龄 [25]：28
角色性别 [女/男]：女
服装描述 [日常休闲装]：白色简约家居服

确认开始生成？(y/n) [y]：y

[1/4] 生成角色定妆照...
  ✅ 角色定妆照已生成：output/character_ref/水润保湿面霜_20260627_143022_ref.png

[2/4] 生成分镜视频片段...
  片段 1/5：static shot, slow push in...
  ✅ 片段 1 已保存：clip_01.mp4
  片段 2/5：handheld tracking shot...
  ✅ 片段 2 已保存：clip_02.mp4
  ...

[3/4] 拼接视频 + 添加转场...
  ✅ 视频拼接完成：水润保湿面霜_20260627_143022_merged.mp4

[4/4] 添加字幕 + 导出最终成片...
  ✅ 最终成片已导出：水润保湿面霜_20260627_143022_final.mp4

🎉 一键成片完成！
📁 输出目录：output/final/
🎬 最终成片：水润保湿面霜_20260627_143022_final.mp4
```

### 示例 2：科技产品 + 电影风格

```bash
$ python one_click_create.py --style kubrick

请输入产品信息（直接回车使用默认值）：

产品名称 [我的产品]：智能降噪耳机
产品类型 [美妆/食品/科技/服装/app]：科技
核心卖点 [一句话描述]：40dB深度降噪，沉浸式音乐体验
目标人群 [如：18-35岁女性]：18-35岁男性
广告风格 [现代简约/清新自然/温暖治愈/极客未来]：极客未来
角色年龄 [25]：26
角色性别 [女/男]：男
服装描述 [日常休闲装]：深色科技感卫衣+耳机

🎥 电影风格：库布里克
   对称构图大师，单点透视，仪式感

确认开始生成？(y/n) [y]：y

[1/4] 生成角色定妆照...
  ✅ 角色定妆照已生成

[2/4] 生成分镜视频片段...
  片段 1/5：Kubrick one-point perspective dolly...
  ✅ 片段 1 已保存（库布里克式推进）
  片段 2/5：Kubrick pull back...
  ✅ 片段 2 已保存
  ...

[3/4] 拼接视频 + 添加转场...
  ✅ 视频拼接完成

[4/4] 添加字幕 + 导出最终成片...
  ✅ 最终成片已导出

🎉 一键成片完成！
```

### 示例 3：美妆产品 + 阿伦诺夫斯基风格

```bash
python one_click_create.py --style aronofsky --mode pro --duration 8
```

**效果**：快速推轨 + 分裂半透镜，前3秒抓住眼球，适合美妆/健身紧迫感广告。

### 示例 4：AI 配音 + 抖音黄金档 15 秒

```bash
python one_click_create.py --voiceover --target-duration 15 --hook question
```

### 示例 5：A/B 测试 2 个版本 + 商品参考图

```bash
python one_click_create.py --ab-versions 2 --product-image product.jpg --style kubrick
```

---

## 🎛️ 完整命令行参数

### 基础参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--style` | `none` | 电影风格（18 种可选，用 `--list-styles` 查看） |
| `--duration` | `5` | 单片段时长（秒） |
| `--mode` | `std` | 生成模式：`std` / `pro` / `4k` |
| `--aspect-ratio` | `9:16` | 画面比例 |
| `--target-duration` | - | 目标总时长（秒）：`10` / `15` / `20` / `25` / `30` / `60` |
| `--rhythm-style` | `moderate` | 节奏风格：`fast` / `moderate` / `cinematic` |
| `--dual-output` | - | 同时生成 9:16 和 16:9 两个版本 |

### 一致性控制

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--product-image` | - | 商品参考图路径（展示类片段自动使用） |
| `--image-fidelity` | `0.85` | 参考图 fidelity [0,1] |
| `--human-fidelity` | `0.8` | 人物 fidelity [0,1] |
| `--seed` | - | 随机种子基准（各片段自动递增） |

### 钩子与脚本

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--hook` | `question` | 钩子类型（8 种，用 `--list-hooks` 查看） |
| `--list-hooks` | - | 列出所有钩子模板 |
| `--script-style` | `standard` | 广告脚本风格（用 `--list-script-styles` 查看） |
| `--list-script-styles` | - | 列出所有广告脚本风格 |
| `--ab-versions` | `1` | A/B 测试版本数量（1-3） |
| `--ab-dim` | - | A/B 测试维度：`hook` / `style` / `script` |

### AI 配音

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--voiceover` | - | 启用 AI 口播配音（豆包 TTS 优先，降级 macOS say） |
| `--voiceover-style` | `standard` | 口播风格：standard / emotional / energetic / professional / storytelling |
| `--voice` | `energetic_female` | 音色：female_young / female_warm / male_pro / male_magnetic / energetic_female |
| `--list-voices` | - | 列出所有音色预设 |

### 生成质量控制

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--best-of` | `2` | 每个分镜生成候选数，自动质量择优 |
| `--quality-frames` | `12` | best-of 择优时的抽帧数量 |
| `--keep-candidates` | - | 保留未被选中的候选片段 |
| `--min-clips` | `3` | 最少成功片段数，低于此数则终止 |
| `--max-workers` | `4` | 并行生成最大线程数 |
| `--stabilize` / `--no-stabilize` | 开启 | 视频稳定化 + 去闪烁 |
| `--multi-shot` | - | 启用可灵多镜头模式（intelligence 分镜） |
| `--kling-model` | - | 指定可灵模型版本（如 `kling-v2-master`） |

### 执行模式

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--preview` / `-p` | - | 快速预览：仅生成第 1 段，跳过后期，用于快速试错 |
| `--serial` | - | 强制串行生成（每段用上一段尾帧，极致一致性） |
| `--strict` / `--no-strict` | 开启 | 严格模式：关键步骤失败时抛出异常而非静默降级 |
| `--force` | - | 强制跳过 high 风险合规拦截（critical 始终拦截） |
| `--no-llm` | - | 禁用 LLM 文案生成，强制走内置模板 |
| `--brand-intro-outro` | - | 在成片首尾加入品牌开场（2s）和收尾动画（1.5s） |

### 模板与其他

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--save TEMPLATE.json` | - | 保存当前参数为模板 |
| `--load TEMPLATE.json` | - | 从模板加载参数，跳过交互 |
| `--list-styles` | - | 列出所有电影风格卡片 |

---

## ⚙️ 配置说明

所有敏感配置通过 `.env` 文件注入（见 `.env.example`），非敏感参数可在 `config.py` 中调整。

### 主要配置项

| 参数 | 来源 | 默认值 | 说明 |
|------|------|--------|------|
| `KLING_ACCESS_KEY` | `.env` | - | 可灵 JWT AccessKey（推荐） |
| `KLING_SECRET_KEY` | `.env` | - | 可灵 JWT SecretKey（推荐） |
| `KLING_API_KEY` | `.env` | - | 旧版 Bearer API Key（兼容） |
| `KLING_BASE_URL` | `.env` / `config.py` | `https://api-beijing.klingai.com` | API 地址 |
| `VOLC_API_KEY` | `.env` | - | 豆包 TTS API Key（可选，不填降级 macOS say） |
| `LLM_API_KEY` | `.env` | - | LLM 文案生成 API Key（可选） |
| `LLM_BASE_URL` | `.env` | - | LLM 接口地址（OpenAI 兼容） |
| `LLM_MODEL` | `.env` | - | LLM 模型名称 |
| `DEFAULT_VIDEO_DURATION` | `config.py` | `5` | 单片段时长（秒） |
| `DEFAULT_ASPECT_RATIO` | `config.py` | `9:16` | 画面比例（竖屏） |
| `DEFAULT_MODE` | `config.py` | `std` | 生成模式（std/pro/4k） |
| `OUTPUT_RESOLUTION` | `config.py` | `1080x1920` | 输出分辨率 |
| `OUTPUT_FPS` | `config.py` | `30` | 输出帧率 |

### 产品类型预设

| 预设 | 场景 | 灯光 | 动作 |
|------|------|------|------|
| `美妆` | clean vanity table | soft natural lighting | applying product on hand |
| `食品` | wooden kitchen table | warm natural sunlight | taking a bite |
| `科技` | minimalist modern desk | cool tech lighting | touching screen |
| `服装` | clean white background | studio soft lighting | showing outfit details |
| `app` | coffee shop table | bright natural light | swiping phone screen |
| `汽车` | coastal highway or city skyline | golden hour sunlight | driving smoothly |
| `房产` | modern living room | soft window light | walking through space |
| `教育` | modern classroom or library | classroom natural light | studying intently |
| `医疗` | modern clinic or lab | bright clinical lighting | professional demonstration |
| `default` | lifestyle setting | natural lighting | using the product naturally |

### 环境变量

```bash
# 设置 API Key
export KLING_API_KEY="your_api_key"

# 设置 API 地址（可选）
export KLING_BASE_URL="https://api-beijing.klingai.com"
```

---

## ✍️ 广告脚本生成

内置完整的 5 段式广告脚本生成引擎，根据产品信息自动生成：

- **痛点钩子** — 从品类痛点库中随机抽取，引发共鸣
- **产品引入** — 自然带出产品，给出期待
- **卖点展示** — 核心卖点拆解 + 场景化描述
- **效果呈现** — 使用后的改变和结果
- **CTA 引导** — 行动号召，引导点击/购买

### 脚本风格

使用 `--script-style` 指定脚本风格，或 `--list-script-styles` 查看全部。

### 输出产物

每次生成视频后，会自动输出：
- 视频内嵌字幕（带动画效果）
- 口播文案（启用 `--voiceover` 时使用）
- 5 个标题选项（可直接用作抖音标题）
- 10 个话题标签（#话题）

---

## 🎣 钩子模板库

8 种开头钩子类型，适配不同产品和风格，用 `--hook` 参数指定：

| 钩子类型 | 名称 | 适用场景 |
|----------|------|----------|
| `question` | 灵魂拷问式 | 美妆/个护/家居/食品 |
| `shocking` | 震惊反差式 | 数码/家居/食品/美妆 |
| `before_after` | 前后对比式 | 美妆/个护/健身/清洁 |
| `demonstration` | 效果展示式 | 美妆/食品/数码/家居 |
| `story` | 故事叙述式 | 美妆/个护/食品/家居 |
| `challenge` | 挑战测试式 | 数码/清洁/个护/食品 |
| `celeb_style` | 明星同款式 | 美妆/个护/时尚/食品 |
| `pain_point` | 痛点直击式 | 家居/个护/食品/数码 |

使用 `--list-hooks` 查看所有钩子详情。

---

## 🎵 BGM 背景音乐系统

### 自动选曲

根据产品品类自动匹配 BGM 风格，数据来自 [FreeToUse Music API](https://freetouse.com/api)（免费、免版权）：

| 品类 | BGM 关键词 |
|------|-----------|
| 美妆 | upbeat / happy / aesthetic / chill |
| 食品 | happy / fun / upbeat / cooking |
| 科技 | technology / corporate / electronic |
| 服装 | fashion / cool / edm / trap |
| 汽车 | energetic / epic / rock |
| 房产 | inspiring / corporate / calm |

### 特性

- **本地缓存** — 下载过的 BGM 自动缓存到 `output/bgm_cache/`，避免重复下载
- **淡入淡出** — 自动添加 1-2 秒的淡入淡出，避免突兀
- **时长适配** — 自动裁剪/循环到视频长度
- **响度对齐** — BGM 音量自动调整到合适水平（默认 0.3）
- **BGM 闪避** — 启用配音时，BGM 自动降低到 0.25 保证人声清晰

---

## 🎙️ AI 口播配音

### 快速开始

```bash
python one_click_create.py --voiceover --voice energetic_female
```

### 5 种口播风格

| 风格 | 名称 | 适用场景 |
|------|------|----------|
| `standard` | 标准带货 | 通用电商 |
| `emotional` | 情感共鸣 | 走心/生活方式 |
| `energetic` | 激情喊麦 | 促销/福利/直播感 |
| `professional` | 专业测评 | 科技/数码/硬核 |
| `storytelling` | 故事叙述 | 品牌/长视频 |

### 5 种音色

| 音色 key | 名称 | 豆包音色 / macOS 降级 |
|----------|------|----------------------|
| `female_young` | 年轻女声 | zh_female_shuangkuaisisi / Tingting |
| `female_warm` | 温暖女声 | zh_female_tianmeixiaoyuan / Sandy |
| `male_pro` | 专业男声 | zh_male_jingqiangkanye / Eddy |
| `male_magnetic` | 磁性男声 | zh_male_qingshuaige / Reed |
| `energetic_female` | 活力女声 | zh_female_renxiaobao / Flo |

> **优先级**：配置了 `VOLC_API_KEY` 时自动使用豆包 TTS V3（`seed-tts-2.0`），否则降级到 macOS `say`（离线免费）。

---

## 🎨 字幕动画

4 种字幕动画效果，默认 `pop`：

| 动画 | 效果 | 适用场景 |
|------|------|----------|
| `pop` | 弹跳弹出 | 活力/年轻向 |
| `slide` | 侧滑进入 | 简洁/专业 |
| `fade` | 淡入淡出 | 文艺/慢节奏 |
| `highlight` | 关键词高亮 | 强调卖点/促销 |

### 抖音适配

- 字号更大（占画面高度 5.5%）
- 底部留出安全区（避让小黄车）
- 关键词自动高亮变色
- 描边更粗，保证小屏可读性

---

## 🎞️ 后期调色

9 种调色预设，自动匹配电影风格：

| 预设 | 风格 | 适用场景 |
|------|------|----------|
| `warm_cinematic` | 暖色电影感 | 默认/通用 |
| `cool_cinematic` | 冷色电影感 | 科技/高端 |
| `vintage` | 复古胶片 | 怀旧/文艺 |
| `teal_orange` | 青橙色调 | 好莱坞/动作 |
| `moody` | 暗调情绪 | 悬疑/高级 |
| `bright_clean` | 明亮清新 | 美妆/食品 |
| `noir` | 黑白胶片 | 高端/极简 |
| `pastel` | 马卡龙 | 甜品/少女向 |
| `none` | 无调色 | 原样输出 |

---

## 🔊 音效设计 SFX + 卡点剪辑

### 音效系统

内置 3 种基础音效（动态生成，无需额外资源）：
- `whoosh` — 转场咻声
- `impact` — 重音冲击
- `ding` — 强调叮声

### 卡点剪辑

自动检测 BGM 节拍点，将转场和字幕对齐到节拍：
- 转场对齐节拍点
- 字幕弹出对齐重拍
- 音效配合节奏点

---

## 🖼️ 封面图生成

视频导出时自动从最佳帧提取封面图：
- 自动选择画面最清晰的帧
- 优先选择有人物正脸的帧
- 输出 JPEG 格式，可直接上传抖音

---

## 🏷️ 品牌 Logo 水印

在 `config.py` 的 `BRAND_CONFIG` 中配置品牌信息后，可自动添加水印：

```python
BRAND_CONFIG = {
    "logo_path": "assets/logo.png",  # 你的 Logo
    "logo_watermark": {
        "enabled": True,
        "position": "top_right",     # top_left / top_right / bottom_left / bottom_right
        "size_ratio": 0.08,          # 宽度占比
        "opacity": 0.9,
        "fade_in": 0.5,
        "fade_out": 0.5,
    },
}
```

---

## 📱 抖音平台适配

内置完整的抖音平台规范适配：

| 维度 | 适配内容 |
|------|----------|
| 视频规格 | 1080x1920 竖屏 / 30fps / H.264 / AAC 160k |
| 时长 | 自动适配 7/15/30/60 秒黄金档 |
| 钩子 | 前 3 秒黄金钩子，强制抓注意力 |
| 字幕 | 大字号 + 粗描边 + 底部安全区 |
| 节奏 | 每 5 秒一个信息点，转场快（0.4s） |
| 口播 | 语速 200 字/分钟，BGM 闪避 |
| CTA | 结尾留 2 秒行动引导 |

---

## ✅ 广告合规检测

自动检测字幕、口播、标题、话题标签中的违规内容：

| 风险等级 | 内容 | 处理方式 |
|----------|------|----------|
| 🔴 高风险 | 最高级词（最/第一/唯一/顶级等）、国家级、虚假承诺 | 必须替换 |
| 🟠 中风险 | 效果保证、投资回报、夸张表述、紧迫营销 | 建议替换 |
| 🟡 低风险 | 数据表述、主观感受、对比词 | 建议核实 |
| ⚫ 敏感 | 政治/色情/暴力/医疗等 | 严禁使用 |

检测完成后输出合规报告，包含具体违规位置和替换建议。

---

## 🔍 视频质量检测

导出成片后自动进行质量检测：

| 检测项 | 内容 |
|--------|------|
| 黑帧检测 | 开头/结尾黑帧，自动裁剪 |
| 冻结帧检测 | 静止画面检测，标记风险 |
| 清晰度检测 | 拉普拉斯方差评估画面锐利度 |
| 闪烁检测 | 相邻帧差异过大的闪烁 |
| 音频质量 | 集成响度（LUFS）/ 真峰值检测 |
| 人脸初筛 | 肤色区域形态学异常初筛 |

输出 0-100 分的质量评分和详细报告。

---

## 📊 模块说明

### one_click_create.py（主脚本）

一键成片入口，执行流程：
1. 交互式输入产品信息
2. 调用 `ad_script.py` 生成完整广告脚本
3. 调用 `kling_client.py` 生成角色定妆照
4. 调用 `kling_client.py` 生成 5 个分镜片段
5. 调用 `video_merger.py` 拼接视频 + 转场 + 卡点
6. 调用 `video_merger.py` 添加字幕动画 + 调色
7. 调用 `bgm_client.py` 智能选曲 BGM
8. 调用 `tts_client.py` AI 口播配音（可选）
9. 调用 `quality_checker.py` 视频质量检测
10. 调用 `compliance_checker.py` 广告合规检测
11. 生成封面图 + 品牌水印 + 标题话题标签
12. 导出最终成片

### kling_client.py（API 客户端）

封装可灵官方 API：
- `generate_character_ref()` - 生成角色定妆照
- `generate_video()` - 生成视频片段
- `create_video_task()` - 创建异步视频任务
- `query_video_task()` - 查询任务状态
- `download_video()` - 下载视频

### video_merger.py（视频处理模块）

基于 ffmpeg 的全链路视频处理：
- `merge_clips_ffmpeg()` - 拼接多个视频片段 + 转场
- `add_fancy_subtitles()` - 字幕动画（pop/slide/fade/highlight）
- `apply_color_grading()` - 后期调色（9 种预设）
- `add_bgm_ffmpeg()` - 添加 BGM + 淡入淡出 + 闪避
- `add_sfx_to_video()` - 音效设计
- `align_subtitles_to_beats()` - 字幕卡点对齐
- `generate_cover_image()` - 封面图生成
- `add_logo_watermark()` - 品牌 Logo 水印
- `auto_trim_black_frames()` - 黑帧自动裁剪
- `color_match_clips()` - 片段色彩匹配
- `export_final_video()` - 导出最终视频

### ad_script.py（广告脚本生成）

5 段式广告文案生成：
- `generate_ad_script()` - 生成完整广告脚本
- `script_to_clip_prompts()` - 脚本转分镜 Prompt
- `script_to_subtitles()` - 脚本转字幕
- `script_to_voiceover()` - 脚本转口播文案
- `generate_title_options()` - 生成标题选项
- `generate_hashtag_options()` - 生成话题标签

### bgm_client.py（BGM 音乐客户端）

FreeToUse Music API 封装：
- `pick_bgm_for_product()` - 按品类智能选曲
- `search_music()` - 搜索音乐
- 本地缓存机制，避免重复下载
- 自动淡入淡出 + 时长适配

### tts_client.py（AI 口播配音）

macOS say 命令封装（可扩展第三方 TTS）：
- `generate_full_voiceover()` - 生成完整配音
- `align_subtitles_to_voiceover()` - 字幕时间轴对齐
- 5 种口播风格 + 5 种音色
- BGM 闪避（ducking）支持

### douyin_adapter.py（抖音平台适配）

抖音规范配置与优化：
- `get_douyin_config()` - 获取抖音平台配置
- `optimize_subtitles_for_douyin()` - 字幕抖音优化
- 黄金 3 秒钩子、字幕安全区、节奏适配

### compliance_checker.py（广告合规检测）

广告法 + 平台规则检测：
- `check_script_compliance()` - 检测脚本合规性
- `print_compliance_report()` - 输出合规报告
- 三级风险等级（高/中/低）+ 敏感词
- 违规位置定位 + 替换建议

### quality_checker.py（视频质量检测）

基于 ffmpeg + OpenCV 的质量评估：
- `check_video_quality()` - 完整质量检测
- `print_quality_report()` - 输出质量报告
- 清晰度 / 黑帧 / 冻结帧 / 闪烁 / 音频质量 / 人脸初筛
- 0-100 分质量评分

---

## 🎥 电影风格系统

### 工作原理

`one_click_create.py` 内置了 18 种经典电影风格，通过 `--style` 参数一键注入所有片段的 Prompt：

```
基础 Prompt + 电影运镜描述 → 可灵生成 → 电影级画面
```

### 风格映射表

| 风格键 | 导演 | 片段1 钩子 | 片段2 转折 | 片段3 展示 | 片段4 结果 | 片段5 CTA |
|--------|------|-----------|-----------|-----------|-----------|----------|
| `hitchcock` | 希区柯克 | Dolly Zoom 推轨变焦 | 螺旋聚焦 |  paranoid surveillance | 拉轨揭示孤立 | 暗角结束 |
| `kubrick` | 库布里克 | 单点透视推进 | 时间跳跃 | 对称环绕 | 对称拉远 | 中心构图 |
| `spielberg` | 斯皮尔伯格 | 期待推进 | 镜头光晕 | 拉轨揭示全景 | 拉轨揭示情绪 | 低角度仰视 |
| `aronofsky` | 阿伦诺夫斯基 | 快速推轨 | 快速跳切 | 分裂半透镜环绕 | 快速拉远 | 快速推轨 |
| `scorsese` | 斯科塞斯 | 跟踪推进 | 自由轨道 | Steadicam 环绕 | 拉远揭示环境 | 冻结帧结束 |
| `nolan` | 诺兰 | IMAX 推进 | 交叉剪辑 | 实景特效环绕 | 实景拉远 | IMAX 固定 |
| `anderson` | 韦斯·安德森 | 对称推进 | 水平转场 | 对称环绕 | 对称拉远 | 对称固定 |

### 自定义风格

编辑 `config.py` 中的 `CINEMATIC_STYLES` 字典，添加你的专属风格：

```python
CINEMATIC_STYLES = {
    "my_style": {
        "name": "我的风格",
        "camera_push": "My custom push in description...",
        "camera_pull": "My custom pull back description...",
        "camera_orbit": "My custom orbit description...",
        "transition_match": "My custom match cut...",
        "transition_light": "My custom light transition...",
        "lighting": "My custom lighting...",
        "color": "My custom color palette...",
        "mood": "My custom mood...",
    },
}
```

### 参考文档

详细每种风格的运镜/转场/光线/色彩说明，查看 `references/cinematic_styles/` 目录：
- `hitchcock.md` - 希区柯克风格详解
- `kubrick.md` - 库布里克风格详解
- `spielberg.md` - 斯皮尔伯格风格详解
- `aronofsky.md` - 阿伦诺夫斯基风格详解

---

## 🎬 输出文件说明

```
output/
├── character_ref/
│   └── {product}_{timestamp}_ref.png       # 角色定妆照
├── clips/
│   ├── clip_01_{timestamp}.mp4             # 片段1：钩子
│   ├── clip_02_{timestamp}.mp4             # 片段2：转折
│   ├── clip_03_{timestamp}.mp4             # 片段3：展示
│   ├── clip_04_{timestamp}.mp4             # 片段4：结果
│   └── clip_05_{timestamp}.mp4             # 片段5：CTA
├── final/
│   ├── {product}_{timestamp}_final.mp4     # ⭐ 9:16 最终成片（含字幕/BGM/调色/配音）
│   ├── {product}_{timestamp}_16x9_final.mp4 # ⭐ 16:9 版本（--dual-output 时生成）
│   ├── {product}_{timestamp}_cover.jpg     # 封面图
│   └── {product}_{timestamp}_script.txt    # 广告脚本 + 标题 + 话题标签
├── bgm_cache/                               # BGM 本地缓存（自动管理）
└── sfx_cache/                               # 音效本地缓存（自动生成）
```

### 双版本输出（--dual-output）

使用 `--dual-output` 参数可同时生成 9:16 竖屏（抖音）和 16:9 横屏（视频号/YouTube）两个版本：

```bash
python one_click_create.py --dual-output
```

16:9 版本通过 ffmpeg 智能裁剪生成，自动保留画面中心主体。

---

## ⚠️ 注意事项

### 1. API 配额与费用
- 可灵 API 按调用次数/时长计费
- 单次视频生成约消耗 5-15s 时长额度
- 运行时会自动预估费用并确认，建议先在可灵官网测试效果
- 详细定价见 `config.py` 中的 `KLING_PRICING` 配置

### 2. 生成时间
- 角色定妆照：约 10-30 秒
- 单个视频片段：约 1-3 分钟
- 5 个片段总计：约 5-15 分钟
- 后期处理（拼接/字幕/调色/配音）：约 1-3 分钟
- 质量检测：约 30 秒

### 3. 一致性保障
- 所有片段自动使用同一张角色定妆照作为参考图
- 通过 `image_fidelity` 和 `human_fidelity` 参数锁定人物特征
- 使用 `--seed` 参数固定随机种子基准，各片段自动递增
- 使用 `--product-image` 传入商品参考图，提升商品一致性
- 片段间自动进行色彩匹配（color_match_clips）

### 4. 失败自动清理

- 如果生成过程中任何步骤失败，脚本会自动清理本次运行产生的所有中间文件（角色定妆照、分镜片段、拼接视频）
- 下次运行时不会残留上次失败的产物，避免混淆
- 如需保留中间文件用于调试，请在 `run_one_click_create()` 的 `except` 块中注释掉 `cleanup_output(output_name)` 调用

### 5. BGM 与配音
- BGM 来自 FreeToUse Music API，免费免版权，自动缓存到本地
- AI 配音基于 macOS `say` 命令，离线可用，无需额外费用
- 启用配音时 BGM 自动闪避（降低到 0.25），保证人声清晰
- 如 BGM 下载失败，自动回退到无 BGM 模式，不影响主流程

---

## 🔧 高级用法

### 自定义 Prompt

编辑 `one_click_create.py` 中的 `generate_clip_prompts()` 函数，修改分镜 Prompt。

### 调整生成参数

在 `config.py` 中修改：
```python
DEFAULT_VIDEO_DURATION = 8  # 延长片段时长
DEFAULT_MODE = "pro"        # 使用高品质模式
```

### 自定义 BGM

默认从 FreeToUse Music API 自动选曲。如需使用本地 BGM，将文件放入 `assets/bgm.mp3` 并在 `config.py` 中设置 `BGM_PATH`。

### 自定义字幕动画

修改 `video_merger.py` 中 `add_fancy_subtitles()` 的动画参数，支持 pop / slide / fade / highlight 四种。

### 品牌水印配置

在 `config.py` 的 `BRAND_CONFIG` 中配置 Logo 路径和水印参数，启用后自动添加。

---

## 🛠️ 故障排查

### 问题 1：API 调用失败

**原因**：API Key 无效或过期  
**解决**：前往可灵开放平台重新生成 API Key

### 问题 2：视频生成超时

**原因**：可灵服务器繁忙或网络问题  
**解决**：脚本已内置轮询机制，最长等待 10 分钟。如仍失败，请重试。

### 问题 3：ffmpeg 报错

**原因**：ffmpeg 未安装或版本过低  
**解决**：`brew install ffmpeg`（macOS）或 `sudo apt install ffmpeg`（Ubuntu）

### 问题 4：人物一致性差

**原因**：参考图未正确传入或 fidelity 参数过低  
**解决**：检查 `reference_image` 参数，适当提高 `image_fidelity`（0.7-0.9）

### 问题 5：16:9 版本生成失败

**原因**：ffmpeg 版本不支持某些滤镜  
**解决**：确保 ffmpeg >= 4.0，或手动使用视频编辑软件裁剪

### 问题 6：BGM 下载失败

**原因**：网络问题或 FreeToUse API 暂时不可用  
**解决**：脚本会自动跳过 BGM，不影响主流程。也可配置本地 BGM 文件路径

### 问题 7：AI 配音没有声音

**原因**：macOS `say` 命令未安装或音色不可用  
**解决**：确保是 macOS 系统，运行 `say -v '?'` 查看可用音色列表

### 问题 8：字幕显示位置不对

**原因**：抖音字幕安全区适配问题  
**解决**：默认已适配抖音底部安全区（避让小黄车），可在 `douyin_adapter.py` 中调整

---

## 🧪 单元测试

项目包含核心逻辑单元测试，使用 pytest 运行：

```bash
cd kling-ad-automation
python -m pytest tests/ -v
```

测试覆盖：
- 电影风格配置完整性（18 种风格）
- 产品预设系统
- 电影风格注入逻辑
- 角色定妆照 Prompt 生成
- 分镜片段 Prompt 生成

---

## 🚀 批量生成（C 用户专业模式）

### 适用场景

- 客户要 3 条不同卖点的视频
- A/B 测试要 2 个不同风格的版本
- 月包客户要 30 条短视频

### 第一步：创建 YAML 配置文件

创建 `batch.yaml`：

```yaml
# 全局默认值（可被单个任务覆盖）
default_style: "kubrick"
default_duration: 8
default_mode: "pro"
default_aspect_ratio: "9:16"

# 批量执行配置
concurrent: 1          # 并发数，建议 1-2，避免触发 API 限流
fail_fast: true        # true：任一任务失败立即终止；false：继续执行剩余任务
output_dir: "output/batch_20240627"

# 任务列表
tasks:
  - product_name: "水润保湿面霜"
    product_type: "美妆"
    selling_point: "24小时深层保湿，肌肤水润透亮"
    audience: "25-40岁女性"
    style: "hitchcock"
    character_age: "28"
    character_gender: "女"
    outfit: "白色简约家居服"

  - product_name: "智能降噪耳机"
    product_type: "科技"
    selling_point: "40dB深度降噪，沉浸式音乐体验"
    audience: "18-35岁男性"
    style: "kubrick"
    character_age: "26"
    character_gender: "男"
    outfit: "深色科技感卫衣"

  - product_name: "手工曲奇饼干"
    product_type: "食品"
    selling_point: "进口黄油，香酥脆口"
    audience: "18-35岁女性"
    style: "spielberg"
    character_age: "22"
    character_gender: "女"
    outfit: "条纹T恤+牛仔裤"
```

### 第二步：运行批量生成

```bash
cd kling-ad-automation
python batch.py --config batch.yaml
```

### 批量生成参数说明

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--config` | YAML 配置文件路径（必填） | - |
| `--concurrent` | 覆盖配置文件中的并发数 | 配置文件中的值 |
| `default_style` | 默认电影风格 | `none` |
| `default_duration` | 默认片段时长（秒） | `5` |
| `default_mode` | 默认生成模式 | `std` |
| `concurrent` | 并发执行数 | `1` |
| `fail_fast` | 失败即停 | `true` |

### 批量生成输出

```
output/batch_20240627/
├── character_ref/
│   ├── 水润保湿面霜_20240627_143022_ref.png
│   ├── 智能降噪耳机_20240627_143045_ref.png
│   └── 手工曲奇饼干_20240627_143108_ref.png
├── clips/
│   ├── clip_01_水润保湿面霜_20240627_143022.mp4
│   ├── clip_02_水润保湿面霜_20240627_143022.mp4
│   ...
├── final/
│   ├── 水润保湿面霜_20240627_143022_final.mp4
│   ├── 智能降噪耳机_20240627_143045_final.mp4
│   └── 手工曲奇饼干_20240627_143108_final.mp4
└── batch_summary_20240627_143022.json  # 批量执行报告
```

---

## 💾 模板保存与加载（快速复用）

### 保存模板

交互式生成一次后，保存参数为模板：

```bash
# 先交互式运行一次，输入产品信息
python one_click_create.py --style kubrick --mode pro

# 然后保存为模板（在交互式运行时添加 --save 参数）
# 注意：--save 需要与 --load 配合使用，或在交互模式中手动记录参数
```

更简单的方式：直接创建一个 JSON 模板文件：

```json
{
  "product_info": {
    "name": "智能降噪耳机",
    "type": "科技",
    "selling_point": "40dB深度降噪，沉浸式音乐体验",
    "audience": "18-35岁男性",
    "style": "极客未来",
    "age": "26",
    "gender": "男",
    "outfit": "深色科技感卫衣+耳机"
  },
  "args": {
    "style": "kubrick",
    "duration": 8,
    "mode": "pro",
    "aspect_ratio": "9:16"
  },
  "created_at": "2026-06-27T14:30:00"
}
```

保存为 `templates/earbuds_kubrick.json`。

### 加载模板

```bash
# 直接使用模板生成，跳过所有交互输入
python one_click_create.py --load templates/earbuds_kubrick.json
```

输出：
```
🎬 可灵 AI 抖音广告视频 - 一键成片（模板模式）
📄 加载模板：templates/earbuds_kubrick.json

📋 已加载的参数：
  name: 智能降噪耳机
  type: 科技
  ...

🎥 电影风格：kubrick
⏱️ 片段时长：8s
🎞️ 生成模式：pro
```

### 模板修改后重新保存

```bash
python one_click_create.py --load templates/earbuds_kubrick.json --save templates/earbuds_kubrick_v2.json
```

---

## 📌 更新日志

### v7.0.0 (2026-07-02) - LLM 文案 + 豆包 TTS + 生成质量升级
- ✅ 新增 `llm_client.py`：接入 LLM 大模型生成广告文案（OpenAI 兼容接口），`--no-llm` 可降级内置模板
- ✅ 新增 `cinematic_language.py`：深度电影语言模块，18 种风格 Prompt 精细化构建（`build_cinematic_prompt_elements`）
- ✅ TTS 升级为豆包大模型语音合成 V3（`seed-tts-2.0`），配置 `VOLC_API_KEY` 即启用，否则自动降级 macOS say
- ✅ 鉴权升级：支持 JWT（AccessKey + SecretKey）+ 旧版 Bearer 双模式，通过 `.env` 注入
- ✅ 新增 `--best-of` 参数：每个分镜并行生成多候选，自动质量择优
- ✅ 新增 `--preview` / `-p`：快速预览模式，仅生成第 1 段，跳过后期
- ✅ 新增 `--serial`：强制串行生成，每段用上一段尾帧实现极致一致性
- ✅ 新增 `--stabilize` / `--no-stabilize`：视频稳定化 + 去闪烁（默认开启）
- ✅ 新增 `--rhythm-style`：节奏风格选择（fast / moderate / cinematic）
- ✅ 新增 `--multi-shot`：启用可灵多镜头 intelligence 分镜模式
- ✅ 新增 `--kling-model`：可指定可灵模型版本（如 `kling-v2-master`）
- ✅ 新增 `--strict` / `--no-strict`：严格模式控制，防止关键步骤静默降级
- ✅ 新增 `--brand-intro-outro`：成片首尾自动插入品牌开场（2s）和收尾动画（1.5s）
- ✅ 新增 `--ab-dim`：A/B 测试维度精细化（hook / style / script）
- ✅ `--target-duration` 新增 10/20/25 秒选项，配合 `--rhythm-style` 自动适配节奏模板
- ✅ 所有敏感配置迁移至 `.env`，`config.py` 仅保留非敏感默认值

### v6.0.0 (2026-06-28) - 抖音全链路优化 + 质量保障
- ✅ 新增 `ad_script.py` 广告脚本生成模块（5段式、痛点库、卖点拆解、标题/话题标签）
- ✅ 新增 `douyin_adapter.py` 抖音平台适配（字幕安全区、黄金3秒钩子、节奏优化、规格配置）
- ✅ 新增 `compliance_checker.py` 广告合规检测（极限词/敏感词、三级风险等级、替换建议）
- ✅ 新增 `quality_checker.py` 视频质量检测（清晰度/黑帧/冻结帧/闪烁/音频质量/人脸初筛）
- ✅ 新增 `--target-duration` 参数，支持 7/15/30/60 秒抖音黄金档时长
- ✅ 新增 `--hook` 钩子模板库（8 种开头类型：拷问/震惊/对比/展示/故事/挑战/明星/痛点）
- ✅ 新增 `--ab-versions` A/B 测试多版本输出（1-3 个版本）
- ✅ 新增 `--script-style` 广告脚本风格选择
- ✅ 新增 `--product-image` 商品参考图 + `--seed` 一致性控制
- ✅ 生成抖音标题和话题标签，可直接复制发布
- ✅ 移除快手相关代码，专注抖音平台

### v5.0.0 (2026-06-28) - BGM 系统 + 配音 + 后期全升级
- ✅ 新增 `bgm_client.py` BGM 音乐模块（FreeToUse API、品类自动选曲、本地缓存、淡入淡出）
- ✅ 新增 `tts_client.py` AI 口播配音（macOS say、5 种风格、5 种音色、字幕对齐、BGM 闪避）
- ✅ 新增字幕动画系统（4 种：pop 弹跳 / slide 侧滑 / fade 淡入 / highlight 高亮）
- ✅ 新增后期调色系统（9 种预设：暖色电影/冷色电影/复古/青橙/暗调/明亮/黑白/马卡龙）
- ✅ 新增音效设计 SFX（whoosh/impact/ding，动态生成）
- ✅ 新增卡点剪辑（转场/字幕/音效对齐 BGM 节拍）
- ✅ 新增封面图自动生成（最佳帧提取）
- ✅ 新增品牌 Logo 水印（可配置位置/大小/透明度/淡入淡出）
- ✅ 新增黑帧/冻结帧自动检测与裁剪
- ✅ 新增片段色彩匹配（color_match_clips）
- ✅ 新增费用/配额提示（KLING_PRICING）
- ✅ 新增字幕安全区适配（抖音小黄车避让）

### v4.0.0 (2026-06-27) - 美学参考库 + 双版本输出 + 测试
- ✅ 电影风格从 7 种扩展至 18 种（新增王家卫、塔可夫斯基、张艺谋、是枝裕和、昆汀、贾樟柯、侯孝贤、奉俊昊、维伦纽瓦、卢贝松、宫崎骏）
- ✅ 新增 `--list-styles` 命令，一键打印风格卡片库
- ✅ 新增 `--dual-output` 参数，同时生成 9:16 + 16:9 两个版本
- ✅ 新增产品类型预设：汽车、房产、教育、医疗
- ✅ 实现失败自动清理（cleanup_output），abort 时删除本次产物
- ✅ 新增 `tests/test_core.py`，23 个单元测试覆盖核心逻辑
- ✅ 更新 README，补充 18 种风格速查表、产品预设表、双版本输出说明

### v3.0.0 (2026-06-27) - 批量生成 + 模板系统
- ✅ 新增 `batch.py` 批量生成脚本，支持 YAML 配置 + 并发控制 + fail_fast
- ✅ `one_click_create.py` 支持 `--save` / `--load` 模板保存与加载
- ✅ 提取核心逻辑到 `run_one_click_create()`，供 batch.py 复用
- ✅ 新增项目结构文档

### v2.0.0 (2026-06-27) - 一键成片版本
- ✅ 新增 `one_click_create.py` 一键成片主脚本
- ✅ 新增 `kling_client.py` 可灵 API 客户端
- ✅ 新增 `video_merger.py` ffmpeg 拼接模块
- ✅ 支持自动生成角色定妆照 + 5 个分镜片段
- ✅ 支持自动拼接 + 转场 + 字幕 + BGM
- ✅ 支持产品类型预设（美妆/食品/科技/服装/App）

### v1.0.0 (2026-06-27) - 初始版本
- 手动复制 Prompt 到可灵的模板版本

---

## 🙋 需要帮助？

- 查看 `.env.example` 了解所有可配置的 Key 和选项
- 查看 `config.py` 了解非敏感默认参数
- 查看 `kling_client.py` 了解 API 调用和 JWT 鉴权细节
- 查看 `video_merger.py` 了解视频处理参数
- 查看 `cinematic_language.py` 了解电影风格 Prompt 构建逻辑
- 查看 `llm_client.py` 了解 LLM 文案生成接口配置
- 运行 `python one_click_create.py --help` 查看完整参数帮助

---

## 📄 License

MIT License - 自由使用和修改
