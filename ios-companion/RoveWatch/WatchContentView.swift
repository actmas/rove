import SwiftUI

struct WatchLiveState: Decodable {
    let active: Bool
    let sessionTitle: String?
    let engine: String?
    let job: WatchJob?
    let approval: WatchApproval?

    enum CodingKeys: String, CodingKey {
        case active
        case sessionTitle = "session_title"
        case engine
        case job
        case approval
    }
}

struct WatchJob: Decodable {
    let id: String
    let status: String
    let elapsedSeconds: Int?

    enum CodingKeys: String, CodingKey {
        case id
        case status
        case elapsedSeconds = "elapsed_seconds"
    }
}

struct WatchApproval: Decodable {
    let id: String
    let command: String
    let reason: String
    let cwd: String
    let isQuestion: Bool?

    enum CodingKeys: String, CodingKey {
        case id
        case command
        case reason
        case cwd
        case isQuestion = "is_question"
    }
}

struct WatchContentView: View {
    @AppStorage("workstationURL") private var gateway = "https://rove.example.com"
    @AppStorage("watchToken") private var watchToken = ""

    @State private var state: WatchLiveState?
    @State private var statusMessage = "等待连接…"
    @State private var busyDecision = false
    @State private var showingSetup = false

    private let pollTimer = Timer.publish(every: 2.0, on: .main, in: .common).autoconnect()

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 10) {
                    statusHeader

                    if let approval = state?.approval {
                        approvalCard(approval)
                    } else if let job = state?.job {
                        activeJobCard(job)
                    } else {
                        idleCard
                    }
                }
                .padding(.horizontal, 6)
                .padding(.bottom, 12)
            }
            .navigationTitle("Rove")
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button {
                        showingSetup = true
                    } label: {
                        Image(systemName: "gearshape")
                    }
                }
            }
            .sheet(isPresented: $showingSetup) {
                WatchSettingsSheet(gateway: $gateway, watchToken: $watchToken) {
                    Task { await fetchState() }
                }
            }
            .task {
                if watchToken.isEmpty {
                    showingSetup = true
                } else {
                    await fetchState()
                }
            }
            .onReceive(pollTimer) { _ in
                guard !watchToken.isEmpty, !showingSetup else { return }
                Task { await fetchState() }
            }
        }
    }

    private var statusHeader: some View {
        HStack(spacing: 6) {
            Circle()
                .fill(statusColor)
                .frame(width: 8, height: 8)
            Text(state?.sessionTitle ?? statusMessage)
                .font(.caption.weight(.semibold))
                .lineLimit(1)
        }
    }

    private var statusColor: Color {
        switch state?.job?.status {
        case "waiting": return .orange
        case "running", "queued": return .green
        default: return .gray
        }
    }

    @ViewBuilder
    private func approvalCard(_ approval: WatchApproval) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Label("等待审批", systemImage: "exclamationmark.shield.fill")
                .font(.caption.weight(.bold))
                .foregroundStyle(.orange)

            Text(approval.command.isEmpty ? approval.reason : approval.command)
                .font(.system(.caption2, design: .monospaced))
                .foregroundStyle(.primary)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(6)
                .background(Color.white.opacity(0.08))
                .clipShape(RoundedRectangle(cornerRadius: 8))

            if !approval.cwd.isEmpty {
                Text(approval.cwd)
                    .font(.system(size: 10))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }

            if approval.isQuestion == true {
                Text("请在 iPhone 或网页端回答交互问题")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            } else {
                Button {
                    Task { await submitDecision(approvalId: approval.id, decision: "accept") }
                } label: {
                    Label("批准执行", systemImage: "checkmark.circle.fill")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)
                .tint(.green)
                .disabled(busyDecision)

                Button(role: .destructive) {
                    Task { await submitDecision(approvalId: approval.id, decision: "decline") }
                } label: {
                    Label("拒绝", systemImage: "xmark.circle")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.bordered)
                .disabled(busyDecision)
            }
        }
        .padding(8)
        .background(Color.orange.opacity(0.12))
        .overlay(
            RoundedRectangle(cornerRadius: 12)
                .stroke(Color.orange.opacity(0.45), lineWidth: 1)
        )
        .clipShape(RoundedRectangle(cornerRadius: 12))
    }

    private func activeJobCard(_ job: WatchJob) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("\(state?.engine?.uppercased() ?? "AGENT") · \(job.status)")
                .font(.caption2.weight(.semibold))
                .foregroundStyle(.green)
            if let secs = job.elapsedSeconds {
                Text("已运行 \(secs / 60)m \(secs % 60)s")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(8)
        .background(Color.white.opacity(0.07))
        .clipShape(RoundedRectangle(cornerRadius: 10))
    }

    private var idleCard: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(watchToken.isEmpty ? "请先配置 Token" : "工作台空闲")
                .font(.caption2.weight(.semibold))
            Text(statusMessage)
                .font(.system(size: 10))
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(8)
        .background(Color.white.opacity(0.06))
        .clipShape(RoundedRectangle(cornerRadius: 10))
    }

    private func cleanBaseURL() -> String {
        gateway.trimmingCharacters(in: .whitespacesAndNewlines).trimmingCharacters(in: CharacterSet(charactersIn: "/"))
    }

    private func fetchState() async {
        let base = cleanBaseURL()
        let tok = watchToken.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !tok.isEmpty,
              var comps = URLComponents(string: "\(base)/api/watch/live/state") else {
            statusMessage = "请检查地址与 Token"
            return
        }
        comps.queryItems = [URLQueryItem(name: "token", value: tok)]
        guard let url = comps.url else { return }

        do {
            let (data, resp) = try await URLSession.shared.data(from: url)
            guard let http = resp as? HTTPURLResponse else { return }
            if http.statusCode == 401 {
                statusMessage = "Token 无效或已轮换"
                return
            }
            guard (200..<300).contains(http.statusCode) else {
                statusMessage = "HTTP \(http.statusCode)"
                return
            }
            state = try JSONDecoder().decode(WatchLiveState.self, from: data)
            statusMessage = "已连接"
        } catch {
            statusMessage = "连接失败"
        }
    }

    private func submitDecision(approvalId: String, decision: String) async {
        let base = cleanBaseURL()
        let tok = watchToken.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !tok.isEmpty,
              var comps = URLComponents(string: "\(base)/api/watch/approve") else { return }
        comps.queryItems = [URLQueryItem(name: "token", value: tok)]
        guard let url = comps.url else { return }

        busyDecision = true
        defer { busyDecision = false }

        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: [
            "approval_id": approvalId,
            "decision": decision,
        ])

        do {
            let (_, resp) = try await URLSession.shared.data(for: req)
            if let http = resp as? HTTPURLResponse, (200..<300).contains(http.statusCode) {
                statusMessage = decision == "accept" ? "已批准" : "已拒绝"
                await fetchState()
            } else {
                statusMessage = "审批失败"
            }
        } catch {
            statusMessage = "网络错误"
        }
    }
}

private struct WatchSettingsSheet: View {
    @Binding var gateway: String
    @Binding var watchToken: String
    var onSave: () -> Void
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        Form {
            Section("工作台地址 (HTTPS)") {
                TextField("https://rove.example.com", text: $gateway)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
            }
            Section("Watch Token") {
                TextField("粘贴设置页中的 Token", text: $watchToken)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
            }
            Button("保存并连接") {
                onSave()
                dismiss()
            }
        }
    }
}
