import Foundation
import Combine

// ===========================================================================
// offline_queue_check — prove the OfflineSendQueue contract (t_42ba90d2):
//
//   1. enqueue persists the message (never lost, survives the queue not being
//      re-seeded);
//   2. flush on `.reachable` delivers each queued message exactly ONCE
//      ("never send twice") and removes it on success;
//   3. a transport failure during flush keeps the message queued (never dropped);
//   4. a permanent rejection removes it (the queue can't fix a 4xx/5xx) with a
//      recorded reason;
//   5. flush is a no-op before reachability / without a handler (messages stay
//      queued, not lost);
//   6. ChatStore.reconcileQueued flips a delivered `.queued` entry to `.prompt`
//      and a non-delivered one to `.failure` — never silently faked as sent.
//
// This compiles the REAL OfflineSendQueue.swift + ConnectionMonitor.swift +
// APIError.swift (for HSCCError) + ChatStore.swift + the REAL ChatEntry enum
// (sliced from OrchestratorChatView.swift) — never redeclared here — together
// with this pure-logic harness into a macOS CLI (no iOS platform runtime on
// this host; a runtime claim is never made). Same pattern as chat_state_check.
// ===========================================================================

var failures = 0
func check(_ name: String, _ cond: Bool, _ detail: String = "") {
    if cond { print("PASS: \(name)") }
    else { print("FAIL: \(name) \(detail)"); failures += 1 }
}

// --- Deterministic start: clear persisted queue + reset shared state. --------
UserDefaults.standard.removeObject(forKey: "hscc.offline.queue.pending")
ConnectionMonitor.shared.reset()

let q = OfflineSendQueue.shared
q.sendHandler = nil
q.reset()

// ConnectionMonitor is NOT on the main actor, but we drive it from the main
// actor here; safe in this single-threaded CLI.

// --- Test 1: enqueue persists + pendingCount + isPending ---------------------
let id1 = q.enqueue(project: "alpha", text: "hello alpha", kind: .orchestratorChat)
check("enqueue bumps pendingCount to 1", q.pendingCount == 1)
check("enqueue returns a pending id", q.isPending(id1))

let storedAfterEnqueue = UserDefaults.standard.data(forKey: "hscc.offline.queue.pending")
check("enqueue persisted to UserDefaults (survives relaunch)", storedAfterEnqueue != nil)

// --- Test 2: flush is a no-op before reachable (messages stay queued) --------
// We have a message queued but monitor is .unknown and no handler set. Flush
// must be a no-op — the message is NOT lost.
await q.flushIfReachable()
check("flush no-op while not reachable keeps the message", q.pendingCount == 1)

// Set handler + reachable, handler returns delivered.
var deliveredCalls = 0
let handler = { (_ msg: OfflineSendQueue.QueuedMessage) async -> OfflineSendQueue.SendOutcome in
    deliveredCalls += 1
    return .delivered
}
q.sendHandler = handler
ConnectionMonitor.shared.requestSucceeded()  // -> .reachable (fires the subscription too)

// --- Test 3: flush delivers + removes, exactly once --------------------------
await q.flushIfReachable()
check("flush delivered+removed the queued message", q.pendingCount == 0)
check("flush called the handler exactly once", deliveredCalls == 1)
check("lastHandled reported .delivered",
      q.lastHandled?.id == id1 && q.lastHandled?.outcome == .delivered)

// --- Test 4: unreachable keeps the message queued ----------------------------
var unreachableCalls = 0
q.sendHandler = { _ in
    unreachableCalls += 1
    return .unreachable
}
let id2 = q.enqueue(project: "alpha", text: "still waiting", kind: .orchestratorChat)
// monitor is reachable, handler returns unreachable (simulates: we thought the
// cluster was reachable via another request but this specific send can't connect)
await q.flushIfReachable()
check("unreachable outcome KEEPS the message queued", q.pendingCount == 1)
check("unreachable calls handler once", unreachableCalls == 1)
check("unreachable recorded in lastHandled",
      q.lastHandled?.id == id2 && q.lastHandled?.outcome == .unreachable)

// --- Test 5: rejected removes with recorded reason ---------------------------
q.sendHandler = { _ in .rejected("server said no") }
await q.flushIfReachable()
check("rejected outcome REMOVES the message", q.pendingCount == 0)
check("rejected recorded in lastHandled",
      q.lastHandled?.id == id2 && q.lastHandled?.outcome == .rejected("server said no"))

// --- Test 6: two messages, both delivered exactly once -----------------------
q.sendHandler = { _ in .delivered }
var callCount = 0
q.sendHandler = { _ in callCount += 1; return .delivered }
let a = q.enqueue(project: "a", text: "m1", kind: .orchestratorChat)
let b = q.enqueue(project: "a", text: "m2", kind: .orchestratorChat)
check("two queued (pendingCount == 2)", q.pendingCount == 2)
await q.flushIfReachable()
await q.flushIfReachable()  // a SECOND flush must not re-send
check("both delivered and removed", q.pendingCount == 0)
check("handler called exactly twice across BOTH flushes (never double-sent)",
      callCount == 2)

// --- Test 7: auto-flush trigger from the ConnectionMonitor subscription ------
// When .reachable fires and there are pending items, the queue auto-flushes.
q.sendHandler = nil
let c = q.enqueue(project: "gamma", text: "auto", kind: .orchestratorChat)
check("queued before auto-flush", q.pendingCount == 1)
q.sendHandler = { _ in .delivered }
ConnectionMonitor.shared.requestSucceeded()  // fires $status sink -> flush
    
// Wait briefly for the async flush launched by the sink to complete.
try? await Task.sleep(nanoseconds: 300_000_000)
check("auto-flushed on .reachable transition", q.pendingCount == 0,
      "pending=\(q.pendingCount)")

// --- Test 8: ChatStore.reconcileQueued ---------------------------------------
// A delivered queued message (job persisted) flips to .prompt; a non-delivered
// one flips to .failure — never silently faked as sent.
do {
    // Keep the app-scoped queue CLEAN for this sub-test: use a distinct load.
    q.sendHandler = nil
    q.reset()

    // Clean the project's transcript + job keys.
    let project = "beta"
    UserDefaults.standard.removeObject(forKey: "hscc.chat.beta.transcript")
    UserDefaults.standard.removeObject(forKey: "hscc.chat.beta.in-flight-job")

    // Case A: delivered. The queue persists the job_id via ChatStore.persistJobID.
    let store = ChatStore(project: project)
    let queuedID = q.enqueue(project: project, text: "msg delivered", kind: .orchestratorChat)
    store.beginSend(prompt: "msg delivered")
    store.markQueued(messageID: queuedID)
    check("store has a .queued entry", {
        if case .queued = store.transcript.last! { return true } else { return false }
    }())
    // Deliver via the queue: handler POSTs and persists a job_id.
    q.sendHandler = { _ in
        ChatStore.persistJobID("job-123", for: project)
        return .delivered
    }
    ConnectionMonitor.shared.requestSucceeded()
    await q.flushIfReachable()
    check("queue removed the delivered message (pendingCount == 0)",
          q.pendingCount == 0)
    store.reconcileQueued()
    check("reconcile flips delivered .queued -> .prompt", {
        if case .prompt = store.transcript.last! { return true } else { return false }
    }())

    // Case B: not delivered (rejected). No job persisted -> flips to .failure.
    let store2 = ChatStore(project: "gamma")
    let queuedID2 = q.enqueue(project: "gamma", text: "msg rejected", kind: .orchestratorChat)
    store2.beginSend(prompt: "msg rejected")
    store2.markQueued(messageID: queuedID2)
    q.sendHandler = { _ in .rejected("nope") }
    ConnectionMonitor.shared.requestSucceeded()
    await q.flushIfReachable()
    check("queue removed the rejected message", q.pendingCount == 0)
    store2.reconcileQueued()
    check("reconcile flips non-delivered .queued -> .failure", {
        if case .failure = store2.transcript.last! { return true } else { return false }
    }())

    // Case C: still pending -> reconcile leaves it as .queued.
    let store3 = ChatStore(project: "delta")
    let queuedID3 = q.enqueue(project: "delta", text: "msg pending", kind: .orchestratorChat)
    store3.beginSend(prompt: "msg pending")
    store3.markQueued(messageID: queuedID3)
    // handler returns unreachable -> still pending
    q.sendHandler = { _ in .unreachable }
    ConnectionMonitor.shared.requestSucceeded()
    await q.flushIfReachable()
    check("queue KEEPS still-pending message", q.isPending(queuedID3))
    store3.reconcileQueued()
    check("reconcile leaves still-pending .queued untouched", {
        if case .queued = store3.transcript.last! { return true } else { return false }
    }())
}

// --- Test 9: cluster-switch drain surfaces (never silently dropped) ----------
do {
    q.sendHandler = nil
    q.reset()
    check("reset after previous tests leaves queue empty", q.pendingCount == 0)
    check("reset clears drained banner", q.drainedDueToClusterSwitch == nil)

    let d1 = q.enqueue(project: "epsilon", text: "will be drained", kind: .orchestratorChat)
    check("drained test has 1 queued", q.pendingCount == 1)

    // Simulate a cluster switch mid-queue.
    q.drainDueToClusterSwitch()
    check("drain clears the queue", q.pendingCount == 0)
    check("drain surfaced the dropped message (not silent)",
          q.drainedDueToClusterSwitch?.count == 1)
    check("drained message is the one we queued",
          q.drainedDueToClusterSwitch?.first?.id == d1)

    q.consumeDrained()
    check("consumeDrained clears the surfaced set",
          q.drainedDueToClusterSwitch == nil)
    check("queue stays empty after drain+consume", q.pendingCount == 0)
}

// --- Summary -----------------------------------------------------------------
print("")
if failures == 0 {
    print("offline queue check: ALL PASS")
} else {
    print("offline queue check: \(failures) FAILURE(S)")
}
exit(failures == 0 ? 0 : 1)
