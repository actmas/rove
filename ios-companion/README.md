# Rove iOS Companion

使用 SwiftUI 和 WKWebView 打开 Rove 工作台。登录、会话、审批与任务操作由网页提供。

在 Xcode 中打开 `Rove.xcodeproj`，为 Rove 主应用选择签名 Team 后运行到 iPhone。最低系统版本为 iOS 16.2。

默认工作台地址占位符为 `https://rove.example.com`。长按页面两秒可设置你自己的 HTTPS 工作台地址，保存后重新加载页面。

运行 `python3 scripts/generate_xcode_project.py` 可重新生成工程。
