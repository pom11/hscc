import CarPlay
import UIKit

// ---------------------------------------------------------------------------
// CarPlay scene (card W5g) — design C, "Rows with gauges".
//
// ONE root CPListTemplate titled "HSCC" with five read-only rows:
//
//   1. Cluster     — four node gauges as a CPListImageRowItem (0-90 °C arc,
//                    green < 70 / orange 70-74 / red >= 75, degrees inside)
//   2. Boards      — stacked bars for the top three boards
//   3. Running     — the longest elapsed time
//   4. Needs you   — the unacknowledged count, or a green check when clear
//   5. Merge queue — the four-stage rebase/gates/push/merge track
//
// Each row pushes a detail CPListTemplate (nodes / boards / cards / alerts /
// queue rows). NOTHING here writes: there is no ack button and no handler that
// reaches WidgetDataClient's GET-only core with anything but a read.
//
// Data comes from the same `WidgetDataClient` routes and multi-address failover
// the widgets use, through the readers in CarPlayReading.swift. The scene
// refreshes every 30 s while connected and stops on disconnect. A failed read
// keeps the last good rows, dims the copy with "offline · HH:mm", and the Needs
// row can never say "All clear" for a cluster it could not reach.
//
// ENTITLEMENT: Apple has not granted HSCC a CarPlay entitlement. The
// simulator-only entitlement file (`Sources/HSCC.simulator.entitlements`) is
// selected by `CODE_SIGN_ENTITLEMENTS[sdk=iphonesimulator*]`; device and
// release builds keep `Sources/HSCC.entitlements` byte for byte (proved by
// scripts/carplay_check.sh).
// ---------------------------------------------------------------------------

@MainActor
final class CarPlaySceneDelegate: UIResponder, CPTemplateApplicationSceneDelegate {
    private var interfaceController: CPInterfaceController?
    private var rootTemplate: CPListTemplate?
    private var state = CarPlayState()
    private var refreshTimer: Timer?
    private var isLoading = false
    private var scale: CGFloat = 2
    private var traits = UITraitCollection.current

    private var images: CarPlayImageRenderer {
        CarPlayImageRenderer(scale: scale, traits: traits)
    }

    // MARK: - Scene lifecycle

    func templateApplicationScene(_ templateApplicationScene: CPTemplateApplicationScene,
                                  didConnect interfaceController: CPInterfaceController) {
        self.interfaceController = interfaceController
        self.scale = templateApplicationScene.carWindow.screen.scale
        self.traits = interfaceController.carTraitCollection

        let root = makeRootTemplate()
        rootTemplate = root
        interfaceController.setRootTemplate(root, animated: false, completion: nil)

        startRefreshTimer()
        refresh()
    }

    func templateApplicationScene(_ templateApplicationScene: CPTemplateApplicationScene,
                                  didDisconnectInterfaceController interfaceController: CPInterfaceController) {
        stopRefreshTimer()
        rootTemplate = nil
        self.interfaceController = nil
        state = CarPlayState()
        isLoading = false
    }

    /// CarPlay's own day/night switch. Re-render the cached state with the new
    /// appearance; no network read is needed to repaint.
    func contentStyleDidChange(_ contentStyle: UIUserInterfaceStyle) {
        traits = UITraitCollection(userInterfaceStyle: contentStyle)
        applyState()
    }

    // MARK: - Refresh (30 s while connected)

    private func startRefreshTimer() {
        stopRefreshTimer()
        let timer = Timer(timeInterval: CarPlayRefresh.seconds, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.refresh() }
        }
        RunLoop.main.add(timer, forMode: .common)
        refreshTimer = timer
    }

    private func stopRefreshTimer() {
        refreshTimer?.invalidate()
        refreshTimer = nil
    }

    private func refresh() {
        guard interfaceController != nil, !isLoading else { return }
        guard let client = WidgetDataClient.makeIfConfigured() else {
            state.markAllFailed()
            applyState()
            return
        }

        isLoading = true
        Task { @MainActor [weak self] in
            async let clusterRead = client.clusterMonitor()
            async let boardsRead = client.mobileSummary()
            async let runningRead = CardsRunningFeed.read(client: client)
            async let alertsRead = client.alerts()
            async let queueRead = client.mergeQueue()

            let clusterResult = await clusterRead
            let boardsResult = await boardsRead
            let runningResult = await runningRead
            let alertsResult = await alertsRead
            let queueResult = await queueRead

            guard let self else { return }
            self.isLoading = false
            // A disconnected scene must not repaint a stale hierarchy.
            guard self.interfaceController != nil else { return }

            let now = Date()
            self.apply(clusterResult, boards: boardsResult, running: runningResult,
                       alerts: alertsResult, queue: queueResult, at: now)
            self.applyState()
        }
    }

    private func apply(_ clusterResult: Result<MonitorResponse, WidgetDataFailure>,
                       boards boardsResult: Result<MobileSummaryResponse, WidgetDataFailure>,
                       running runningResult: Result<CardsRunningProjection, WidgetDataFailure>,
                       alerts alertsResult: Result<AlertsLite, WidgetDataFailure>,
                       queue queueResult: Result<MergeQueueLite, WidgetDataFailure>,
                       at now: Date) {
        switch clusterResult {
        case .success(let response):
            let reading = CarPlayClusterReading(response: response, fetchedAt: now)
            state.applyCluster(reading, failed: reading == nil, now: now)
        case .failure:
            state.applyCluster(nil, failed: true, now: now)
        }

        switch boardsResult {
        case .success(let summary):
            let reading = CarPlayBoardsReading(summary: summary)
            state.applyBoards(reading, failed: reading == nil, now: now)
        case .failure:
            state.applyBoards(nil, failed: true, now: now)
        }

        switch runningResult {
        case .success(let projection):
            state.applyRunning(CarPlayRunningReading(projection: projection,
                                                     fetchedAt: now),
                               failed: false, now: now)
        case .failure:
            state.applyRunning(nil, failed: true, now: now)
        }

        switch alertsResult {
        case .success(let payload):
            let feed = NeedsMeFeed.from(payload)
            state.applyNeeds(CarPlayNeedsReading(feed: feed, failed: false,
                                                 fetchedAt: now),
                             failed: false, now: now)
        case .failure:
            // Keep the cached rows (if any) but mark them offline. When there
            // is no cache, install an explicitly failed empty feed so the row
            // can never render as a calm "All clear".
            if state.needs == nil {
                state.applyNeeds(CarPlayNeedsReading(feed: .empty, failed: true,
                                                     fetchedAt: now),
                                 failed: true, now: now)
            } else {
                state.applyNeeds(nil, failed: true, now: now)
            }
        }

        switch queueResult {
        case .success(let payload):
            let reading = CarPlayQueueReading(feed: MergeQueueFeed.from(payload))
            // `ok:false` is the route saying it could not produce the queue;
            // that is offline, not an empty queue.
            state.applyQueue(reading, failed: reading.feed.isUnavailable, now: now)
        case .failure:
            state.applyQueue(nil, failed: true, now: now)
        }
    }

    // MARK: - The root template

    private func makeRootTemplate() -> CPListTemplate {
        let section = CPListSection(items: rootItems(), header: state.headerLabel,
                                    sectionIndexTitle: nil)
        return CPListTemplate(title: "HSCC", sections: [section])
    }

    private func sectionHeader() -> String {
        state.headerLabel
    }

    private func clusterItem() -> CPListImageRowItem {
        let renderer = images
        let cluster = CPListImageRowItem(text: state.clusterText,
                                         elements: (state.cluster?.gauges ?? []).map { node in
            CPListImageRowItemRowElement(image: renderer.nodeGauge(node, dimmed: state.clusterDimmed),
                                         title: node.rowTitle,
                                         subtitle: node.rowSubtitle)
        }, allowsMultipleLines: false)
        cluster.listImageRowHandler = { [weak self] _, _, completion in
            self?.openClusterDetail()
            completion()
        }
        cluster.handler = { [weak self] _, completion in
            self?.openClusterDetail()
            completion()
        }
        return cluster
    }

    /// CPListItem's text/detailText/image are read-only, so a refresh rebuilds
    /// the five rows and `updateSections` swaps them in. The root template and
    /// any pushed detail stay where they are.
    private func rootItems() -> [any CPListTemplateItem] {
        let renderer = images
        let boardsItem = CPListItem(text: "Boards", detailText: state.boardsDetail,
                                    image: renderer.boards(state.boards, dimmed: state.boardsDimmed),
                                    accessoryImage: nil,
                                    accessoryType: .disclosureIndicator)
        boardsItem.handler = { [weak self] _, completion in
            self?.openBoardsDetail()
            completion()
        }
        let runningItem = CPListItem(text: "Running", detailText: state.runningDetail,
                                     image: renderer.running(state.running, dimmed: state.runningDimmed),
                                     accessoryImage: nil,
                                     accessoryType: .disclosureIndicator)
        runningItem.handler = { [weak self] _, completion in
            self?.openRunningDetail()
            completion()
        }
        let needsItem = CPListItem(text: "Needs you", detailText: state.needsDetail,
                                   image: renderer.needs(state.needs, dimmed: state.needsDimmed,
                                   failed: state.needsFailed),
                                   accessoryImage: nil,
                                   accessoryType: .disclosureIndicator)
        needsItem.handler = { [weak self] _, completion in
            self?.openNeedsDetail()
            completion()
        }
        let queueItem = CPListItem(text: "Merge queue", detailText: state.queueDetail,
                                   image: renderer.queue(state.queue, dimmed: state.queueDimmed),
                                   accessoryImage: nil,
                                   accessoryType: .disclosureIndicator)
        queueItem.handler = { [weak self] _, completion in
            self?.openQueueDetail()
            completion()
        }
        return [clusterItem(), boardsItem, runningItem, needsItem, queueItem]
    }

    private func applyState() {
        rootTemplate?.updateSections([
            CPListSection(items: rootItems(), header: sectionHeader(),
                          sectionIndexTitle: nil),
        ])
    }

    // MARK: - Detail templates (read-only)

    private func push(_ template: CPListTemplate) {
        interfaceController?.pushTemplate(template, animated: true, completion: nil)
    }

    private func detailList(_ title: String, header: String,
                            items: [CPListItem]) -> CPListTemplate {
        let section = CPListSection(items: items, header: header,
                                    sectionIndexTitle: nil)
        return CPListTemplate(title: title, sections: [section])
    }

    private func emptyRow(_ text: String, detail: String) -> CPListItem {
        CPListItem(text: text, detailText: detail)
    }

    private func openClusterDetail() {
        let renderer = images
        let cluster = state.cluster
        let items: [CPListItem]
        if let cluster, !cluster.nodes.isEmpty {
            items = cluster.nodes.map { node in
                CPListItem(text: node.rowTitle, detailText: node.detailText,
                           image: renderer.nodeGauge(node, size: CGSize(width: 44, height: 44),
                                                   dimmed: state.clusterDimmed))
            }
        } else {
            items = [emptyRow("No nodes reported", detail: "offline · —")]
        }
        let header = cluster.map { "\($0.detailLine) · \($0.modelLine)" } ?? "offline"
        push(detailList("Cluster", header: header, items: items))
    }

    private func openBoardsDetail() {
        let renderer = images
        let boards = state.boards
        let items: [CPListItem]
        if let boards, !boards.boards.isEmpty {
            items = boards.boards.map { board in
                CPListItem(text: board.board, detailText: board.detailLine,
                           image: renderer.boardBar(board, maxOpen: boards.maxOpen,
                                                       dimmed: state.boardsDimmed))
            }
        } else {
            items = [emptyRow("No open cards", detail: state.boardsDetail)]
        }
        push(detailList("Boards", header: state.boardsDetail, items: items))
    }

    private func openRunningDetail() {
        let running = state.running
        let items: [CPListItem]
        if let running, !running.cards.isEmpty {
            items = running.cards.map { card in
                CPListItem(text: card.title, detailText: card.detailLine)
            }
        } else {
            items = [emptyRow("Nothing running", detail: state.runningDetail)]
        }
        let header = running.map { "\($0.imageText) longest · \($0.cards.count) running" }
            ?? "offline"
        push(detailList("Running", header: header, items: items))
    }

    private func openNeedsDetail() {
        let needs = state.needs
        let items: [CPListItem]
        if let needs, !needs.items.isEmpty {
            items = needs.items.map { item in
                CPListItem(text: item.title,
                           detailText: "\(item.ageLabel()) · \(item.level.label)")
            }
        } else {
            items = [emptyRow(state.needsEmptyText, detail: state.needsDetail)]
        }
        let header = needs.map { "\($0.countText) · read-only" } ?? "offline"
        push(detailList("Needs you", header: header, items: items))
    }

    private func openQueueDetail() {
        let queue = state.queue
        let items: [CPListItem]
        if let queue, queue.feed.isOff {
            items = [emptyRow(queue.offLine, detail: "turn it on from hscc on the Mac")]
        } else if let queue, !queue.feed.rows.isEmpty {
            items = queue.feed.rows.map { row in
                let position = row.position.map { "\($0). " } ?? ""
                let detail = [row.project,
                              MergeQueueReading.stageName(row.state),
                              row.ageLabel,
                              MergeQueueReading.resultMark(row.state)]
                    .joined(separator: " · ")
                return CPListItem(text: position + row.prLabel, detailText: detail)
            }
        } else {
            items = [emptyRow("Nothing queued", detail: state.queueDetail)]
        }
        let header = queue.map { $0.detailLine } ?? "offline"
        push(detailList("Merge queue", header: header, items: items))
    }
}
