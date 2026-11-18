import SwiftUI

// MARK: - HSCCMark
//
// The block-H motif drawn live in SwiftUI, so any in-app brand surface carries
// the same mark as the app icon (operator-selected design, 2026-11-18): a flat
// accent-orange H built from three rectangles on a black field. Coordinates are
// unit-square fractions of the 1024pt icon canvas, so the mark scales with
// `size` and stays geometrically identical to assets-master/*.svg.

struct HSCCMark: View {
    var size: CGFloat = 28

    /// Left bar, right bar and crossbar, in unit-square coordinates. These are
    /// the icon's measured rectangles normalised by 1024: left bar x 254–346,
    /// right bar x 676–768, both y 241–745; crossbar x 346–676, y 447–539.
    private static let bars: [CGRect] = [
        CGRect(x: 0.2480, y: 0.2354, width: 0.0898, height: 0.4922),
        CGRect(x: 0.6602, y: 0.2354, width: 0.0898, height: 0.4922),
        CGRect(x: 0.3379, y: 0.4365, width: 0.3223, height: 0.0898),
    ]

    var body: some View {
        Path { path in
            for bar in Self.bars {
                path.addRect(CGRect(x: bar.minX * size,
                                    y: bar.minY * size,
                                    width: bar.width * size,
                                    height: bar.height * size))
            }
        }
        .fill(Theme.Semantic.accent)
        .frame(width: size, height: size)
        .accessibilityHidden(true)
    }
}
