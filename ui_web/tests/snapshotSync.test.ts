// SnapshotSync regression tests (code review finding 3).
import { SnapshotSync } from "../src/state/snapshotSync";
import type { Snapshot } from "../src/api/types";

import { assert, type Test } from "./t";

type Deferred = { resolve: (s: Snapshot | null) => void; promise: Promise<Snapshot | null> };
const deferred = (): Deferred => {
  let resolve!: (s: Snapshot | null) => void;
  const promise = new Promise<Snapshot | null>((r) => (resolve = r));
  return { resolve, promise };
};
const snap = (seq: number, screen = "ready") => ({ event_seq: seq, screen }) as unknown as Snapshot;
const tick = () => new Promise((r) => setTimeout(r, 0));

function harness() {
  const fetches: Deferred[] = [];
  const applied: number[] = [];
  const sync = new SnapshotSync(() => {
    const d = deferred();
    fetches.push(d);
    return d.promise;
  }, (s: Snapshot) => applied.push(s.event_seq ?? -1));
  return { sync, fetches, applied };
}

export const tests: Test[] = [
  ["event during an in-flight refresh queues another refresh", async () => {
    const { sync, fetches, applied } = harness();
    sync.snapshot(snap(1), 1);
    assert.ok(sync.accept(2));
    const first = sync.refresh();                     // fetch #1 in flight
    assert.ok(sync.accept(3));
    void sync.refresh();                              // old code: skipped and lost
    fetches[0].resolve(snap(2));                      // response predates event 3
    await tick();
    assert.equal(fetches.length, 2, "a second fetch was queued");
    fetches[1].resolve(snap(3));
    await first;
    assert.deepEqual(applied, [1, 3]);
    assert.equal(sync.lastSeq, 3);
  }],
  ["a response older than an already seen event is fetched again", async () => {
    const { sync, fetches, applied } = harness();
    sync.snapshot(snap(1), 1);
    assert.ok(sync.accept(2));
    const p = sync.refresh();
    assert.ok(sync.accept(3));                        // e.g. an incremental feature event, no refresh call
    fetches[0].resolve(snap(2));
    await tick();
    assert.equal(fetches.length, 2);
    fetches[1].resolve(snap(3));
    await p;
    assert.deepEqual(applied, [1, 3]);
  }],
  ["duplicates and stale events are rejected", async () => {
    const { sync } = harness();
    sync.snapshot(snap(5), 5);
    assert.equal(sync.accept(5), false);
    assert.equal(sync.accept(4), false);
    assert.equal(sync.accept(6), true);
    assert.equal(sync.accept(6), false);
  }],
  ["network error ends the refresh without looping", async () => {
    const { sync, fetches, applied } = harness();
    const p = sync.refresh();
    fetches[0].resolve(null);
    await p;
    assert.equal(fetches.length, 1);
    assert.deepEqual(applied, []);
  }],
  ["stop() ends a pending loop", async () => {
    const { sync, fetches } = harness();
    assert.ok(sync.accept(9));
    const p = sync.refresh();
    sync.stop();
    fetches[0].resolve(snap(1));
    await p;
    assert.equal(fetches.length, 1);
  }],
];
