// swift-tools-version:5.9
import PackageDescription

// Shared code for the Cloudfronts iPhone app and its File Provider extension:
// the cfds HTTP API client, OIDC sign-in helpers (PKCE), and Keychain token storage.
let package = Package(
    name: "CFDSKit",
    platforms: [.iOS(.v16), .macOS(.v13)],
    products: [
        .library(name: "CFDSKit", targets: ["CFDSKit"]),
    ],
    targets: [
        .target(name: "CFDSKit"),
        .testTarget(name: "CFDSKitTests", dependencies: ["CFDSKit"]),
    ]
)
