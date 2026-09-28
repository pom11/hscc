import Foundation
import Combine

/// App-side store for the shared Widgets & Live Activities settings
/// (t_dea36c48) — a published `ObservableObject` the Settings UI reads/writes,
/// backed by the SAME App-Group `AppGroupWidgetSettings` blob every widget /
/// Live Activity extension reads read-only.
///
/// This mirrors `SettingsStore` exactly: the app owns the editable
/// `ObservableObject`, the extensions read the raw shared model straight from
/// the App Group suite (via `AppGroupWidgetSettings.load()`), so there is one
/// source of truth and the two sides cannot drift. Only the app mutates; the
/// extensions are read-only consumers.
final class WidgetSettingsStore: ObservableObject {
    /// The whole registry (all four surfaces). Published so Settings re-renders
    /// as the operator edits any surface.
    @Published private(set) var settings: AppGroupWidgetSettings

    /// The shared suite the extensions read from too. STATIC on purpose — same
    /// reason as `SettingsStore.suite`.
    private static var suite: UserDefaults {
        UserDefaults(suiteName: AppGroup.suiteName) ?? .standard
    }

    init() {
        // Load the saved registry; fall back to sensible defaults (all on,
        // 5 min for widgets / 15 min for Live Activities, all sections) on a
        // fresh install or a corrupt blob. We do NOT persist defaults on init:
        // a surface the operator never touched should keep defaulting ON so a
        // pre-existing widget renders unchanged (backward compatible) — writing
        // here would eagerly materialize entries that then read as \"explicitly
        // configured\".
        self.settings = AppGroupWidgetSettings.load() ?? AppGroupWidgetSettings.defaults()
    }

    // MARK: - Per-surface reads (convenience for the Settings UI)

    /// The current settings for one surface, bounded to its known sections.
    func setting(for kind: WidgetSurfaceKind) -> WidgetSurfaceSettings {
        settings.setting(for: kind)
    }

    // MARK: - Mutation (each persists immediately to the shared suite)

    /// Toggle whether a surface is active/enabled.
    func setActive(_ isActive: Bool, for kind: WidgetSurfaceKind) {
        update(kind) { $0.isActive = isActive }
    }

    /// Set a surface's refresh cadence (minutes). 0 = Off (surface shown but no
    /// scheduled refresh); only negatives are clamped so the store never holds
    /// a value that would schedule an instant refresh loop.
    func setRefreshMinutes(_ minutes: Int, for kind: WidgetSurfaceKind) {
        update(kind) { $0.refreshMinutes = max(0, minutes) }
    }

    /// Toggle one content section on/off for a surface.
    func toggleSection(_ section: String, for kind: WidgetSurfaceKind) {
        update(kind) { entry in
            if entry.showSections.contains(section) {
                entry.showSections.remove(section)
            } else {
                entry.showSections.insert(section)
            }
        }
    }

    /// Apply a closure to one surface's settings and persist the whole blob.
    private func update(_ kind: WidgetSurfaceKind, _ mutate: (inout WidgetSurfaceSettings) -> Void) {
        var entry = settings.setting(for: kind)
        mutate(&entry)
        settings.surfaces[kind] = entry
        persist()
    }

    // MARK: - Persistence

    private func persist() {
        guard let data = try? JSONEncoder().encode(settings) else { return }
        Self.suite.set(data, forKey: AppGroupWidgetSettings.storageKey)
    }
}
