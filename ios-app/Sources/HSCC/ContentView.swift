import SwiftUI

// MARK: - Preserved root-view vocabulary (IOS_REDESIGN 2026-10-14 Phase 2)
//
// The three-tab TabView root was REPLACED by `AppShell` (a drawer over one
// app-wide NavigationStack) and HSCCApp now roots AppShell. Everything the old
// root owned — approvals badge poller, offline-queue drop banner, deep-link
// handling, Live Activity sweeps, client seeding — moved there verbatim.
//
// What stays here is deliberately small:
//
//   * `ContentView.Tab` — the deep-link router's PUBLIC tab vocabulary
//     (DeepLink.swift:143,156 and the deeplink_check harness compile against
//     it). AppShell maps each tab onto a drawer destination
//     (`AppDestination(tab:)`), so Siri/notification/Handoff targets survive
//     the drawer cutover unchanged.
//   * `ConnectionBanner` — the most-recent-API-outcome banner. Spec §6 retires
//     it in favour of `HSConnectionDot` per screen; the cutover keeps it
//     rendering exactly where it rendered before so no screen loses its honest
//     "can't reach the cluster" / "App Group broken" signal in this step.
//     Retiring the remaining sites is the screens-phase work, tracked there.

/// The legacy root tabs — still the router's public vocabulary, mapped by
/// AppShell onto drawer destinations.
struct ContentView {
    enum Tab: Hashable {
        case projects, cluster, settings
    }
}

/// Connection status banner — driven by the shared `ConnectionMonitor`, which
/// `HSCCClient` updates on EVERY completed real request (not a one-shot launch
/// probe). It reflects the MOST RECENT API OUTCOME, so a screenful of
/// freshly-loaded data can never sit under a stale "\"Can't reach the cluster\""
/// alarm (t_4889e978).
///
/// Placement: rendered as the FIRST element inside a screen's content, BELOW
/// the nav bar. It must never be attached with `.safeAreaInset(edge: .top)` on
/// the stack root, which draws over the nav bar and toolbar items.
struct ConnectionBanner: View {
    @EnvironmentObject private var settings: SettingsStore
    @ObservedObject private var monitor = ConnectionMonitor.shared

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 8) {
                Image(systemName: bannerIcon)
                    .foregroundColor(bannerColor)
                Text(bannerText)
                    .font(.footnote)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            if settings.appGroupUnavailable {
                Label(
                    "Sharing broken: the widget & Siri can't see this. Reinstall the app to fix the App Group.",
                    systemImage: "exclamationmark.triangle.fill"
                )
                .font(.caption2)
                .foregroundColor(Theme.Semantic.bad)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .padding(.horizontal)
        .padding(.vertical, 8)
        .background(Theme.Semantic.tintOverlay(bannerColor))
    }

    private var bannerText: String {
        // A broken App Group is more important than reachability: the app
        // would report "Connected" while the widget/intents read nothing.
        if settings.appGroupUnavailable {
            return settings.isConfigured
                ? "App Group unavailable — check the install."
                : "Set host, port, and token in Settings to connect."
        }
        if !settings.isConfigured {
            return "Set host, port, and token in Settings to connect."
        }
        // The MOST RECENT REAL API OUTCOME. `.unknown` = not yet known (neutral);
        // `.reachable` = a real request reached the API (clears the alarm);
        // `.unreachable` = a transport failure (the only state that alarms red).
        switch monitor.status {
        case .unknown:
            return "Configured — waiting for the first request…"
        case .reachable:
            return "Connected to \(settings.host)."
        case .unreachable(let message):
            return message
        }
    }

    private var bannerIcon: String {
        if settings.appGroupUnavailable {
            return "exclamationmark.triangle.fill"
        }
        switch monitor.status {
        case .reachable: return "checkmark.circle.fill"
        case .unreachable: return "exclamationmark.triangle.fill"
        case .unknown: return "circle.dotted"
        }
    }

    private var bannerColor: Color {
        if settings.appGroupUnavailable {
            return Theme.Semantic.bad
        }
        switch monitor.status {
        case .reachable: return Theme.Semantic.ok
        case .unreachable: return Theme.Semantic.bad
        case .unknown: return Theme.Semantic.neutral
        }
    }
}
