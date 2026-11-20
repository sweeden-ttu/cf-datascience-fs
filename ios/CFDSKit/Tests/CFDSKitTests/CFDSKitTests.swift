import XCTest

@testable import CFDSKit

final class PKCETests: XCTestCase {
    func testChallengeMatchesRFC7636AppendixB() {
        // The worked example from RFC 7636, Appendix B.
        let pkce = PKCE(verifier: "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk")
        XCTAssertEqual(pkce.challenge, "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM")
        XCTAssertEqual(pkce.method, "S256")
    }

    func testRandomVerifierIsURLSafeAndLongEnough() {
        let verifier = PKCE().verifier
        XCTAssertGreaterThanOrEqual(verifier.count, 43)
        XCTAssertNil(verifier.rangeOfCharacter(from: CharacterSet(charactersIn: "+/=")))
        XCTAssertNotEqual(verifier, PKCE().verifier)
    }
}

final class AuthorizationRequestTests: XCTestCase {
    private let discovery = OIDCDiscovery(
        issuer: "https://oidc.cloudfronts.group",
        authorizationEndpoint: URL(string: "https://oidc.cloudfronts.group/oauth2/authorize")!,
        tokenEndpoint: URL(string: "https://oidc.cloudfronts.group/oauth2/token")!,
        endSessionEndpoint: nil, revocationEndpoint: nil, userinfoEndpoint: nil)

    private func query(_ url: URL) -> [String: String] {
        let items = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems ?? []
        return Dictionary(uniqueKeysWithValues: items.map { ($0.name, $0.value ?? "") })
    }

    func testAuthorizationURLCarriesPKCEAndClient() {
        let request = AuthorizationRequest(
            discovery: discovery, config: .production, createAccount: false)
        let q = query(request.url)
        XCTAssertEqual(q["response_type"], "code")
        XCTAssertEqual(q["client_id"], "cfds-ios")
        XCTAssertEqual(q["redirect_uri"], "groups.cloudfronts.datascientist:/oauth2/callback")
        XCTAssertEqual(q["scope"], "openid profile email")
        XCTAssertEqual(q["code_challenge"], request.pkce.challenge)
        XCTAssertEqual(q["code_challenge_method"], "S256")
        XCTAssertEqual(q["state"], request.state)
        XCTAssertNil(q["prompt"])
    }

    func testSignUpAsksForRegistration() {
        let request = AuthorizationRequest(
            discovery: discovery, config: .production, createAccount: true)
        XCTAssertEqual(query(request.url)["prompt"], "create")
    }

    func testCallbackChecksState() throws {
        let request = AuthorizationRequest(
            discovery: discovery, config: .production, createAccount: false)
        let good = URL(
            string: "groups.cloudfronts.datascientist:/oauth2/callback?code=abc&state=\(request.state)")!
        XCTAssertEqual(try request.code(fromCallback: good), "abc")

        let forged = URL(string: "groups.cloudfronts.datascientist:/oauth2/callback?code=abc&state=x")!
        XCTAssertThrowsError(try request.code(fromCallback: forged))

        let denied = URL(
            string: "groups.cloudfronts.datascientist:/oauth2/callback?error=access_denied")!
        XCTAssertThrowsError(try request.code(fromCallback: denied)) { error in
            XCTAssertEqual(error as? CFDSError, .cancelled)
        }
    }
}

final class TokenSetTests: XCTestCase {
    func testExpiry() {
        let now = Date()
        let tokens = TokenSet(
            accessToken: "a", refreshToken: nil, idToken: nil, expiresAt: now.addingTimeInterval(30))
        XCTAssertTrue(tokens.isExpiring(within: 60, now: now))
        XCTAssertFalse(tokens.isExpiring(within: 10, now: now))
    }

    func testIDTokenClaimsAreDecoded() {
        // {"alg":"none"}.{"sub":"alice","email":"a@example.com"}.
        let jwt = "eyJhbGciOiJub25lIn0.eyJzdWIiOiJhbGljZSIsImVtYWlsIjoiYUBleGFtcGxlLmNvbSJ9."
        let tokens = TokenSet(accessToken: "a", refreshToken: nil, idToken: jwt, expiresAt: Date())
        XCTAssertEqual(tokens.idTokenClaims?["sub"] as? String, "alice")
        XCTAssertEqual(tokens.idTokenClaims?["email"] as? String, "a@example.com")
    }

    func testFormEncodingEscapesReservedCharacters() {
        XCTAssertEqual(
            TokenEndpoint.formEncode(["b": "x y+z", "a": "1/2"]), "a=1%2F2&b=x%20y%2Bz")
    }
}

final class RemotePathTests: XCTestCase {
    func testNormalizeJoinParent() {
        XCTAssertEqual(RemotePath.normalize("home//a/./b/"), "/home/a/b")
        XCTAssertEqual(RemotePath.join("/home", "x.csv"), "/home/x.csv")
        XCTAssertEqual(RemotePath.join("/", "home"), "/home")
        XCTAssertEqual(RemotePath.parent("/home/x.csv"), "/home")
        XCTAssertEqual(RemotePath.parent("/home"), "/")
        XCTAssertEqual(RemotePath.parent("/"), "/")
        XCTAssertEqual(RemotePath.lastComponent("/home/x.csv"), "x.csv")
    }

    func testIsWithin() {
        XCTAssertTrue(RemotePath.isWithin("/kaggle-datasets/titanic", "/kaggle-datasets"))
        XCTAssertTrue(RemotePath.isWithin("/kaggle-datasets", "/kaggle-datasets"))
        XCTAssertFalse(RemotePath.isWithin("/kaggle-datasets-2", "/kaggle-datasets"))
    }

    func testValidNames() {
        XCTAssertTrue(RemotePath.isValidName("train.csv"))
        XCTAssertFalse(RemotePath.isValidName(".."))
        XCTAssertFalse(RemotePath.isValidName("a/b"))
        XCTAssertFalse(RemotePath.isValidName(""))
    }
}

final class ModelDecodingTests: XCTestCase {
    func testAccountFromAPIDocExample() throws {
        let json = """
            {"user": {"sub": "alice", "email": "alice@example.com", "name": "Alice",
                      "roles": ["free"]},
             "quota": {"limit": 107374182400, "used": 52428800, "available": 107321753600},
             "home": "/home",
             "datasets": "/kaggle-datasets",
             "default_path": "/kaggle-datasets",
             "mounts": [
               {"path": "/kaggle-datasets", "writable": false, "description": "Kaggle datasets"},
               {"path": "/home", "writable": true, "description": "Your files"}]}
            """
        let account = try JSONDecoder().decode(AccountInfo.self, from: Data(json.utf8))
        XCTAssertEqual(account.home, "/home")
        XCTAssertEqual(account.startPath, "/kaggle-datasets")
        XCTAssertEqual(account.quota.limit, CFDSConfiguration.freeTierBytes)
        XCTAssertEqual(account.allMounts.count, 2)
        XCTAssertEqual(account.quota.fractionUsed, 52428800.0 / 107374182400.0, accuracy: 1e-12)
    }

    func testListingWithNullEntriesAndUnknownType() throws {
        let json = """
            {"info": {"name": "/home/x", "size": 3, "type": "socket", "mtime": 1.5,
                      "islink": false, "writable": true, "etag": "\\"a-3\\""},
             "entries": null}
            """
        let listing = try JSONDecoder().decode(Listing.self, from: Data(json.utf8))
        XCTAssertNil(listing.entries)
        XCTAssertEqual(listing.info.type, .other)
        XCTAssertEqual(listing.info.etag, "\"a-3\"")
        XCTAssertEqual(listing.info.writable, true)
    }

    func testErrorMapping() {
        func body(_ code: String) -> Data {
            Data(#"{"error": {"code": "\#(code)", "message": "m"}}"#.utf8)
        }
        XCTAssertEqual(CFDSClient.error(status: 401, body: body("unauthorized"), path: "/"), .notAuthenticated)
        XCTAssertEqual(CFDSClient.error(status: 403, body: body("read_only"), path: "/k"), .readOnly("/k"))
        XCTAssertEqual(CFDSClient.error(status: 507, body: body("quota_exceeded"), path: "/h"), .quotaExceeded)
        XCTAssertEqual(CFDSClient.error(status: 409, body: body("not_empty"), path: "/h"), .directoryNotEmpty("/h"))
        XCTAssertEqual(CFDSClient.error(status: 409, body: body("exists"), path: "/h"), .alreadyExists("/h"))
        XCTAssertEqual(CFDSClient.error(status: 404, body: body("parent_not_found"), path: "/h"), .parentNotFound("/h"))
        XCTAssertEqual(CFDSClient.error(status: 412, body: body("precondition_failed"), path: "/h"), .changedOnServer("/h"))
        XCTAssertEqual(CFDSClient.error(status: 400, body: body("cross_mount"), path: "/h"), .crossMount("/h"))
        XCTAssertEqual(CFDSClient.error(status: 409, body: body("source_changed"), path: "/h"), .changedOnServer("/h"))
        // The server could not reach the sign-in provider: not a reason to sign in again.
        XCTAssertEqual(
            CFDSClient.error(status: 503, body: body("auth_unavailable"), path: "/h"),
            .server(status: 503, code: "auth_unavailable", message: "m"))
    }
}

/// Exercises the client against canned HTTP responses.
final class ClientTests: XCTestCase {
    override func tearDown() {
        StubProtocol.handler = nil
        super.tearDown()
    }

    private func makeClient(tokens: AccessTokenProvider = StaticTokenProvider("t0k")) -> CFDSClient {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StubProtocol.self]
        return CFDSClient(
            baseURL: URL(string: "https://storage.example")!, tokens: tokens,
            session: URLSession(configuration: configuration))
    }

    func testListSendsBearerTokenAndEncodesPath() async throws {
        StubProtocol.handler = { request in
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer t0k")
            XCTAssertEqual(request.url?.path, "/v1/ls")
            XCTAssertEqual(request.url?.query, "path=/home/a%2Bb%20c.csv")
            let body = #"{"info": {"name": "/home/a+b c.csv", "size": 1, "type": "file", "mtime": 0}, "entries": null}"#
            return (200, Data(body.utf8))
        }
        let listing = try await makeClient().list("/home/a+b c.csv")
        XCTAssertEqual(listing.info.name, "/home/a+b c.csv")
    }

    func testRetriesOnceWithRefreshedTokenAfter401() async throws {
        final class Rotating: AccessTokenProvider, @unchecked Sendable {
            var forced = 0
            func accessToken(forceRefresh: Bool) async throws -> String {
                if forceRefresh { forced += 1 }
                return forceRefresh ? "new" : "old"
            }
        }
        let tokens = Rotating()
        StubProtocol.handler = { request in
            if request.value(forHTTPHeaderField: "Authorization") == "Bearer old" {
                return (401, Data(#"{"error": {"code": "unauthorized", "message": "expired"}}"#.utf8))
            }
            return (204, Data())
        }
        try await makeClient(tokens: tokens).remove("/home/x", recursive: true)
        XCTAssertEqual(tokens.forced, 1)
    }

    func testMoveFallsBackToInfoWhenBodyIsNotAnEntry() async throws {
        StubProtocol.handler = { request in
            if request.url?.path == "/v1/mv" {
                XCTAssertEqual(request.httpMethod, "POST")
                return (200, Data("{}".utf8))
            }
            let body = #"{"info": {"name": "/home/b", "size": 0, "type": "directory", "mtime": 0}, "entries": null}"#
            return (200, Data(body.utf8))
        }
        let entry = try await makeClient().move("/home/a", to: "/home/b")
        XCTAssertEqual(entry.name, "/home/b")
        XCTAssertTrue(entry.isDirectory)
    }
}

final class StubProtocol: URLProtocol {
    nonisolated(unsafe) static var handler: ((URLRequest) -> (Int, Data))?

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        guard let handler = Self.handler else {
            client?.urlProtocol(self, didFailWithError: URLError(.badServerResponse))
            return
        }
        let (status, data) = handler(request)
        let response = HTTPURLResponse(
            url: request.url!, statusCode: status, httpVersion: "HTTP/1.1",
            headerFields: ["Content-Type": "application/json"])!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: data)
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}
