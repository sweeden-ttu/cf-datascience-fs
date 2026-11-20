import Foundation

/// Hands out a valid access token, refreshing it with the refresh token when needed.
public protocol AccessTokenProvider: Sendable {
    /// A current access token. `forceRefresh` is set after the server answered 401.
    func accessToken(forceRefresh: Bool) async throws -> String
}

/// Shared by the app and the File Provider extension. Both processes read and write the
/// same Keychain item, so before refreshing it re-reads the item: if the other process
/// already refreshed, its newer token is used instead of spending the refresh token again
/// (which a provider that rotates refresh tokens would reject).
public actor AuthSession: AccessTokenProvider {
    public let config: CFDSConfiguration
    private let store: TokenStore
    private let urlSession: URLSession
    private var discovery: OIDCDiscovery?
    private var refreshTask: Task<TokenSet, Error>?

    public init(config: CFDSConfiguration, store: TokenStore, urlSession: URLSession = .shared) {
        self.config = config
        self.store = store
        self.urlSession = urlSession
    }

    public var isSignedIn: Bool { (try? store.load()) != nil }

    /// True when there is no usable session: no tokens, or an expired access token with
    /// no refresh token (the provider may not issue refresh tokens to public clients).
    public var needsSignIn: Bool {
        guard let tokens = try? store.load() else { return true }
        return tokens.isExpiring(within: 0) && tokens.refreshToken == nil
    }

    public func currentTokens() -> TokenSet? { try? store.load() }

    public func discover() async throws -> OIDCDiscovery {
        if let discovery { return discovery }
        let fetched = try await OIDCDiscovery.fetch(issuer: config.issuer, session: urlSession)
        discovery = fetched
        return fetched
    }

    /// Stores the tokens from a completed sign-in.
    public func signedIn(with tokens: TokenSet) throws {
        try store.save(tokens)
    }

    /// Forgets the tokens. Revokes the refresh token when the provider supports it;
    /// a failed revocation does not stop the sign-out.
    public func signOut() async {
        let tokens = try? store.load()
        try? store.clear()
        guard let refresh = tokens?.refreshToken,
            let endpoint = try? await discover().revocationEndpoint
        else { return }
        var request = URLRequest(url: endpoint)
        request.httpMethod = "POST"
        request.setValue("application/x-www-form-urlencoded", forHTTPHeaderField: "Content-Type")
        request.httpBody = TokenEndpoint.formEncode([
            "token": refresh, "token_type_hint": "refresh_token", "client_id": config.clientID,
        ]).data(using: .utf8)
        _ = try? await urlSession.data(for: request)
    }

    public func accessToken(forceRefresh: Bool) async throws -> String {
        guard let tokens = try store.load() else { throw CFDSError.notAuthenticated }
        if !forceRefresh && !tokens.isExpiring() { return tokens.accessToken }
        return try await refresh(rejected: forceRefresh ? tokens.accessToken : nil).accessToken
    }

    private func refresh(rejected: String?) async throws -> TokenSet {
        if let refreshTask { return try await refreshTask.value }
        let task = Task { () throws -> TokenSet in
            // Another process may have refreshed while we were not looking.
            guard let latest = try store.load() else { throw CFDSError.notAuthenticated }
            if latest.accessToken != rejected && !latest.isExpiring() { return latest }
            guard let refreshToken = latest.refreshToken else { throw CFDSError.notAuthenticated }
            let endpoint = try await discover().tokenEndpoint
            do {
                let fresh = try await TokenEndpoint.request(
                    endpoint,
                    form: [
                        "grant_type": "refresh_token",
                        "refresh_token": refreshToken,
                        "client_id": config.clientID,
                    ],
                    previousRefreshToken: refreshToken,
                    session: urlSession
                )
                try store.save(fresh)
                return fresh
            } catch CFDSError.notAuthenticated {
                try? store.clear()
                throw CFDSError.notAuthenticated
            }
        }
        refreshTask = task
        defer { refreshTask = nil }
        return try await task.value
    }
}

/// A fixed token, for tests or a token pasted in by hand.
public struct StaticTokenProvider: AccessTokenProvider {
    public let token: String
    public init(_ token: String) { self.token = token }
    public func accessToken(forceRefresh: Bool) async throws -> String {
        if forceRefresh { throw CFDSError.notAuthenticated }
        return token
    }
}
