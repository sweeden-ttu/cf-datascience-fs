import CFDSKit
import FileProvider
import UniformTypeIdentifiers

/// Item identifiers are server paths ("/home/a.csv"). The server's "/" is the Files
/// app's root container; it lists `/home` and `/kaggle-datasets`.
enum ItemID {
    static func path(for identifier: NSFileProviderItemIdentifier) -> String? {
        if identifier == .rootContainer { return "/" }
        if identifier == .trashContainer || identifier == .workingSet { return nil }
        return RemotePath.normalize(identifier.rawValue)
    }

    static func identifier(for path: String) -> NSFileProviderItemIdentifier {
        let path = RemotePath.normalize(path)
        return path == "/" ? .rootContainer : NSFileProviderItemIdentifier(path)
    }
}

final class FileProviderItem: NSObject, NSFileProviderItem {
    let entry: RemoteEntry

    init(_ entry: RemoteEntry) {
        self.entry = entry
    }

    var itemIdentifier: NSFileProviderItemIdentifier { ItemID.identifier(for: entry.name) }

    var parentItemIdentifier: NSFileProviderItemIdentifier {
        entry.name == "/" ? .rootContainer : ItemID.identifier(for: RemotePath.parent(entry.name))
    }

    var filename: String {
        switch entry.name {
        case "/": return "Cloudfronts"
        case "/home": return "My Files"
        case "/kaggle-datasets": return "Kaggle Datasets"
        default: return entry.filename
        }
    }

    var contentType: UTType {
        if entry.isDirectory { return .folder }
        let ext = (entry.filename as NSString).pathExtension
        return UTType(filenameExtension: ext) ?? .data
    }

    var documentSize: NSNumber? { entry.isDirectory ? nil : NSNumber(value: entry.size) }

    var contentModificationDate: Date? { entry.modificationDate }

    var capabilities: NSFileProviderItemCapabilities {
        // `writable` comes from the server; the root and anything under
        // /kaggle-datasets are read-only, as is a mount root itself (no rename/delete).
        let writable = entry.writable ?? false
        let isMountRoot = RemotePath.parent(entry.name) == "/"
        if entry.isDirectory {
            var caps: NSFileProviderItemCapabilities = [.allowsContentEnumerating, .allowsReading]
            if writable {
                caps.insert(.allowsAddingSubItems)
                if !isMountRoot {
                    caps.formUnion([.allowsRenaming, .allowsReparenting, .allowsDeleting])
                }
            }
            return caps
        }
        var caps: NSFileProviderItemCapabilities = [.allowsReading]
        if writable {
            caps.formUnion([.allowsWriting, .allowsRenaming, .allowsReparenting, .allowsDeleting])
        }
        return caps
    }

    /// The content version is the server's ETag, so the system re-downloads exactly when
    /// the file changed. The metadata version also covers the modification time.
    var itemVersion: NSFileProviderItemVersion {
        let content = Data((entry.etag ?? "dir").utf8)
        let metadata = Data("\(entry.etag ?? "")|\(entry.mtime)|\(entry.writable ?? false)".utf8)
        return NSFileProviderItemVersion(contentVersion: content, metadataVersion: metadata)
    }

    /// The ETag the system holds for this item, recovered from a version it hands back.
    static func etag(from version: NSFileProviderItemVersion) -> String? {
        let value = String(decoding: version.contentVersion, as: UTF8.self)
        return value.isEmpty || value == "dir" ? nil : value
    }
}
