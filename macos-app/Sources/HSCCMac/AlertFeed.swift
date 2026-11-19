import Foundation

// ---------------------------------------------------------------------------
// AlertFeed.swift — the alert feed's display projection and poll policy.
//
// Card N4: the menu bar app polls `GET /v1/alerts?since=<id>` every 5 s and
// announces each new row. Everything here is Foundation-only so the executed
// gate (`scripts/alert_feed_check.sh`) can compile and run THIS file, the way
// `cli_dropdown_check.sh` runs the dropdown's reading layer. The view draws
// `AlertFeedRow`; the monitor owns the network and the notifications.
//
// The rules this file owns:
//   * a first launch is a BASELINE: show the newest rows, notify nothing, and
//     start the cursor at the newest id — reopening the app must not replay
//     yesterday's alerts as banners;
//   * every subsequent poll advances `since`, so each alert is delivered once
//     and a feed clear (latest id below our cursor) resets the baseline instead
//     of silently going quiet forever;
//   * the dropdown keeps only the newest rows; a burst is capped to a few
//     banners plus one "more alerts" line, because notification spam is an
//     explicit anti-goal of the design.
// ---------------------------------------------------------------------------

/// The alert levels the feed defines. Unknown values degrade to `.info`, never
/// to a crash or an invented fourth level.
enum AlertFeedLevel: String, Codable, CaseIterable {
    case info
    case action
    case critical

    static func parse(_ raw: String?) -> AlertFeedLevel {
        guard let raw, let level = AlertFeedLevel(rawValue: raw.lowercased()) else {
            return .info
        }
        return level
    }

    var label: String {
        switch self {
        case .info: return "Info"
        case .action: return "Action"
        case .critical: return "Critical"
        }
    }

    /// The SF Symbol the dropdown draws. The view maps the level to the one
    /// Theme colour; this file never imports SwiftUI.
    var symbolName: String {
        switch self {
        case .info: return "bell"
        case .action: return "bell.badge"
        case .critical: return "exclamationmark.triangle.fill"
        }
    }
}

/// What the dropdown persists and draws. Deliberately the PROJECTION (id,
/// title, level, timestamp), never the wire body: the notification needs the
/// full alert once, and storing every body in the App Group would keep prose
/// the menu bar never renders.
struct AlertFeedRow: Identifiable, Codable, Equatable {
    let id: Int
    let title: String
    let level: String
    let ts: String?

    init(alert: AlertFeedAlert) {
        id = alert.id
        title = alert.title
        level = alert.level ?? AlertFeedLevel.info.rawValue
        ts = alert.ts
    }

    var levelKind: AlertFeedLevel { AlertFeedLevel.parse(level) }

    /// Never a blank line: a title is required by the route, but a corrupt row
    /// must not render as an invisible item in the list.
    var titleText: String {
        let trimmed = title.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? "(untitled alert)" : trimmed
    }

    /// The relative age the dropdown shows beside the level. An unparseable
    /// timestamp renders nothing rather than "now" — the one thing worse than
    /// no age is a wrong one.
    func ageText(now: Date = Date()) -> String {
        guard let date = AlertFeedTime.parse(ts) else { return "" }
        let formatter = RelativeDateTimeFormatter()
        formatter.unitsStyle = .abbreviated
        return formatter.localizedString(for: date, relativeTo: now)
    }
}

enum AlertFeedTime {
    static func parse(_ iso: String?) -> Date? {
        guard let iso, !iso.isEmpty else { return nil }
        let plain = ISO8601DateFormatter()
        plain.formatOptions = [.withInternetDateTime]
        if let date = plain.date(from: iso) { return date }
        // The store writes second-resolution `...Z`; the fractional variant is
        // defensive for a producer that ever adds sub-second precision.
        let fractional = ISO8601DateFormatter()
        fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return fractional.date(from: iso)
    }
}

/// The cursor + last rows for ONE cluster, persisted in the App Group. `knownID`
/// is the exclusive `since` cursor (nil until the first baseline); `viewedID` is
/// the newest id the operator has opened the dropdown for; `unseen` is the
/// badge the menu bar and the dropdown draw.
struct AlertFeedState: Codable, Equatable {
    var rows: [AlertFeedRow] = []
    var knownID: Int?
    var viewedID: Int?
    var unseen: Int = 0
}

/// What one poll should do with the page it just read.
struct AlertFeedAdvance: Equatable {
    let state: AlertFeedState
    /// Ascending, at most `maxBanners`, so the newest banner is posted last and
    /// therefore appears on top.
    let notify: [AlertFeedAlert]
    /// Older alerts in the same poll that are represented by one summary line
    /// instead of their own banners.
    let suppressed: Int
}

enum AlertFeedPolicy {
    /// How many rows the dropdown keeps. The feed itself holds 500; this is a
    /// menu, not a mailbox.
    static let displayLimit = 6
    /// A burst larger than this is coalesced: one banner per alert while
    /// running is the normal case, and a backlog after a restart must not
    /// become a wall of notifications.
    static let maxBannersPerPoll = 5

    /// A first launch (or a cluster with no stored cursor). Shows the newest
    /// rows and starts the cursor at the store's newest id, NOTIFYING NOTHING:
    /// history is not news.
    static func baseline(page: AlertFeedPage,
                         displayLimit: Int = displayLimit) -> AlertFeedState {
        let rows = page.alerts
            .sorted { $0.id > $1.id }
            .prefix(displayLimit)
            .map(AlertFeedRow.init)
        // 0 is a real cursor for an empty feed: ids start at 1, so the first
        // alert that ever arrives is still "since 0" and gets a banner. nil
        // would make every later poll a baseline and swallow it.
        let newest = page.latestId ?? page.alerts.map(\.id).max() ?? 0
        return AlertFeedState(rows: Array(rows),
                              knownID: newest,
                              viewedID: newest,
                              unseen: 0)
    }

    /// True when the store's newest id is BELOW our cursor: the feed was
    /// cleared (or a different store replaced it), so `since` would return
    /// nothing forever. The caller re-baselines with a no-`since` read.
    static func isFeedReset(page: AlertFeedPage, knownID: Int?) -> Bool {
        guard let knownID, knownID > 0 else { return false }
        return (page.latestId ?? 0) < knownID
    }

    /// A normal poll page, fetched with `since == state.knownID`. Only ids above
    /// the cursor count as new (the server already guarantees it; the filter is
    /// the client's own guard against a stale/duplicated page).
    static func advance(page: AlertFeedPage,
                        from state: AlertFeedState,
                        displayLimit: Int = displayLimit,
                        maxBanners: Int = maxBannersPerPoll) -> AlertFeedAdvance {
        guard let known = state.knownID else {
            return AlertFeedAdvance(state: baseline(page: page, displayLimit: displayLimit),
                                    notify: [],
                                    suppressed: 0)
        }

        let fresh = page.alerts
            .filter { $0.id > known }
            .sorted { $0.id < $1.id }

        var seen = Set<Int>()
        var rows: [AlertFeedRow] = []
        for alert in fresh.reversed() where seen.insert(alert.id).inserted {
            rows.append(AlertFeedRow(alert: alert))
        }
        for row in state.rows where seen.insert(row.id).inserted {
            rows.append(row)
        }

        // next_since, never latest_id, advances the cursor: on a full page
        // latest_id can name alerts the page did not include, and using it
        // would silently skip them. With no cursor in the answer, stay put
        // (the page was empty) or fall back to the store's newest id.
        let nextKnown: Int
        if let next = page.nextSince {
            nextKnown = max(known, next)
        } else {
            nextKnown = max(known, page.latestId ?? known)
        }
        let notify = Array(fresh.suffix(max(0, maxBanners)))
        return AlertFeedAdvance(
            state: AlertFeedState(rows: Array(rows.prefix(displayLimit)),
                                  knownID: nextKnown,
                                  viewedID: state.viewedID,
                                  unseen: state.unseen + fresh.count),
            notify: notify,
            suppressed: max(0, fresh.count - notify.count)
        )
    }
}
