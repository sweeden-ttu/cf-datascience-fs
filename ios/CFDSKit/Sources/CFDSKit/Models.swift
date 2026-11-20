import Foundation

/// The kinds of entry `/v1/ls` reports.
public enum EntryType: String, Codable, Sendable {
    case file
    case directory
    case other

    public init(from decoder: Decoder) throws {
        let raw = try decoder.singleValueContainer().decode(String.self)
        self = EntryType(rawValue: raw) ?? .other
    }
}

/// One file or directory as described by the server (`/v1/ls`).
public struct RemoteEntry: Codable, Sendable, Equatable, Hashable {
    /// Absolute server path, for example `/home/models/model.pkl`.
    public var name: String
    public var size: Int64
    public var type: EntryType
    /// Modification time, seconds since 1970.
    public var mtime: Double
    public var islink: Bool
    /// Present for files. Changes whenever the file's content changes. Opaque.
    public var etag: String?
    /// Whether the signed-in user can change this path. `nil` from older servers.
    public var writable: Bool?

    public init(
        name: String, size: Int64, type: EntryType, mtime: Double, islink: Bool = false,
        etag: String? = nil, writable: Bool? = nil
    ) {
        self.name = name
        self.size = size
        self.type = type
        self.mtime = mtime
        self.islink = islink
        self.etag = etag
        self.writable = writable
    }

    public var isDirectory: Bool { type == .directory }
    public var modificationDate: Date { Date(timeIntervalSince1970: mtime) }
    public var filename: String { RemotePath.lastComponent(name) }

    enum CodingKeys: String, CodingKey { case name, size, type, mtime, islink, etag, writable }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        name = try c.decode(String.self, forKey: .name)
        size = try c.decodeIfPresent(Int64.self, forKey: .size) ?? 0
        type = try c.decodeIfPresent(EntryType.self, forKey: .type) ?? .other
        mtime = try c.decodeIfPresent(Double.self, forKey: .mtime) ?? 0
        islink = try c.decodeIfPresent(Bool.self, forKey: .islink) ?? false
        etag = try c.decodeIfPresent(String.self, forKey: .etag)
        writable = try c.decodeIfPresent(Bool.self, forKey: .writable)
    }
}

/// Body of `GET /v1/ls`.
public struct Listing: Codable, Sendable, Equatable {
    public var info: RemoteEntry
    /// `nil` for a file, or when only `info` was asked for.
    public var entries: [RemoteEntry]?
}

/// Body of `GET /v1/me`: who is signed in, their quota and where their folders are.
public struct AccountInfo: Codable, Sendable, Equatable {
    public struct User: Codable, Sendable, Equatable {
        public var sub: String
        public var email: String?
        public var name: String?
        /// For example `["free"]`.
        public var roles: [String]?
    }

    public struct Quota: Codable, Sendable, Equatable {
        /// Bytes allowed. 100 GiB on the free tier.
        public var limit: Int64
        /// Bytes in use under `home`.
        public var used: Int64
        public var available: Int64?

        public var fractionUsed: Double {
            limit > 0 ? min(1, max(0, Double(used) / Double(limit))) : 0
        }
    }

    public struct Mount: Codable, Sendable, Equatable, Hashable {
        public var path: String
        public var writable: Bool
        public var description: String?
    }

    public var user: User
    public var quota: Quota
    /// The user's own writable folder (`/home`).
    public var home: String
    /// The shared, read-only Kaggle datasets folder (`/kaggle-datasets`, which is
    /// `/mnt/TMP/ai-rig/kaggle-datasets` on the drive).
    public var datasets: String
    /// Where a client should open by default.
    public var defaultPath: String?
    public var mounts: [Mount]?

    enum CodingKeys: String, CodingKey {
        case user, quota, home, datasets, mounts
        case defaultPath = "default_path"
    }

    public var startPath: String { defaultPath ?? datasets }

    /// The mounts, or the two well-known folders when the server did not list them.
    public var allMounts: [Mount] {
        mounts ?? [
            Mount(path: datasets, writable: false, description: "Kaggle datasets (shared, read-only)"),
            Mount(path: home, writable: true, description: "Your files"),
        ]
    }
}

/// Error body: `{"error": {"code": "...", "message": "..."}}`.
struct ServerErrorBody: Decodable {
    struct Detail: Decodable {
        var code: String
        var message: String
    }

    var error: Detail
}
