import SwiftUI

/// The dismissible update banner at the top of the main screen.
///
/// It only appears when the last successful check found a newer release that
/// has not been dismissed for that exact version. "What's new" opens the
/// published notes page; "Install" opens the release's install URL in Safari
/// (the feed currently points at the Ad Hoc install page, which is also where
/// the `itms-services` manifest lives).
struct UpdateBanner: View {
    @EnvironmentObject private var updates: UpdateChecker
    @Environment(\.openURL) private var openURL

    var body: some View {
        if let update = updates.bannerUpdate {
            VStack(alignment: .leading, spacing: Theme.Spacing.sm.rawValue) {
                HStack(spacing: Theme.Spacing.sm.rawValue) {
                    Image(systemName: "arrow.down.circle.fill")
                        .foregroundColor(Theme.Semantic.accent)
                    Text("Update \(update.version) available")
                        .font(.subheadline.weight(.semibold))
                        .foregroundColor(Theme.Semantic.onSurface)
                    Spacer(minLength: 0)
                    Button {
                        updates.dismiss(update)
                    } label: {
                        Image(systemName: "xmark")
                            .foregroundColor(Theme.Semantic.onSurfaceMuted)
                    }
                    .buttonStyle(.borderless)
                    .accessibilityLabel("Dismiss update \(update.version)")
                }

                HStack(spacing: Theme.Spacing.md.rawValue) {
                    Button("What's new") {
                        openURL(update.notesURL)
                    }
                    .buttonStyle(.bordered)

                    Button {
                        openURL(update.installURL)
                    } label: {
                        Text("Install")
                            .frame(minWidth: 72)
                    }
                    .buttonStyle(.borderedProminent)
                    .tint(Theme.Semantic.accent)
                }
            }
            .padding(.vertical, Theme.Spacing.xs.rawValue)
        }
    }
}
