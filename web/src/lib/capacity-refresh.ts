export interface CapacityRefresh {
  run: () => Promise<void>;
  dispose: () => void;
}

export function capacityPercentages(
  usedPercent: number | null,
): { used: number; remaining: number } | null {
  if (usedPercent === null) return null;
  const used = Math.min(100, Math.max(0, usedPercent));
  return { used, remaining: Math.max(0, Math.round(100 - used)) };
}

/** Keep polling single-flight and prevent an obsolete profile/unmounted view
 * from publishing a response that completed after its replacement. */
export function createCapacityRefresh<T>(
  request: () => Promise<T>,
  publish: (value: T) => void,
): CapacityRefresh {
  let active = true;
  let inflight: Promise<void> | null = null;

  const run = () => {
    if (inflight) return inflight;
    inflight = request()
      .then((value) => {
        if (active) publish(value);
      })
      .catch(() => {
        // Keep the last good snapshot on transient polling failures.
      })
      .finally(() => {
        inflight = null;
      });
    return inflight;
  };

  return {
    run,
    dispose: () => { active = false; },
  };
}