import CFDSKit
import FileProvider

/// Lists one folder. The server has no change feed, so change enumeration answers
/// "sync anchor expired", which makes the system re-list the folder when it next looks.
final class FolderEnumerator: NSObject, NSFileProviderEnumerator {
    private let path: String
    private let client: CFDSClient
    private var task: Task<Void, Never>?

    init(path: String, client: CFDSClient) {
        self.path = path
        self.client = client
    }

    func invalidate() {
        task?.cancel()
    }

    func enumerateItems(for observer: NSFileProviderEnumerationObserver, startingAt page: NSFileProviderPage) {
        task = Task {
            do {
                let listing = try await client.list(path)
                let items = (listing.entries ?? []).map(FileProviderItem.init)
                observer.didEnumerate(items)
                observer.finishEnumerating(upTo: nil)
            } catch {
                observer.finishEnumeratingWithError(FileProviderErrors.map(error))
            }
        }
    }

    func enumerateChanges(for observer: NSFileProviderChangeObserver, from anchor: NSFileProviderSyncAnchor) {
        observer.finishEnumeratingWithError(NSFileProviderError(.syncAnchorExpired))
    }

    func currentSyncAnchor(completionHandler: @escaping (NSFileProviderSyncAnchor?) -> Void) {
        completionHandler(SyncAnchor.now())
    }
}

/// The working set (recents, search, offline files). Items are tracked through their
/// folders, so this reports nothing and never expires.
final class WorkingSetEnumerator: NSObject, NSFileProviderEnumerator {
    func invalidate() {}

    func enumerateItems(for observer: NSFileProviderEnumerationObserver, startingAt page: NSFileProviderPage) {
        observer.finishEnumerating(upTo: nil)
    }

    func enumerateChanges(for observer: NSFileProviderChangeObserver, from anchor: NSFileProviderSyncAnchor) {
        observer.finishEnumeratingChanges(upTo: anchor, moreComing: false)
    }

    func currentSyncAnchor(completionHandler: @escaping (NSFileProviderSyncAnchor?) -> Void) {
        completionHandler(NSFileProviderSyncAnchor(Data("working-set".utf8)))
    }
}

enum SyncAnchor {
    static func now() -> NSFileProviderSyncAnchor {
        NSFileProviderSyncAnchor(Data(String(Date().timeIntervalSince1970).utf8))
    }
}
