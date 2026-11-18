import Foundation
import SwiftUI

#if canImport(Sparkle)
import Sparkle
#endif

/// Sparkle 2 wiring for the menu bar app (t_2a1e5913).
///
/// `SPUStandardUpdaterController` owns the whole update lifecycle: the appcast
/// feed (`SUFeedURL`), EdDSA verification (`SUPublicEDKey`), the standard
/// update window, downloading, replacing the app bundle and relaunching it.
/// The desktop widget lives INSIDE that bundle (`Contents/PlugIns/
/// HSCCMacWidgets.appex`), so Sparkle updates it with the app — the extension
/// never updates itself.
///
/// The controller is started after the interval is set, so a fresh install
/// checks daily. Sparkle persists the two user choices itself; the published
/// properties below mirror them for the Settings toggles and the menu item.
///
/// `#if canImport(Sparkle)` keeps `macos-app/scripts/build_check.sh` working
/// with plain `swiftc` (it does not resolve SwiftPM). An Xcode build links the
/// pinned Sparkle package and compiles the real branch; the fallback is only
/// for the package-less compile gate, and it can never start an update.
final class SparkleUpdater: NSObject, ObservableObject {
    static let dailyCheckInterval: TimeInterval = 24 * 60 * 60

    @Published private(set) var availableVersion: String?
    @Published private(set) var lastCheckResult: String?
    @Published private(set) var isChecking = false
    @Published private(set) var automaticallyChecksForUpdates = true
    @Published private(set) var automaticallyDownloadsUpdates = false
    @Published private(set) var canCheckForUpdates = false

#if canImport(Sparkle)
    private var controller: SPUStandardUpdaterController?
#endif

    override init() {
        super.init()

#if canImport(Sparkle)
        // A dev build has an empty public key. Starting Sparkle against an
        // unverifiable feed would only raise a misconfiguration alert, so keep
        // the updater off until the release path fills SUPublicEDKey.
        let publicKey = (Bundle.main.object(forInfoDictionaryKey: "SUPublicEDKey") as? String ?? "")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard !publicKey.isEmpty else {
            automaticallyChecksForUpdates = false
            lastCheckResult = "Updates are not configured in this build."
            return
        }

        let controller = SPUStandardUpdaterController(startingUpdater: false,
                                                      updaterDelegate: self,
                                                      userDriverDelegate: nil)
        controller.updater.updateCheckInterval = Self.dailyCheckInterval
        self.controller = controller
        controller.startUpdater()

        automaticallyChecksForUpdates = controller.updater.automaticallyChecksForUpdates
        automaticallyDownloadsUpdates = controller.updater.automaticallyDownloadsUpdates
        canCheckForUpdates = controller.updater.canCheckForUpdates
#endif
    }

    /// Menu item / Settings button: Sparkle's standard check UI. It reports
    /// "up to date" or offers the update for download and install.
    func checkForUpdates() {
#if canImport(Sparkle)
        guard let controller else {
            lastCheckResult = "Updates are not configured in this build."
            return
        }
        guard controller.updater.canCheckForUpdates else { return }
        isChecking = true
        lastCheckResult = "Checking…"
        controller.checkForUpdates(nil)
#else
        lastCheckResult = "Updates are not available in this compile-only build."
#endif
    }

    func setAutomaticallyChecksForUpdates(_ enabled: Bool) {
#if canImport(Sparkle)
        controller?.updater.automaticallyChecksForUpdates = enabled
        automaticallyChecksForUpdates = controller?.updater.automaticallyChecksForUpdates ?? enabled
        automaticallyDownloadsUpdates = controller?.updater.automaticallyDownloadsUpdates ?? false
        canCheckForUpdates = controller?.updater.canCheckForUpdates ?? false
#else
        automaticallyChecksForUpdates = enabled
#endif
    }

    func setAutomaticallyDownloadsUpdates(_ enabled: Bool) {
#if canImport(Sparkle)
        controller?.updater.automaticallyDownloadsUpdates = enabled
        automaticallyDownloadsUpdates = controller?.updater.automaticallyDownloadsUpdates ?? enabled
#else
        automaticallyDownloadsUpdates = enabled
#endif
    }

    /// Sparkle delegate callbacks arrive on the main thread in practice; the
    /// hop is here so a published-value write is always main-thread safe.
    private func onMain(_ block: @escaping () -> Void) {
        if Thread.isMainThread {
            block()
        } else {
            DispatchQueue.main.async(execute: block)
        }
    }
}

#if canImport(Sparkle)
extension SparkleUpdater: SPUUpdaterDelegate {
    func updater(_ updater: SPUUpdater, didFindValidUpdate item: SUAppcastItem) {
        let version = item.displayVersionString
        onMain { [weak self] in
            guard let self else { return }
            self.availableVersion = version
            self.lastCheckResult = "Update \(version) available."
            self.isChecking = false
            self.canCheckForUpdates = updater.canCheckForUpdates
        }
    }

    func updaterDidNotFindUpdate(_ updater: SPUUpdater, error: Error) {
        onMain { [weak self] in
            guard let self else { return }
            self.availableVersion = nil
            self.lastCheckResult = "Up to date."
            self.isChecking = false
            self.canCheckForUpdates = updater.canCheckForUpdates
        }
    }

    func updater(_ updater: SPUUpdater, didAbortWithError error: Error) {
        onMain { [weak self] in
            guard let self else { return }
            self.lastCheckResult = "Couldn't check for updates."
            self.isChecking = false
            self.canCheckForUpdates = updater.canCheckForUpdates
        }
    }

    func updater(_ updater: SPUUpdater, willInstallUpdate item: SUAppcastItem) {
        onMain { [weak self] in
            self?.lastCheckResult = "Installing \(item.displayVersionString)…"
        }
    }

    func updater(_ updater: SPUUpdater,
                 didFinishUpdateCycleFor updateCheck: SPUUpdateCheck,
                 error: Error?) {
        onMain { [weak self] in
            guard let self else { return }
            self.isChecking = false
            self.canCheckForUpdates = updater.canCheckForUpdates
        }
    }
}
#endif
