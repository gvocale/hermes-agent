export interface CapacityRefresh {
  run: () => Promise<void>;
  dispose: () => void;
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