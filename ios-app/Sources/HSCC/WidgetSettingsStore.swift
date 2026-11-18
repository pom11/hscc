import Foundation
import Combine
import WidgetKit

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
    /// The whole registry (every surface). Published so Settings re-renders
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

    /// What one surface will actually render: its own scope when the operator
    /// picked one for it here, otherwise the fleet-wide scope the surface's
    /// configuration intent wrote (t_3dd38411). Same call the widget
    /// extensions make, so the screen can never display a scope the store does
    /// not hold.
    func scope(for kind: WidgetSurfaceKind) -> WidgetScope? {
        settings.scope(for: kind)
    }

    /// The GLOBAL appearance (t_444519ae), or the app Theme default when the
    /// operator never edited one.
    var globalStyle: WidgetStyle {
        settings.style ?? WidgetStyle()
    }

    /// This surface's own override, or nil when it follows the global style —
    /// the "Use global" state the editor's toggle reads.
    func style(for kind: WidgetSurfaceKind) -> WidgetStyle? {
        settings.style(for: kind)
    }

    /// What this surface actually renders: own override → global → default.
    func resolvedStyle(for kind: WidgetSurfaceKind) -> WidgetStyle {
        settings.resolvedStyle(for: kind)
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

    /// Point ONE surface at a board / project / node (t_3dd38411). The screen
    /// hands this the folded selection (exactly one field set) or an explicit
    /// `WidgetScope()` for "All for this widget", and `nil` to drop the
    /// surface's own choice so it inherits the fleet default again. The write
    /// itself is the existing `update` path — same blob, same key, same
    /// persist, so a per-widget scope cannot survive without cadence and
    /// sections surviving with it.
    func setScope(_ scope: WidgetScope?, for kind: WidgetSurfaceKind) {
        update(kind) { $0.scope = scope }
    }

    /// Replace the GLOBAL appearance every surface inherits (t_444519ae).
    /// Re-reads first for the same reason `update` does: a configuration
    /// intent may have written the blob while Settings was open.
    func setGlobalStyle(_ style: WidgetStyle) {
        if let fresh = AppGroupWidgetSettings.load() { settings = fresh }
        settings.style = style.normalized
        persist()
    }

    /// Give ONE surface its own appearance, or pass nil for "Use global" so it
    /// inherits again. The nil/empty distinction is the whole toggle: storing
    /// a copy of the global style would pin it and silently stop inheriting.
    func setStyle(_ style: WidgetStyle?, for kind: WidgetSurfaceKind) {
        update(kind) { $0.style = style?.normalized }
    }

    /// "Use global" in one call, for the editor's toggle.
    func useGlobalStyle(for kind: WidgetSurfaceKind) {
        setStyle(nil, for: kind)
    }

    /// Apply a closure to one surface's settings and persist the whole blob.
    private func update(_ kind: WidgetSurfaceKind, _ mutate: (inout WidgetSurfaceSettings) -> Void) {
        // Re-read the blob before mutating. The widget's configuration intent
        // (`WidgetScope.apply`) writes the FLEET-wide scope from the app
        // process too, and it can do that while this screen is open; without
        // the re-read, this write would persist the store's copy from launch
        // and silently roll that choice back. Surfaces still come from the
        // bounded read path, so nothing else changes.
        if let fresh = AppGroupWidgetSettings.load() { settings = fresh }
        var entry = settings.setting(for: kind)
        mutate(&entry)
        settings.surfaces[kind] = entry
        persist()
    }

    // MARK: - Persistence

    private func persist() {
        guard let data = try? JSONEncoder().encode(settings) else { return }
        Self.suite.set(data, forKey: AppGroupWidgetSettings.storageKey)
        // Every saved change repaints one or more surfaces: ask WidgetKit to
        // rebuild the Home Screen timelines now rather than at the next
        // scheduled refresh (t_444519ae). The Live Activity drivers have no
        // push-only hook — their `refreshOnce()` also starts an activity — so
        // an already-running bubble picks the style up on its next scheduled
        // push instead of having this store launch network work for it.
        WidgetCenter.shared.reloadAllTimelines()
    }
}
