import Foundation

public enum CFDSError: Error, Equatable, LocalizedError {
    /// No tokens, or the refresh token was rejected: the user must sign in again.
    case notAuthenticated
    case cancelled
    case authorizationFailed(String)
    case notFound(String)
    case forbidden(String)
    /// The path is in the read-only Kaggle datasets area.
    case readOnly(String)
    /// Something already exists at the destination.
    case alreadyExists(String)
    /// The folder a new item should go into does not exist.
    case parentNotFound(String)
    /// `mv` between `/home` and `/kaggle-datasets`; copy instead.
    case crossMount(String)
    /// A directory still has entries and the delete was not recursive.
    case directoryNotEmpty(String)
    /// `If-Match` failed: the file changed on the server since it was read.
    case changedOnServer(String)
    /// The 100 GB free tier (or the account's limit) is full.
    case quotaExceeded
    case server(status: Int, code: String, message: String)
    case invalidResponse(String)

    public var errorDescription: String? {
        switch self {
        case .notAuthenticated: return "Please sign in to Cloudfronts again."
        case .cancelled: return "Sign-in was cancelled."
        case .authorizationFailed(let message): return "Sign-in failed: \(message)"
        case .notFound(let path): return "Not found: \(path)"
        case .forbidden(let path): return "You don't have access to \(path)."
        case .readOnly(let path): return "\(path) is read-only."
        case .alreadyExists(let path): return "\(path) already exists."
        case .parentNotFound(let path): return "The folder for \(path) does not exist."
        case .crossMount(let path): return "\(path) can't be moved there. Copy it instead."
        case .directoryNotEmpty(let path): return "\(path) is not empty."
        case .changedOnServer(let path): return "\(path) changed on the server."
        case .quotaExceeded: return "Your Cloudfronts storage is full."
        case .server(let status, _, let message): return "Server error \(status): \(message)"
        case .invalidResponse(let message): return message
        }
    }
}
