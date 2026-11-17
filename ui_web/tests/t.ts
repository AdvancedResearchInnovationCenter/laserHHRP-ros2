// Minimal test helpers: no framework, compiled by tsc and run by node (npm test).
export type Test = [string, () => void | Promise<void>];

export const assert = {
  ok(v: unknown, msg = "expected true") { if (!v) throw new Error(msg); },
  equal(a: unknown, b: unknown, msg?: string) { if (a !== b) throw new Error(msg ?? `${String(a)} !== ${String(b)}`); },
  deepEqual(a: unknown, b: unknown) {
    if (JSON.stringify(a) !== JSON.stringify(b)) throw new Error(`${JSON.stringify(a)} != ${JSON.stringify(b)}`);
  },
  near(a: number, b: number, eps = 1e-6, msg?: string) {
    if (Math.abs(a - b) > eps) throw new Error(msg ?? `${a} !~ ${b}`);
  },
  includes(hay: string, needle: string) { if (!hay.includes(needle)) throw new Error(`missing ${JSON.stringify(needle)}`); },
  excludes(hay: string, needle: string | RegExp) {
    const hit = typeof needle === "string" ? hay.includes(needle) : needle.test(hay);
    if (hit) throw new Error(`unexpected ${String(needle)}`);
  },
};
