import Foundation

/// Needs-me alert-feed decode + projection checks (card R3).
func checkNeedsMeWidget(_ c: DecodeCheck) {
    // ---- The "Needs me" alert feed (card R3) ----
    //
    // `v1_alerts.json` is the route's own shape with placeholder text: the live feed
    // is operator-private, so the fixture carries the shape and no real alert. The
    // assertions are the surface's contract: newest first like `hscc notify list`,
    // acknowledged rows excluded, the CLI's age vocabulary, and only the first body
    // line on a row.
    c.check(AlertsLite.self, "v1_alerts.json", "AlertsLite (GET /v1/alerts)")
    c.assertions("v1_alerts.json", "the Needs me projection keeps unacknowledged rows newest first") {
        let payload = try JSONDecoder().decode(AlertsLite.self,
                                               from: try c.load("v1_alerts.json"))
        let feed = NeedsMeFeed.from(payload)
        c.expect("v1_alerts.json", "4 alerts decode, 3 unacknowledged rows remain",
                 payload.alerts.count == 4 && feed.count == 3,
                 "alerts=\(payload.alerts.count) items=\(feed.count)")
        c.expect("v1_alerts.json", "newest first: ids 44, 43, 41",
                 feed.items.map(\.id) == [44, 43, 41],
                 "ids=\(feed.items.map(\.id))")
        c.expect("v1_alerts.json", "the acknowledged row is excluded",
                 !feed.items.contains { $0.id == 42 },
                 "acked id 42 reached the surface")
        let now = ISO8601DateFormatter().date(from: "2026-11-19T12:30:00Z")!
        c.expect("v1_alerts.json", "ages use the CLI vocabulary (2m / 30m / 2h)",
                 feed.items[0].ageLabel(now: now) == "2m"
                    && feed.items[1].ageLabel(now: now) == "30m"
                    && feed.items[2].ageLabel(now: now) == "2h",
                 "ages=\(feed.items.map { $0.ageLabel(now: now) })")
        c.expect("v1_alerts.json", "levels normalise to action / info / critical",
                 feed.items.map(\.level) == [.action, .info, .critical],
                 "levels=\(feed.items.map(\.level))")
        c.expect("v1_alerts.json", "a row carries only the first body line",
                 feed.items[2].bodyFirstLine == "Example body line one."
                    && !feed.items[2].bodyFirstLine.contains("\n"),
                 "body line=\(feed.items[2].bodyFirstLine)")
        c.expect("v1_alerts.json", "the inline copy is N need you / nothing needs you",
                 NeedsMeReading.inlineLine(count: 3) == "3 need you"
                    && NeedsMeReading.inlineLine(count: 0) == "nothing needs you"
                    && NeedsMeReading.nothingNeedsYou == "Nothing needs you",
                 "inline=\(NeedsMeReading.inlineLine(count: 3))")
    }
}
