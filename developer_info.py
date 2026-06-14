#!/usr/bin/env python3
"""
开发者信息模块

注意：Base64 编码不是真正的保护，源码可被直接编辑。
要定制开发者信息，可使用 setup_developer_info.py（读取 MY_INFO.yml 注入）；
或者直接编辑本文件中的 _ENCODED_INFO 字典和 get_start_message 文本。
"""
import base64

# 开发者信息（Base64 编码）
_ENCODED_INFO = {
    'name': base64.b64encode('狼哥'.encode()).decode(),
    'github': base64.b64encode('luoyanglang'.encode()).decode(),
    'repo': base64.b64encode('AI-Anti-Spam-Bot'.encode()).decode(),
    'channel': base64.b64encode('@langgefabu'.encode()).decode(),
    'contact': base64.b64encode('@luoyanglang'.encode()).decode(),
}

def get_developer_info():
    """获取开发者信息（解码）"""
    return {
        'name': base64.b64decode(_ENCODED_INFO['name']).decode(),
        'github_username': base64.b64decode(_ENCODED_INFO['github']).decode(),
        'project_repo': base64.b64decode(_ENCODED_INFO['repo']).decode(),
        'telegram_channel': base64.b64decode(_ENCODED_INFO['channel']).decode(),
        'telegram_contact': base64.b64decode(_ENCODED_INFO['contact']).decode(),
    }

def get_start_message():
    """获取 /start 命令的消息"""
    info = get_developer_info()
    return (
        "👋 你好！我是 AI 反垃圾广告机器人\n\n"
        "🛡️ 功能：智能识别文字、图片中的垃圾广告\n"
        "📊 管理员使用 /admin 查看管理面板\n"
        "🎯 超级管理员可使用 /add_ad 管理广告按钮\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👨‍💻 开发者：{info['name']} ({info['telegram_contact']})\n"
        f"📦 官方项目：https://github.com/{info['github_username']}/{info['project_repo']}\n"
        f"📢 发布频道：{info['telegram_channel']}\n"
        f"💬 交流群组：@langgepython\n"
        f"🎯 演示 Bot：@xiaolangzaibot\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "💡 把我添加到群组并设为管理员即可开始工作！\n\n"
        "⭐ 觉得有用？给项目一个 Star\n"
        "☕ 请作者喝杯咖啡 (USDT TRC20):\n"
        "`请将本占位符替换为你的真实收款地址`"
    )

def get_contact_section():
    """获取联系方式部分（用于 README）"""
    info = get_developer_info()
    return f"""## 📮 联系

- 📦 GitHub: [@{info['github_username']}](https://github.com/{info['github_username']})
- 🌟 项目: [github.com/{info['github_username']}/{info['project_repo']}](https://github.com/{info['github_username']}/{info['project_repo']})
- 📢 Telegram 频道: [{info['telegram_channel']}](https://t.me/{info['telegram_channel'].lstrip('@')})
- 💬 Telegram 联系: [{info['telegram_contact']}](https://t.me/{info['telegram_contact'].lstrip('@')})"""

# 防止直接运行
if __name__ == '__main__':
    print("⚠️  此模块不应直接运行")
    print("如需定制开发者信息，请编辑本文件或使用 setup_developer_info.py")
