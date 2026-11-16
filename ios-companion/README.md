# Rove iOS & Apple Watch Companion

包含两个原生 SwiftUI Target：

1. **Rove (`ios-companion/Rove`)**：iPhone / iPad 伴侣应用（`WKWebView` 封装，最低 iOS 16.2）。长按页面两秒可配置你自己的 HTTPS 工作台地址。
2. **RoveWatch (`ios-companion/RoveWatch`)**：独立 Apple Watch 原生应用（最低 watchOS 9.0），实时轮询 `/api/watch/live/state`，在手表端直接弹出 **「✓ 批准执行」** 与 **「✕ 拒绝」** 按钮完成审批。

## 构建方式

1. 运行 `python3 scripts/generate_xcode_project.py`（或直接打开 `ios-companion/Rove.xcodeproj`）。
2. 在 Xcode 中为 `Rove` 与 `RoveWatch` 两个 Target 选择你的签名 Team。
3. 将 `RoveWatch` 运行到配对的 Apple Watch，点击右上角齿轮填入工作台 HTTPS 地址与 Rove 设置页中的 Watch Token 即可使用。
