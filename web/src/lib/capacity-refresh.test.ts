import { describe, expect, it, vi } from "vitest";

import { createCapacityRefresh, capacityPercentages } from "./capacity-refresh";

describe("capacityPercentages", () => {
  it("keeps unknown capacity unavailable instead of reporting 100% remaining", () => {
    expect(capacityPercentages(null)).toBeNull();
    expect(capacityPercentages(40)).toEqual({ used: 40, remaining: 60 });
  });
});

describe("createCapacityRefresh", () => {
  it("coalesces overlapping refreshes and ignores results after disposal", async () => {
    let resolve!: (value: number) => void;
    const request = vi.fn(() => new Promise<number>((done) => { resolve = done; }));
    const publish = vi.fn();
    const refresh = createCapacityRefresh(request, publish);

    const first = refresh.run();
    const second = refresh.run();
    expect(request).toHaveBeenCalledTimes(1);

    refresh.dispose();
    resolve(7);
    await Promise.all([first, second]);
    expect(publish).not.toHaveBeenCalled();
  });
});