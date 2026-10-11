{
    "name": "Field Multimodal UX - Core",
    "summary": "Odoo 现场多模态交互增强平台 · 内核（输入协议、能力注册、执行守卫、审计）",
    "description": """
Odoo Field Multimodal UX 内核模块。
提供多模态输入协议、Provider 适配抽象、能力白名单注册（Capability Registry）、
意图解析、服务器端执行守卫（ACL / Record Rules / 风险分级 / 幂等）与交互审计日志。
遵循原则：Odoo 是业务事实的唯一权威来源；不向 LLM 暴露任意 ORM 操作。
    """,
    "version": "19.0.6.0.0",
    "category": "Productivity/Field Operations",
    "author": "Field Multimodal UX Team",
    "license": "LGPL-3",
    "depends": ["stock", "barcodes", "mail"],
    "data": [
        "security/mmux_security.xml",
        "security/ir.model.access.csv",
        "data/mmux_scenario_data.xml",
        "views/mmux_views.xml",
        "views/mmux_capability_toggle_views.xml",
        "views/mmux_flow_override_views.xml",
        "views/mmux_settings_views.xml",
    ],
    "assets": {
        "web.assets_frontend": [],
        "web.assets_backend": [
            "mmux_core/static/src/**/*",
        ],
    },
    "application": False,
    "installable": True,
}
