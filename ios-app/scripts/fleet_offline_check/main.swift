import Foundation
// @testable-style harness: compile the REAL LoadState.swift (which contains the
// Offline enum) against minimal stubs for the two HSCC types it references.
// Proves the offline path's semantics: a failed fetch WITH a held/cached value
// must yield .stale (last known), NOT .failed; with nothing, .failed.

// ---- stubs ----
var cacheStore: [String: Data] = [:]
var cacheAges: [String: TimeInterval] = [:]

enum HSCCError: Error {
    case transport(underlying: Error)
    var localizedDescription: String { "can't reach the cluster" }
}

struct HSCCClient {
    func cachedValue<T: Decodable>(_ type: T.Type, for key: String) -> T? {
        guard let data = cacheStore[key] else { return nil }
        return try? JSONDecoder().decode(type, from: data)
    }
    func cacheAge(for key: String) -> TimeInterval? { cacheAges[key] }
}

// decode fixture
struct HealthResponse: Decodable, Encodable { let ok: Bool; let speak: String }

struct DummyError: Error {}
struct ThrowingClient {
    static func boom() async throws -> HealthResponse { throw HSCCError.transport(underlying: DummyError()) }
}

@main
struct FleetOfflineCheck {
    static var failures = 0
    static func check(_ name: String, _ cond: Bool) {
        print((cond ? "PASS " : "FAIL ") + name)
        if !cond { failures += 1 }
    }

    static func main() async {
        // Encode a cached HealthResponse into the stub cache.
        let cachedHealth = HealthResponse(ok: true, speak: "All checks passed.")
        let cachedData = try! JSONEncoder().encode(cachedHealth)
        cacheStore["/v1/health"] = cachedData
        cacheAges["/v1/health"] = 360

        let client = HSCCClient()

        // 1) Failed fetch WITH cached value → must be .stale, not .failed.
        let result1 = await Offline.load(LoadState<HealthResponse>.loaded(cachedHealth),
                                         cacheKey: "/v1/health",
                                         client: client) {
            try await ThrowingClient.boom()
        }
        if case .stale(let v, let msg) = result1 {
            check("offline WITH cached value → .stale (msg: \(msg))",
                  v.ok == true && msg.contains("showing state from"))
            print("   stale payload speak=\(v.speak), age msg=\(msg)")
        } else {
            check("offline WITH cached value → .stale", false)
            print("   GOT: \(result1)")
        }

        // 2) Failed fetch with NO cached value and NO held value → .failed.
        let result2 = await Offline.load(LoadState<HealthResponse>.idle,
                                         cacheKey: "/v1/health-missing",
                                         client: client) {
            try await ThrowingClient.boom()
        }
        if case .failed = result2 {
            check("offline with NO value → .failed", true)
        } else {
            check("offline with NO value → .failed", false)
            print("   GOT: \(result2)")
        }

        // 3) Successful fetch → .loaded
        let result3 = await Offline.load(LoadState<HealthResponse>.idle,
                                         cacheKey: "/v1/health",
                                         client: client) {
            cachedHealth
        }
        if case .loaded = result3 {
            check("successful fetch → .loaded", true)
        } else {
            check("successful fetch → .loaded", false)
            print("   GOT: \(result3)")
        }

        print(failures == 0 ? "ALL OFFLINE SEMANTICS PASS" : "\(failures) FAILURES")
        exit(failures == 0 ? 0 : 1)
    }
}
