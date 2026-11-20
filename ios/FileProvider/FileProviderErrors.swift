import CFDSKit
import FileProvider
import Foundation

/// Translates client errors into the errors the Files app knows how to show.
enum FileProviderErrors {
    static func map(_ error: Error) -> Error {
        if error is CancellationError { return CocoaError(.userCancelled) }
        if let error = error as? CFDSError {
            switch error {
            case .notAuthenticated, .cancelled, .authorizationFailed:
                // Files shows "Sign in" and opens the app.
                return NSFileProviderError(.notAuthenticated)
            case .notFound, .parentNotFound:
                return NSFileProviderError(.noSuchItem)
            case .alreadyExists:
                return NSFileProviderError(.filenameCollision)
            case .directoryNotEmpty:
                return NSFileProviderError(.directoryNotEmpty)
            case .quotaExceeded:
                return NSFileProviderError(.insufficientQuota)
            case .readOnly, .forbidden, .crossMount:
                return CocoaError(.fileWriteNoPermission)
            case .changedOnServer:
                // The system fetches the server's version and retries the change.
                return NSFileProviderError(.cannotSynchronize)
            case .server(let status, _, _) where status >= 500:
                return NSFileProviderError(.serverUnreachable)
            case .server, .invalidResponse:
                return NSFileProviderError(.cannotSynchronize)
            }
        }
        if let error = error as? URLError {
            switch error.code {
            case .notConnectedToInternet, .networkConnectionLost, .dataNotAllowed:
                return NSFileProviderError(.serverUnreachable)
            case .cannotFindHost, .cannotConnectToHost, .timedOut, .dnsLookupFailed:
                return NSFileProviderError(.serverUnreachable)
            case .cancelled:
                return CocoaError(.userCancelled)
            default:
                return NSFileProviderError(.cannotSynchronize)
            }
        }
        return error
    }
}
