import CarPlay
import UIKit

// ---------------------------------------------------------------------------
// CarPlay images (card W5g): the five rows' pictures, drawn with
// `UIGraphicsImageRenderer` at the car screen's own scale.
//
// Apple's CarPlay templates are not free-form UI: colour and gauges can only
// travel inside a rendered `UIImage`. Everything here is a function of the
// reading types in `CarPlayReading.swift`, so the picture and the sentence
// beside it can never disagree about a number.
//
// Light and dark: colours are semantic (`UIColor.label`, `.systemGreen`, …)
// resolved against the CarPlay window's trait collection at draw time. The
// scene re-renders the cached state when `contentStyleDidChange` fires, so a
// day/night switch repaints without a network read.
// ---------------------------------------------------------------------------

struct CarPlayImageRenderer {
    /// `templateApplicationScene.carWindow.screen.scale`, not the phone's.
    let scale: CGFloat
    let traits: UITraitCollection

    // MARK: Cluster node gauge

    /// A 0-90 °C arc with the reading in the middle. The element's title and
    /// subtitle carry the node name and the util/tok line; this image is only
    /// the gauge + degrees.
    func nodeGauge(_ node: CarPlayNodeReading,
                   size: CGSize = CGSize(width: 64, height: 64),
                   dimmed: Bool = false) -> UIImage {
        image(size: size, dimmed: dimmed) { context in
            let center = CGPoint(x: size.width / 2, y: size.height / 2)
            let lineWidth = max(4, size.width * 0.09)
            let radius = (min(size.width, size.height) - lineWidth) / 2 - 1
            let start = CGFloat.pi * 0.75
            let sweep = CGFloat.pi * 1.5

            strokeArc(context.cgContext, center: center, radius: radius,
                      start: start, end: start + sweep,
                      color: track, width: lineWidth)

            if node.tempC != nil {
                let fraction = min(max(node.gaugeValue / 90, 0), 1)
                if fraction > 0 {
                    strokeArc(context.cgContext, center: center, radius: radius,
                              start: start, end: start + sweep * CGFloat(fraction),
                              color: color(for: node.tempBand), width: lineWidth)
                }
            }

            drawCentered(node.tempText,
                         in: CGRect(origin: .zero, size: size),
                         font: roundedFont(size: size.width * 0.30, weight: .bold),
                         color: node.tempC == nil ? secondary : ink,
                         yOffset: 0)
        }
    }

    // MARK: Boards (top three stacked bars)

    func boards(_ reading: CarPlayBoardsReading?,
                size: CGSize = CGSize(width: 132, height: 56),
                dimmed: Bool = false) -> UIImage {
        image(size: size, dimmed: dimmed) { context in
            guard let reading, !reading.topBoards.isEmpty else {
                drawCentered("—", in: CGRect(origin: .zero, size: size),
                             font: roundedFont(size: 24, weight: .bold),
                             color: secondary)
                return
            }
            let rows = reading.topBoards
            let barHeight: CGFloat = 10
            let gap = (size.height - barHeight * CGFloat(rows.count))
                / CGFloat(max(rows.count - 1, 1))
            for (index, board) in rows.enumerated() {
                let y = CGFloat(index) * (barHeight + max(gap, 4))
                drawBar(board, maxOpen: reading.maxOpen,
                        rect: CGRect(x: 0, y: y, width: size.width, height: barHeight),
                        context: context.cgContext)
            }
        }
    }

    /// One board's small bar, for the pushed detail list.
    func boardBar(_ board: CarPlayBoardReading, maxOpen: Int,
                  size: CGSize = CGSize(width: 96, height: 22),
                  dimmed: Bool = false) -> UIImage {
        image(size: size, dimmed: dimmed) { context in
            drawBar(board, maxOpen: maxOpen,
                    rect: CGRect(x: 0, y: (size.height - 8) / 2,
                                 width: size.width, height: 8),
                    context: context.cgContext)
        }
    }

    // MARK: Running (longest elapsed)

    func running(_ reading: CarPlayRunningReading?,
                 size: CGSize = CGSize(width: 132, height: 56),
                 dimmed: Bool = false) -> UIImage {
        image(size: size, dimmed: dimmed) { context in
            let text = reading?.imageText ?? "—"
            drawCentered(text, in: CGRect(origin: .zero, size: size),
                         font: roundedFont(size: 28, weight: .bold),
                         color: ink)
        }
    }

    // MARK: Needs you (count / all clear)

    func needs(_ reading: CarPlayNeedsReading?,
               size: CGSize = CGSize(width: 132, height: 56),
               dimmed: Bool = false, failed: Bool = false) -> UIImage {
        image(size: size, dimmed: dimmed) { _ in
            // The STATE's latest failure wins: cached calm data must never
            // draw a green check after a failed fetch.
            guard let reading, !reading.failed, !failed else {
                drawCentered("—", in: CGRect(origin: .zero, size: size),
                             font: roundedFont(size: 24, weight: .bold),
                             color: secondary)
                return
            }
            if reading.isClear {
                drawCentered("✓", in: CGRect(x: 0, y: 0, width: size.width, height: size.height * 0.62),
                             font: roundedFont(size: 28, weight: .bold),
                             color: state(.ok))
                drawCentered("All clear",
                             in: CGRect(x: 0, y: size.height * 0.52,
                                        width: size.width, height: size.height * 0.42),
                             font: .systemFont(ofSize: 12, weight: .semibold),
                             color: secondary)
            } else {
                drawCentered("\(reading.count)",
                             in: CGRect(origin: .zero, size: size),
                             font: roundedFont(size: 30, weight: .bold),
                             color: state(.bad))
            }
        }
    }

    // MARK: Merge queue (four-stage track)

    func queue(_ reading: CarPlayQueueReading?,
               size: CGSize = CGSize(width: 132, height: 30),
               dimmed: Bool = false) -> UIImage {
        image(size: size, dimmed: dimmed) { context in
            let labels = reading?.stageLabels ?? ["rebase", "gates", "push", "merge"]
            let progress = reading?.stageProgress ?? 0
            let gap: CGFloat = 6
            let width = (size.width - gap * CGFloat(labels.count - 1))
                / CGFloat(labels.count)
            let height = min(size.height, 12)
            for index in 0..<labels.count {
                let rect = CGRect(x: CGFloat(index) * (width + gap),
                                  y: (size.height - height) / 2,
                                  width: width, height: height)
                let colour: UIColor
                if index < progress {
                    colour = state(.ok)
                } else if index == progress, progress < labels.count {
                    colour = accent
                } else {
                    colour = track
                }
                context.cgContext.setFillColor(colour.cgColor)
                context.cgContext.addPath(UIBezierPath(roundedRect: rect,
                                                       cornerRadius: height / 2).cgPath)
                context.cgContext.fillPath()
            }
        }
    }

    // MARK: - Primitives

    private enum StateColour { case ok, warn, bad }

    private var ink: UIColor { UIColor.label.resolvedColor(with: traits) }
    private var secondary: UIColor { UIColor.secondaryLabel.resolvedColor(with: traits) }
    private var accent: UIColor { UIColor.systemBlue.resolvedColor(with: traits) }
    private var track: UIColor {
        UIColor.quaternarySystemFill.resolvedColor(with: traits)
    }

    private func state(_ colour: StateColour) -> UIColor {
        switch colour {
        case .ok: return UIColor.systemGreen.resolvedColor(with: traits)
        case .warn: return UIColor.systemOrange.resolvedColor(with: traits)
        case .bad: return UIColor.systemRed.resolvedColor(with: traits)
        }
    }

    private func color(for band: CarPlayTempBand) -> UIColor {
        switch band {
        case .ok: return state(.ok)
        case .warn: return state(.warn)
        case .bad: return state(.bad)
        case .unknown: return secondary
        }
    }

    private func image(size: CGSize, dimmed: Bool = false,
                       _ draw: (UIGraphicsImageRendererContext) -> Void) -> UIImage {
        let format = UIGraphicsImageRendererFormat.default()
        format.scale = max(scale, 1)
        format.opaque = false
        return UIGraphicsImageRenderer(size: size, format: format).image { context in
            // The Native kit's 50% dim for stale/offline content.
            if dimmed { context.cgContext.setAlpha(0.5) }
            draw(context)
        }
    }

    private func drawBar(_ board: CarPlayBoardReading, maxOpen: Int,
                         rect: CGRect, context: CGContext) {
        let radius = rect.height / 2
        let width = max(4, rect.width * CGFloat(board.openCards) / CGFloat(max(maxOpen, 1)))
        let filled = CGRect(x: rect.minX, y: rect.minY, width: width, height: rect.height)
        let path = UIBezierPath(roundedRect: filled, cornerRadius: radius)
        context.saveGState()
        context.addPath(path.cgPath)
        context.clip()
        context.setFillColor(track.cgColor)
        context.fill(filled)

        var x = filled.minX
        for segment in board.segments where segment.count > 0 {
            let segmentWidth = filled.width * CGFloat(segment.count)
                / CGFloat(max(board.openCards, 1))
            let colour: UIColor
            switch segment.kind {
            case .running: colour = accent
            case .todo: colour = secondary
            case .blocked: colour = state(.warn)
            }
            context.setFillColor(colour.cgColor)
            context.fill(CGRect(x: x, y: filled.minY,
                                width: segmentWidth, height: filled.height))
            x += segmentWidth
        }
        context.restoreGState()
    }

    private func strokeArc(_ context: CGContext, center: CGPoint, radius: CGFloat,
                           start: CGFloat, end: CGFloat, color: UIColor, width: CGFloat) {
        context.setStrokeColor(color.cgColor)
        context.setLineWidth(width)
        context.setLineCap(.round)
        context.addArc(center: center, radius: radius,
                       startAngle: start, endAngle: end, clockwise: false)
        context.strokePath()
    }

    private func drawCentered(_ text: String, in rect: CGRect, font: UIFont,
                              color: UIColor, yOffset: CGFloat = 0) {
        let attributes: [NSAttributedString.Key: Any] = [
            .font: font,
            .foregroundColor: color,
        ]
        let string = text as NSString
        let size = string.size(withAttributes: attributes)
        string.draw(at: CGPoint(x: rect.midX - size.width / 2,
                                y: rect.midY - size.height / 2 + yOffset),
                    withAttributes: attributes)
    }

    private func roundedFont(size: CGFloat, weight: UIFont.Weight) -> UIFont {
        let base = UIFont.systemFont(ofSize: size, weight: weight)
        guard let descriptor = base.fontDescriptor.withDesign(.rounded) else { return base }
        return UIFont(descriptor: descriptor, size: size)
    }
}
