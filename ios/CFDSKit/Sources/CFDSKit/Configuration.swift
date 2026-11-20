import Foundation

/// Where the app signs in and which storage server it talks to.
///
/// The values come from the app's (or extension's) Info.plist, which in turn are filled
/// from `Config/Base.xcconfig` at build time, so a different deployment only needs a
/// different xcconfig. Every key has a default that points at the production hosts.
public struct CFDSConfiguration: Sendable, Equatable {
    /// OIDC issuer. Discovery is read from `<issuer>/.well-known/openid-configuration`.
    public var issuer: URL
    /// Public OAuth client id registered for the iPhone app (no client secret).
    public var clientID: String
    /// Redirect URI registered with the identity provider for this client.
    public var redirectURI: URL
    /// Base URL of the cfds storage server (the `/v1/...` endpoints hang off it).
    public var apiBaseURL: URL
    /// Web page that explains the free tier; shown as a link on the sign-in screen.
    public var signupWebURL: URL
    public var scopes: [String]
    /// App group shared by the app and the File Provider extension.
    public var appGroup: String
    /// Keychain access group shared by both targets, including the team id prefix.
    public var keychainAccessGroup: String?

    /// Storage every new account gets for free: 100 GiB.
    public static let freeTierBytes: Int64 = 107_374_182_400

    public static let production = CFDSConfiguration(
        issuer: URL(string: "https://oidc.cloudfronts.group")!,
        clientID: "cfds-ios",
        redirectURI: URL(string: "groups.cloudfronts.datascientist:/oauth2/callback")!,
        apiBaseURL: URL(string: "https://storage.cloudfronts.group")!,
        signupWebURL: URL(string: "https://datascientist.cloudfronts.group")!,
        scopes: ["openid", "profile", "email"],
        appGroup: "group.com.cloudfronts.datascientist",
        keychainAccessGroup: nil
    )

    public init(
        issuer: URL,
        clientID: String,
        redirectURI: URL,
        apiBaseURL: URL,
        signupWebURL: URL,
        scopes: [String],
        appGroup: String,
        keychainAccessGroup: String?
    ) {
        self.issuer = issuer
        self.clientID = clientID
        self.redirectURI = redirectURI
        self.apiBaseURL = apiBaseURL
        self.signupWebURL = signupWebURL
        self.scopes = scopes
        self.appGroup = appGroup
        self.keychainAccessGroup = keychainAccessGroup
    }

    /// The URL scheme of `redirectURI`, which `ASWebAuthenticationSession` listens for.
    public var callbackScheme: String? { redirectURI.scheme }

    /// Reads the `CFDS*` keys from a bundle's Info.plist, falling back to `production`.
    public static func fromBundle(_ bundle: Bundle = .main) -> CFDSConfiguration {
        var config = CFDSConfiguration.production
        func string(_ key: String) -> String? {
            guard let value = bundle.object(forInfoDictionaryKey: key) as? String else { return nil }
            let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
            // An unset build setting leaves an empty string or the literal "$(NAME)".
            return trimmed.isEmpty || trimmed.hasPrefix("$(") ? nil : trimmed
        }
        func url(_ key: String) -> URL? { string(key).flatMap(URL.init(string:)) }

        if let value = url("CFDSIssuer") { config.issuer = value }
        if let value = string("CFDSClientID") { config.clientID = value }
        if let value = url("CFDSRedirectURI") { config.redirectURI = value }
        if let value = url("CFDSAPIBaseURL") { config.apiBaseURL = value }
        if let value = url("CFDSSignupWebURL") { config.signupWebURL = value }
        if let value = string("CFDSScopes") {
            config.scopes = value.split(separator: " ").map(String.init)
        }
        if let value = string("CFDSAppGroup") { config.appGroup = value }
        if let value = string("CFDSKeychainAccessGroup") { config.keychainAccessGroup = value }
        return config
    }
}
