import Foundation

/// Helpers for the server's POSIX-style paths (always absolute, `/` separated).
public enum RemotePath {
    /// Collapses empty and `.` segments and guarantees a leading `/`. `..` is kept
    /// as-is; the server rejects it.
    public static func normalize(_ path: String) -> String {
        let parts = path.split(separator: "/", omittingEmptySubsequences: true)
            .filter { $0 != "." }
        return "/" + parts.joined(separator: "/")
    }

    public static func join(_ directory: String, _ name: String) -> String {
        normalize(normalize(directory) + "/" + name)
    }

    /// Parent directory; the parent of `/` is `/`.
    public static func parent(_ path: String) -> String {
        let normalized = normalize(path)
        guard let slash = normalized.lastIndex(of: "/"), normalized != "/" else { return "/" }
        let parent = String(normalized[..<slash])
        return parent.isEmpty ? "/" : parent
    }

    public static func lastComponent(_ path: String) -> String {
        let normalized = normalize(path)
        return normalized == "/" ? "/" : String(normalized.split(separator: "/").last ?? "")
    }

    /// True when `path` is `root` or below it.
    public static func isWithin(_ path: String, _ root: String) -> Bool {
        let path = normalize(path)
        let root = normalize(root)
        if root == "/" || path == root { return true }
        return path.hasPrefix(root + "/")
    }

    /// A file name the server will accept: not empty, no `/`, NUL, `.` or `..`.
    public static func isValidName(_ name: String) -> Bool {
        !name.isEmpty && name != "." && name != ".." && !name.contains("/")
            && !name.contains("\u{0}")
    }
}
