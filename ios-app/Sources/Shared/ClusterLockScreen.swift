import Foundation

// ---------------------------------------------------------------------------
// The Lock Screen numbers — four of them, and one projection that owns all four.
//
// A Lock Screen family has room for roughly one line, so the only question it
// can answer is the one an operator actually asks when they glance at it:
// IS THE CLUSTER HOT, AND IS IT WORKING. The four metrics that answer it:
//
//   * the HOTTEST temperature anywhere in the fleet (and whose, and which
//     sensor — "87 C" with no owner is a rumour),
//   * how busy the GPUs are,
//   * what the fleet is drawing in watts,
//   * how much work is in the engines (running / waiting).
//
// Everything here is projected from `ClusterMonitorViewModel` — the same
// view-model `hscc cluster monitor`'s tables are built from — so a Lock Screen
// number and the CLI's cell for the same host can never disagree: the
// temperature a family shows IS the CLI's `CPU Temp` / `GPU Temp` cell string
// for that host, taken from `CLIMonitorHostRow` and never re-rounded.
//
// TWO REDUCTIONS, AND WHY THEY ARE DIFFERENT.
//
//   * GPU utilisation is a MEAN over the hosts that reported one. A SUM of
//     percentages is not a percentage: four hosts at 96 % do not make 384 %,
//     and printing 384 would be the fabricated number this card's brief
//     forbids. The mean is the app's own aggregate — the CLI prints one GPU %
//     per host and never a fleet figure — so every surface that draws it says
//     `avg`, and `ClusterMonitorViewModel`'s header says the same about the
//     thresholds.
//   * Power IS a sum. Watts add: the fleet's draw is the sum of its hosts'
//     draws, and the CLI's per-host `GPU Power` cell is a term of that sum,
//     not the total.
//
// Both carry `hostsReporting` / `hostsListed`, and the copy rules print `3/7`
// when they differ: a total over three of seven listed hosts is not the
// fleet's total, and a partial sum of a quantity that only grows is the worst
// silent understatement in the app (§6 rule 1).
//
// NOTHING IS INVENTED WHEN THE ROUTE DID NOT SAY. A metric nobody reported
// renders a dash that KEEPS its unit (`— W`, `— C`, `GPU —`) so the line never
// loses which metric is missing. Running/waiting are nil — never 0 — when the
// serving poll never answered, and also when it answered with no backends at
// all, because `serving_table` prints `no backends behind the proxy` for that
// case rather than a `total (0)` row: there is no queue to count.
//
// A PARKED MACHINE IS NOT A DOWN BACKEND. `serving_table` consults the pause
// overlay ONLY inside its error branch, so a parked box whose engine stopped
// answering is a deliberate stop: it leaves `backendsDown` and gets its own
// `N backends paused` clause. That is the same reduction t_3c0b54b6 made for the
// node tally (`FleetVerdict`), applied to the one surface that prints the
// sentence before the operator has opened the app.
//
// Foundation-only, like the view-model it reads: no SwiftUI, no WidgetKit, no
// `Color`, and no `WidgetFamily` (the macOS SDK marks the accessory cases
// unavailable, so a type naming them cannot be compiled by the executed gate).
// Lock Screen colour is deliberately absent: `widget_style_check.sh` rule 4
// requires accessories to stay system-tinted, so the thermal verdict travels
// in `CLILockScreenCopy.voice`, where a VoiceOver user actually reads it.
// ---------------------------------------------------------------------------

/// One number reduced over the hosts that reported it.
struct CLIFleetAggregate: Equatable {
    /// `mean` for a percentage (see the header: percentages do not add), `sum`
    /// for a wattage (power does).
    enum Rule: String { case mean, sum }

    let value: Double
    let rule: Rule
    /// The hosts this number is made of.
    let hostsReporting: Int
    /// Every host the route listed, errored ones included — the denominator a
    /// reader needs to know whether the number covers the fleet.
    let hostsListed: Int

    /// nil when NO host reported the metric: "measured, and it was zero" is a
    /// different sentence from "nobody told us", and only the first has a
    /// number. A host that reported a literal `0` counts as reporting.
    init?(values: [Double?], rule: Rule) {
        let reported = values.compactMap { $0 }
        guard !reported.isEmpty else { return nil }
        self.rule = rule
        self.hostsReporting = reported.count
        self.hostsListed = values.count
        let total = reported.reduce(0, +)
        self.value = rule == .mean ? total / Double(reported.count) : total
    }

    /// A listed host had no reading, so this is a reduction over part of the
    /// fleet. Every copy rule must say so rather than let it read as the
    /// fleet's own number.
    var isPartial: Bool { hostsReporting < hostsListed }
}

/// The four Lock Screen metrics for one monitor payload.
struct CLILockScreenMetrics: Equatable {
    enum Sensor: String { case cpu, gpu }

    /// The single hottest reading in the fleet, and whose it is. `cell` is the
    /// CLI's own formatted cell for that column, which is why a temperature can
    /// carry one decimal (`CPU Temp` is `{:.1f} C`) or none (`GPU Temp` is
    /// `{:.0f} C`): that asymmetry is the CLI's and the parity goldens hold it.
    struct Hottest: Equatable {
        let celsius: Double
        let sensor: Sensor
        /// The host's display label, never the wire address (§7).
        let host: String
        let cell: String

        /// The view-model's own thermal band (`ClusterMetricLevel`'s header
        /// says whose thresholds these are — the CLI colours no temperature).
        var level: ClusterMetricLevel { .level(celsius, warn: 80, critical: 90) }
    }

    /// Hosts the route listed (the CLI's `N host(s)`).
    let hostCount: Int
    let hottest: Hottest?
    /// Mean GPU % over the hosts that reported one.
    let gpuUtil: CLIFleetAggregate?
    /// Summed GPU power over the hosts that reported one.
    let gpuPower: CLIFleetAggregate?
    /// nil when the serving poll never answered, or answered with no backends.
    let running: Int?
    let waiting: Int?
    /// Live backends the totals are made of (the `n` in the CLI's `total (n)`).
    let backendsReporting: Int
    /// Down backends on a machine NOBODY parked: the only ones this sentence may
    /// call `down`.
    ///
    /// `CLIMonitorServingRow.isDown` stays `row.error != nil` — the overlay keeps
    /// a parked row's error on purpose, and the committed CLI parity golden pins
    /// its `down` byte — so the subtraction happens HERE, the one place the lock
    /// screen's sentence is decided. It is the same reduction t_3c0b54b6 made for
    /// the node tally (`FleetVerdict`): a machine the operator parked with
    /// `hscc node pause` is a choice, not a fault, and this is the surface that
    /// shows the sentence first thing in the morning.
    let backendsDown: Int
    /// Down backends on a machine the operator parked. Its own clause, never
    /// folded into `backendsDown` (that is the alarm again) and never folded into
    /// the queue (a stopped engine is not "nothing to do"). The word is
    /// `FleetFormat.pausedWord` — the one owner of the verdict word, the same one
    /// the node rows draw — so the lock screen invents no second phrase.
    let backendsPaused: Int
    /// Whether the serving section exists at all (§6 rule 1).
    let servingReported: Bool

    /// True when every metric is missing: there is nothing for a Lock Screen
    /// family to draw, and the caller falls back to its failure/last-known copy
    /// instead of rendering a row of dashes.
    var hasNothing: Bool {
        hottest == nil && gpuUtil == nil && gpuPower == nil && running == nil
    }

    init(model: ClusterMonitorViewModel, servingReported: Bool) {
        let rows = model.hostRows
        hostCount = model.hostCount
        self.servingReported = servingReported

        // Route order, GPU before CPU within a host: on a tie the GPU wins (it
        // is the part an operator means by "the cluster is hot") and the
        // earlier host wins, so the answer never depends on a dictionary.
        var hot: Hottest?
        for row in rows {
            for (sensor, value, cell) in [(Sensor.gpu, row.gpuTempC, row.gpuTemp),
                                          (Sensor.cpu, row.cpuTempC, row.cpuTemp)] {
                guard let value else { continue }
                if let best = hot, value <= best.celsius { continue }
                hot = Hottest(celsius: value, sensor: sensor, host: row.displayLabel, cell: cell)
            }
        }
        hottest = hot

        let gpuReads = rows.map { $0.gpuUtilPct }
        let powerReads = rows.map { $0.gpuPowerW }
        gpuUtil = CLIFleetAggregate(values: gpuReads, rule: .mean)
        gpuPower = CLIFleetAggregate(values: powerReads, rule: .sum)

        // `isPaused` is `isDown && the overlay parks this row's node`, so these
        // two filters PARTITION the errored rows: a backend lands in exactly one
        // count, never both. A payload with no pause overlay — or a caller that
        // has no pause knowledge, the CLI's `pauses=None` rule — leaves
        // `backendsPaused` at 0 and every byte of the sentence where it was.
        backendsPaused = model.backendRows.filter { $0.isPaused }.count
        backendsDown = model.backendRows.filter { $0.isDown && !$0.isPaused }.count
        backendsReporting = model.totalRows.reduce(0) { $0 + $1.backends }
        // No total rows means no live backend: the CLI prints its `no backends
        // behind the proxy` note for that, not a total row, so the queue here
        // is "not told" rather than 0.
        if servingReported && !model.totalRows.isEmpty {
            running = model.totalRows.reduce(0) { $0 + $1.running }
            waiting = model.totalRows.reduce(0) { $0 + $1.waiting }
        } else {
            running = nil
            waiting = nil
        }
    }

    /// The Lock Screen numbers for one decoded `/v1/cluster/monitor` body, or
    /// nil when that read never answered. `servingReported` cannot come from
    /// the view-model: `project` turns a missing `serving` key and an empty
    /// list into the same empty `backendRows`, and those two sentences are not
    /// the same, so the raw payload decides.
    init?(_ response: MonitorResponse?) {
        guard let response, let model = ClusterMonitorViewModel.project(response) else {
            return nil
        }
        self.init(model: model,
                  servingReported: response.serving != nil && response.serving_error == nil)
    }
}

// ---------------------------------------------------------------------------
// The copy. One rule per string, so the three accessory families cannot
// disagree about the same entry, and no view composes a number of its own.
// ---------------------------------------------------------------------------

enum CLILockScreenCopy {
    /// The families this file speaks for. A POSITIVE list, so a newly added
    /// surface defaults to the system layout rather than silently inheriting
    /// Lock Screen copy it was never sized for.
    static func isAccessory(_ surface: FleetSurface) -> Bool {
        switch surface {
        case .inline, .circular, .rectangular: return true
        default: return false
        }
    }

    /// The hottest temperature, as the CLI's own cell for it — which already
    /// carries its unit (`78 C`), so no unit is appended here.
    static func tempToken(_ metrics: CLILockScreenMetrics) -> String {
        metrics.hottest?.cell ?? dashWith("C")
    }

    /// The GPU load. `avg` is load-bearing: the number is a mean over the hosts
    /// that reported, not a column the CLI has.
    static func gpuToken(_ metrics: CLILockScreenMetrics) -> String {
        guard let gpu = metrics.gpuUtil else { return "GPU " + FleetFormat.dash }
        return "GPU avg " + FleetFormat.pct(gpu.value) + coverage(gpu)
    }

    /// The number the circular gauge's value label draws. Deliberately WITHOUT
    /// the `n/N` coverage marker: the ring has about six characters before the
    /// digit clips, and the gate asserts that bound (`lockScreenRingBounds`).
    /// Coverage is not dropped — it is in `voice`, which every family uses as
    /// its accessibility label so a VoiceOver user always hears it, and in the
    /// rectangular line, which is the family with room for a row. Saying where
    /// a metric cannot be said is the honest version of shrinking it.
    static func gpuValueToken(_ metrics: CLILockScreenMetrics) -> String {
        guard let gpu = metrics.gpuUtil else { return FleetFormat.dash }
        return FleetFormat.pct(gpu.value)
    }

    /// The fleet's draw. A sum of the hosts that reported, with the coverage
    /// marker when they are not all of them (a missing host makes a sum too
    /// SMALL, which is the understatement worth flagging).
    static func wattsToken(_ metrics: CLILockScreenMetrics) -> String {
        guard let power = metrics.gpuPower else { return dashWith("W") }
        return wattsValue(power) + coverage(power)
    }

    /// The ring's caption: the same watts, without its marker, for the same
    /// six-character reason as `gpuValueToken`.
    static func wattsValueToken(_ metrics: CLILockScreenMetrics) -> String {
        guard let power = metrics.gpuPower else { return dashWith("W") }
        return wattsValue(power)
    }

    static func wattsValue(_ power: CLIFleetAggregate) -> String {
        // The CLI prints a host's power with two decimals (`{:.2f} W`); a fleet
        // total at that precision would be three digits of noise, so the total
        // is whole watts. This rounding is THIS file's rule, not the CLI's.
        String(format: "%.0f W", power.value)
    }

    /// The whole queue: running, waiting, and the backends that answered
    /// nothing — so `0 wait` beside a dead backend does not read as an idle
    /// fleet. The rectangular family, which has room for it.
    static func queueToken(_ metrics: CLILockScreenMetrics) -> String {
        guard let running = metrics.running, let waiting = metrics.waiting else {
            return FleetFormat.dash + " run"
        }
        var parts = [FleetFormat.count(running, singular: "run", plural: "run"),
                     FleetFormat.count(waiting, singular: "wait", plural: "wait")]
        if metrics.backendsDown > 0 {
            parts.append(backendPhrase(metrics.backendsDown, word: "down"))
        }
        // The parked clause beside it, not inside it: a queue of 0 next to a
        // parked backend says "nothing to do on purpose", and next to a faulting
        // one says "nothing to do because it is broken". Two sentences.
        if metrics.backendsPaused > 0 {
            parts.append(backendPhrase(metrics.backendsPaused, word: FleetFormat.pausedWord))
        }
        return parts.joined(separator: " · ")
    }

    /// The ONE queue fact a single Lock Screen line has room for, in the order
    /// that answers "is it working" most sharply: a queue that exists (waiting
    /// is the number that means work is being turned away), else what is
    /// actually running, else why nothing is queued when a backend is dead,
    /// else the CLI's own word for an engine that measured idle
    /// (`_load_style`'s `dim`). Every branch prints a number the route sent or
    /// the state the route implied; none of them pads the line.
    static func queueHeadline(_ metrics: CLILockScreenMetrics) -> String {
        guard let running = metrics.running, let waiting = metrics.waiting else {
            return FleetFormat.dash + " run"
        }
        if waiting > 0 {
            return FleetFormat.count(running, singular: "run", plural: "run") + " · "
                 + FleetFormat.count(waiting, singular: "wait", plural: "wait")
        }
        if running > 0 {
            return FleetFormat.count(running, singular: "run", plural: "run")
        }
        if metrics.backendsDown > 0 {
            return backendPhrase(metrics.backendsDown, word: "down")
        }
        // Nothing queued and nothing faulting, but the engine is stopped on a
        // parked box: say which of the two the operator is looking at. This
        // branch sits AFTER the faulting one on purpose — when a payload has
        // both, the real fault is the sharper answer to "is it working".
        if metrics.backendsPaused > 0 {
            return backendPhrase(metrics.backendsPaused, word: FleetFormat.pausedWord)
        }
        return "idle"
    }

    static func hostToken(_ metrics: CLILockScreenMetrics) -> String {
        FleetFormat.count(metrics.hostCount, singular: "host", plural: "hosts")
    }

    /// The lines one family draws. nil for a surface this file does not speak
    /// for, so the caller keeps its own text model — one door per surface, no
    /// half-overridden family.
    static func text(for surface: FleetSurface,
                     _ metrics: CLILockScreenMetrics,
                     failure: String? = nil,
                     ageMinutes: Int? = nil) -> FleetText? {
        guard isAccessory(surface) else { return nil }
        var footParts: [String] = []
        if let failure, !failure.isEmpty { footParts.append(failure) }
        if let age = FleetFormat.age(ageMinutes) { footParts.append(age) }
        let footer = footParts.isEmpty ? nil : footParts.joined(separator: " · ")

        switch surface {
        case .inline:
            // One line, and only the two metrics that answer the question at
            // glance: hot (temperature) and working (the one queue fact that
            // matters). Power and GPU load cost more characters than a Lock
            // Screen line has.
            return FleetText(headline: [tempToken(metrics), queueHeadline(metrics)]
                                .joined(separator: " · "),
                             detail: nil, rows: [], footer: footer)
        case .circular:
            // Primary: how busy the GPUs are (a capacity ring is a percentage's
            // own shape). Secondary: what that costs in watts. Both stay inside
            // the character bounds below, and the voice label carries everything
            // the ring has no room for.
            return FleetText(headline: gpuValueToken(metrics),
                             detail: wattsValueToken(metrics), rows: [], footer: nil)
        case .rectangular:
            // The row of metrics, then the queue and how many hosts it covers.
            return FleetText(headline: [tempToken(metrics), gpuToken(metrics), wattsToken(metrics)]
                                .joined(separator: " · "),
                             detail: [queueToken(metrics), hostToken(metrics)]
                                .joined(separator: " · "),
                             rows: [], footer: footer)
        default:
            return nil
        }
    }

    /// The sentence VoiceOver reads, and the only place the thermal band and
    /// the coverage counts appear as words. Accessories are system-tinted by
    /// rule (widget_style_check 4), so a `warn` / `critical` temperature cannot
    /// be red here — saying it out loud is how the verdict survives.
    static func voice(_ metrics: CLILockScreenMetrics) -> String {
        var parts: [String] = []
        if let hot = metrics.hottest {
            // The cell keeps the CLI's rounding (`GPU Temp` is whole degrees,
            // `CPU Temp` carries one decimal — the parity goldens hold that
            // asymmetry), and only the unit is spelled out: VoiceOver reads a
            // bare "92 C" as a letter, not as a temperature.
            parts.append("\(hot.sensor == .gpu ? "GPU" : "CPU") temperature "
                         + hot.cell.replacingOccurrences(of: " C", with: " degrees C")
                         + " on \(hot.host), \(hot.level.rawValue)")
        } else {
            parts.append("no temperature read")
        }
        if let gpu = metrics.gpuUtil {
            parts.append("GPU " + FleetFormat.pct(gpu.value) + " average over "
                         + "\(gpu.hostsReporting) of \(gpu.hostsListed) hosts")
        } else {
            parts.append("no GPU utilisation read")
        }
        if let power = metrics.gpuPower {
            parts.append("drawing " + wattsValue(power) + " from "
                         + "\(power.hostsReporting) of \(power.hostsListed) hosts")
        } else {
            parts.append("no power read")
        }
        if let running = metrics.running, let waiting = metrics.waiting {
            parts.append("\(running) running, \(waiting) waiting")
            if metrics.backendsDown > 0 {
                // The committed sentence, kept byte-for-byte — the gate holds an
                // unpaused payload to exactly these bytes. Its missing plural is
                // pre-existing wording and this card is not free to move it.
                parts.append("\(metrics.backendsDown) backend down")
            }
            // The parked half goes through the one phrase helper, so the verdict
            // word here IS `FleetFormat.pausedWord` — the same word the node rows
            // draw — and this file invents no second phrase for the slot.
            if metrics.backendsPaused > 0 {
                parts.append(backendPhrase(metrics.backendsPaused, word: FleetFormat.pausedWord))
            }
        } else {
            // The note words are `CLIMonitorNote`'s, never re-typed here: the
            // empty-serving sentence is the CLI's own dim line, and only the
            // missing-section case (where the CLI prints nothing at all) keeps
            // the app's neutral wording.
            parts.append(metrics.servingReported
                ? CLIMonitorNote.noBackends.text : "serving poll did not answer")
        }
        return parts.joined(separator: ", ") + "."
    }

    /// A Lock Screen ring clips at roughly these character widths, so the two
    /// strings it draws are bounded and `cluster_widget_check.sh` measures them
    /// against these numbers: `93%` and `195 W` fit, `32% 3/7` does not — which
    /// is exactly why the coverage marker lives in `voice` and in the
    /// rectangular line instead of here.
    static let ringValueLimit = 6
    static let ringCaptionLimit = 7

    /// The gauge's label — the words the ring spends no digit on. Single-sited
    /// so the gate can require that the ring says `avg` next to a mean.
    static let gaugeLabel = "GPU avg"

    /// The coverage marker `n/N`, printed beside a metric that only some of the
    /// listed hosts reported.
    private static func coverage(_ aggregate: CLIFleetAggregate) -> String {
        aggregate.isPartial ? " \(aggregate.hostsReporting)/\(aggregate.hostsListed)" : ""
    }

    /// `1 backend down` / `2 backends paused`. ONE site, because the two words
    /// fill the same slot and must not be worded twice: `down` is the CLI's word
    /// for a backend that answered nothing, and the parked half passes
    /// `FleetFormat.pausedWord` — the one owner of the verdict word, as
    /// `FleetVerdict.pausedWord` is the owner of the fleet-level one.
    private static func backendPhrase(_ count: Int, word: String) -> String {
        FleetFormat.count(count, singular: "backend \(word)", plural: "backends \(word)")
    }

    private static func dashWith(_ unit: String) -> String {
        FleetFormat.dash + " " + unit
    }
}
