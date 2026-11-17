import type { Result } from "../api/client";

/** Runs a backend command; failures are shown as a toast and returned. */
export type Run = <T>(p: Promise<Result<T>>) => Promise<Result<T>>;
