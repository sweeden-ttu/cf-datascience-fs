import Foundation

/// Client for the cfds storage server's HTTP API.
///
/// Follows the v1 contract shared by the server, the Python client and the VS Code
/// extension (`cfds-api/API.md`). Every route the app uses is in this file, so a change
/// to the server's routes only touches this type.
public final class CFDSClient: Sendable {
    public let baseURL: URL
    private let tokens: AccessTokenProvider
    private let session: URLSession

    public init(baseURL: URL, tokens: AccessTokenProvider, session: URLSession = .shared) {
        self.baseURL = baseURL
        self.tokens = tokens
        self.session = session
    }

    // MARK: - Reads

    /// `GET /v1/me`: the signed-in user, their quota, and their home and datasets folders.
    public func account() async throws -> AccountInfo {
        let (data, _) = try await send(method: "GET", endpoint: "me", query: [:])
        return try decode(AccountInfo.self, data)
    }

    /// `GET /v1/ls?path=`: the entry and, for a directory, its children.
    public func list(_ path: String) async throws -> Listing {
        let (data, _) = try await send(
            method: "GET", endpoint: "ls", query: ["path": RemotePath.normalize(path)])
        return try decode(Listing.self, data)
    }

    /// `GET /v1/ls?path=&list=0`: the entry alone.
    public func info(_ path: String) async throws -> RemoteEntry {
        let (data, _) = try await send(
            method: "GET", endpoint: "ls",
            query: ["path": RemotePath.normalize(path), "list": "0"])
        return try decode(Listing.self, data).info
    }

    /// `GET /v1/file?path=`: downloads the whole file to `destination` (replacing it).
    /// With `ifMatch`, fails with `.changedOnServer` if the file is no longer that version.
    @discardableResult
    public func download(
        _ path: String, to destination: URL, ifMatch etag: String? = nil
    ) async throws -> HTTPURLResponse {
        var headers: [String: String] = [:]
        if let etag { headers["If-Match"] = etag }
        let request = try await makeRequest(
            method: "GET", endpoint: "file", query: ["path": RemotePath.normalize(path)],
            headers: headers, forceRefresh: false)
        var (location, response) = try await session.download(for: request)
        if (response as? HTTPURLResponse)?.statusCode == 401 {
            try? FileManager.default.removeItem(at: location)
            let retry = try await makeRequest(
                method: "GET", endpoint: "file", query: ["path": RemotePath.normalize(path)],
                headers: headers, forceRefresh: true)
            (location, response) = try await session.download(for: retry)
        }
        guard let http = response as? HTTPURLResponse else {
            throw CFDSError.invalidResponse("no HTTP response")
        }
        guard (200..<300).contains(http.statusCode) else {
            let body = (try? Data(contentsOf: location)) ?? Data()
            try? FileManager.default.removeItem(at: location)
            throw Self.error(status: http.statusCode, body: body, path: path)
        }
        let fm = FileManager.default
        if fm.fileExists(atPath: destination.path) { try fm.removeItem(at: destination) }
        try fm.moveItem(at: location, to: destination)
        return http
    }

    // MARK: - Writes

    /// How an upload treats an existing file at the destination.
    public enum UploadMode: Sendable, Equatable {
        /// Create or replace.
        case createOrReplace
        /// Fail with `.alreadyExists` if anything is there.
        case createOnly
        /// Replace only the version with this ETag; `.changedOnServer` otherwise.
        case replace(ifMatch: String)
    }

    /// `PUT /v1/file?path=` with the file's bytes as the body. Writes are atomic on the
    /// server: a failed upload leaves the old file as it was.
    public func upload(
        fileAt source: URL, to path: String, mode: UploadMode = .createOrReplace
    ) async throws -> RemoteEntry {
        let path = RemotePath.normalize(path)
        var headers = ["Content-Type": "application/octet-stream"]
        var query = ["path": path]
        switch mode {
        case .createOrReplace: break
        case .createOnly: query["overwrite"] = "0"
        case .replace(let etag):
            query["create"] = "0"
            headers["If-Match"] = etag
        }
        var (data, http) = try await uploadOnce(source, query, headers, forceRefresh: false)
        if http.statusCode == 401 {
            (data, http) = try await uploadOnce(source, query, headers, forceRefresh: true)
        }
        guard (200..<300).contains(http.statusCode) else {
            throw Self.error(status: http.statusCode, body: data, path: path)
        }
        return try await entry(from: data, fallbackPath: path)
    }

    /// `POST /v1/mkdir?path=`.
    public func makeDirectory(_ path: String) async throws -> RemoteEntry {
        let path = RemotePath.normalize(path)
        let (data, _) = try await send(method: "POST", endpoint: "mkdir", query: ["path": path])
        return try await entry(from: data, fallbackPath: path)
    }

    /// `DELETE /v1/file?path=[&recursive=1]`. A non-empty directory needs `recursive`.
    public func remove(_ path: String, recursive: Bool) async throws {
        var query = ["path": RemotePath.normalize(path)]
        if recursive { query["recursive"] = "1" }
        _ = try await send(method: "DELETE", endpoint: "file", query: query)
    }

    /// `POST /v1/mv?src=&dst=`: rename or move within one mount. Fails with
    /// `.alreadyExists` if `destination` exists, `.crossMount` between mounts.
    public func move(_ source: String, to destination: String) async throws -> RemoteEntry {
        let destination = RemotePath.normalize(destination)
        let (data, _) = try await send(
            method: "POST", endpoint: "mv",
            query: ["src": RemotePath.normalize(source), "dst": destination, "overwrite": "0"])
        return try await entry(from: data, fallbackPath: destination)
    }

    /// `POST /v1/cp?src=&dst=`: server-side copy of one file, for example a dataset file
    /// into `/home`. Counts against the quota.
    public func copy(_ source: String, to destination: String) async throws -> RemoteEntry {
        let destination = RemotePath.normalize(destination)
        let (data, _) = try await send(
            method: "POST", endpoint: "cp",
            query: ["src": RemotePath.normalize(source), "dst": destination, "overwrite": "0"])
        return try await entry(from: data, fallbackPath: destination)
    }

    // MARK: - Plumbing

    public static let userAgent = "cfds-ios/1.0"

    private func url(endpoint: String, query: [String: String]) -> URL {
        var components = URLComponents(
            url: baseURL.appendingPathComponent("v1").appendingPathComponent(endpoint),
            resolvingAgainstBaseURL: false)!
        if !query.isEmpty {
            components.queryItems = query.keys.sorted().map { URLQueryItem(name: $0, value: query[$0]) }
            // URLComponents leaves "+" alone, but servers decode it as a space.
            components.percentEncodedQuery = components.percentEncodedQuery?
                .replacingOccurrences(of: "+", with: "%2B")
        }
        return components.url!
    }

    private func makeRequest(
        method: String, endpoint: String, query: [String: String], headers: [String: String],
        forceRefresh: Bool
    ) async throws -> URLRequest {
        var request = URLRequest(url: url(endpoint: endpoint, query: query))
        request.httpMethod = method
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue(Self.userAgent, forHTTPHeaderField: "User-Agent")
        let token = try await tokens.accessToken(forceRefresh: forceRefresh)
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        for (key, value) in headers { request.setValue(value, forHTTPHeaderField: key) }
        return request
    }

    /// Sends a request with no body, retrying once with a refreshed token after a 401.
    private func send(
        method: String, endpoint: String, query: [String: String],
        headers: [String: String] = [:]
    ) async throws -> (Data, HTTPURLResponse) {
        var lastStatus = 0
        var lastData = Data()
        for forceRefresh in [false, true] {
            let request = try await makeRequest(
                method: method, endpoint: endpoint, query: query, headers: headers,
                forceRefresh: forceRefresh)
            let (data, response) = try await session.data(for: request)
            guard let http = response as? HTTPURLResponse else {
                throw CFDSError.invalidResponse("no HTTP response")
            }
            if (200..<300).contains(http.statusCode) { return (data, http) }
            lastStatus = http.statusCode
            lastData = data
            if http.statusCode != 401 { break }
        }
        throw Self.error(status: lastStatus, body: lastData, path: query["path"] ?? query["dst"] ?? "")
    }

    private func uploadOnce(
        _ source: URL, _ query: [String: String], _ headers: [String: String], forceRefresh: Bool
    ) async throws -> (Data, HTTPURLResponse) {
        let request = try await makeRequest(
            method: "PUT", endpoint: "file", query: query, headers: headers,
            forceRefresh: forceRefresh)
        let (data, response) = try await session.upload(for: request, fromFile: source)
        guard let http = response as? HTTPURLResponse else {
            throw CFDSError.invalidResponse("no HTTP response")
        }
        return (data, http)
    }

    /// Write endpoints answer with the new entry (bare, or as `{"info": ...}`). If the
    /// body is anything else, ask the server for the entry.
    private func entry(from data: Data, fallbackPath: String) async throws -> RemoteEntry {
        let decoder = JSONDecoder()
        if let entry = try? decoder.decode(RemoteEntry.self, from: data) { return entry }
        if let listing = try? decoder.decode(Listing.self, from: data) { return listing.info }
        return try await info(fallbackPath)
    }

    private func decode<T: Decodable>(_ type: T.Type, _ data: Data) throws -> T {
        do {
            return try JSONDecoder().decode(type, from: data)
        } catch {
            throw CFDSError.invalidResponse("unexpected response from the server: \(error)")
        }
    }

    /// Maps an HTTP error response onto `CFDSError`.
    static func error(status: Int, body: Data, path: String) -> CFDSError {
        let detail = (try? JSONDecoder().decode(ServerErrorBody.self, from: body))?.error
        let code = detail?.code ?? ""
        let message = detail?.message ?? HTTPURLResponse.localizedString(forStatusCode: status)
        switch (status, code) {
        case (401, _): return .notAuthenticated
        case (_, "read_only"): return .readOnly(path)
        case (_, "quota_exceeded"), (507, _): return .quotaExceeded
        case (_, "parent_not_found"): return .parentNotFound(path)
        case (_, "cross_mount"): return .crossMount(path)
        case (_, "not_empty"): return .directoryNotEmpty(path)
        case (403, _): return .forbidden(path)
        case (404, _): return .notFound(path)
        case (409, _): return .alreadyExists(path)
        case (412, _): return .changedOnServer(path)
        default: return .server(status: status, code: code, message: message)
        }
    }
}
