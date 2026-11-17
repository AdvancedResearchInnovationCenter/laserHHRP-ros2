// npm test: every suite, one line per test, exit 1 on any failure.
import type { Test } from "./t";
import { tests as sync } from "./snapshotSync.test";
import { tests as logic } from "./logic.test";
import { tests as screens } from "./screens.test";

declare const process: { exit(code: number): never };

async function main() {
  const suites: [string, Test[]][] = [["snapshotSync", sync], ["map / circles / format", logic], ["screens", screens]];
  let failed = 0, total = 0;
  for (const [suite, tests] of suites) {
    console.log(`# ${suite}`);
    for (const [name, fn] of tests) {
      total++;
      try {
        await fn();
        console.log(`PASS  ${name}`);
      } catch (e) {
        failed++;
        console.log(`FAIL  ${name}\n      ${(e as Error).message}`);
      }
    }
  }
  console.log(`${total - failed}/${total} passed`);
  process.exit(failed ? 1 : 0);
}
void main();
