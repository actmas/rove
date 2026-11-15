import SwiftUI
import WebKit

struct ContentView: View {
    @State private var webView: WKWebView = {
        let configuration = WKWebViewConfiguration()
        configuration.allowsInlineMediaPlayback = true
        return WKWebView(frame: .zero, configuration: configuration)
    }()
    @State private var showingGateway = false
    @State private var gatewayDraft = ""
    @AppStorage("workstationURL") private var gateway = "https://rove.example.com"

    private var gatewayURL: URL {
        Self.validGateway(gateway) ?? URL(string: "https://rove.example.com")!
    }

    private static func validGateway(_ value: String) -> URL? {
        guard let url = URL(string: value.trimmingCharacters(in: .whitespacesAndNewlines)),
              url.scheme == "https", url.host != nil,
              url.user == nil, url.password == nil else { return nil }
        return url
    }

    var body: some View {
        WorkstationWebView(webView: webView, url: gatewayURL)
            .ignoresSafeArea(.keyboard)
            .onLongPressGesture(minimumDuration: 2) {
                gatewayDraft = gateway
                showingGateway = true
            }
            .alert("工作台地址（HTTPS）", isPresented: $showingGateway) {
                TextField("https://rove.example.com", text: $gatewayDraft)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                Button("保存") {
                    if let url = Self.validGateway(gatewayDraft) {
                        gateway = url.absoluteString
                        webView.load(URLRequest(url: url))
                    }
                }
                Button("取消", role: .cancel) {}
            }
    }
}

struct WorkstationWebView: UIViewRepresentable {
    let webView: WKWebView
    let url: URL

    func makeUIView(context: Context) -> WKWebView {
        webView.isOpaque = false
        webView.backgroundColor = .black
        webView.scrollView.backgroundColor = .black
        webView.load(URLRequest(url: url))
        return webView
    }

    func updateUIView(_ uiView: WKWebView, context: Context) {}
}
