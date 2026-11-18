import Foundation
import ServiceManagement
import SwiftUI

// ---------------------------------------------------------------------------
// The macOS app's small preference set — menu bar stat, refresh cadence and
// launch-at-login. Connection settings and the cluster list live in the
// REUSED `SettingsStore` / App Group store; nothing here duplicates them.
//
// These keys are app-local UserDefaults in the same shared suite so a future
// widget extension can read the cadence without a second store. They are NOT
// the iOS widget-registry blob: a menu bar app's cadence is about the menu bar
// surface, not a Home Screen widget.
// ---------------------------------------------------------------------------

/// Which optional short stat rides next to the menu bar H.
enum MenuBarStat: String, CaseIterable, Identifiable {
    case off
    case runningCards
    case clusterNodes

    var id: String { rawValue }

    var label: String {
        switch self {
        case .off: return "Icon only"
        case .runningCards: return "Running cards"
        case .clusterNodes: return "Cluster nodes up"
        }
    }
}

final class MacPreferences: ObservableObject {
    private enum Keys {
        static let menuBarStat = "hscc.mac.menuBarStat"
        static let refreshSeconds = "hscc.mac.refreshSeconds"
        static let launchAtLogin = "hscc.mac.launchAtLogin"
    }

    /// The cadences the Settings picker offers. 30 s is the floor: a menu bar
    /// read every few seconds is noise, and the daemon's own state changes on a
    /// minutes scale.
    static let cadences = [30, 60, 120, 300, 600]
    static let defaultCadence = 60

    private static var suite: UserDefaults {
        UserDefaults(suiteName: AppGroup.suiteName) ?? .standard
    }

    @Published var menuBarStat: MenuBarStat {
        didSet { Self.suite.set(menuBarStat.rawValue, forKey: Keys.menuBarStat) }
    }

    @Published var refreshSeconds: Int {
        didSet {
            if !Self.cadences.contains(refreshSeconds) {
                refreshSeconds = Self.defaultCadence
            }
            Self.suite.set(refreshSeconds, forKey: Keys.refreshSeconds)
        }
    }

    /// Mirrors the actual Login Items state. Toggling it goes through
    /// `SMAppService`, and a failure is surfaced rather than silently flipped.
    @Published private(set) var launchAtLogin: Bool
    @Published private(set) var launchAtLoginError: String?

    init() {
        let defaults = Self.suite
        menuBarStat = MenuBarStat(rawValue: defaults.string(forKey: Keys.menuBarStat) ?? "") ?? .clusterNodes
        let storedCadence = defaults.integer(forKey: Keys.refreshSeconds)
        refreshSeconds = Self.cadences.contains(storedCadence)
            ? storedCadence
            : Self.defaultCadence
        launchAtLogin = SMAppService.mainApp.status == .enabled
        launchAtLoginError = nil
    }

    /// The cadence for the polling loop. Reads the stored value so a change in
    /// Settings is picked up on the next loop iteration without a second
    /// publisher chain.
    static func storedRefreshSeconds() -> Int {
        let stored = suite.integer(forKey: Keys.refreshSeconds)
        return cadences.contains(stored) ? stored : defaultCadence
    }

    func setLaunchAtLogin(_ enabled: Bool) {
        launchAtLoginError = nil
        do {
            if enabled {
                if SMAppService.mainApp.status != .enabled {
                    try SMAppService.mainApp.register()
                }
            } else {
                try SMAppService.mainApp.unregister()
            }
            launchAtLogin = enabled
            Self.suite.set(enabled, forKey: Keys.launchAtLogin)
            if enabled && SMAppService.mainApp.status == .requiresApproval {
                launchAtLoginError = "Approve HSCC in System Settings → General → Login Items."
            }
        } catch {
            launchAtLogin = SMAppService.mainApp.status == .enabled
            launchAtLoginError = "Could not update Login Items: \(error.localizedDescription)"
        }
    }
}
