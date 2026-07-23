"""
可灵 AI 抖音广告视频 - 自动化项目配置文件

使用说明：
1. 复制 config.example.py 为 config.py
2. 填入你的可灵 API Key
3. 运行 one_click_create.py 即可一键成片
"""

import os
import logging
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ============================================================
# 项目路径
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = PROJECT_ROOT / "output"

# ── 新增：系统模块数据路径 ──
ASSET_LIBRARY_PATH = PROJECT_ROOT / "data" / "assets"
FEEDBACK_DB_PATH = PROJECT_ROOT / "data" / "feedback.db"
EXPERIMENT_DB_PATH = PROJECT_ROOT / "data" / "experiments.db"
LOCAL_ASSET_INDEX_PATH = PROJECT_ROOT / "data" / "local_asset_index"

# ============================================================
# 可灵 API 配置
# ============================================================

# 可灵官方 API 地址
KLING_BASE_URL = os.getenv("KLING_BASE_URL", "https://api-beijing.klingai.com")

# 鉴权方式一（推荐）：AccessKey + SecretKey → 自动生成 JWT
# 在可灵开放平台 https://app.klingai.com/cn/dev 获取
KLING_ACCESS_KEY = os.getenv("KLING_ACCESS_KEY", "")   # ak-xxxxxxxx
KLING_SECRET_KEY = os.getenv("KLING_SECRET_KEY", "")   # sk-xxxxxxxx

# 鉴权方式二（兼容）：直接使用 API Key（Bearer token）
# 部分旧版 Key 格式为 api-key-kling-xxx，直接 Bearer 即可
KLING_API_KEY = os.getenv("KLING_API_KEY", "")  # 留空时自动使用 ACCESS_KEY+SECRET_KEY

# API 端点
KLING_IMAGE_ENDPOINT = "/v1/images/generations"
KLING_IMAGE_QUERY_ENDPOINT = "/v1/images/generations/{}"  # P0-2 修复：图片查询独立端点
KLING_VIDEO_ENDPOINT = "/v1/videos/omni-video"          # #1 修复：Omni 体系正确端点
KLING_QUERY_ENDPOINT = "/v1/videos/omni-video/{}"       # #1 修复：查询端点与创建端点保持一致

# 模型版本
KLING_IMAGE_MODEL = "kling-v2-1"  # 图片生成模型：kling-v2-1 / kling-v3
KLING_VIDEO_MODEL = "kling-v3-omni"  # #2 修复：全小写，Omni 体系正确模型名

# ============================================================
# API 定价配置（可灵官方价格，仅供参考）
# ============================================================
# 图片生成：约 0.05 元/张（std 模式）
# 视频生成：按秒计费，不同模式价格不同
# 以下为估算价格，实际以官方为准
KLING_PRICING = {
    "image": {
        "std": 0.05,  # 元/张
        "pro": 0.10,  # 元/张
    },
    "video": {
        "std": 0.30,   # 元/秒
        "pro": 0.60,   # 元/秒
        "4k":  1.20,   # 元/秒
    },
}

# 生成参数默认值
DEFAULT_VIDEO_DURATION = 5  # 单片段时长（秒）
DEFAULT_ASPECT_RATIO = "9:16"  # 竖屏
DEFAULT_MODE = "pro"  # #14 修复：std 已废弃，默认改 pro

# 一致性控制默认值
DEFAULT_IMAGE_FIDELITY = 0.9  # 参考图 fidelity [0,1]
DEFAULT_HUMAN_FIDELITY = 0.9  # 人物 fidelity [0,1]
DEFAULT_SEED = None  # 随机种子（None 表示不固定）

# 参考图数量上限（性价比策略：生图远便宜于生视频，用更多参考图换视频一次成功率）
# 主角色2张(正面+角度) + 产品1张 + 关键帧1张 + 场景连续帧1张 = 5张最优组合
MAX_REF_IMAGES = 5

# ============================================================
# LLM 文案生成配置
# ============================================================

LLM_ENABLED = True  # 是否启用 LLM 生成文案（False 时走纯模板）
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://apihub.agnes-ai.com/v1")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "agnes-2.0-flash")
LLM_TEMPERATURE = 0.8
LLM_MAX_TOKENS = 2000
LLM_TIMEOUT = 60
LLM_MAX_RETRIES = 2

# ============================================================
# 本地视频素材视觉分析配置
# ============================================================

VISION_ENABLED = os.getenv("VISION_ENABLED", "false").lower() in ("1", "true", "yes", "on")
VISION_BASE_URL = os.getenv("VISION_BASE_URL", "")
VISION_API_KEY = os.getenv("VISION_API_KEY", "")
VISION_MODEL = os.getenv("VISION_MODEL", "")
VISION_TIMEOUT = int(os.getenv("VISION_TIMEOUT", "60"))
VISION_MAX_RETRIES = int(os.getenv("VISION_MAX_RETRIES", "2"))

LOCAL_ASSET_WINDOW_SECONDS = float(os.getenv("LOCAL_ASSET_WINDOW_SECONDS", "4"))
LOCAL_ASSET_WINDOW_STRIDE = float(os.getenv("LOCAL_ASSET_WINDOW_STRIDE", "2"))
LOCAL_ASSET_CONTACT_SHEET_FRAMES = int(os.getenv("LOCAL_ASSET_CONTACT_SHEET_FRAMES", "12"))
LOCAL_ASSET_MAX_WINDOWS = int(os.getenv("LOCAL_ASSET_MAX_WINDOWS", "120"))

# ============================================================
# 视频拼接配置
# ============================================================

# 输出视频参数
OUTPUT_RESOLUTION = "1080x1920"  # 9:16 竖屏
OUTPUT_FPS = 30
OUTPUT_BITRATE = "10M"  # 抖音竖屏 1080p 推荐 8-12Mbps（P2-9：提升码率防止快速转场段块状失真）

# 转场参数
TRANSITION_DURATION = 0.3  # 默认转场时长（秒）

# 转场风格：fast（快节奏）/ cinematic（电影感）/ default（默认均衡）
TRANSITION_STYLE = "default"

# 默认转场序列（按顺序用于各片段之间，循环使用）
# 15 种可用转场：fade / dissolve / fadeblack / fadewhite /
#   slideright / slideleft / slideup / slidedown /
#   circlecrop / circleclose / zoomin / zoomout /
#   wipeleft / wiperight / rectcrop
DEFAULT_TRANSITIONS = [
    {"type": "dissolve", "duration": 0.3},
    {"type": "slideright", "duration": 0.25},
    {"type": "zoomin", "duration": 0.4},
    {"type": "fadeblack", "duration": 0.5},
]

# 字幕参数
SUBTITLE_FONT_SIZE = 70
SUBTITLE_COLOR = "white"
SUBTITLE_STROKE_COLOR = "black"
SUBTITLE_STROKE_WIDTH = 3

# 默认字幕模板（5 段式，按总时长均匀分布）
DEFAULT_SUBTITLE_TEMPLATE = [
    {"text": "你是不是也...？", "segment": 0, "ratio_start": 0.1, "ratio_end": 0.7},
    {"text": "直到我用了...", "segment": 1, "ratio_start": 0.1, "ratio_end": 0.7},
    {"text": "{selling_point}", "segment": 2, "ratio_start": 0.0, "ratio_end": 1.0},
    {"text": "真的绝了", "segment": 3, "ratio_start": 0.2, "ratio_end": 0.8},
    {"text": "点击左下角购买", "segment": 4, "ratio_start": 0.2, "ratio_end": 0.8},
]

# BGM 参数
BGM_VOLUME = 0.55         # BGM 音量比例（纯 BGM 场景推荐 0.5-0.7）
BGM_VOLUME_VOICEOVER = 0.2  # 口播场景 BGM 基础音量（人声清晰度优先，sidechain 在此基础上额外压低）
BGM_PATH = "assets/bgm.mp3"  # 相对于项目根目录（本地 fallback，优先级低于 API 选曲）
BGM_FADE_IN = 1.0  # BGM 淡入时长（秒）
BGM_FADE_OUT = 2.0  # BGM 淡出时长（秒）
BGM_CACHE_DIR = "output/bgm_cache"  # BGM 本地缓存目录（相对于项目根目录）
SFX_CACHE_DIR = "output/sfx_cache"  # 音效本地缓存目录（相对于项目根目录）

# ============================================================
# 提示词模板配置
# ============================================================

# 负面提示词（通用视频生成）
# 注意：禁止加入 "multiple people" / "crowd" / "group shot"，与多角色功能矛盾
# 优化原则：按权重排序（影响越大越靠前）、去冗余、去冲突（电影风格相关的词不加，交给风格系统控制）
NEGATIVE_PROMPT = (
    # ── P0：人物/产品一致性（最影响可用性，放最前面）──
    "different person, different face, identity change, inconsistent facial features, "
    "changing hair color, changing hairstyle, changing clothes, different outfit, "
    "face swap, morphing face, face morphing, "
    # ── P0：时间一致性（AI 视频重灾区）──
    "flickering, flicker, temporal inconsistency, jitter, jittering, "
    "melting skin, warped body, shape shifting, object morphing, size changing, "
    "ghosting, double exposure artifact, color banding, "
    "sudden jump cut artifact, shaking artifacts, pixel noise, "
    # ── P1：人体结构（Kling 重灾区：手/指/肢）──
    "finger distortion, extra fingers, missing fingers, fused fingers, deformed hands, "
    "extra hands, missing hands, extra limbs, missing limbs, extra arms, extra legs, "
    "disconnected limbs, floating limbs, floating fingers, broken fingers, "
    "bad anatomy, wrong proportions, unrealistic proportions, long neck, "
    # ── P1：面部质量（人物镜头的核心）──
    "distorted face, deformed, malformed, ugly, poorly drawn, "
    "cross-eyed, asymmetric eyes, uneven eyes, lazy eye, "
    "deformed mouth, crooked teeth, missing teeth, "
    "plastic skin, smooth waxy skin, doll-like face, "
    "dead eyes, glassy eyes, unrealistic eyes, "
    # ── P1：文字水印（抖音广告硬伤）──
    "text watermark, unrelated logo, unrelated brand mark, "
    "Chinese text, watermark text, in-frame text overlay, embedded subtitles, "
    "sign text, poster text, newspaper text, "
    # ── P2：基础画质──
    "blurry, low quality, out of focus, soft focus, "
    "overexposed, underexposed, washed out, faded colors, "
    "color cast, yellow tint, green tint, magenta tint, "
    "motion blur artifacts, motion smear, "
    # ── P2：镜头质量──
    "handheld camera shake, camera shake, shaky footage, "
    "lens distortion, barrel distortion, fisheye distortion, "
    "chromatic aberration, color fringing, purple fringing, "
    # ── P2：场景/物理──
    "floating objects, defying gravity, physics-defying, "
    "transparent objects, see-through, clipping, "
    "mirror artifact, reflection error, "
    # ── P2：构图问题──
    "cropped head, cut off head, cut off face, "
    "out of frame, partially visible, truncated subject"
)

# 角色定妆照负面提示词（图片生成专用，更强调肖像质量）
CHARACTER_NEGATIVE_PROMPT = (
    # P0：人物一致性
    "different person, different face, identity change, different outfit, "
    # P0：面部结构
    "distorted face, deformed, malformed, ugly, poorly drawn, "
    "cross-eyed, asymmetric eyes, uneven eyes, lazy eye, "
    "deformed mouth, crooked teeth, missing teeth, "
    "plastic skin, smooth waxy skin, doll-like face, "
    "dead eyes, glassy eyes, unrealistic eyes, "
    # P1：人体结构
    "finger distortion, extra fingers, missing fingers, fused fingers, deformed hands, "
    "extra hands, missing hands, extra limbs, missing limbs, "
    "bad anatomy, wrong proportions, unrealistic proportions, "
    # P1：文字水印
    "text watermark, unrelated logo, unrelated brand mark, "
    "Chinese text, watermark text, in-frame text overlay, "
    # P2：基础画质
    "blurry, low quality, out of focus, "
    "overexposed, underexposed, washed out, "
    "chromatic aberration, color fringing, "
    # P2：构图
    "cropped head, cut off head, cut off face, "
    "out of frame, partially visible"
)

# 一致性描述模板（用于生成片段 Prompt 时注入）
CONSISTENCY_TEMPLATES = {
    "character": (
        "EXACT same person from reference image, identical facial features, "
        "same hairstyle, same outfit, same {gender} as reference"
    ),
    "product": (
        "{name} with EXACT same {brand_packaging}, identical packaging design, "
        "same color and logo placement, {brand} brand product"
    ),
    "brand": (
        "{brand} brand aesthetic throughout, {primary_color}, "
        "brand elements subtly integrated"
    ),
    "scene": (
        "SAME location and environment as reference image, identical room/scene setup, "
        "same furniture and props in same positions, same lighting direction and color temperature, "
        "same time of day, consistent camera angle and perspective, "
        "continuous scene, seamless transition from previous shot"
    ),
    "lighting": (
        "EXACT same lighting as reference image, same key light position, "
        "same fill light intensity, same color temperature, same shadow direction, "
        "consistent mood and atmosphere"
    ),
}

# 场景连续性策略配置
SCENE_CONTINUITY_CONFIG = {
    # 是否使用全局场景锚点（推荐 True，防止场景漂移）
    "use_scene_anchor": True,
    # 全局场景锚点从第几段提取（默认第 1 段，因为第 1 段场景最完整）
    "anchor_clip_index": 0,
    # 从锚点片段提取几张关键帧作为场景参考（建议 1 张，多了反而稀释权重）
    "anchor_keyframes": 1,
    # 是否同时使用前一段最后一帧（局部衔接）
    "use_previous_last_frame": True,
    # 场景参考图的 fidelity（越低越自由，越高越严格）
    "scene_fidelity": 0.7,
    # 是否在 Prompt 中注入强场景一致性描述
    "inject_scene_prompt": True,
    # 转场类型（用于掩盖跳切）
    "transition_type": "dissolve",
    # 转场时长（秒），抖音快节奏标准 0.2-0.3s
    "transition_duration": 0.25,
}

# ============================================================
# 品牌一致性配置
# ============================================================

BRAND_CONFIG = {
    "name": "我的品牌",
    "logo_path": "assets/logo.png",  # 品牌 Logo 图片路径（PNG 透明底，留空则不显示）
    "logo_description": "minimalist brand logo, clean typography, appears subtly in bottom right corner",
    "primary_color": "#FF6B6B",   # 品牌主色（HEX）
    "secondary_color": "#4ECDC4", # 品牌辅助色（HEX）
    "accent_color": "#4ECDC4",    # 强调色/高亮色（字幕花字用），默认同 secondary_color
    "slogan": "卓越品质，值得拥有",
    "packaging_description": "consistent product packaging, same color and design",
    # Logo 水印配置
    "logo_watermark": {
        "enabled": False,  # 是否启用 Logo 水印（需要配置 logo_path）
        "position": "top_right",  # 位置：top_left / top_right / bottom_left / bottom_right
        "size_ratio": 0.08,  # Logo 宽度占视频宽度的比例
        "margin_ratio": 0.03,  # 边距比例（相对视频宽/高）
        "opacity": 0.9,  # 透明度 0-1
        "fade_in": 0.5,  # 淡入时长（秒）
        "fade_out": 0.5,  # 淡出时长（秒）
    },
}

# ============================================================
# 调色预设
# ============================================================

COLOR_GRADING_PRESETS = {
    "none": {
        "name": "无调色",
        "brightness": 1.0,
        "contrast": 1.0,
        "saturation": 1.0,
        "temperature": 0,  # -1 冷 ~ 1 暖
        "tint": 0,  # -1 绿 ~ 1 品红
        "gamma": 1.0,
        "color_overlay": None,  # (hex_color, opacity)
    },
    "warm_cinematic": {
        "name": "暖色电影感",
        "brightness": 1.05,
        "contrast": 1.15,
        "saturation": 0.9,
        "temperature": 0.25,
        "tint": 0.05,
        "gamma": 1.05,
        "color_overlay": ("#FFA500", 0.05),  # 橙色叠加
    },
    "cool_cinematic": {
        "name": "冷色电影感",
        "brightness": 0.95,
        "contrast": 1.2,
        "saturation": 0.85,
        "temperature": -0.2,
        "tint": -0.05,
        "gamma": 0.95,
        "color_overlay": ("#1E90FF", 0.05),  # 蓝色叠加
    },
    "vintage": {
        "name": "复古胶片",
        "brightness": 1.0,
        "contrast": 1.1,
        "saturation": 0.7,
        "temperature": 0.15,
        "tint": 0.1,
        "gamma": 1.1,
        "color_overlay": ("#D2B48C", 0.08),  # 棕褐色
    },
    "teal_orange": {
        "name": "青橙色调（好莱坞）",
        "brightness": 1.0,
        "contrast": 1.25,
        "saturation": 1.1,
        "temperature": 0.1,
        "tint": 0,
        "gamma": 1.0,
        "color_overlay": None,
        # 青橙色调通过 colorbalance 实现
        "shadows_red": -0.08,
        "shadows_green": 0.05,
        "shadows_blue": 0.12,
        "highlights_red": 0.1,
        "highlights_green": 0.03,
        "highlights_blue": -0.08,
    },
    "moody": {
        "name": "暗调情绪",
        "brightness": 0.85,
        "contrast": 1.3,
        "saturation": 0.75,
        "temperature": -0.1,
        "tint": 0,
        "gamma": 0.9,
        "color_overlay": ("#2F4F4F", 0.06),
    },
    "bright_clean": {
        "name": "明亮清新",
        "brightness": 1.15,
        "contrast": 1.05,
        "saturation": 1.1,
        "temperature": 0.05,
        "tint": -0.02,
        "gamma": 1.05,
        "color_overlay": None,
    },
    "pastel": {
        "name": "马卡龙",
        "brightness": 1.1,
        "contrast": 0.9,
        "saturation": 0.8,
        "temperature": 0.05,
        "tint": 0.03,
        "gamma": 1.1,
        "color_overlay": ("#FFB6C1", 0.04),  # 粉色
    },
    "noir": {
        "name": "黑色电影",
        "brightness": 0.9,
        "contrast": 1.4,
        "saturation": 0,  # 黑白
        "temperature": 0,
        "tint": 0,
        "gamma": 0.95,
        "color_overlay": None,
    },
}

# 默认调色预设
DEFAULT_COLOR_GRADING = "warm_cinematic"

# ============================================================
# 钩子模板库（Hook Templates）
# ============================================================

HOOK_TEMPLATES = {
    "question": {
        "name": "灵魂拷问式",
        "description": "开头提出痛点问题，引发观众共鸣和好奇",
        "hook_subtitle": "你是不是也...？",
        "camera_type": "push",   # #9 修复：标注镜头语言，不同 hook 类型应有差异
        "hook_prompt": (
            "close-up shot, slow push in, {character} looking directly at camera with frustrated expression, "
            "head slightly shaking, relatable pain point, {preset_scene} scene, {preset_lighting}, "
            "realistic lifestyle style, intense eye contact, grab attention in first 3 seconds, "
            "9:16 vertical, close-up on face, "
            "{character_consistency}, {brand_consistency}"
        ),
        "tone": "empathetic",
        "best_for": ["美妆", "个护", "家居", "食品"],
    },
    "shocking": {
        "name": "震惊反差式",
        "description": "用惊人的事实或对比开场，制造认知冲击",
        "hook_subtitle": "大多数人都不知道的事！",
        "camera_type": "static",  # #9 震惊反差用静止镜头，动作本身就是驱动力
        "hook_prompt": (
            "extreme close-up, static shot, {character} eyes wide with shock and disbelief, "
            "hand covering mouth, dramatic reveal moment, {preset_scene} scene, "
            "dramatic lighting with high contrast, "
            "viral TikTok style, mind-blown expression, 9:16 vertical, "
            "{character_consistency}, {brand_consistency}"
        ),
        "tone": "dramatic",
        "best_for": ["数码", "家居", "食品", "美妆"],
    },
    "before_after": {
        "name": "前后对比式",
        "description": "直接展示使用前后的巨大差异，视觉冲击力强",
        "hook_subtitle": "这变化也太大了吧！",
        "camera_type": "static",  # #9 对比式用静止镜头，让左右帧公平
        "hook_prompt": (
            "split screen style, left side shows {character} with problem/tired look, "
            "right side shows {character} glowing and happy after using product, "
            "dramatic before and after comparison, {preset_scene} scene, "
            "clean composition, side by side comparison, 9:16 vertical, "
            "{character_consistency}, {brand_consistency}"
        ),
        "tone": "transformative",
        "best_for": ["美妆", "个护", "健身", "家居清洁"],
    },
    "demonstration": {
        "name": "效果展示式",
        "description": "直接展示产品最惊艳的效果，用画面说话",
        "hook_subtitle": "这效果也太惊人了！",
        "camera_type": "orbit",   # #9 展示式用 orbit，360度展示产品细节
        "hook_prompt": (
            "extreme close-up macro shot, orbital camera, {name} in action, "
            "visually satisfying product demonstration, {demo_action}, "
            "slow motion reveal of amazing result, "
            "soft product lighting, commercial photography style, "
            "ASMR visual, satisfying to watch, 9:16 vertical, "
            "{product_consistency}, {brand_consistency}"
        ),
        "tone": "satisfying",
        "best_for": ["美妆", "食品", "数码", "家居"],
    },
    "story": {
        "name": "故事叙述式",
        "description": "用一个小故事开场，引导观众代入情感",
        "hook_subtitle": "那天我终于明白了...",
        "camera_type": "pull",    # #9 故事式用 pull back，遇家源挂感
        "hook_prompt": (
            "medium shot, slow pull back, {character} looking thoughtfully into distance, "
            "contemplative expression, storytelling atmosphere, {preset_scene} scene, "
            "soft natural lighting, warm tones, emotional and relatable, "
            "vlog style opening, 9:16 vertical, "
            "{character_consistency}, {brand_consistency}"
        ),
        "tone": "emotional",
        "best_for": ["美妆", "个护", "食品", "家居"],
    },
    "challenge": {
        "name": "挑战测试式",
        "description": "发起一个挑战或极限测试，展现产品硬核实力",
        "hook_subtitle": "敢不敢来挑战？！",
        "hook_prompt": (
            "dynamic action shot, {character} holding {name} with determined expression, "
            "ready for a challenge, intense and energetic, {preset_scene} scene, "
            "dramatic lighting with strong highlights, "
            "experiment/test vibe, bold and confident, 9:16 vertical, "
            "{character_consistency}, {product_consistency}, {brand_consistency}"
        ),
        "tone": "energetic",
        "best_for": ["数码", "家居清洁", "个护", "食品"],
    },
    "celeb_style": {
        "name": "明星同款式",
        "description": "营造明星/博主推荐的信任感和种草感",
        "hook_subtitle": "博主都在推的秘密！",
        "hook_prompt": (
            "glamour shot, {character} with flawless look, holding {name} elegantly, "
            "celebrity endorsement vibe, red carpet aesthetic, "
            "soft ring light, beauty influencer style, polished and aspirational, "
            "9:16 vertical, close-up on face and product, "
            "{character_consistency}, {product_consistency}, {brand_consistency}"
        ),
        "tone": "aspirational",
        "best_for": ["美妆", "个护", "时尚", "食品"],
    },
    "pain_point": {
        "name": "痛点直击式",
        "description": "精准戳中用户日常痛点，让观众觉得'说的就是我'",
        "hook_subtitle": "谁懂啊！这也太烦了",
        "hook_prompt": (
            "close-up, {character} struggling with a daily annoyance, "
            "frustrated and annoyed expression, relatable everyday problem, "
            "{preset_scene} scene, natural lighting, "
            "authentic and real, like looking in a mirror, "
            "9:16 vertical, {character_consistency}, {brand_consistency}"
        ),
        "tone": "relatable",
        "best_for": ["家居", "个护", "食品", "数码"],
    },
}

# 默认钩子类型
DEFAULT_HOOK_TYPE = "question"

# ============================================================
# 产品类型预设
# ============================================================

PRODUCT_PRESETS = {
    "美妆": {
        "style": "清新自然",
        "lighting": "soft natural lighting",
        "scene": "clean vanity table with soft light",
        "demo_action": "applying product on hand, showing texture",
        "result": "skin looks radiant and glowing, confident smile",
    },
    "食品": {
        "style": "warm and cozy",
        "lighting": "warm natural sunlight",
        "scene": "wooden kitchen table",
        "demo_action": "taking a bite, showing texture",
        "result": "satisfied expression, happy smile",
    },
    "家居": {
        "style": "warm cozy",
        "lighting": "soft natural window light",
        "scene": "clean modern living room",
        "demo_action": "using the product, showing cleaning effect",
        "result": "space looks tidy and fresh, relaxed expression",
    },
    "数码": {
        "style": "modern tech",
        "lighting": "cool tech lighting",
        "scene": "minimalist modern desk",
        "demo_action": "touching screen, showing features",
        "result": "task completed, satisfied nod",
    },
    "个护": {
        "style": "clean fresh",
        "lighting": "soft natural lighting",
        "scene": "clean bathroom counter",
        "demo_action": "applying product, showing texture",
        "result": "feeling fresh and confident, relaxed smile",
    },
    "服饰": {
        "style": "fashion forward",
        "lighting": "studio soft lighting",
        "scene": "clean white background",
        "demo_action": "showing outfit details, turning around",
        "result": "confident pose, perfect look",
    },
    "app": {
        "style": "clean modern",
        "lighting": "bright natural light",
        "scene": "coffee shop table",
        "demo_action": "swiping phone screen, showing interface",
        "result": "task done, relaxed expression",
    },
    "汽车": {
        "style": "dynamic luxury",
        "lighting": "golden hour sunlight",
        "scene": "coastal highway or city skyline",
        "demo_action": "driving smoothly, showing dashboard and exterior",
        "result": "powerful and free, confident smile",
    },
    "房产": {
        "style": "warm homey",
        "lighting": "soft window light",
        "scene": "modern living room with natural light",
        "demo_action": "walking through space, touching furniture",
        "result": "peaceful and content, feeling at home",
    },
    "教育": {
        "style": "bright inspiring",
        "lighting": "classroom natural light",
        "scene": "modern classroom or library",
        "demo_action": "studying intently, raising hand, interacting",
        "result": "enlightened and motivated, smiling with understanding",
    },
    "医疗": {
        "style": "clean professional",
        "lighting": "bright clinical lighting",
        "scene": "modern clinic or lab",
        "demo_action": "professional demonstration, caring interaction",
        "result": "trustworthy and reassuring, professional smile",
    },
    "default": {
        "style": "modern minimalist",
        "lighting": "natural lighting",
        "scene": "lifestyle setting",
        "demo_action": "using the product naturally",
        "result": "satisfied expression",
    },
}

# ============================================================
# 分镜结构配置
# ============================================================
# 每个片段定义：
#   camera: 运镜类型（push/pull/orbit/static），对应电影风格中的 camera_push/camera_pull/camera_orbit
#   narrative: 叙事角色（hook/turning_point/showcase/result/cta）
#   base_prompt: 基础 prompt 模板，可用 {character} {name} {preset_scene} {preset_lighting} {demo_action} {selling_point} {preset_result} 占位
#   可通过增删改此列表来调整分镜数量、节奏和内容

CLIP_STRUCTURE = [
    {
        "camera": "push",
        "narrative": "hook",
        "base_prompt": (
            "close-up on face, slow push in, {character} looking at phone with frustrated expression, "
            "confused and stressed, {preset_scene} scene visible in background, {preset_lighting}, "
            "realistic lifestyle style, tense mood, grab attention in first 3 seconds, "
            "9:16 vertical, "
            "{character_consistency}, {brand_consistency}"
        ),
    },
    {
        "camera": "push",
        "narrative": "turning_point",
        "base_prompt": (
            "medium shot, {character} turns and picks up {name}, "
            "eyes light up with surprise, {name} displayed in hand, "
            "{preset_scene} environment around them, "
            "warm lighting, lifestyle photography style, emotional turning point, "
            "same person from reference image, 9:16 vertical"
        ),
    },
    {
        "camera": "orbit",
        "narrative": "showcase",
        "base_prompt": (
            "camera orbit around {name}, {name} placed on {preset_scene}, "
            "{character} fingers operating, {demo_action}, "
            "close-up on product details, soft product lighting, commercial photography style, "
            "highlight {selling_point}, same person from reference image, 9:16 vertical"
        ),
    },
    {
        "camera": "pull",
        "narrative": "result",
        "base_prompt": (
            "medium shot, slow pull back, {character} smiling with satisfaction, {name} placed beside, "
            "{preset_result}, {preset_scene} environment becoming visible as camera pulls back, "
            "warm golden hour lighting, emotional climax, "
            "same person from reference image, 9:16 vertical"
        ),
    },
    {
        "camera": "push",
        "narrative": "cta",
        "base_prompt": (
            "{character} holding {name} and looking directly into camera with confident smile, "
            "pointing at {name} with one hand, enthusiastic and welcoming gesture, "
            "\"shop now\" energy, urgency and excitement, bright commercial lighting, "
            "{preset_scene} scene, product clearly visible, "
            "close-up on face and product together, authentic endorsement moment, "
            "9:16 vertical, {character_consistency}, {product_consistency}, {brand_consistency}"
        ),
    },
]

# ============================================================
# 电影风格预设
# ============================================================

CINEMATIC_STYLES = {
    "hitchcock": {
        "name": "希区柯克",
        "name_en": "Hitchcock",
        "description": "心理悬疑大师，推轨变焦制造焦虑",
        "camera_push": "Hitchcock dolly zoom: camera pushes in slowly while background visually stretches, Vertigo effect, psychological tension",
        "camera_pull": "Hitchcock pull back: camera pulls back to reveal isolation, subject looks small and vulnerable, voyeuristic tension",
        "camera_orbit": "Hitchcock orbit: camera circles subject at distance, paranoid surveillance, voyeuristic tension, 1960s thriller style",
        "transition_match": "Spiral focus transition: camera spirals into subject's face, Vertigo-style, obsession theme",
        "transition_light": "Lens flare transition: camera moves toward bright light, lens flare washes out, mystery and discovery",
        "lighting": "cold gray tones, high contrast, chiaroscuro, side-backlight creating deep shadows on face",
        "color": "desaturated colors, green tint, high contrast",
        "bgm_keywords": ['suspense', 'thriller', 'mysterious', 'tension', 'dark ambient'],
        "mood": "psychological tension, dread, voyeuristic unease",
    },
    "kubrick": {
        "name": "库布里克",
        "name_en": "Kubrick",
        "description": "对称构图大师，单点透视，仪式感",
        "camera_push": "Kubrick one-point perspective dolly: camera pushes straight down center of symmetrical corridor, everything aligns to vanishing point, 2001 style, cosmic dread",
        "camera_pull": "Kubrick dolly out: camera pulls back from subject to reveal vast symmetrical space, Barry Lyndon style, isolation and scale",
        "camera_orbit": "Kubrick symmetrical orbit: camera circles subject maintaining perfect center composition, 2001-style, cosmic grandeur",
        "transition_match": "Match cut from 2001: A Space Odyssey: cut from object flying up to spaceship, matching shape and movement, Kubrick style, evolution theme",
        "transition_light": "Kubrick time jump: sudden cut from bright daylight to dark night, same location, same composition, The Shining style",
        "lighting": "strong frontal key light, deep shadows on sides, dramatic chiaroscuro, Barry Lyndon candlelight style",
        "color": "high saturation red/blue/green, or warm candlelight yellow",
        "bgm_keywords": ['classical', 'epic', 'dramatic', 'orchestral', 'cinematic'],
        "mood": "cosmic dread, meticulous precision, fatalistic grandeur",
    },
    "spielberg": {
        "name": "斯皮尔伯格",
        "name_en": "Spielberg",
        "description": "娱乐与情感大师，拉轨揭示，平民英雄",
        "camera_push": "Spielberg push in: slow push toward subject, wonder building, Jaws style, anticipation and dread",
        "camera_pull": "Spielberg dolly out reveal: camera slowly pulls back from subject's face to reveal vast isolation, Jaws style, awe and wonder, subject small against environment",
        "camera_orbit": "Spielberg orbit: camera circles subject with rim light, E.T. style, magical halo, wonder and discovery",
        "transition_match": "Spielberg lens flare transition: camera moves toward bright light source, lens flare washes out image, Jaws/Close Encounters style",
        "transition_light": "Wide reveal: camera pulls back from extreme close-up to show full context, Jurassic Park style, awe and spectacle",
        "lighting": "rim light creating halo around subject, side-backlight, magical glow, warm golden hour",
        "color": "warm yellow + teal blue contrast, or soft natural light",
        "bgm_keywords": ['adventure', 'magical', 'orchestral', 'wonder', 'cinematic'],
        "mood": "wonder, awe, emotional catharsis, Spielberg magic",
    },
    "aronofsky": {
        "name": "阿伦诺夫斯基",
        "name_en": "Aronofsky",
        "description": "心理惊悚大师，快速推轨，分裂半透镜",
        "camera_push": "Aronofsky rapid push-in: fast dolly toward subject's face, Requiem for a Dream style, claustrophobic tension, split diopter, foreground and background both sharp, frantic energy",
        "camera_pull": "Aronofsky pull back: fast pull back from extreme close-up, paranoid atmosphere, Pi style, obsession and compulsion",
        "camera_orbit": "Aronofsky tight orbit: camera circles rapidly, split diopter, compressed space, Requiem drug sequence style, paranoid surveillance",
        "transition_match": "Aronofsky jump cut: rapid succession of similar frames, subject's expression intensifies, time compression, disorienting",
        "transition_light": "Eye match cut: cut from one character's eye to object, Aronofsky style, obsession theme, Pi/Requiem style",
        "lighting": "hard light, heavy shadows, high contrast, cold green or warm yellow, split diopter keeping foreground and background sharp",
        "color": "high contrast, desaturated with occasional vivid colors, green or yellow tint",
        "bgm_keywords": ['intense', 'dark electronic', 'experimental', 'psychological', 'tension'],
        "mood": "claustrophobic tension, paranoia, obsession, frantic energy",
    },
    "scorsese": {
        "name": "斯科塞斯",
        "name_en": "Scorsese",
        "description": "跟踪镜头大师， steadicam，街头叙事",
        "camera_push": "Scorsese push in: slow push through crowded scene, Goodfellas style, tracking through space, narrative momentum",
        "camera_pull": "Scorsese pull back: camera pulls back from subject in crowded environment, Taxi Driver style, urban isolation",
        "camera_orbit": "Scorsese tracking orbit: Steadicam tracking shot circling subject through environment, Goodfellas Copacabana style, smooth narrative flow",
        "transition_match": "Scorsese freeze frame: sudden freeze frame on subject's face, then cut to next scene, Goodfellas style, narrative punctuation",
        "transition_light": "Scorsese iris out: iris closes on subject, then opens on next scene, vintage cinema style, Taxi Driver style",
        "lighting": "practical lights from environment, neon signs, street lamps, naturalistic urban lighting, Raging Bull style",
        "color": "warm golden tones or cool blue urban night, vintage film stock look",
        "bgm_keywords": ['rock', 'classic rock', 'blues', 'urban', 'street'],
        "mood": "urban energy, narrative momentum, violent beauty, Catholic guilt",
    },
    "nolan": {
        "name": "诺兰",
        "name_en": "Nolan",
        "description": "实景特效大师， IMAX 比例，时间 Manipulation",
        "camera_push": "Nolan IMAX push in: massive IMAX push in on subject's face, practical effects, Inception style, overwhelming scale",
        "camera_pull": "Nolan pull back: camera pulls back from small subject to reveal massive practical set, Inception dream fold style, scale and disorientation",
        "camera_orbit": "Nolan IMAX orbit: camera circles subject on massive practical set, Interstellar style, cosmic scale, IMAX grandeur",
        "transition_match": "Nolan cross-cut: cut between two simultaneous actions, Inception style, parallel narrative tension",
        "transition_light": "Nolan practical effect transition: cut from dream to reality through practical effect, Inception kick style, temporal distortion",
        "lighting": "practical lights only, naturalistic, IMAX quality, high dynamic range, Dunkirk natural light",
        "color": "desaturated cool tones or warm practical lights, IMAX film stock",
        "bgm_keywords": ['epic', 'dramatic', 'orchestral', 'cinematic', 'powerful'],
        "mood": "grand scale, temporal disorientation, practical awe, intellectual tension",
    },
    "anderson": {
        "name": "韦斯·安德森",
        "name_en": "Wes Anderson",
        "description": "对称构图女王， pastel 色彩，复古布景",
        "camera_push": "Wes Anderson push in: symmetrical push in on centered subject, Grand Budapest Hotel style, meticulous composition, pastel colors",
        "camera_pull": "Wes Anderson pull back: symmetrical pull back to reveal perfect tableau, Royal Tenenbaums style, emotional distance and beauty",
        "camera_orbit": "Wes Anderson orbit: camera circles subject maintaining perfect symmetry, Moonrise Kingdom style, nostalgic whimsy",
        "transition_match": "Wes Anderson wipe: iris wipe or horizontal wipe to next scene, Fantastic Mr. Fox style, playful transition",
        "transition_light": "Wes Anderson chapter title: text card with symmetrical framing, Grand Budapest style, narrative whimsy",
        "lighting": "soft even lighting, no harsh shadows, pastel color palette, vintage aesthetic",
        "color": "pastel pinks, yellows, blues, symmetrical color blocking",
        "bgm_keywords": ['whimsical', 'vintage', 'acoustic', 'indie folk', 'quirky'],
        "mood": "nostalgic whimsy, emotional distance, meticulous beauty, bittersweet",
    },
    "wong-kar-wai": {
        "name": "王家卫",
        "name_en": "Wong Kar-wai",
        "description": "霓虹美学大师，慢快门，都市孤独与浪漫",
        "camera_push": "Wong Kar-wai push in: slow push with step-printing effect, Chungking Express style, neon lights blur, urban loneliness and romance",
        "camera_pull": "Wong Kar-wai pull back: slow pull back from extreme close-up, rain-soaked streets, In the Mood for Love style, unspoken desire",
        "camera_orbit": "Wong Kar-wai orbit: camera circles through narrow hallway, Fallen Angels style, neon reflections, chaotic intimacy",
        "transition_match": "Wong Kar-wai match cut: cut from one character's face to another's matching expression, In the Mood for Love style, parallel longing",
        "transition_light": "Wong Kar-wai light leak: lens flare and light leak transition, 2001: A Space Odyssey style, temporal distortion",
        "lighting": "neon signs, rain reflections, low-key lighting, warm tungsten mixed with cool neon",
        "color": "saturated reds, greens, blues, high contrast, teal and orange",
        "bgm_keywords": ['jazz', 'blues', 'moody', 'atmospheric', 'noir'],
        "mood": "urban loneliness, unspoken desire, nostalgic romance, temporal dislocation",
    },
    "tarkovsky": {
        "name": "塔可夫斯基",
        "name_en": "Tarkovsky",
        "description": "诗意长镜头，自然意象，时间雕塑",
        "camera_push": "Tarkovsky slow push: extremely slow dolly, long take, nature imagery, Mirror style, poetic realism, water and fire motifs",
        "camera_pull": "Tarkovsky pull back: slow pull back from intimate detail to vast landscape, Stalker style, Zone atmosphere, desolate beauty",
        "camera_orbit": "Tarkovsky orbit: camera circles subject with long take, Solaris style, oceanic imagery, psychological depth",
        "transition_match": "Tarkovsky match cut: cut from one natural element to another, water to fire, Mirror style, thematic resonance",
        "transition_light": "Tarkovsky light transition: gradual light change over long take, Nostalghia style, passage of time",
        "lighting": "natural lighting, candlelight, overcast sky, long shadows, poetic realism",
        "color": "muted earth tones, desaturated, occasional vivid natural colors, water reflections",
        "bgm_keywords": ['ambient', 'minimal', 'meditative', 'atmospheric', 'classical'],
        "mood": "poetic realism, spiritual longing, temporal meditation, desolate beauty",
    },
    "zhang-yimou": {
        "name": "张艺谋",
        "name_en": "Zhang Yimou",
        "description": "东方色彩美学，对称构图，民俗仪式",
        "camera_push": "Zhang Yimou push in: symmetrical push in on vibrant color field, Hero style, red and gold dominance, poetic martial arts",
        "camera_pull": "Zhang Yimou pull back: pull back from intimate face to vast crowd, Raise the Red Lantern style, symmetrical courtyard",
        "camera_orbit": "Zhang Yimou orbit: camera circles subject in symmetrical frame, House of Flying Daggers style, peacock umbrella dance",
        "transition_match": "Zhang Yimou color match: cut from one vibrant color to another, Hero style, emotional progression through color",
        "transition_light": "Zhang Yimou lantern light: warm lantern light transition, Raise the Red Lantern style, intimate to vast",
        "lighting": "strong directional light, vibrant color blocking, lantern light, natural sunlight through lattice",
        "color": "vibrant reds, golds, yellows, blues, saturated earth tones, Chinese color symbolism",
        "bgm_keywords": ['chinese traditional', 'epic', 'orchestral', 'dramatic', 'folk'],
        "mood": "poetic martial arts, collective ritual, tragic romance, visual grandeur",
    },
    "koreeda": {
        "name": "是枝裕和",
        "name_en": "Koreeda",
        "description": "日常诗意，家庭纽带，克制温情",
        "camera_push": "Koreeda push in: slow push in on mundane detail, After the Storm style, domestic poetry, quiet observation",
        "camera_pull": "Koreeda pull back: pull back from family moment to reveal context, Shoplifters style, chosen family bonds",
        "camera_orbit": "Koreeda orbit: camera circles family at table, Our Little Sister style, seaside town, gentle rhythm",
        "transition_match": "Koreeda match cut: cut from one family member to another, After Life style, memory and loss",
        "transition_light": "Koreeda window light: soft window light transition, Nobody Knows style, childhood resilience",
        "lighting": "soft natural window light, overcast sky, indoor practical lights, gentle and diffused",
        "color": "muted earth tones, soft pastels, desaturated blues and greens, Japanese aesthetic",
        "bgm_keywords": ['piano', 'gentle', 'acoustic', 'peaceful', 'emotional'],
        "mood": "quiet observation, familial bonds, gentle melancholy, everyday heroism",
    },
    "tarantino": {
        "name": "昆汀",
        "name_en": "Tarantino",
        "description": "类型拼贴，暴力美学，流行文化引用",
        "camera_push": "Tarantino push in: steady push in on tense face, Pulp Fiction style, extreme close-up, pop culture reference",
        "camera_pull": "Tarantino pull back: pull back from trunk reveal, Pulp Fiction style, surprise and dark humor",
        "camera_orbit": "Tarantino orbit: camera circles with steadycam, Reservoir Dogs style, warehouse tension, pop soundtrack",
        "transition_match": "Tarantino match cut: cut from one violent act to another, Kill Bill style, genre pastiche",
        "transition_light": "Tarantino title card: bold text card transition, Pulp Fiction style, chapter break, irreverence",
        "lighting": "practical lights, neon signs, car headlights, high contrast, noir influences",
        "color": "saturated primary colors, black and white interludes, retro film stock",
        "bgm_keywords": ['rock', 'funk', 'soul', 'retro', 'classic'],
        "mood": "dark humor, genre pastiche, pop culture bravado, stylized violence",
    },
    "jia-zhangke": {
        "name": "贾樟柯",
        "name_en": "Jia Zhangke",
        "description": "中国社会变迁，纪实美学，边缘人物",
        "camera_push": "Jia Zhangke push in: slow push in on weathered face, Still Life style, documentary realism, Chinese social change",
        "camera_pull": "Jia Zhangke pull back: pull back from intimate moment to demolition site, Platform style, economic transformation",
        "camera_orbit": "Jia Zhangke orbit: camera circles in crowded public space, A Touch of Sin style, social tension",
        "transition_match": "Jia Zhangke match cut: cut from traditional to modern China, Still Life style, Fengjie and Three Gorges",
        "transition_light": "Jia Zhangke train light: train window light transition, Platform style, journey and displacement",
        "lighting": "available light, harsh sunlight, fluorescent lights, documentary realism, Chinese urban landscapes",
        "color": "desaturated earth tones, occasional vivid reds, documentary palette, transitional China",
        "bgm_keywords": ['ambient', 'documentary', 'minimal', 'atmospheric', 'realistic'],
        "mood": "social realism, melancholic observation, displaced persons, quiet resilience",
    },
    "hou-hsiao-hsien": {
        "name": "侯孝贤",
        "name_en": "Hou Hsiao-hsien",
        "description": "长镜头美学，历史记忆，东方哲思",
        "camera_push": "Hou Hsiao-hsien push in: extremely slow push, long take, Three Times style, historical layering, contemplative pace",
        "camera_pull": "Hou Hsiao-hsien pull back: pull back from intimate gesture to historical context, The Assassin style, Tang dynasty atmosphere",
        "camera_orbit": "Hou Hsiao-hsien orbit: camera circles through architectural space, Millennium Mambo style, temporal layering",
        "transition_match": "Hou Hsiao-hsien match cut: cut from one historical period to another, Three Times style, temporal meditation",
        "transition_light": "Hou Hsiao-hsien candle light: candle and lantern light transition, The Assassin style, period authenticity",
        "lighting": "natural window light, candlelight, lantern light, diffused and atmospheric, historical authenticity",
        "color": "muted earth tones, desaturated blues and greens, historical palette, Japanese and Chinese aesthetics",
        "bgm_keywords": ['minimal', 'ambient', 'traditional', 'meditative', 'peaceful'],
        "mood": "temporal meditation, historical memory, quiet observation, philosophical melancholy",
    },
    "bong-joon-ho": {
        "name": "奉俊昊",
        "name_en": "Bong Joon-ho",
        "description": "类型混合，社会讽刺，空间政治",
        "camera_push": "Bong Joon-ho push in: slow push with vertical movement, Parasite style, semi-basement to mansion, social stratification",
        "camera_pull": "Bong Joon-ho pull back: pull back to reveal spatial metaphor, Parasite style, rain flood, class warfare",
        "camera_orbit": "Bong Joon-ho orbit: camera circles through vertical space, Snowpiercer style, train cars, dystopian hierarchy",
        "transition_match": "Bong Joon-ho match cut: cut from one social class to another, Parasite style, vertical metaphor",
        "transition_light": "Bong Joon-ho rain light: rain and flood light transition, Parasite style, social deluge",
        "lighting": "practical lights, fluorescent tubes, natural window light, high contrast, social realism",
        "color": "desaturated earth tones, occasional vivid greens, social class color coding, Korean aesthetic",
        "bgm_keywords": ['tension', 'dark', 'satirical', 'dramatic', 'thriller'],
        "mood": "social satire, genre hybridity, dark humor, class consciousness, tension between spaces",
    },
    "denis-villeneuve": {
        "name": "维伦纽瓦",
        "name_en": "Denis Villeneuve",
        "description": "宏大尺度，静谧恐惧，宇宙诗意",
        "camera_push": "Villeneuve push in: massive push in on subject's face, Arrival style, linguistic alienation, cosmic scale",
        "camera_pull": "Villeneuve pull back: camera pulls back from small human to massive alien craft, Arrival style, overwhelming scale",
        "camera_orbit": "Villeneuve orbit: camera circles subject in vast desert, Dune style, sandworm scale, epic silence",
        "transition_match": "Villeneuve match cut: cut from human to alien perspective, Arrival style, non-linear time",
        "transition_light": "Villeneuve light burst: sudden light burst transition, Dune style, spice revelation, cosmic awe",
        "lighting": "natural harsh light, desert sun, soft interior light, chiaroscuro, IMAX scale",
        "color": "desaturated oranges and blues, Dune desert palette, muted cosmic tones",
        "bgm_keywords": ['epic', 'ambient', 'dramatic', 'cinematic', 'powerful'],
        "mood": "cosmic awe, linguistic mystery, political tension, quiet dread, epic scale",
    },
    "luc-besson": {
        "name": "卢贝松",
        "name_en": "Luc Besson",
        "description": "视觉诗歌，街头诗学，少女与杀手",
        "camera_push": "Besson push in: slow push in with lyrical camera, Léon style, flower and plant motifs, visual poetry",
        "camera_pull": "Besson pull back: pull back from intimate moment to urban violence, La Femme Nikita style, redemption",
        "camera_orbit": "Besson orbit: camera circles through stylized city, The Fifth Element style, retro-futurism, visual excess",
        "transition_match": "Besson match cut: cut from flower to gun, Léon style, visual metaphor, beauty and violence",
        "transition_light": "Besson light burst: golden light burst transition, The Fifth Element style, opera and space opera",
        "lighting": "golden hour sunlight, stylized neon, cinematic lighting, French visual poetry",
        "color": "vivid primary colors, saturated reds and blues, retro-futuristic palette",
        "bgm_keywords": ['electronic', 'cinematic', 'dramatic', 'urban', 'thriller'],
        "mood": "visual poetry, stylized violence, redemption, romantic fatalism, European cool",
    },
    "miyazaki": {
        "name": "宫崎骏",
        "name_en": "Miyazaki",
        "description": "手绘诗意，飞行幻想，自然崇拜",
        "camera_push": "Miyazaki push in: gentle push in with hand-painted detail, Spirited Away style, magical transformation, nature spirits",
        "camera_pull": "Miyazaki pull back: pull back from character to vast landscape, Castle in the Sky style, floating island, pastoral beauty",
        "camera_orbit": "Miyazaki orbit: camera circles with gentle flight, My Neighbor Totoro style, forest canopy, ecological harmony",
        "transition_match": "Miyazaki match cut: cut from human to spirit world, Spirited Away style, magical realism",
        "transition_light": "Miyazaki light transition: soft golden light through trees, Princess Mononoke style, forest spirit glow",
        "lighting": "soft natural light, dappled sunlight through trees, hand-painted lighting, Studio Ghibli aesthetic",
        "color": "soft pastels, lush greens, sky blues, hand-painted watercolor texture",
        "bgm_keywords": ['piano', 'orchestral', 'magical', 'whimsical', 'fantasy'],
        "mood": "magical realism, ecological wonder, childhood innocence, gentle melancholy, hand-crafted beauty",
    },
}

DEFAULT_CINEMATIC_STYLE = "auto"

# ============================================================
# 辅助函数
# ============================================================

def get_preset(product_type: str) -> dict:
    """获取产品类型预设"""
    return PRODUCT_PRESETS.get(product_type, PRODUCT_PRESETS["default"])


def ensure_dirs(output_dir: Path = OUTPUT_DIR) -> None:
    """确保所有必要目录存在

    Args:
        output_dir: 输出根目录，默认 OUTPUT_DIR
    """
    dirs = [
        output_dir / "character_ref",
        output_dir / "clips",
        output_dir / "final",
        output_dir / "batch",
        output_dir / "bgm_cache",
        output_dir / "sfx_cache",
    ]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)


# ============================================================
# 日志配置
# ============================================================

LOG_LEVEL = os.getenv("KLING_LOG_LEVEL", "INFO").upper()
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logger(name: str = "kling_ad", level: str = LOG_LEVEL) -> logging.Logger:
    """创建并配置 logger 实例

    Args:
        name: logger 名称
        level: 日志级别（DEBUG/INFO/WARNING/ERROR）

    Returns:
        配置好的 logger 实例
    """
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(level)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT))
    logger.addHandler(handler)
    logger.propagate = False
    return logger


logger = setup_logger()


# ============================================================
# 自定义异常类
# ============================================================

class KlingAIError(Exception):
    """可灵 AI 项目基础异常类"""
    error_code = "E0000"
    message = "未知错误"

    def __init__(self, message: str = "", error_code: str = ""):
        if error_code:
            self.error_code = error_code
        if message:
            self.message = message
        super().__init__(self.message)

    def __str__(self) -> str:
        return f"[{self.error_code}] {self.message}"


class APIKeyError(KlingAIError):
    """API Key 配置错误"""
    error_code = "E1001"
    message = "可灵 API Key 未配置"


class APICallError(KlingAIError):
    """API 调用失败"""
    error_code = "E1002"
    message = "可灵 API 调用失败"


class VideoGenerationError(KlingAIError):
    """视频生成失败"""
    error_code = "E1003"
    message = "视频生成失败"


class ImageGenerationError(KlingAIError):
    """图片生成失败"""
    error_code = "E1004"
    message = "图片生成失败"


class FFmpegError(KlingAIError):
    """FFmpeg 处理失败"""
    error_code = "E2001"
    message = "FFmpeg 处理失败"


class ConfigError(KlingAIError):
    """配置错误"""
    error_code = "E3001"
    message = "配置错误"


# ============================================================
# 质量前置控制配置（Quality Gate - 生成前预检）
# ============================================================
# 核心理念：在调用API生成前，通过预检查消除质量隐患，
# 避免"生成→检测→失败→重试"的成本浪费模式。

QUALITY_GATE_CONFIG = {
    # 是否启用前置质量控制（推荐 True）
    "enabled": True,

    # ── 参考图预检 ──
    "reference_check": {
        "enabled": True,
        # 参考图最小尺寸（像素）
        "min_width": 512,
        "min_height": 512,
        # 参考图最大尺寸（像素，过大可能导致API拒绝或质量下降）
        "max_width": 4096,
        "max_height": 4096,
        # 亮度范围（0-255，超出范围可能导致生成偏色）
        "min_brightness": 15,
        "max_brightness": 240,
        # 对比度阈值（标准差，过低表示图像几乎纯色）
        "min_contrast": 5,
        # 透明像素比例上限（过高表示主体不清晰）
        "max_transparent_ratio": 0.95,
        # 是否允许空白/纯色图
        "allow_blank": False,
    },

    # ── Prompt 预校验 ──
    "prompt_check": {
        "enabled": True,
        # 最小长度（字符）
        "min_length": 50,
        # 最大长度（字符，过长可能被截断）
        "max_length": 2000,
        # 必须包含的关键词类型（按优先级）
        "required_keywords": {
            "character": ["person", "face", "same", "reference"],
            "product": ["product", "packaging", "same", "reference"],
            "action": ["holding", "using", "showing", "applying"],
            "quality": ["sharp", "high quality", "cinematic", "professional"],
        },
        # 是否检查负面提示词完整性
        "check_negative_prompt": True,
        # 负面提示词必须包含的关键词
        "required_negative_keywords": [
            "blurry", "low quality", "distorted", "extra limbs",
            "text", "watermark", "flickering", "jitter",
        ],
        # ── 新增：自动优化机制 ──
        # 是否自动应用优化后的 Prompt（推荐 True）
        "auto_apply_optimization": True,
        # 是否自动修复语义冲突（推荐 True）
        "auto_fix_conflicts": True,
        # 是否自动去除重复描述（推荐 True）
        "auto_remove_repetitions": True,
        # 是否自动补全缺失关键词（推荐 True）
        "auto_complete_keywords": True,
    },

    # ── 新增：片段连贯性预检 ──
    "coherence_check": {
        "enabled": True,
        # 是否检查叙事流畅度
        "check_narrative_flow": True,
        # 是否检查场景过渡合理性
        "check_scene_transition": True,
        # 是否检查情绪曲线合理性
        "check_emotion_curve": True,
        # 是否检查角色一致性描述
        "check_character_consistency": True,
        # 是否检查产品展示逻辑
        "check_product_logic": True,
        # 叙事段顺序（严格）
        "required_segment_order": ["hook", "turning_point", "showcase", "result", "cta"],
        # 情绪曲线要求（hook高→turning低→showcase中→result高→cta稳）
        "emotion_curve_expectation": {
            "hook": "high",
            "turning_point": "low",
            "showcase": "medium",
            "result": "high",
            "cta": "stable",
        },
    },

    # ── 新增：历史数据驱动优化 ──
    "history_driven_optimization": {
        "enabled": True,
        # 成功案例数据库路径
        "success_cases_db": "success_cases.json",
        # 失败案例数据库路径
        "failure_cases_db": "failure_cases.json",
        # 是否基于历史成功率调整参数
        "adjust_parameters_by_history": True,
        # 高成功率阈值（>= 此值使用历史最优参数）
        "high_success_threshold": 0.85,
        # 低成功率阈值（<= 此值自动预警）
        "low_success_threshold": 0.30,
        # 是否记录当前配置到历史数据库
        "record_to_history": True,
    },

    # ── 新增：失败预测模型 ──
    "failure_prediction": {
        "enabled": True,
        # 是否预测当前配置成功率
        "predict_success_rate": True,
        # 预测失败时是否阻止生成
        "block_on_low_prediction": True,
        # 低预测成功率阈值（低于此值阻止生成）
        "min_prediction_threshold": 0.50,
        # 失败特征权重
        "failure_feature_weights": {
            "reference_image_quality": 0.25,
            "prompt_quality": 0.30,
            "parameter_config": 0.15,
            "script_structure": 0.15,
            "coherence": 0.15,
        },
    },

    # ── 参数锁定机制（保证可复现性）──
    "parameter_lock": {
        # 是否强制使用固定 seed（推荐 True，保证结果稳定）
        "force_seed": True,
        # 默认 seed 值（None 表示自动生成一个固定值）
        "default_seed": 42,
        # 是否锁定 image_fidelity
        "lock_fidelity": True,
        # 是否锁定 negative_prompt
        "lock_negative_prompt": True,
        # 是否锁定 aspect_ratio
        "lock_aspect_ratio": True,
        # 是否锁定 duration
        "lock_duration": True,
    },

    # ── 成本控制 ──
    "cost_control": {
        "enabled": True,
        # 单次生成预算上限（元）
        "max_budget": 100.0,
        # 单片段最大重试次数
        "max_retries_per_clip": 2,
        # 总片段最大重试次数
        "max_total_retries": 5,
        # 是否在超预算时自动降级（降低模式/分辨率）
        "auto_downgrade": True,
        # 降级策略：standard -> std, pro -> standard, 4k -> pro
        "downgrade_order": ["4k", "pro", "standard", "std"],
    },

    # ── 契约验证 ──
    "contract_verification": {
        "enabled": True,
        # 检查脚本完整性（必须包含所有叙事段）
        "check_script_structure": True,
        # 检查角色圣经完整性
        "check_character_bible": True,
        # 检查商品圣经完整性
        "check_product_bible": True,
        # 检查场景连续性配置
        "check_scene_continuity": True,
    },

    # ── 新增：图片先行验证策略 ──
    "image_first_strategy": {
        "enabled": True,
        # 默认模式：minimal/standard/full
        "default_mode": "standard",
        # 每个片段生成几张备选图片
        "n_variants_per_segment": 2,
        # 是否使用生成的最佳图片作为视频首帧参考
        "use_best_keyframe_as_reference": True,
        # 图片质量门严格程度
        "strict_quality_gate": True,
        # 图片生成分辨率（1k足够验证，成本最低）
        "image_resolution": "1k",
        # 图片生成模式（std足够验证）
        "image_generation_mode": "std",
        # 是否允许图片未通过时降级到直接生成视频
        "allow_fallback_to_direct_video": False,
        # 图片质量评分阈值（低于此值不通过）
        "min_quality_score": 70.0,
        # 商品一致性阈值
        "min_product_similarity": 0.65,
        # 角色一致性阈值
        "min_character_similarity": 0.55,
    },

    # ── 预检失败行为 ──
    "failure_behavior": {
        # 是否阻止生成（True=严格模式，False=警告但继续）
        "block_on_failure": True,
        # 是否提供自动修复建议
        "suggest_fixes": True,
        # 是否允许用户手动确认继续
        "allow_override": True,
    },

    # ── v2 新增：综合评分通过阈值 ──
    "min_pass_score": 55,

    # ── v2 新增：自进化机制 ──
    "self_evolution": {
        "enabled": True,
        # 是否自动记录生成结果反馈
        "auto_record_feedback": True,
        # 反馈数据存储路径（相对 output 目录）
        "feedback_db_path": "quality_feedback/feedback_records.json",
        # 多少条数据后开始给出优化建议
        "insight_min_records": 10,
        # 误判率阈值（超过则建议放宽）
        "false_positive_threshold": 0.20,
        # 漏判率阈值（超过则建议收紧）
        "false_negative_threshold": 0.10,
    },
}


# ============================================================
# 参考图质量标准（Reference Image Quality Standards）
# ============================================================
# 不同类型参考图的质量要求

REFERENCE_QUALITY_STANDARDS = {
    "product": {
        "name": "商品参考图",
        "requirements": [
            "主体完整，无遮挡",
            "背景简洁，便于AI识别",
            "光照均匀，无过曝或过暗",
            "分辨率 >= 512x512",
            "无明显水印或文字",
            "包装/logo清晰可见",
        ],
        "checks": [
            "主体检测（是否居中）",
            "背景复杂度检测",
            "文字水印检测",
            "包装完整性检测",
        ],
    },
    "character": {
        "name": "角色参考图",
        "requirements": [
            "正面清晰肖像",
            "面部特征完整可见",
            "发型/发色清晰",
            "服装完整可见",
            "无遮挡或模糊",
            "自然表情",
        ],
        "checks": [
            "人脸检测",
            "面部关键点检测",
            "表情分析",
            "服装识别",
        ],
    },
    "scene": {
        "name": "场景参考图",
        "requirements": [
            "空间布局清晰",
            "主要物体位置明确",
            "光照方向一致",
            "无杂乱元素",
        ],
        "checks": [
            "场景结构分析",
            "光照方向检测",
            "物体识别",
        ],
    },
}


# ============================================================
# Prompt 质量评分标准（Prompt Quality Scoring）
# ============================================================
# Prompt 质量评估规则

PROMPT_QUALITY_RULES = {
    "length": {
        "weight": 0.2,
        "rules": [
            {"condition": lambda x: len(x) < 50, "score": 20, "message": "Prompt 过短"},
            {"condition": lambda x: 50 <= len(x) < 100, "score": 50, "message": "Prompt 偏短"},
            {"condition": lambda x: 100 <= len(x) < 500, "score": 80, "message": "Prompt 长度适中"},
            {"condition": lambda x: 500 <= len(x) <= 1500, "score": 100, "message": "Prompt 长度理想"},
            {"condition": lambda x: len(x) > 1500, "score": 70, "message": "Prompt 过长可能被截断"},
        ],
    },
    "keyword_coverage": {
        "weight": 0.3,
        "rules": [
            {"condition": lambda x: len([k for k in ["same", "reference", "consistent"] if k in x.lower()]) >= 2, "score": 100, "message": "一致性关键词充足"},
            {"condition": lambda x: len([k for k in ["same", "reference", "consistent"] if k in x.lower()]) == 1, "score": 60, "message": "一致性关键词不足"},
            {"condition": lambda x: len([k for k in ["same", "reference", "consistent"] if k in x.lower()]) == 0, "score": 20, "message": "缺少一致性关键词"},
        ],
    },
    "action_clarity": {
        "weight": 0.2,
        "rules": [
            {"condition": lambda x: len([k for k in ["holding", "using", "showing", "applying", "demonstrating"] if k in x.lower()]) >= 1, "score": 100, "message": "动作描述清晰"},
            {"condition": lambda x: len([k for k in ["holding", "using", "showing", "applying", "demonstrating"] if k in x.lower()]) == 0, "score": 40, "message": "缺少动作描述"},
        ],
    },
    "quality_adjectives": {
        "weight": 0.15,
        "rules": [
            {"condition": lambda x: len([k for k in ["sharp", "high quality", "cinematic", "professional", "realistic"] if k in x.lower()]) >= 2, "score": 100, "message": "质量形容词充足"},
            {"condition": lambda x: len([k for k in ["sharp", "high quality", "cinematic", "professional", "realistic"] if k in x.lower()]) == 1, "score": 60, "message": "质量形容词不足"},
            {"condition": lambda x: len([k for k in ["sharp", "high quality", "cinematic", "professional", "realistic"] if k in x.lower()]) == 0, "score": 20, "message": "缺少质量形容词"},
        ],
    },
    "composition": {
        "weight": 0.15,
        "rules": [
            {"condition": lambda x: len([k for k in ["close-up", "medium shot", "wide shot", "frame", "composition"] if k in x.lower()]) >= 1, "score": 100, "message": "构图描述清晰"},
            {"condition": lambda x: len([k for k in ["close-up", "medium shot", "wide shot", "frame", "composition"] if k in x.lower()]) == 0, "score": 40, "message": "缺少构图描述"},
        ],
    },
}


# ============================================================
# 成本估算规则（Cost Estimation Rules）
# ============================================================

COST_ESTIMATION_RULES = {
    # 图片生成成本（元/张）
    "image": {
        "std": 0.05,
        "pro": 0.10,
        "hd": 0.20,
    },
    # 视频生成成本（元/秒）
    "video": {
        "std": 0.30,
        "standard": 0.45,
        "pro": 0.60,
        "hd": 0.80,
        "4k": 1.20,
    },
    # 额外费用
    "additional": {
        "tts": 0.02,          # 口播生成（元/字符）
        "bgm_search": 0.00,   # BGM搜索（免费）
        "quality_check": 0.00, # 质量检测（本地）
        "post_processing": 0.00, # 后处理（本地）
    },
}


# ============================================================
# 生成参数模板（Generation Parameter Templates）
# ============================================================
# 预定义的高质量参数组合，避免手动配置错误

GENERATION_PARAMETER_TEMPLATES = {
    "standard": {
        "name": "标准模式",
        "mode": "standard",
        "fidelity": 0.9,
        "seed": None,
        "duration": 5,
        "aspect_ratio": "9:16",
        "cost_per_clip": 0.45 * 5,  # 元
    },
    "pro": {
        "name": "专业模式",
        "mode": "pro",
        "fidelity": 0.95,
        "seed": None,
        "duration": 5,
        "aspect_ratio": "9:16",
        "cost_per_clip": 0.60 * 5,
    },
    "fast": {
        "name": "快速模式",
        "mode": "standard",
        "fidelity": 0.85,
        "seed": None,
        "duration": 3,
        "aspect_ratio": "9:16",
        "cost_per_clip": 0.45 * 3,
    },
    "high_quality": {
        "name": "高质量模式",
        "mode": "pro",
        "fidelity": 0.98,
        "seed": 42,
        "duration": 5,
        "aspect_ratio": "9:16",
        "cost_per_clip": 0.60 * 5,
    },
    "4k_cinematic": {
        "name": "4K电影模式",
        "mode": "4k",
        "fidelity": 0.98,
        "seed": 42,
        "duration": 5,
        "aspect_ratio": "16:9",
        "cost_per_clip": 1.20 * 5,
    },
}


# ============================================================
# 新增模块配置（v2.0 系统增强）
# ============================================================

# ── 素材资产库配置 ──
ASSET_LIBRARY_CONFIG = {
    "enabled": True,
    "auto_save_generated": True,      # 自动生成时保存到资产库
    "deduplication": True,            # 去重检测
    "max_versions_per_asset": 5,      # 每个资产最多保存几个版本
}

# ── 多模型路由器配置 ──
MODEL_ROUTER_CONFIG = {
    "enabled": True,
    "default_mode": "balanced",       # quality / speed / cost / balanced
    "fallback_enabled": True,         # 启用故障转移
    "max_consecutive_failures": 3,    # 连续失败几次触发熔断
    "circuit_breaker_timeout": 300,   # 熔断恢复时间（秒）
    "backends": {
        "kling": {
            "cost_per_sec": 0.60,
            "success_rate": 0.85,
            "avg_latency": 45,
            "queue_time": 30,
            "supports_reference": True,
            "max_duration": 10,
        },
        # 预留其他后端配置位
        "runway": {
            "cost_per_sec": 0.80,
            "success_rate": 0.80,
            "avg_latency": 60,
            "queue_time": 60,
            "supports_reference": True,
            "max_duration": 16,
        },
        "pika": {
            "cost_per_sec": 0.50,
            "success_rate": 0.75,
            "avg_latency": 30,
            "queue_time": 15,
            "supports_reference": True,
            "max_duration": 3,
        },
    },
}

# ── 反馈闭环配置 ──
FEEDBACK_LOOP_CONFIG = {
    "enabled": True,
    "auto_collect": True,             # 自动生成后收集反馈
    "min_rating_for_success": 4,      # 4星以上视为成功案例
    "issue_threshold": 3,             # 同一问题出现几次触发自动修复
    "export_report_interval": 7,      # 几天导出一次学习报告
}

# ── 实验追踪配置 ──
EXPERIMENT_TRACKER_CONFIG = {
    "enabled": True,
    "auto_track": True,               # 自动生成时自动追踪
    "min_experiments_for_recommendation": 10,  # 最少几次实验才推荐参数
    "param_candidates": [             # 可实验的参数
        "seed",
        "image_fidelity",
        "human_fidelity",
        "mode",
        "duration",
    ],
}

# ── AutoPrompt优化器配置 ──
AUTOPROMPT_CONFIG = {
    "enabled": True,
    "max_generations": 3,
    "population_size": 5,
    "auto_optimize_on_low_score": True,
    "score_threshold": 60,
}

# ── 故事板生成器配置 ──
STORYBOARD_CONFIG = {
    "enabled": True,
    "default_style": "cinematic",
    "default_shots": 6,
    "min_duration_per_shot": 2.0,
    "max_duration_per_shot": 5.0,
}

# ── 多人物一致性管理器配置 ──
MULTI_CHARACTER_CONFIG = {
    "enabled": True,
    "consistency_threshold": 0.55,
    "generate_group_reference": True,
    "max_characters_per_group": 10,
}

# ── 电影级运镜系统配置 ──
CINEMATIC_CAMERA_CONFIG = {
    "enabled": True,
    "default_preset": "cinematic_warm",
    "prefer_extreme_slow_movement": True,
    "emotion_curve_enabled": True,
}

# ── 品牌尾帧生成器配置 ──
BRAND_ENDING_CONFIG = {
    "enabled": True,
    "default_template": "cinematic",
    "default_duration": 3.0,
    "available_templates": ["cinematic", "minimal", "warm", "tech", "commercial"],
}

# ── 时间一致性检测器配置 ──
TEMPORAL_CONSISTENCY_CONFIG = {
    "enabled": True,
    "sample_interval": 2,
    "consistency_threshold": 0.5,
    "motion_smoothness_threshold": 0.5,
    "object_integrity_threshold": 0.5,
}

# ── AI视频增强器配置 ──
AI_ENHANCEMENT_CONFIG = {
    "enabled": True,
    "default_enhancements": ["upscale", "denoise", "color_enhance", "deflicker"],
    "target_resolution": "1080p",
    "target_fps": 30,
}

# ── 场景过渡检查器配置 ──
SCENE_TRANSITION_CONFIG = {
    "enabled": True,
    "color_match_threshold": 0.5,
    "brightness_match_threshold": 0.5,
    "alignment_threshold": 0.3,
    "motion_continuity_threshold": 0.3,
}

# ── 角色分析器配置 ──
CHARACTER_ANALYZER_CONFIG = {
    "enabled": True,
    "min_characters": 1,
    "max_characters": 6,
    "auto_detect_background": True,
    "consistency_levels": {
        "protagonist": 0.95,
        "supporting": 0.85,
        "service": 0.75,
        "background": 0.0,
    },
}
