{
    "name": "Field Multimodal UX - AI Providers",
    "summary": "OpenAI 兼容 ASR/LLM/视觉适配器 + Mock 测试替身",
    "description": """
为 mmux_core 的 Provider Registry 提供 OpenAI 兼容的服务适配器：
 - asr_openai: /v1/audio/transcriptions（whisper 等）
 - llm_openai: /v1/chat/completions（严格 JSON 意图输出）
 - vision_openai: 图像理解（货架异常、铭牌识别）
全部通过 ir.config_parameter 配置，可随时切换到任何 OpenAI 兼容端点或 Mock。
    """,
    "version": "19.0.6.0.0",
    "category": "Productivity/Field Operations",
    "author": "jeffery9@gmail.com & geninit",
    "license": "LGPL-3",
    "copyright": "Copyright 2026 jeffery9@gmail.com & geninit. Licensed under LGPL-3.",
    "depends": ["mmux_core"],
    "installable": True,
}
