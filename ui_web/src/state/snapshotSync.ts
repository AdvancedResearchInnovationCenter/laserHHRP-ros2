import type { Snapshot } from "../api/types";

// Keeps the client's snapshot in step with the authoritative backend.
// - event_seq makes repeated delivery harmless (accept() returns false for anything already seen);
// - refresh() never runs twice at once, but an event during an in-flight refresh queues one more, and a
//   response older than an event already seen is not applied and is fetched again: the last state change
//   is never left unrendered (code review of 3d7f52e, finding 3).
export class SnapshotSync {
  private seq = 0;
  private inFlight = false;
  private again = false;
  private stopped = false;

  constructor(
    private readonly fetchState: () => Promise<Snapshot | null>,
    private readonly apply: (s: Snapshot) => void,
  ) {}

  get lastSeq(): number {
    return this.seq;
  }

  stop(): void {
    this.stopped = true;
  }

  /** WebSocket snapshot (connect / reconnect): authoritative, replaces everything. */
  snapshot(s: Snapshot, eventSeq: number): void {
    this.seq = eventSeq;
    this.apply(s);
  }

  /** An incremental event: false if it was already seen (duplicate or older than the snapshot). */
  accept(eventSeq: number): boolean {
    if (eventSeq <= this.seq) return false;
    this.seq = eventSeq;
    return true;
  }

  async refresh(): Promise<void> {
    if (this.inFlight) {
      this.again = true;
      return;
    }
    this.inFlight = true;
    try {
      do {
        this.again = false;
        let s: Snapshot | null;
        try {
          s = await this.fetchState();
        } catch {
          s = null;
        }
        if (s === null || this.stopped) break;          // network error: the next event or reconnect retries
        const at = s.event_seq ?? 0;
        if (at >= this.seq) {
          this.seq = at;
          this.apply(s);
        } else {
          this.again = true;                            // older than an event already seen: fetch again
        }
      } while (this.again && !this.stopped);
    } finally {
      this.inFlight = false;
    }
  }
}
