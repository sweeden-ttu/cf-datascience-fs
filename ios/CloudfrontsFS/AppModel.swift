import CFDSKit
import FileProvider
import Foundation

/// App-wide state: who is signed in, their account, and the Files app domain.
@MainActor
final class AppModel: ObservableObject {
    enum Phase: Equatable {
        case loading
        case signedOut
        case signedIn
    }

    @Published private(set) var phase: Phase = .loading
    @Published private(set) var account: AccountInfo?
    @Published private(set) var isWorking = false
    @Published var errorMessage: String?

    let config: CFDSConfiguration
    let auth: AuthSession
    let client: CFDSClient
    private let authenticator = WebAuthenticator()

    init(config: CFDSConfiguration = .fromBundle()) {
        self.config = config
        let store = KeychainTokenStore(accessGroup: config.keychainAccessGroup)
        self.auth = AuthSession(config: config, store: store)
        self.client = CFDSClient(baseURL: config.apiBaseURL, tokens: auth)
    }

    func start() async {
        if await auth.needsSignIn {
            phase = .signedOut
            return
        }
        phase = .signedIn
        await refreshAccount()
    }

    /// Opens the Cloudfronts sign-in page; with `createAccount` the sign-up page.
    /// Sign-up is free and comes with the 100 GiB free tier.
    func signIn(createAccount: Bool) async {
        guard !isWorking else { return }
        isWorking = true
        defer { isWorking = false }
        do {
            let discovery = try await auth.discover()
            let request = AuthorizationRequest(
                discovery: discovery, config: config, createAccount: createAccount)
            guard let scheme = config.callbackScheme else {
                throw CFDSError.authorizationFailed("the redirect URI has no scheme")
            }
            let callback = try await authenticator.authenticate(
                url: request.url, callbackScheme: scheme)
            let code = try request.code(fromCallback: callback)
            let tokens = try await request.exchange(code: code)
            try await auth.signedIn(with: tokens)
            phase = .signedIn
            await refreshAccount()
            await FilesDomain.register()
        } catch CFDSError.cancelled {
            // The user closed the sheet; nothing to report.
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func refreshAccount() async {
        do {
            account = try await client.account()
            await FilesDomain.register()
        } catch CFDSError.notAuthenticated {
            phase = .signedOut
            account = nil
            await FilesDomain.signalNeedsAuthentication()
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func signOut() async {
        await auth.signOut()
        account = nil
        await FilesDomain.remove()
        phase = .signedOut
    }
}

/// The "Cloudfronts" location in the Files app.
enum FilesDomain {
    static let domain = NSFileProviderDomain(
        identifier: NSFileProviderDomainIdentifier("cloudfronts"), displayName: "Cloudfronts")

    static func register() async {
        do {
            try await NSFileProviderManager.add(domain)
            if let manager = NSFileProviderManager(for: domain) {
                try? await manager.signalEnumerator(for: .rootContainer)
                try? await manager.signalEnumerator(for: .workingSet)
            }
        } catch {
            NSLog("Cloudfronts: could not add the Files domain: \(error)")
        }
    }

    /// Lets the Files app retry after the user signs in again.
    static func signalNeedsAuthentication() async {
        guard let manager = NSFileProviderManager(for: domain) else { return }
        try? await manager.signalEnumerator(for: .rootContainer)
    }

    static func remove() async {
        do {
            try await NSFileProviderManager.remove(domain)
        } catch {
            NSLog("Cloudfronts: could not remove the Files domain: \(error)")
        }
    }
}
