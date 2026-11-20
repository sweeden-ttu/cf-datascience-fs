import CFDSKit
import FileProvider
import Foundation

/// The "Cloudfronts" location in the Files app.
///
/// It is a replicated File Provider: the system keeps the local copies and calls this
/// class to list folders, download files on demand and push local changes. Every call
/// goes straight to the cfds server with the access token the app stored in the shared
/// Keychain group. Writes go to `/home`; `/kaggle-datasets` is read-only.
final class FileProviderExtension: NSObject, NSFileProviderReplicatedExtension {
    private let domain: NSFileProviderDomain
    private let client: CFDSClient
    private let manager: NSFileProviderManager?

    required init(domain: NSFileProviderDomain) {
        self.domain = domain
        let config = CFDSConfiguration.fromBundle()
        let auth = AuthSession(
            config: config, store: KeychainTokenStore(accessGroup: config.keychainAccessGroup))
        self.client = CFDSClient(baseURL: config.apiBaseURL, tokens: auth)
        self.manager = NSFileProviderManager(for: domain)
        super.init()
    }

    func invalidate() {}

    // MARK: - Reading

    func item(
        for identifier: NSFileProviderItemIdentifier, request: NSFileProviderRequest,
        completionHandler: @escaping (NSFileProviderItem?, Error?) -> Void
    ) -> Progress {
        run(completionHandler: { completionHandler(nil, $0) }) { [client] in
            guard let path = ItemID.path(for: identifier) else {
                throw NSFileProviderError(.noSuchItem)
            }
            let entry = try await client.info(path)
            completionHandler(FileProviderItem(entry), nil)
        }
    }

    func fetchContents(
        for itemIdentifier: NSFileProviderItemIdentifier,
        version requestedVersion: NSFileProviderItemVersion?, request: NSFileProviderRequest,
        completionHandler: @escaping (URL?, NSFileProviderItem?, Error?) -> Void
    ) -> Progress {
        run(completionHandler: { completionHandler(nil, nil, $0) }) { [client, manager] in
            guard let path = ItemID.path(for: itemIdentifier) else {
                throw NSFileProviderError(.noSuchItem)
            }
            let entry = try await client.info(path)
            guard entry.type == .file else { throw NSFileProviderError(.noSuchItem) }
            let directory =
                try manager?.temporaryDirectoryURL() ?? FileManager.default.temporaryDirectory
            let destination = directory.appendingPathComponent(UUID().uuidString)
            // Pin the download to the version just described so item and bytes agree.
            try await client.download(path, to: destination, ifMatch: entry.etag)
            completionHandler(destination, FileProviderItem(entry), nil)
        }
    }

    // MARK: - Writing

    func createItem(
        basedOn itemTemplate: NSFileProviderItem, fields: NSFileProviderItemFields,
        contents url: URL?, options: NSFileProviderCreateItemOptions = [],
        request: NSFileProviderRequest,
        completionHandler: @escaping (NSFileProviderItem?, NSFileProviderItemFields, Bool, Error?) -> Void
    ) -> Progress {
        run(completionHandler: { completionHandler(nil, [], false, $0) }) { [client] in
            guard let parent = ItemID.path(for: itemTemplate.parentItemIdentifier),
                RemotePath.isValidName(itemTemplate.filename)
            else { throw CocoaError(.fileWriteInvalidFileName) }
            let path = RemotePath.join(parent, itemTemplate.filename)
            let isFolder = itemTemplate.contentType == .folder

            let entry: RemoteEntry
            if isFolder {
                do {
                    entry = try await client.makeDirectory(path)
                } catch CFDSError.alreadyExists where options.contains(.mayAlreadyExist) {
                    entry = try await client.info(path)
                }
            } else if itemTemplate.contentType == .symbolicLink {
                throw CocoaError(.featureUnsupported)
            } else {
                // Files sometimes creates an item first and sends its contents later.
                let source = try url ?? Self.emptyFile()
                entry = try await client.upload(
                    fileAt: source, to: path,
                    mode: options.contains(.mayAlreadyExist) ? .createOrReplace : .createOnly)
            }
            completionHandler(FileProviderItem(entry), [], false, nil)
        }
    }

    func modifyItem(
        _ item: NSFileProviderItem, baseVersion version: NSFileProviderItemVersion,
        changedFields: NSFileProviderItemFields, contents newContents: URL?,
        options: NSFileProviderModifyItemOptions = [], request: NSFileProviderRequest,
        completionHandler: @escaping (NSFileProviderItem?, NSFileProviderItemFields, Bool, Error?) -> Void
    ) -> Progress {
        run(completionHandler: { completionHandler(nil, [], false, $0) }) { [client] in
            guard var path = ItemID.path(for: item.itemIdentifier), path != "/" else {
                throw CocoaError(.fileWriteNoPermission)
            }
            var entry: RemoteEntry?

            // Rename and/or move first, so new contents land at the final path.
            if !changedFields.isDisjoint(with: [.filename, .parentItemIdentifier]) {
                guard let parent = ItemID.path(for: item.parentItemIdentifier),
                    RemotePath.isValidName(item.filename)
                else { throw CocoaError(.fileWriteInvalidFileName) }
                let destination = RemotePath.join(parent, item.filename)
                if destination != path {
                    entry = try await client.move(path, to: destination)
                    path = destination
                }
            }

            if changedFields.contains(.contents), let newContents {
                // Only replace the version the user edited; if someone else changed the
                // file meanwhile the server answers 412 and the system resolves it.
                let mode: CFDSClient.UploadMode =
                    FileProviderItem.etag(from: version).map { .replace(ifMatch: $0) }
                    ?? .createOrReplace
                entry = try await client.upload(fileAt: newContents, to: path, mode: mode)
            }

            // Other fields (dates, tags, favourite rank, last used) are not stored on
            // the server. Reporting none of them as pending lets the system keep them
            // locally.
            let result: RemoteEntry
            if let entry {
                result = entry
            } else {
                result = try await client.info(path)
            }
            completionHandler(FileProviderItem(result), [], false, nil)
        }
    }

    func deleteItem(
        identifier: NSFileProviderItemIdentifier, baseVersion version: NSFileProviderItemVersion,
        options: NSFileProviderDeleteItemOptions = [], request: NSFileProviderRequest,
        completionHandler: @escaping (Error?) -> Void
    ) -> Progress {
        run(completionHandler: { completionHandler($0) }) { [client] in
            guard let path = ItemID.path(for: identifier), path != "/" else {
                throw CocoaError(.fileWriteNoPermission)
            }
            do {
                try await client.remove(path, recursive: options.contains(.recursive))
            } catch CFDSError.notFound {
                // Already gone: that is what was asked for.
            }
            completionHandler(nil)
        }
    }

    // MARK: - Enumeration

    func enumerator(
        for containerItemIdentifier: NSFileProviderItemIdentifier, request: NSFileProviderRequest
    ) throws -> NSFileProviderEnumerator {
        if containerItemIdentifier == .workingSet { return WorkingSetEnumerator() }
        guard let path = ItemID.path(for: containerItemIdentifier) else {
            throw NSFileProviderError(.noSuchItem)
        }
        return FolderEnumerator(path: path, client: client)
    }

    // MARK: - Helpers

    /// Runs `body` in a task that the returned `Progress` can cancel, and reports any
    /// thrown error, translated for the Files app, through `completionHandler`.
    private func run(
        completionHandler: @escaping (Error) -> Void,
        _ body: @escaping () async throws -> Void
    ) -> Progress {
        let progress = Progress(totalUnitCount: 1)
        let task = Task {
            do {
                try await body()
            } catch {
                completionHandler(FileProviderErrors.map(error))
            }
            progress.completedUnitCount = 1
        }
        progress.cancellationHandler = { task.cancel() }
        return progress
    }

    private static func emptyFile() throws -> URL {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try Data().write(to: url)
        return url
    }
}
