import AuthenticationServices
import CFDSKit
import UIKit

/// Runs one browser sign-in (or sign-up) with `ASWebAuthenticationSession` and returns
/// the redirect URL.
@MainActor
final class WebAuthenticator: NSObject, ASWebAuthenticationPresentationContextProviding {
    private var session: ASWebAuthenticationSession?

    func authenticate(url: URL, callbackScheme: String) async throws -> URL {
        try await withCheckedThrowingContinuation { continuation in
            let session = ASWebAuthenticationSession(
                url: url, callbackURLScheme: callbackScheme
            ) { callback, error in
                if let callback {
                    continuation.resume(returning: callback)
                } else if let error = error as? ASWebAuthenticationSessionError,
                    error.code == .canceledLogin
                {
                    continuation.resume(throwing: CFDSError.cancelled)
                } else {
                    continuation.resume(throwing: error ?? CFDSError.cancelled)
                }
            }
            session.presentationContextProvider = self
            // Share cookies with Safari so a user already signed in at
            // datascientist.cloudfronts.group is not asked again.
            session.prefersEphemeralWebBrowserSession = false
            self.session = session
            if !session.start() {
                continuation.resume(throwing: CFDSError.cancelled)
            }
        }
    }

    nonisolated func presentationAnchor(for session: ASWebAuthenticationSession)
        -> ASPresentationAnchor
    {
        MainActor.assumeIsolated {
            let scenes = UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }
            let window = scenes.flatMap(\.windows).first(where: \.isKeyWindow)
            return window ?? ASPresentationAnchor()
        }
    }
}

