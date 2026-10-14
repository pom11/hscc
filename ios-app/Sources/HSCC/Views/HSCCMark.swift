import SwiftUI

// MARK: - HSCCMark (IOS_REDESIGN 2026-10-14 section 7)
//
// The "Orbit" motif drawn live in SwiftUI so the drawer header and the home
// dock carry the same mark as the app icon: three nodes on a circle, two
// accent and one neutral. Coordinates are unit-square, so it scales with `size`.

struct HSCCMark: View {
    var size: CGFloat = 28

    // Unit-square node centers + whether each is accent-colored, matching the
    // AppIcon: top + right are accent, bottom-left is neutral.
    private static let nodes: [(CGPoint, Bool)] = [
        (CGPoint(x: 0.50, y: 0.20), true),
        (CGPoint(x: 0.74, y: 0.66), true),
        (CGPoint(x: 0.26, y: 0.66), false),
    ]

    var body: some View {
        ZStack {
            Path { p in
                for i in 0..<Self.nodes.count {
                    let a = Self.nodes[i].0
                    let b = Self.nodes[(i + 1) % Self.nodes.count].0
                    p.move(to: CGPoint(x: a.x * size, y: a.y * size))
                    p.addLine(to: CGPoint(x: b.x * size, y: b.y * size))
                }
            }
            .stroke(Theme.Semantic.onSurfaceMuted, style: StrokeStyle(lineWidth: size * 0.07, lineCap: .round))

            ForEach(0..<Self.nodes.count, id: \.self) { i in
                Circle()
                    .fill(Self.nodes[i].1 ? Theme.Semantic.accent : Theme.Semantic.neutral)
                    .frame(width: size * 0.30, height: size * 0.30)
                    .position(x: Self.nodes[i].0.x * size, y: Self.nodes[i].0.y * size)
            }
        }
        .frame(width: size, height: size)
        .accessibilityHidden(true)
    }
}
