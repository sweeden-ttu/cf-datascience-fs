import CryptoKit
import Foundation

/// A PKCE verifier and its S256 challenge (RFC 7636).
public struct PKCE: Sendable, Equatable {
    public let verifier: String
    public let challenge: String
    public let method = "S256"

    /// A fresh verifier of 32 random bytes (43 base64url characters).
    public init() {
        self.init(verifier: Self.randomURLSafeString(byteCount: 32))
    }

    public init(verifier: String) {
        self.verifier = verifier
        self.challenge = Self.base64URL(Data(SHA256.hash(data: Data(verifier.utf8))))
    }

    public static func randomURLSafeString(byteCount: Int) -> String {
        // SystemRandomNumberGenerator is a CSPRNG on Apple platforms.
        var generator = SystemRandomNumberGenerator()
        let bytes = (0..<byteCount).map { _ in UInt8.random(in: .min ... .max, using: &generator) }
        return base64URL(Data(bytes))
    }

    public static func base64URL(_ data: Data) -> String {
        data.base64EncodedString()
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
    }
}

/// The parts of the OpenID Provider metadata the app uses.
public struct OIDCDiscovery: Codable, Sendable, Equatable {
    public var issuer: String
    public var authorizationEndpoint: URL
    public var tokenEndpoint: URL
    public var endSessionEndpoint: URL?
    public var revocationEndpoint: URL?
    public var userinfoEndpoint: URL?

    enum CodingKeys: String, CodingKey {
        case issuer
        case authorizationEndpoint = "authorization_endpoint"
        case tokenEndpoint = "token_endpoint"
        case endSessionEndpoint = "end_session_endpoint"
        case revocationEndpoint = "revocation_endpoint"
        case userinfoEndpoint = "userinfo_endpoint"
    }

    public static func fetch(
        issuer: URL, session: URLSession = .shared
    ) async throws -> OIDCDiscovery {
        let url = issuer.appendingPathComponent(".well-known/openid-configuration")
        let (data, response) = try await session.data(from: url)
        guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
            throw CFDSError.invalidResponse("OIDC discovery failed for \(url.absoluteString)")
        }
        let discovery = try JSONDecoder().decode(OIDCDiscovery.self, from: data)
        // OpenID Connect Discovery 1.0, section 4.3: the issuer must match exactly.
        let expected = issuer.absoluteString.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        let reported = discovery.issuer.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        guard expected == reported else {
            throw CFDSError.invalidResponse("issuer mismatch: \(discovery.issuer)")
        }
        return discovery
    }
}

/// Access, refresh and ID tokens with the access token's expiry.
public struct TokenSet: Codable, Sendable, Equatable {
    public var accessToken: String
    public var refreshToken: String?
    public var idToken: String?
    public var expiresAt: Date

    public init(accessToken: String, refreshToken: String?, idToken: String?, expiresAt: Date) {
        self.accessToken = accessToken
        self.refreshToken = refreshToken
        self.idToken = idToken
        self.expiresAt = expiresAt
    }

    /// True when the access token expires within `leeway` seconds.
    public func isExpiring(within leeway: TimeInterval = 60, now: Date = Date()) -> Bool {
        expiresAt.timeIntervalSince(now) < leeway
    }

    /// Claims from the ID token, for display only. The signature is not checked here:
    /// the storage server validates the access token on every request, and the app never
    /// makes an access decision from these claims.
    public var idTokenClaims: [String: Any]? {
        guard let idToken else { return nil }
        let parts = idToken.split(separator: ".")
        guard parts.count >= 2 else { return nil }
        var payload = String(parts[1])
            .replacingOccurrences(of: "-", with: "+")
            .replacingOccurrences(of: "_", with: "/")
        while payload.count % 4 != 0 { payload += "=" }
        guard let data = Data(base64Encoded: payload) else { return nil }
        return (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
    }
}

/// One authorization-code-with-PKCE round trip, from building the URL to the tokens.
public struct AuthorizationRequest: Sendable {
    public let discovery: OIDCDiscovery
    public let config: CFDSConfiguration
    public let pkce: PKCE
    public let state: String
    public let nonce: String
    /// `true` asks the provider to show its sign-up page (`prompt=create`).
    public let createAccount: Bool

    public init(discovery: OIDCDiscovery, config: CFDSConfiguration, createAccount: Bool) {
        self.discovery = discovery
        self.config = config
        self.pkce = PKCE()
        self.state = PKCE.randomURLSafeString(byteCount: 16)
        self.nonce = PKCE.randomURLSafeString(byteCount: 16)
        self.createAccount = createAccount
    }

    public var url: URL {
        var components = URLComponents(
            url: discovery.authorizationEndpoint, resolvingAgainstBaseURL: false)!
        var items = components.queryItems ?? []
        items += [
            URLQueryItem(name: "response_type", value: "code"),
            URLQueryItem(name: "client_id", value: config.clientID),
            URLQueryItem(name: "redirect_uri", value: config.redirectURI.absoluteString),
            URLQueryItem(name: "scope", value: config.scopes.joined(separator: " ")),
            URLQueryItem(name: "state", value: state),
            URLQueryItem(name: "nonce", value: nonce),
            URLQueryItem(name: "code_challenge", value: pkce.challenge),
            URLQueryItem(name: "code_challenge_method", value: pkce.method),
        ]
        if createAccount {
            // OpenID Connect "Initiating User Registration via OpenID Connect 1.0".
            items.append(URLQueryItem(name: "prompt", value: "create"))
        }
        components.queryItems = items
        return components.url!
    }

    /// Pulls the authorization code out of the redirect, checking `state`.
    public func code(fromCallback callback: URL) throws -> String {
        let items = URLComponents(url: callback, resolvingAgainstBaseURL: false)?.queryItems ?? []
        func value(_ name: String) -> String? { items.first { $0.name == name }?.value }
        if let error = value("error") {
            if error == "access_denied" { throw CFDSError.cancelled }
            throw CFDSError.authorizationFailed(value("error_description") ?? error)
        }
        guard value("state") == state else {
            throw CFDSError.authorizationFailed("the sign-in response did not match the request")
        }
        guard let code = value("code"), !code.isEmpty else {
            throw CFDSError.authorizationFailed("the sign-in response had no code")
        }
        return code
    }

    public func exchange(code: String, session: URLSession = .shared) async throws -> TokenSet {
        try await TokenEndpoint.request(
            discovery.tokenEndpoint,
            form: [
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": config.redirectURI.absoluteString,
                "client_id": config.clientID,
                "code_verifier": pkce.verifier,
            ],
            previousRefreshToken: nil,
            session: session
        )
    }
}

enum TokenEndpoint {
    struct Response: Decodable {
        var access_token: String
        var token_type: String?
        var expires_in: Double?
        var refresh_token: String?
        var id_token: String?
    }

    struct ErrorResponse: Decodable {
        var error: String
        var error_description: String?
    }

    static func request(
        _ endpoint: URL, form: [String: String], previousRefreshToken: String?,
        session: URLSession
    ) async throws -> TokenSet {
        var request = URLRequest(url: endpoint)
        request.httpMethod = "POST"
        request.setValue("application/x-www-form-urlencoded", forHTTPHeaderField: "Content-Type")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.httpBody = formEncode(form).data(using: .utf8)
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else {
            throw CFDSError.invalidResponse("no HTTP response from the token endpoint")
        }
        guard http.statusCode == 200 else {
            if let body = try? JSONDecoder().decode(ErrorResponse.self, from: data) {
                if body.error == "invalid_grant" { throw CFDSError.notAuthenticated }
                throw CFDSError.authorizationFailed(body.error_description ?? body.error)
            }
            throw CFDSError.authorizationFailed("token endpoint returned \(http.statusCode)")
        }
        let body = try JSONDecoder().decode(Response.self, from: data)
        return TokenSet(
            accessToken: body.access_token,
            // Providers may omit the refresh token on refresh; keep using the old one.
            refreshToken: body.refresh_token ?? previousRefreshToken,
            idToken: body.id_token,
            expiresAt: Date().addingTimeInterval(body.expires_in ?? 300)
        )
    }

    static func formEncode(_ form: [String: String]) -> String {
        var allowed = CharacterSet.alphanumerics
        allowed.insert(charactersIn: "-._~")
        return form.keys.sorted().map { key in
            let value = form[key] ?? ""
            let k = key.addingPercentEncoding(withAllowedCharacters: allowed) ?? key
            let v = value.addingPercentEncoding(withAllowedCharacters: allowed) ?? value
            return "\(k)=\(v)"
        }.joined(separator: "&")
    }
}
