import CFDSKit
import SwiftUI

/// A simple in-app browser for one folder. Editing happens in the Files app; here a
/// dataset file can be copied into "My Files" on the server, without downloading it.
struct BrowserView: View {
    @EnvironmentObject private var model: AppModel
    let path: String
    let title: String

    @State private var entries: [RemoteEntry] = []
    @State private var isLoading = true
    @State private var message: String?

    var body: some View {
        List {
            if let message {
                Text(message).font(.callout).foregroundStyle(.secondary)
            }
            ForEach(entries, id: \.name) { entry in
                if entry.isDirectory {
                    NavigationLink {
                        BrowserView(path: entry.name, title: entry.filename)
                    } label: {
                        row(entry)
                    }
                } else {
                    row(entry)
                        .swipeActions {
                            if canCopyToHome(entry) {
                                Button("Copy to My Files") {
                                    Task { await copyToHome(entry) }
                                }
                                .tint(.accentColor)
                            }
                        }
                }
            }
        }
        .overlay {
            if isLoading {
                ProgressView()
            } else if entries.isEmpty && message == nil {
                Text("This folder is empty.").foregroundStyle(.secondary)
            }
        }
        .navigationTitle(title)
        .task { await load() }
        .refreshable { await load() }
    }

    private func row(_ entry: RemoteEntry) -> some View {
        Label {
            VStack(alignment: .leading) {
                Text(entry.filename)
                if !entry.isDirectory {
                    Text(
                        ByteCountFormatter.string(fromByteCount: entry.size, countStyle: .binary)
                            + " · " + entry.modificationDate.formatted(date: .abbreviated, time: .shortened)
                    )
                    .font(.caption)
                    .foregroundStyle(.secondary)
                }
            }
        } icon: {
            Image(systemName: entry.isDirectory ? "folder" : "doc")
        }
    }

    private func canCopyToHome(_ entry: RemoteEntry) -> Bool {
        guard let home = model.account?.home else { return false }
        return entry.type == .file && !RemotePath.isWithin(entry.name, home)
    }

    private func load() async {
        isLoading = true
        defer { isLoading = false }
        do {
            entries = try await model.client.list(path).entries ?? []
            message = nil
        } catch {
            message = error.localizedDescription
        }
    }

    private func copyToHome(_ entry: RemoteEntry) async {
        guard let home = model.account?.home else { return }
        do {
            _ = try await model.client.copy(entry.name, to: RemotePath.join(home, entry.filename))
            message = "Copied \(entry.filename) to My Files."
            await model.refreshAccount()
        } catch {
            message = error.localizedDescription
        }
    }
}
