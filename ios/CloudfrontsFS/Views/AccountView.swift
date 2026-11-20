import CFDSKit
import SwiftUI

/// Signed-in home: quota, the folders, how to find them in Files, and sign-out.
struct AccountView: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        NavigationStack {
            List {
                if let account = model.account {
                    Section("Account") {
                        LabeledContent("Signed in as") {
                            Text(account.user.email ?? account.user.name ?? account.user.sub)
                        }
                        if let roles = account.user.roles, !roles.isEmpty {
                            LabeledContent("Plan", value: roles.joined(separator: ", ").capitalized)
                        }
                        QuotaView(quota: account.quota)
                    }
                    Section("Folders") {
                        ForEach(account.allMounts, id: \.path) { mount in
                            NavigationLink {
                                BrowserView(path: mount.path, title: title(for: mount))
                            } label: {
                                Label {
                                    VStack(alignment: .leading) {
                                        Text(title(for: mount))
                                        Text(mount.writable ? "Read and write" : "Read-only")
                                            .font(.caption)
                                            .foregroundStyle(.secondary)
                                    }
                                } icon: {
                                    Image(systemName: mount.writable ? "folder" : "books.vertical")
                                }
                            }
                        }
                    }
                } else {
                    Section { ProgressView() }
                }
                Section("In the Files app") {
                    Text(
                        "Open Files, tap Browse, and choose Cloudfronts. Files in your folder can be opened, edited and saved from any app. Kaggle datasets are read-only; copy a file into your folder to change it."
                    )
                    .font(.callout)
                }
                Section {
                    Button("Sign out", role: .destructive) {
                        Task { await model.signOut() }
                    }
                }
            }
            .navigationTitle("Cloudfronts")
            .refreshable { await model.refreshAccount() }
        }
    }

    private func title(for mount: AccountInfo.Mount) -> String {
        if mount.path == model.account?.home { return "My Files" }
        if mount.path == model.account?.datasets { return "Kaggle Datasets" }
        return mount.description ?? RemotePath.lastComponent(mount.path)
    }
}

struct QuotaView: View {
    let quota: AccountInfo.Quota

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            ProgressView(value: quota.fractionUsed)
                .tint(quota.fractionUsed > 0.9 ? .red : .accentColor)
            Text("\(format(quota.used)) of \(format(quota.limit)) used")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .padding(.vertical, 4)
    }

    private func format(_ bytes: Int64) -> String {
        ByteCountFormatter.string(fromByteCount: bytes, countStyle: .binary)
    }
}
